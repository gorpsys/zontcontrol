"""Local BOOTTIME instants and the agreed, period-specific drift policy.

These are conditional timing assumptions, not measured bounds on ZONT or clocks.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID, uuid4
import math
import time

from ..control_types import UUIDFactory

if TYPE_CHECKING:
    from .ports import Clock, EpochSampler


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


def _instant(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("Expected a finite clock instant")
    return value


def _uuid(value):
    if not isinstance(value, UUID) or not value.int:
        raise ValueError("Expected a nonzero UUID")
    return value


@dataclass(frozen=True)
class LeaderTimeline:
    """One process incarnation's fixed UNIX/BOOTTIME binding, immune to NTP.

    Snapshot uncertainty is bounded by delta. Accuracy and pairwise absolute
    skew are model assumptions, not proved by capture. Further reads use BOOTTIME.
    """

    _clock: 'Clock'
    epoch0: float
    boot0: float
    clock_id: UUID

    def __post_init__(self):
        _instant(self.epoch0)
        _duration(self.boot0)
        _uuid(self.clock_id)

    @classmethod
    def capture(cls, clock: 'Clock', *, delta: float,
                epoch_sampler: 'EpochSampler' = time.time,
                uuid_factory: UUIDFactory = uuid4):
        from .timing import TimingViolation

        _duration(delta, positive=True)
        clock_id = _uuid(uuid_factory())
        before = _duration(clock.now())
        epoch = _instant(epoch_sampler())
        after = _duration(clock.now())
        if not 0 <= after - before <= delta:
            raise TimingViolation("Ambiguous leader clock snapshot")
        return cls(clock, epoch, before + (after - before) / 2, clock_id)

    def now(self):
        return self.epoch0 + (_duration(self._clock.now()) - self.boot0)


@dataclass(frozen=True)
class GrantInterval:
    """Executor-local admission bounds; the upper bound is exclusive."""

    usable_from: float
    expires_at: float

    def __post_init__(self):
        from .timing import TimingViolation

        _duration(self.usable_from)
        _duration(self.expires_at)
        if self.usable_from >= self.expires_at:
            raise TimingViolation("Grant has no usable local interval")

    def contains(self, now):
        return self.usable_from <= _duration(now) < self.expires_at


@dataclass(frozen=True)
class ClockSample:
    """Causal sample for one matched nonce/revision, retained for repeated reads.

    sent_at is the *first* send of that nonce, received_at its first matched
    reply. Callers must match the reply's nonce/clock_id before constructing it;
    arbitrary register reads cannot establish or refresh this causal binding.
    """

    clock_id: UUID
    nonce: UUID
    sampled_at: float
    sent_at: float
    received_at: float
    lease_seconds: float
    clock_rho: float

    def __post_init__(self):
        _uuid(self.clock_id)
        _uuid(self.nonce)
        _instant(self.sampled_at)
        _duration(self.sent_at)
        _duration(self.received_at)
        _duration(self.lease_seconds, positive=True)
        if (self.received_at < self.sent_at
                or type(self.clock_rho) not in (int, float)
                or self.clock_rho != clock_rho(self.lease_seconds)):
            raise ValueError("Invalid causal sample or original-period rho")

    @property
    def factor(self):
        return (1 + self.clock_rho) / (1 - self.clock_rho)

    def leader_bounds(self, now):
        _duration(now)
        if now < self.received_at:
            raise ValueError("Cannot project before receipt of the matched reply")
        return (self.sampled_at + (now - self.received_at) / self.factor,
                self.sampled_at + (now - self.sent_at) * self.factor)

    def project(self, *, valid_from, valid_until, local_lease_deadline):
        _instant(valid_from)
        _instant(valid_until)
        _duration(local_lease_deadline)
        if valid_from >= valid_until:
            raise ValueError("Invalid grant interval")
        return GrantInterval(
            max(self.received_at,
                self.received_at + self.factor * (valid_from - self.sampled_at)),
            min(self.sent_at + (valid_until - self.sampled_at) / self.factor,
                local_lease_deadline),
        )


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
