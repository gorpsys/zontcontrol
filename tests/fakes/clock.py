"""An explicitly advanced clock, including configurable oscillator drift."""

import math
from datetime import datetime, timedelta, timezone
from threading import Lock


class FakeClock:
    def __init__(self, *, start=0.0, rate=1.0, wall_start=None):
        if not math.isfinite(start) or not math.isfinite(rate) or start < 0 or rate <= 0:
            raise ValueError("invalid fake clock")
        self._now = float(start)
        self._rate = float(rate)
        self._wall = wall_start or datetime(2026, 10, 15, tzinfo=timezone.utc)
        if self._wall.tzinfo is None or self._wall.utcoffset() is None:
            raise ValueError("fake wall clock requires a timezone")
        self._lock = Lock()

    def now(self):
        with self._lock:
            return self._now

    def wall_now(self):
        with self._lock:
            return self._wall

    def wait(self, cancelled, seconds):
        """Advance a local-duration wait without sleeping in offline tests."""
        if cancelled.is_set():
            return True
        self.advance(seconds / self._rate)
        return cancelled.is_set()

    def advance(self, real_seconds):
        """Advance elapsed time, also usable to model a suspended participant."""
        if not math.isfinite(real_seconds) or real_seconds < 0:
            raise ValueError("cannot move fake time backwards")
        with self._lock:
            self._now += real_seconds * self._rate
            self._wall += timedelta(seconds=real_seconds)
