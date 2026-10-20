"""Explicitly advanced elapsed and wall time for offline lifecycle tests."""

import math
from datetime import datetime, timedelta, timezone
from threading import Lock


class FakeClock:
    def __init__(self, *, start=0.0, wall_start=None):
        if not math.isfinite(start) or start < 0:
            raise ValueError("invalid fake clock")
        self._now = float(start)
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
        self.advance(seconds)
        return cancelled.is_set()

    def advance(self, seconds):
        """Advance both clocks by the same duration."""
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("cannot move fake time backwards")
        with self._lock:
            self._now += seconds
            self._wall += timedelta(seconds=seconds)
