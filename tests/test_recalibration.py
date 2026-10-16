import random
import unittest
from datetime import datetime, timedelta, timezone

from scripts.recalibrate_archive import FastTraces, trace_window
from zontcontrol.anomaly import DetectionConfig, HeatingSample


class FastReplayTests(unittest.TestCase):
    def test_overlapping_windows_match_independent_replays_with_cycles_and_gaps(self):
        randomizer = random.Random(20260828)
        start = datetime(2026, 7, 22, tzinfo=timezone.utc)
        samples = []
        water = 50
        for minute in range(240):
            if minute < 65 or minute > 180:
                water += randomizer.choice((-5, -1, -0.2, 0, 0.3, 1, 8))
            else:
                water -= 0.1
            samples.append(HeatingSample(start + timedelta(minutes=minute), 22, 23,
                                         None if minute == 80 else water, 85,
                                         70 if minute == 150 else 60,
                                         minute in (120, 121), minute != 170,
                                         start + timedelta(minutes=122) if minute >= 122 else None))
        for gap in (3, 8):
            for gain in (0.5, 3):
                with self.subTest(gap=gap, gain=gain):
                    config = DetectionConfig(supply_gap=gap, pulse_gain=gain)
                    fast = FastTraces(samples, config)
                    for i in range(44, len(samples)):
                        self.assertEqual(fast.window(i), trace_window(samples[i - 44:i + 1], config), i)


if __name__ == "__main__":
    unittest.main()
