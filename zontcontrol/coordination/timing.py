"""Unconnected v2 timing policy and local guards for bounded protocol I/O.

These checks detect violations of the assumed timing model. They cannot cancel
an already sent write or prove server deadlines, linearizability or clock bounds.
"""

from dataclasses import dataclass

from .clock import _duration, clock_factor, clock_rho


class TimingViolation(ValueError):
    """A local operation, role or expected wakeup exceeded its timing bound."""


class FreezeGuard:
    """Keep the expected resume instant, including an intentional wait's target."""

    def __init__(self, clock, delta, *, expected_resume_at=None):
        self._clock = clock
        self._delta = _duration(delta, positive=True)
        self._expected_resume_at = _duration(
            clock.now() if expected_resume_at is None else expected_resume_at)
        self._failed = False

    def expect_resume(self, when):
        self._expected_resume_at = _duration(when)

    def check(self):
        return self._check_at(_duration(self._clock.now()))

    def _check_at(self, now):
        gap = max(0, now - self._expected_resume_at)
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

    @property
    def started_at(self):
        return self._started_at

    @property
    def deadline(self):
        return self._deadline

    @property
    def remaining(self):
        return max(0, self._deadline - _duration(self._clock.now()))

    def _check(self, *, completed=False, role_deadline=None, stop_margin=0):
        now = _duration(self._clock.now())
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
class V2LeaseTiming:
    """Candidate-selected period; no pacing choices or runtime v1 integration."""

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
