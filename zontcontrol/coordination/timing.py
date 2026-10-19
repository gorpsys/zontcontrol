"""V2 timing policy and local guards for bounded protocol I/O.

These checks detect violations of the assumed timing model. They cannot cancel
an already sent write or prove server deadlines, linearizability or clock bounds.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from itertools import count
from os import getpid

from .clock import _duration, clock_factor, clock_rho
from .statistics import observe


_WINDOW_NUMBERS = count(1)


class TimingViolation(ValueError):
    """A local operation, role or expected wakeup exceeded its timing bound."""


class FreezeGuard:
    """Keep the expected resume instant, including an intentional wait's target."""

    def __init__(self, clock, delta, *, expected_resume_at=None, statistics=None, **context):
        self._clock = clock
        self._delta = _duration(delta, positive=True)
        self._expected_resume_at = _duration(
            clock.now() if expected_resume_at is None else expected_resume_at)
        self._failed = False
        self._statistics, self._context = statistics, context

    def expect_resume(self, when):
        self._expected_resume_at = _duration(when)

    def check(self):
        return self._check_at(_duration(self._clock.now()))

    def _check_at(self, now):
        gap = max(0, now - self._expected_resume_at)
        observe(self._statistics, 'freeze', **self._context, stage='freeze',
                freeze_gap_s=gap, expected_resume_at=self._expected_resume_at,
                finished_at=now, delta_s=self._delta, exceeded=int(gap > self._delta))
        if self._failed or gap > self._delta:
            self._failed = True
            raise TimingViolation("Unexpected process pause exceeded Delta")
        return gap


class OperationWindow:
    """One original READ-to-WRITE/confirmation budget; checks never rebase it.

    At the deadline a completed response fits the bound, but no new I/O may
    start. role_deadline is local BOOTTIME. Before a write, the entire remaining
    window plus the role's stop margin must still fit inside that role.
    """

    def __init__(self, clock, seconds, *, started_at=None, expected_resume_at=None):
        self._clock = clock
        now = _duration(clock.now())
        seconds = _duration(seconds, positive=True)
        self._started_at = now if started_at is None else _duration(started_at)
        if self._started_at > now:
            raise ValueError("Operation cannot start in the future")
        self._deadline = self._started_at + seconds
        self._freeze_guard = FreezeGuard(
            clock, seconds, expected_resume_at=(self._started_at if expected_resume_at is None
                                               else expected_resume_at))
        self._failed = False
        self._last_at = now
        self._statistics = None
        self._observation_context = {}
        self._observation_started = False
        self._observation_finished = False
        self._observation_depth = 0

    @contextmanager
    def observation(self):
        """Only the outer owner closes a whole window, including failed first READs.

        Nested stores may attach to the same window, but cannot finish the
        caller's multi-operation measurement early. No timing checks are added.
        """
        owner = self._observation_depth == 0 and not self._observation_finished
        self._observation_depth += 1
        if owner:
            self.begin_observation()
        try:
            yield
        except BaseException as error:
            if owner:
                self.finish_observation(error)
            raise
        else:
            if owner:
                self.finish_observation()
        finally:
            self._observation_depth -= 1

    def observe_with(self, statistics, *, device_id, role):
        """Attach optional output only; retain the original operation start."""
        if statistics is None or self._statistics is not None:
            return
        try:
            # A local observation label, not a protocol UUID. Do not consume the
            # shared UUIDFactory or change the protocol's identifier sequence.
            label = f'{getpid()}-{int(self._started_at * 1e9)}-{next(_WINDOW_NUMBERS)}'
            context = dict(device_id=device_id, role=role, window_id=label)
            self._statistics, self._observation_context = statistics, context
            # The window owns this internal guard. Attach the same observer
            # without reconstructing its original wakeup target or failed state.
            self._freeze_guard._statistics = statistics
            self._freeze_guard._context = context
        except Exception:
            observe(statistics, 'error', device_id=device_id, phase='metadata', outcome='unknown')

    def begin_observation(self):
        if not self._observation_started:
            self._observation_started = True
            self.record('sync_started', stage='read')

    def record(self, event, *, stage, finished_at=None, **fields):
        observe(self._statistics, event, **self._observation_context,
                started_at=self._started_at,
                finished_at=self._last_at if finished_at is None else finished_at,
                delta_s=self._deadline - self._started_at, stage=stage, **fields)

    def finish_observation(self, error=None):
        if self._observation_finished:
            return
        self._observation_finished = True
        if error is not None:
            try:
                self._last_at = _duration(self._clock.now())
            except Exception:
                return  # Missing data, never replace the operation's exception.
        unknown = isinstance(error, TimingViolation)
        if error is not None:
            from ..control_types import ControllerError
            unknown = unknown or isinstance(error, ControllerError)
        elapsed = max(0, self._last_at - self._started_at)
        self.record('sync_window', stage='confirmation', sync_window_s=elapsed,
                    exceeded=int(elapsed > self._deadline - self._started_at),
                    outcome='success' if error is None else 'unknown' if unknown else 'failure')

    @property
    def started_at(self):
        return self._started_at

    @property
    def deadline(self):
        return self._deadline

    @property
    def remaining(self):
        return max(0, self._deadline - _duration(self._clock.now()))

    @property
    def elapsed(self):
        return max(0, _duration(self._clock.now()) - self._started_at)

    def _check(self, *, completed=False, role_deadline=None, stop_margin=0):
        now = _duration(self._clock.now())
        self._last_at = now
        _duration(stop_margin)
        if role_deadline is not None:
            _duration(role_deadline)
        self._freeze_guard._check_at(now)
        if (self._failed or now > self._deadline or not completed and now == self._deadline
                or role_deadline is not None and (
                    now >= role_deadline - stop_margin
                    or not completed and self._deadline > role_deadline - stop_margin)):
            self._failed = True
            raise TimingViolation("Operation window or leader role expired")
        return now

    def before_io(self):
        return self._deadline - self._check()

    def guard_write(self, *, role_deadline, stop_margin=0):
        return self._deadline - self._check(role_deadline=role_deadline,
                                            stop_margin=stop_margin)

    def complete(self, *, role_deadline=None, stop_margin=0):
        return self._check(completed=True, role_deadline=role_deadline,
                           stop_margin=stop_margin) - self._started_at


@dataclass(frozen=True)
class LeaseTiming:
    """Candidate-selected period and distinct physical command tail."""

    period: float
    delta: float
    delta_cmd: float
    expected_work: float = 0

    def __post_init__(self):
        for value in (self.period, self.delta, self.delta_cmd):
            _duration(value, positive=True)
        _duration(self.expected_work)
        minimum = max(8 * self.delta, 3 * self.delta + self.barrier + self.quarantine
                      + self.expected_work + self.stop_margin)
        if self.period < minimum or self.lease_s > 1200:
            raise ValueError("Lease cannot cover acquisition, quarantine, work and stop margin")

    @property
    def clock_rho(self):
        return clock_rho(self.period)

    @property
    def lease_s(self):
        return self.period * clock_factor(self.period)

    @property
    def valid_for(self):
        return self.period

    @property
    def barrier(self):
        return 2 * self.delta

    @property
    def stop_margin(self):
        return self.delta

    @property
    def quarantine(self):
        return self.command_guard

    @property
    def command_guard(self):
        return self.delta_cmd * clock_factor(self.delta_cmd)

    @property
    def term_budget(self):
        return 1200 / clock_factor(1200)

    def lease_deadline(self, sent_at, *, term_started_at):
        _duration(sent_at)
        _duration(term_started_at)
        if sent_at < term_started_at:
            raise ValueError("Renewal predates the original acquisition send")
        return min(sent_at + self.valid_for, term_started_at + self.term_budget)
