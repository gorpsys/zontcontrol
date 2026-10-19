"""Conditional Fischer role; every name/R1 write passes one bounded guard."""

from dataclasses import replace
from threading import Event, RLock
import time
from uuid import uuid4

from ..control_types import ControllerError
from .clock import BootClock, LeaderTimeline, _duration, clock_factor
from .models import LeaderRecord, R1Record, RecordError, _uuid
from .statistics import observe
from .timing import FreezeGuard, OperationWindow, TimingViolation


class LeaseLost(Exception):
    def __init__(self):
        super().__init__('Coordinator lease lost or operation window unavailable')


class LeaderElection:
    def __init__(self, store, owner, timing, *, clock=None, timeline=None,
                 epoch_sampler=time.time, uuid_factory=None, statistics=None,
                 poll_seconds=1, retry_seconds=1, jitter_fraction=.1, renewal_fraction=.33):
        _uuid(owner)
        if (store.profile.delta_seconds != timing.delta
                or store.profile.delta_cmd_seconds != timing.delta_cmd):
            raise RecordError()
        self._store, self._owner, self.timing = store, owner, timing
        self._clock = BootClock() if clock is None else clock
        self._uuid_factory = uuid4 if uuid_factory is None else uuid_factory
        self.timeline = timeline if timeline is not None else LeaderTimeline.capture(
            self._clock, delta=timing.delta, epoch_sampler=epoch_sampler,
            uuid_factory=self._uuid_factory)
        self._statistics = statistics
        self.poll_seconds, self.retry_seconds = poll_seconds, retry_seconds
        self.jitter_fraction, self.renewal_fraction = jitter_fraction, renewal_fraction
        self._lock = RLock()
        self.role_cancelled = Event()
        self._active = False
        self.record = None
        self.expires_at = None
        self.term_expires_at = None
        self.quarantine_until = None
        self._term_started = None
        self._observation = None
        self._wake_guard = FreezeGuard(self._clock, timing.delta)

    def _observe(self, event, **fields):
        observe(self._statistics, event, device_id=self._store.device_id,
                run_id=self._owner, **fields)

    def _lose(self):
        self._active = False
        self.role_cancelled.set()
        self._observation = None
        self._observe('leader_lost', outcome='unknown')
        raise LeaseLost() from None

    def stop(self):
        self.role_cancelled.set()
        self._active = False

    def expect_resume(self, when):
        self._wake_guard.expect_resume(when)

    def can_coordinate(self, duration):
        _duration(duration)
        with self._lock:
            if not self._active:
                return False
            if (self.role_cancelled.is_set()
                    or self._clock.now() >= self.expires_at - self.timing.stop_margin):
                self.stop()
                return False
            return self._clock.now() + duration <= self.expires_at - self.timing.stop_margin

    def _window(self, seconds=None):
        self._wake_guard.check()
        return OperationWindow(self._clock, self.timing.delta if seconds is None else seconds)

    def _read(self, window):
        record = self._store.read_r1(window=window)
        window.complete()
        return record

    def _write(self, record, window, *, deadline, candidate=False):
        # The process wakeup expectation spans scheduler pauses and planned sleeps.
        self._wake_guard.check()
        if self.role_cancelled.is_set():
            self._lose()
        self._store.write_r1(record, window=window, role_deadline=deadline,
                             stop_margin=0 if candidate else self.timing.stop_margin)
        self._wake_guard.expect_resume(self._clock.now())

    def try_acquire(self, *, cancelled=None):
        with self._lock:
            if cancelled is not None and cancelled.is_set():
                return False
            if self._active:
                if not self.can_coordinate(0):
                    self._lose()
                return True
            self.role_cancelled = Event()
            # A failed guard ends its role. A new candidate starts by observing.
            self._wake_guard = FreezeGuard(self._clock, self.timing.delta)
            try:
                window = self._window()
                observed = self._read(window)
                received = self._clock.now()
                if observed is not None and observed.leader is not None:
                    if self._observation is None or self._observation[0] != observed:
                        self._observation = (observed, received)
                        return False
                    old = observed.leader
                    wait = (old.leader_until - old.sampled_at) * clock_factor(old.period_s)
                    if observed.profile != self._store.profile:
                        wait += (observed.profile.delta_cmd_seconds
                                 * clock_factor(observed.profile.delta_cmd_seconds))
                    # Use READ send, never its response, as the final endpoint.
                    if window.started_at - self._observation[1] < wait:
                        return False
                term_id = str(self._uuid_factory())
                record_id = str(self._uuid_factory())
                sent = self._clock.now()
                self._term_started = sent
                self.term_expires_at = sent + self.timing.term_budget
                self.expires_at = self.timing.lease_deadline(sent, term_started_at=sent)
                tau = self.timeline.now()
                own = LeaderRecord(self._owner, term_id, str(self.timeline.clock_id),
                                   self.timing.period, self.timing.clock_rho, tau,
                                   tau + self.expires_at - sent, tau)
                record = R1Record(self._store.profile, record_id, own,
                    handled_request=observed.handled_request if observed is not None else None)
                self.record = own
                self._write(record, window, deadline=self.expires_at, candidate=True)
                target = self._clock.now() + self.timing.barrier
                self.expect_resume(target)
                if self._clock.wait(cancelled if cancelled is not None else self.role_cancelled,
                                    self.timing.barrier):
                    self._lose()
                self._wake_guard.check()
                if self._clock.now() < target:
                    self._lose()
                confirmation = self._window()
                if (self._read(confirmation) != record or self.role_cancelled.is_set()
                        or cancelled is not None and cancelled.is_set()
                        or self._clock.now() >= self.expires_at - self.timing.stop_margin):
                    self._lose()
                self._active = True
                self.quarantine_until = self.timeline.now() + self.timing.quarantine
                self._wake_guard.expect_resume(self._clock.now())
                self._observation = None
                self._observe('leader_acquired', wait_s=self._clock.now() - sent)
                return True
            except (ControllerError, RecordError, TimingViolation):
                self._lose()

    def _confirm(self, window):
        if not self.can_coordinate(window.remaining):
            self._lose()
        record = self._read(window)
        if record is None or record.leader != self.record or record.profile != self._store.profile:
            self._lose()
        return record

    def confirm(self, duration):
        with self._lock:
            try:
                window = self._window(min(duration, self.timing.delta))
                record = self._confirm(window)
                self._wake_guard.expect_resume(self._clock.now())
                return record
            except (ControllerError, RecordError, TimingViolation):
                self._lose()

    def coordinate(self, operation, duration):
        """Internal callback gets record and original operation window."""
        with self._lock:
            try:
                window = self._window(min(duration, self.timing.delta))
                record = self._confirm(window)
                result = operation(record, window)
                window.complete(role_deadline=self.expires_at, stop_margin=self.timing.stop_margin)
                self._wake_guard.expect_resume(self._clock.now())
                return result
            except (ControllerError, RecordError, TimingViolation):
                self._lose()

    def publish(self, record, window):
        """Only called inside coordinate, under its role lock and original window."""
        self._write(record, window, deadline=self.expires_at)

    def renew(self):
        with self._lock:
            def write(record, window):
                record_id = str(self._uuid_factory())
                sent = self._clock.now()
                deadline = self.timing.lease_deadline(sent, term_started_at=self._term_started)
                tau = self.timeline.now()
                renewed = replace(self.record, revision=self.record.revision + 1,
                                  sampled_at=tau, leader_until=tau + deadline - sent)
                self._write(replace(record, record_id=record_id, leader=renewed), window,
                            deadline=self.expires_at)
                return renewed, deadline
            renewed, deadline = self.coordinate(write, self.timing.delta)
            self.record, self.expires_at = renewed, deadline
            self._observe('leader_renewed')

    def release(self):
        """Natural expiry preserves old leader horizon, grants and command tails."""
        self.stop()
        self._observe('leader_released')
