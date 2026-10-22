import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from zontcontrol.history import cli_period, decode_delta_array, merge_history_points, project_history
from zontcontrol.controller.contracts import (
    HistoryPoint, Quality, SeriesKind, ValueDescriptor, ValueKey, ValueType,
)


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
                     [[True, 21]], [[1000, 21], [-60.0, 22]],
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


class WorktimeHistoryTests(unittest.TestCase):
    def project(self, rows, *, kind='circuit', field='worktime', value_type=ValueType.INTEGER,
                start=START, end=END, received_at=END):
        descriptor = ValueDescriptor(
            ValueKey(42, kind, 10, field), 'History', None, 'archive', 'value',
            value_type, True, True, SeriesKind.STEP)
        if kind == 'sensor':
            response = {'z3k_temperature': {'10': rows}}
        elif kind == 'toggle_buttons':
            response = {'z3k_web_element': {'10': {'state': rows}}}
        else:
            response = {'z3k_heating_circuit': {'10': {field: rows}}}
        return project_history(response, descriptor, start, end, received_at=received_at)

    @staticmethod
    def at_start(*candidates):
        # Each delta has its own anchor outside the window. Its expanded time
        # matches START, but its origin must not inherit the anchor's priority.
        rows = []
        for absolute, value in candidates:
            rows.extend([[1000, value]] if absolute else [[940, 0], [-60, value]])
        return rows

    def test_absolute_wins_in_both_orders_and_original_example(self):
        for timestamp in (1000, 1792676460):
            moment = datetime.fromtimestamp(timestamp, timezone.utc)
            for absolute_first in (False, True):
                absolute = [[timestamp, 0]]
                delta = [[timestamp - 60, 0], [-60, 60]]
                rows = absolute + delta if absolute_first else delta + absolute
                with self.subTest(timestamp=timestamp, absolute_first=absolute_first):
                    points, reasons = self.project(rows, start=moment,
                                                  end=moment + timedelta(minutes=2),
                                                  received_at=moment + timedelta(minutes=2))
                    self.assertEqual(points, (HistoryPoint(moment, 0, Quality.GOOD),))
                    self.assertEqual(reasons, ('missing_end',))
                    # The public decoder keeps its original format and duplicates.
                    self.assertEqual(decode_delta_array(rows, moment, moment), [
                        {'time': moment.isoformat(), 'value': value}
                        for value in ([0, 60] if absolute_first else [60, 0])])

    def test_equal_duplicates_collapse_at_either_priority(self):
        for candidates in (((True, 60), (True, 60)),
                           ((False, 60), (False, 60)),
                           ((True, 60), (False, 60))):
            with self.subTest(candidates=candidates):
                points, reasons = self.project(self.at_start(*candidates))
                self.assertEqual(points, (HistoryPoint(START, 60),))
                self.assertEqual(reasons, ('missing_end',))

    def test_equal_priority_conflict_survives_later_records(self):
        for absolute in (False, True):
            for tail in ((), ((absolute, 60),), ((False, 0),)):
                with self.subTest(absolute=absolute, tail=tail):
                    rows = self.at_start((absolute, 60), (absolute, 0), *tail)
                    points, reasons = self.project(rows)
                    self.assertEqual(points, (HistoryPoint(START, None, Quality.UNKNOWN),))
                    self.assertEqual(set(reasons), {
                        'conflicting_timestamps', 'missing_end', 'null_values'})

    def test_absolute_resolves_conflicting_lower_priority_group_in_any_position(self):
        deltas = ((False, 60), (False, 30))
        for candidates in (((True, 0), *deltas),
                           (deltas[0], (True, 0), deltas[1]),
                           (*deltas, (True, 0))):
            with self.subTest(candidates=candidates):
                points, reasons = self.project(self.at_start(*candidates))
                self.assertEqual(points, (HistoryPoint(START, 0),))
                self.assertEqual(reasons, ('missing_end',))

    def test_absolute_null_wins_and_discarded_null_delta_does_not_make_gap(self):
        for absolute_value, delta_value in ((None, 60), (0, None)):
            for absolute_first in (False, True):
                candidates = ((True, absolute_value), (False, delta_value))
                if not absolute_first:
                    candidates = candidates[::-1]
                with self.subTest(candidates=candidates):
                    points, reasons = self.project(self.at_start(*candidates))
                    quality = Quality.MISSING if absolute_value is None else Quality.GOOD
                    self.assertEqual(points, (HistoryPoint(START, absolute_value, quality),))
                    self.assertEqual('null_values' in reasons, absolute_value is None)
                    self.assertNotIn('conflicting_timestamps', reasons)

    def test_future_points_are_filtered_before_duplicate_selection(self):
        points, reasons = self.project([[1000, 0], [-60, 60], [1060, 0]], received_at=START)
        self.assertEqual(points, (HistoryPoint(START, 0),))
        self.assertEqual(set(reasons), {'future_points', 'missing_end'})
        points, reasons = self.project([[1060, 0]], received_at=START)
        self.assertEqual(points, ())
        self.assertEqual(set(reasons), {'future_points', 'missing_part'})

    def test_different_seconds_in_one_minute_keep_both_values(self):
        first, second = (datetime.fromtimestamp(t, timezone.utc) for t in (1001, 1031))
        for rows in ([[1001, 0], [-30, 60]], [[1001, 0], [-30, 60], [1001, 0]]):
            with self.subTest(rows=rows):
                points, reasons = self.project(rows)
                self.assertEqual(points, (HistoryPoint(first, 0), HistoryPoint(second, 60)))
                self.assertEqual(reasons, ('missing_start', 'missing_end'))

    def test_normalized_parts_keep_conflict_without_timestamp_origin(self):
        # Priority is local to one raw response; ordinary HistoryPoint DTOs do
        # not carry the source timestamp representation across request parts.
        absolute = self.project(self.at_start((True, 0)))
        delta = self.project(self.at_start((False, 60)))
        for parts in ((absolute, delta, absolute), (delta, absolute, delta)):
            with self.subTest(parts=parts):
                points, reasons = merge_history_points(parts)
                self.assertEqual(points, (HistoryPoint(START, None, Quality.UNKNOWN),))
                self.assertIn('conflicting_timestamps', reasons)

    def test_other_fields_keep_conservative_conflicts(self):
        for kind, field, value_type in (
            ('sensor', 'value', ValueType.NUMBER),
            ('circuit', 'target_temp', ValueType.NUMBER),
            ('circuit', 'setpoint_temp', ValueType.NUMBER),
            ('circuit', 'mode_id', ValueType.INTEGER),
            ('circuit', 'target_sensor_id', ValueType.INTEGER),
            ('toggle_buttons', 'active', ValueType.BOOLEAN),
        ):
            for absolute_first in (False, True):
                candidates = ((True, 0), (False, 1))
                if not absolute_first:
                    candidates = candidates[::-1]
                with self.subTest(kind=kind, field=field, absolute_first=absolute_first):
                    part = self.project(self.at_start(*candidates), kind=kind, field=field,
                                        value_type=value_type)
                    points, reasons = merge_history_points((part,))
                    self.assertEqual(points, (HistoryPoint(START, None, Quality.UNKNOWN),))
                    self.assertIn('conflicting_timestamps', reasons)

    def test_invalid_lower_priority_worktime_is_not_hidden(self):
        for invalid in (True, 1.5):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.project(self.at_start((False, invalid), (True, 0)))
