"""Conditional Fischer leader role, separate from executor control ownership.

No CAS, fencing, linearizability or 99.9% reliability is claimed. In particular,
an executor's stale R1 read/modify/write can replay a leader projection. Exact
confirmation detects some conflicts but cannot remove this accepted model risk.
The caller schedules polls/renewals; no thread or automatic retry is hidden here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from secrets import token_hex
from threading import Event, RLock
from typing import NoReturn, TypeVar, cast

from ..control_types import ControllerError
from .clock import BootClock, LeaseTiming, _duration
from .models import LeaderRecord, R1Record, RecordError, _uuid
from .ports import Clock
from .statistics import ObservationValue, Observer, error_event, observe
from .store import LeaderStore


_Result = TypeVar('_Result')


class LeaseLost(Exception):
    def __init__(self) -> None:
        super().__init__("Coordinator lease lost or operation window unavailable")


class LeaderElection:
    """One serialized coordinator role for one device, with cooperative loss.

    role_cancelled belongs only to this role; it must never revoke an algorithm's
    control session or cancel its finally. A new acquisition creates a new event.
    The store remains internal to coordination, unavailable to algorithms.
    """

    def __init__(self, store: LeaderStore, owner: str, timing: LeaseTiming, *,
                 clock: Clock | None = None, token_factory: Callable[[], str] | None = None,
                 statistics: Observer | None = None) -> None:
        _uuid(owner)
        self._store = store
        self._owner = owner
        self.timing = timing
        self._clock = clock if clock is not None else BootClock()
        self._token_factory = token_factory or (lambda: token_hex(16))
        self._lock = RLock()
        self.role_cancelled = Event()
        self._active = False
        self.record: LeaderRecord | None = None
        self.expires_at: float | None = None
        self.term_expires_at: float | None = None
        self._observation: tuple[LeaderRecord, float] | None = None
        self._statistics = statistics

    def _observe(self, event: str, **fields: ObservationValue) -> None:
        context: dict[str, ObservationValue] = {'device_id': self._store.device_id, 'run_id': self._owner}
        if self.record is not None:
            context['leader_term'] = self.record.token
        observe(self._statistics, event, **context, **fields)

    def _lose(self) -> NoReturn:
        self._observe('leader_lost', outcome='unknown')
        self._active = False
        self.role_cancelled.set()
        self._observation = None
        raise LeaseLost() from None

    def stop(self) -> None:
        """Stop this role, without touching metadata or executor ownership."""
        # Do not wait for the state lock: a barrier/HTTP may hold it.
        self.role_cancelled.set()

    def can_coordinate(self, duration: float) -> bool:
        """Require the whole local window strictly before both deadlines."""
        _duration(duration)
        with self._lock:
            if not self._active:
                return False
            now = self._clock.now()
            if self.role_cancelled.is_set() or now >= cast(float, self.expires_at):
                self._active = False
                self.role_cancelled.set()
                return False
            return now + duration < cast(float, self.expires_at)

    def _require(self, duration: float) -> None:
        if not self.can_coordinate(duration):
            self._lose()

    def _read(self) -> tuple[R1Record, float, float]:
        sent = self._clock.now()
        try:
            record = self._store.read_r1()
        except (ControllerError, RecordError) as error:
            self._observe(error_event(error), outcome='unknown')
            self._lose()
        received = self._clock.now()
        self._observe('rtt', rtt_s=received - sent)
        if received - sent > self.timing.delta:
            self._observe('delta_breach', elapsed_s=received - sent, delta_s=self.timing.delta)
            self._lose()
        return record, sent, received

    def try_acquire(self, *, cancelled: Event | None = None) -> bool:
        """Poll once; return False while another projection is unexpired.

        FREE or a conservatively observed expiry permits a single write, then
        an interruptible W barrier and exact bounded confirmation. Any uncertain
        write abandons this attempt; it never writes FREE as compensation.
        """
        with self._lock:
            if cancelled is not None and cancelled.is_set():
                return False
            self._observe('leader_attempt')
            if self._active:
                self._require(0)
                return True
            self.role_cancelled = Event()
            observed, read_send, received = self._read()
            projection = observed.leader
            if projection is not None:
                if self._observation is None or self._observation[0] != projection:
                    self._observation = (projection, received)
                    return False
                if read_send - self._observation[1] < projection.lease_s:
                    return False
            own = LeaderRecord(self._owner, self._token_factory(), 0,
                               self.timing.lease_s, self.timing.clock_rho)
            sent = self._clock.now()
            if sent - read_send >= self.timing.delta or self.role_cancelled.is_set():
                self._lose()
            self.record = own
            self.term_expires_at = sent + self.timing.term_budget
            self.expires_at = min(sent + self.timing.valid_for, cast(float, self.term_expires_at))
            try:
                self._store.write_r1(R1Record(own, observed.request))
            except (ControllerError, RecordError) as error:
                self._observe(error_event(error), outcome='unknown')
                self._lose()
            ack = self._clock.now()
            if ack - read_send > self.timing.delta:
                self._observe('delta_breach', elapsed_s=ack - read_send, delta_s=self.timing.delta)
                self._lose()
            if self._clock.wait(cancelled if cancelled is not None else self.role_cancelled,
                                self.timing.barrier):
                self._lose()
            # CLOCK_BOOTTIME includes suspend. Recheck elapsed, never trust wait
            # returning as evidence of either a sufficient barrier or live lease.
            if self._clock.now() - ack < self.timing.barrier:
                self._lose()
            confirmed, _, _ = self._read()
            if (confirmed.leader != own or self.role_cancelled.is_set()
                    or cancelled is not None and cancelled.is_set()
                    or self._clock.now() >= cast(float, self.expires_at)):
                self._lose()
            self._active = True
            self._observation = None
            self._observe('leader_acquired', wait_s=self._clock.now() - sent)
            return True

    def _confirm(self, duration: float) -> R1Record:
        self._require(duration)
        start = self._clock.now()
        record, _, _ = self._read()
        if record.leader != self.record or self._clock.now() - start > duration:
            self._lose()
        self._require(0)
        return record

    def confirm(self, duration: float) -> R1Record:
        """Bounded R1 confirmation; duration covers this entire caller window."""
        _duration(duration)
        with self._lock:
            return self._confirm(duration)

    def coordinate(self, operation: Callable[[R1Record], _Result], duration: float) -> _Result:
        """Confirm R1 and run an internal operation under one state lock.

        duration includes confirmation, pauses, send and completion. The caller
        supplies an operation budget; no executor grants are issued here.
        """
        _duration(duration, positive=True)
        with self._lock:
            start = self._clock.now()
            record = self._confirm(duration)
            if self._clock.now() - start >= duration:
                self._lose()
            try:
                result = operation(record)
            except (ControllerError, RecordError) as error:
                self._observe(error_event(error), outcome='unknown')
                self._lose()
            if self._clock.now() - start > duration:
                self._observe('delta_breach', elapsed_s=self._clock.now() - start, delta_s=duration)
                self._lose()
            self._require(0)
            return result

    def renew(self) -> None:
        """One renewal attempt using the old budget and original term cap."""
        with self._lock:
            sent = None
            renewed = replace(self.record, seq=self.record.seq + 1) if self.record else None

            def write(record: R1Record) -> None:
                nonlocal sent
                sent = self._clock.now()
                self._store.write_r1(R1Record(renewed, record.request))

            self.coordinate(write, self.timing.delta)
            self.record = renewed
            self.expires_at = min(cast(float, sent) + self.timing.valid_for,
                                  cast(float, self.term_expires_at))
            self._observe('leader_renewed')

    def release(self) -> None:
        """Release after caller has settled grants and service in-flight work.

        No new grants may be issued during this call. The role ends before FREE
        is sent, including when its outcome is unknown; a second FREE is refused.
        """
        with self._lock:
            start = self._clock.now()
            record = self._confirm(self.timing.delta)
            if self._clock.now() - start >= self.timing.delta:
                self._lose()
            self._active = False
            self.role_cancelled.set()
            self._observation = None
            try:
                self._store.write_r1(R1Record(request=record.request))
            except (ControllerError, RecordError) as error:
                self._observe(error_event(error), outcome='unknown')
                self._lose()
            if self._clock.now() - start > self.timing.delta:
                self._observe('delta_breach', elapsed_s=self._clock.now() - start,
                              delta_s=self.timing.delta)
                self._lose()
            self._observe('leader_released')
