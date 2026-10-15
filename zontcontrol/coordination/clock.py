"""Local BOOTTIME instants and the agreed, period-specific drift policy.

These are conditional timing assumptions, not measured bounds on ZONT or clocks.
No absolute instant is exchanged with another participant.
"""

from dataclasses import dataclass
import math
import time


def _duration(value, *, positive=False):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or value < 0 or positive and value == 0):
        raise ValueError("Expected a finite duration")
    return value


def clock_rho(period):
    """Choose once, using the original period before increasing its duration."""
    _duration(period, positive=True)
    return .1 if period <= 60 else .05 if period <= 300 else .01 if period <= 600 else .005


def clock_factor(period):
    rho = clock_rho(period)
    return (1 + rho) / (1 - rho)


class BootClock:
    """Linux suspend-inclusive monotonic clock, deliberately without fallback."""

    def __init__(self):
        if not hasattr(time, "CLOCK_BOOTTIME"):
            raise RuntimeError("Leader coordination requires CLOCK_BOOTTIME")
        self._clock_id = time.CLOCK_BOOTTIME
        try:
            self.now()
        except (OSError, ValueError):
            raise RuntimeError("Leader coordination requires CLOCK_BOOTTIME") from None

    def now(self):
        return time.clock_gettime(self._clock_id)

    def wait(self, cancelled, seconds):
        return cancelled.wait(_duration(seconds))


@dataclass(frozen=True)
class LeaseTiming:
    """Candidate-selected useful period; serialized L is K(P)*P, at most 1200s."""

    period: float
    delta: float
    expected_work: float = 0
    barrier_jitter: float = 0
    poll_jitter: float = 0

    def __post_init__(self):
        for value in (self.period, self.delta):
            _duration(value, positive=True)
        for value in (self.expected_work, self.barrier_jitter, self.poll_jitter):
            _duration(value)
        minimum = max(8 * self.delta, 3 * self.delta + self.barrier + self.expected_work)
        if (self.period < minimum or self.lease_s > 1200
                or self.barrier <= self.delta_factor * self.delta):
            raise ValueError("Lease cannot cover acquisition, expected work and release")

    @property
    def clock_rho(self):
        return clock_rho(self.period)

    @property
    def lease_s(self):
        return self.period * clock_factor(self.period)

    @property
    def valid_for(self):
        return self.lease_s / clock_factor(self.period)

    @property
    def renew_after(self):
        return self.valid_for / 3

    @property
    def poll_after(self):
        return self.lease_s / 10 + self.poll_jitter

    @property
    def delta_factor(self):
        return clock_factor(self.delta)

    @property
    def barrier(self):
        return 2 * self.delta + self.barrier_jitter

    @property
    def term_budget(self):
        return 1200 / clock_factor(1200)
