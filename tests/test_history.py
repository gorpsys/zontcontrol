import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from zontcontrol.history import cli_period, decode_delta_array, merge_history_points
from zontcontrol.controller.contracts import HistoryPoint, Quality


START = datetime.fromtimestamp(1000, timezone.utc)
END = datetime.fromtimestamp(1200, timezone.utc)


def device():
    return {
        "id": 42, "name": "Дом", "online": True, "device_info": {"model": "SMART 2.0"},
        "sensors": [{"id": 7, "name": "Воздух", "type": "temperature", "status": "ok"}],
        "circuits": [{"id": 10, "name": "Отопление", "type": "consumer", "active": False,
                      "is_off": False, "in_summer_mode": False}],
        "modes": [{"id": 20, "name": "Расписание"}],
    }


def response():
    return {
        "device_id": 42, "ok": True, "time_truncated": False,
        "z3k_temperature": {"7": [[1000, 21], [-60, 21.5], [-60, None]],
                            "99": [[1000, 0]]},
        "z3k_heating_circuit": {
            "10": {"target_temp": [[1000, 22]], "setpoint_temp": [[1000, 15], [-60, 45]],
                   "mode_id": [[1000, 20], [-100, 0]], "worktime": [[1000, 0]]},
            "11": {"target_temp": [[1000, 15]]},
        },
    }


def encoded(data):
    return io.BytesIO(json.dumps(data).encode())


class DeltaArrayTests(unittest.TestCase):
    def test_merge_conflict_remains_unknown_after_more_duplicate_points(self):
        one = HistoryPoint(START, 1)
        two = HistoryPoint(START, 2)
        points, reasons = merge_history_points([
            ((one,), ('missing_end',)), ((two,), ('missing_start',)), ((one,), ())])
        self.assertEqual(points, (HistoryPoint(START, None, Quality.UNKNOWN),))
        self.assertEqual(reasons, ('missing_end', 'missing_start', 'conflicting_timestamps'))

    def test_negative_delta_is_forward_time_and_absolute_reset_is_supported(self):
        points = decode_delta_array([[1000, 21], [-60, 22], [1150, 20]], START, END)
        times = [datetime.fromisoformat(point["time"]).timestamp() for point in points]
        self.assertEqual(times, [1000, 1060, 1150])
        self.assertEqual([point["value"] for point in points], [21, 22, 20])

    def test_window_nulls_and_zero_are_preserved_without_interpolation(self):
        points = decode_delta_array([[900, 20], [-100, None], [-50, 0], [1300, 22]], START, END)
        self.assertEqual([point["value"] for point in points], [None, 0])

    def test_invalid_points_and_values_fail(self):
        for rows in [[[-60, 21]], [[0, 21]], [[1000.5, 21]], [[1000, True]],
                     [[1000, float("nan")]], [[1000, "21"]], [[1000]], None]:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                decode_delta_array(rows, START, END)
        with self.assertRaises(ValueError):
            decode_delta_array([[1000, 1.5]], START, END, integer=True)

    def test_period_timezone_and_relative_hours(self):
        start, end = cli_period("2026-10-12T10:00:00+03:00", "2026-10-12T11:00:00+03:00", None)
        self.assertEqual(start.hour, 7)
        self.assertEqual(end - start, timedelta(hours=1))
        start, end = cli_period(None, "2026-10-12T11:00:00Z", 24)
        self.assertEqual(end - start, timedelta(hours=24))
        for args in [(None, None, 0), (None, None, float("nan")),
                     ("2026-10-12T10:00:00", None, None),
                     ("2026-10-12T10:00:00Z", None, 24),
                     ("2026-10-12T11:00:00Z", "2026-10-12T10:00:00Z", None)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                cli_period(*args)
