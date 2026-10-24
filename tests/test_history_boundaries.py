"""Historical step anchors use their real timestamps; no live API calls."""

from datetime import datetime, timedelta, timezone
from threading import Event
import unittest
from unittest.mock import Mock

from zontcontrol.algorithms.boiler_reset import BoilerReset
from zontcontrol.anomaly import NoHeatDetector
from zontcontrol.autonomy.contracts import ResultStatus, RunContext
from zontcontrol.controller.contracts import (
    HistoryPoint, HistoryResult, HistorySeries, InvalidArguments, Quality,
    SeriesKind, ValueDescriptor, ValueKey, ValueType,
)
from zontcontrol.heating_adapter import from_controller_history
from zontcontrol.history import decode_delta_array, merge_history_points, project_history
from zontcontrol.monitor import assess_history, history_samples


START = datetime.fromtimestamp(1000, timezone.utc) + timedelta(microseconds=665005)
END = datetime.fromtimestamp(1200, timezone.utc)


def descriptor(field='target_temp', *, kind=SeriesKind.STEP, value_type=ValueType.NUMBER):
    return ValueDescriptor(ValueKey(42, 'circuit', 10, field), 'Synthetic history', None,
                           'archive', 'value', value_type, True, True, kind)


def project(rows, *, field='target_temp', kind=SeriesKind.STEP, value_type=ValueType.NUMBER,
            start=START, end=END, received_at=END):
    response = {'z3k_heating_circuit': {'10': {field: rows}}}
    part = project_history(response, descriptor(field, kind=kind, value_type=value_type),
                           start, end, received_at=received_at)
    return merge_history_points((part,))


class StepBoundaryTests(unittest.TestCase):
    def test_last_state_before_fractional_start_keeps_original_timestamp(self):
        rows = [[990, 20], [-10, 23.5], [-100, 24], [1300, 25]]
        points, reasons = project(rows)
        self.assertEqual(points, (HistoryPoint(START.replace(microsecond=0), 23.5),
                                  HistoryPoint(datetime.fromtimestamp(1100, timezone.utc), 24)))
        self.assertNotIn('missing_start', reasons)

    def test_exact_start_state_makes_previous_anchor_unnecessary(self):
        points, reasons = project([[990, 20], [-10, 23.5]], start=START.replace(microsecond=0))
        self.assertEqual(points, (HistoryPoint(START.replace(microsecond=0), 23.5),))
        self.assertNotIn('missing_start', reasons)

    def test_only_previous_state_is_retained_without_later_updates(self):
        points, reasons = project([[990, 20], [1000, 23.5], [995, 22], [1300, 25]])
        self.assertEqual(points, (HistoryPoint(START.replace(microsecond=0), 23.5),))
        self.assertNotIn('missing_start', reasons)
        self.assertIn('missing_end', reasons)

    def test_missing_anchor_does_not_fall_back_to_older_good_state(self):
        points, reasons = project([[990, 23.5], [1000, None]])
        self.assertEqual(points, (HistoryPoint(START.replace(microsecond=0), None, Quality.MISSING),))
        self.assertIn('null_values', reasons)

    def test_conflicting_anchor_remains_unknown(self):
        points, reasons = project([[990, 20], [1000, 23.5], [995, 21], [1000, 24], [1000, 23.5]])
        self.assertEqual(points, (HistoryPoint(START.replace(microsecond=0), None, Quality.UNKNOWN),))
        self.assertIn('conflicting_timestamps', reasons)

    def test_worktime_anchor_keeps_absolute_priority_and_equal_priority_conflicts(self):
        for rows, value, quality in (
            ([[940, 0], [-60, 60], [1000, 0]], 0, Quality.GOOD),
            ([[1000, 0], [1000, 60]], None, Quality.UNKNOWN),
            ([[940, 0], [-60, 60], [1000, None]], None, Quality.MISSING),
        ):
            with self.subTest(rows=rows):
                points, _ = project(rows, field='worktime', value_type=ValueType.INTEGER)
                self.assertEqual(points, (HistoryPoint(START.replace(microsecond=0), value, quality),))

    def test_future_state_is_excluded_even_when_an_anchor_is_available(self):
        points, reasons = project([[1000, 23.5], [1100, 24]], received_at=START)
        self.assertEqual(points, (HistoryPoint(START.replace(microsecond=0), 23.5),))
        self.assertIn('future_points', reasons)

    def test_measurements_and_public_decoder_still_use_exact_window(self):
        rows = [[1000, 23.5], [1100, 24], [1300, 25]]
        points, reasons = project(rows, kind=SeriesKind.MEASUREMENT)
        self.assertEqual(points, (HistoryPoint(datetime.fromtimestamp(1100, timezone.utc), 24),))
        self.assertIn('missing_start', reasons)
        self.assertEqual(decode_delta_array(rows, START, END), [
            {'time': datetime.fromtimestamp(1100, timezone.utc).isoformat(), 'value': 24}])


class SetpointPriorityTests(unittest.TestCase):
    def test_absolute_setpoint_wins_in_either_order_and_at_left_anchor(self):
        for start in (START, START.replace(microsecond=0)):
            for rows in (
                [[940, 55], [-60, 57.6], [1000, 57.7]],
                [[1000, 57.7], [940, 55], [-60, 57.6]],
            ):
                with self.subTest(start=start, rows=rows):
                    points, reasons = project(rows, field='setpoint_temp', start=start)
                    self.assertEqual(points, (HistoryPoint(START.replace(microsecond=0), 57.7),))
                    self.assertNotIn('conflicting_timestamps', reasons)

    def test_null_absolute_wins_and_discarded_null_delta_does_not_taint_quality(self):
        for rows, value, quality, missing in (
            ([[940, 55], [-60, 57.6], [1000, None]], None, Quality.MISSING, True),
            ([[1000, None], [940, 55], [-60, 57.6]], None, Quality.MISSING, True),
            ([[940, 55], [-60, None], [1000, 57.7]], 57.7, Quality.GOOD, False),
        ):
            with self.subTest(rows=rows):
                points, reasons = project(rows, field='setpoint_temp')
                self.assertEqual(points, (HistoryPoint(START.replace(microsecond=0), value, quality),))
                self.assertEqual('null_values' in reasons, missing)

    def test_equal_priority_conflicts_stay_unknown_and_equal_values_deduplicate(self):
        for rows, value, quality in (
            ([[1000, 57.7], [1000, 57.6]], None, Quality.UNKNOWN),
            ([[1000, 57.7], [1000, 57.7]], 57.7, Quality.GOOD),
            ([[940, 55], [-60, 57.7], [940, 55], [-60, 57.6]], None, Quality.UNKNOWN),
            ([[1000, 57.7], [1000, 57.6], [940, 55], [-60, 57.7]], None, Quality.UNKNOWN),
        ):
            with self.subTest(rows=rows):
                points, reasons = project(rows, field='setpoint_temp')
                self.assertEqual(points, (HistoryPoint(START.replace(microsecond=0), value, quality),))
                self.assertEqual('conflicting_timestamps' in reasons, quality is Quality.UNKNOWN)

    def test_conflicts_between_parts_stay_unknown(self):
        parts = [project([[1000, value]], field='setpoint_temp') for value in (57.6, 57.7)]
        points, reasons = merge_history_points(parts)
        self.assertEqual(points, (HistoryPoint(START.replace(microsecond=0), None, Quality.UNKNOWN),))
        self.assertIn('conflicting_timestamps', reasons)

    def test_other_fields_and_public_decoder_keep_their_conflicts(self):
        rows = [[940, 55], [-60, 57.6], [1000, 57.7]]
        points, reasons = project(rows, field='target_temp')
        self.assertEqual(points, (HistoryPoint(START.replace(microsecond=0), None, Quality.UNKNOWN),))
        self.assertIn('conflicting_timestamps', reasons)
        decoded = decode_delta_array(rows, START.replace(microsecond=0), END)
        self.assertEqual([p['value'] for p in decoded], [57.6, 57.7])

    def test_setpoint_conflict_keeps_detector_buffer_and_normal_assessment(self):
        end = datetime(2026, 10, 24, 21, 0, 57, 994500, tzinfo=timezone.utc)
        start = end - timedelta(minutes=46)
        anchor = int(start.replace(second=0, microsecond=0).timestamp())
        conflict = anchor + 44 * 60
        response = {
            'z3k_temperature': {
                '7': [[anchor + minute * 60, 23.3] for minute in range(47)],
                '8': [[anchor + minute * 60, 40.5] for minute in range(47)],
            },
            'z3k_heating_circuit': {'10': {
                'target_temp': [[anchor, 23.5]],
                'target_sensor_id': [[anchor, 7]],
                'setpoint_temp': [[anchor, 57.5], [-2640, 57.6], [conflict, 57.7],
                                  [-60, 57.8], [-60, 57.9]],
                'worktime': [[anchor, 0]],
            }},
            'z3k_web_element': {'9': {'state': [[anchor, 0]]}},
        }
        rows = []
        for kind, element_id, field, value_type in (
            ('sensor', 7, 'value', ValueType.NUMBER),
            ('sensor', 8, 'value', ValueType.NUMBER),
            ('circuit', 10, 'target_temp', ValueType.NUMBER),
            ('circuit', 10, 'target_sensor_id', ValueType.INTEGER),
            ('circuit', 10, 'setpoint_temp', ValueType.NUMBER),
            ('circuit', 10, 'worktime', ValueType.INTEGER),
            ('toggle_buttons', 9, 'active', ValueType.BOOLEAN),
        ):
            series_kind = SeriesKind.MEASUREMENT if kind == 'sensor' else SeriesKind.STEP
            desc = ValueDescriptor(ValueKey(42, kind, element_id, field), 'Synthetic', None,
                                   'archive', 'value', value_type, True, True, series_kind)
            points, reasons = merge_history_points((project_history(
                response, desc, start, end, received_at=end),))
            rows.append(HistorySeries(desc.key, value_type, None, 'archive', series_kind, points, reasons))
        history = from_controller_history(HistoryResult(42, start, end, end, tuple(rows)),
            device_id=42, circuit_id=10, air_id=7, coolant_id=8, reset_id=9)
        target = next(p for p in history.coolant_target.points if p.measured_at.timestamp() == conflict)
        self.assertEqual((target.value, target.quality), (57.7, 'good'))
        detector = NoHeatDetector(require_known_reset=True)
        for sample in history_samples(history):
            detector.update(sample)
        self.assertEqual(len(detector.samples), 40)
        assessment = assess_history(history)
        self.assertEqual(assessment['state_at_period_end'], 'normal')
        self.assertEqual(assessment['valid_temperature_samples'], 45)


class HistoryAnchorContractTests(unittest.TestCase):
    def series(self, points, *, kind=SeriesKind.STEP):
        return HistorySeries(ValueKey(42, 'circuit', 10, 'target_temp'), ValueType.NUMBER,
                             None, 'archive', kind, tuple(points))

    def test_one_step_anchor_before_start_preserves_coverage_and_quality(self):
        anchor = HistoryPoint(START.replace(microsecond=0), 23.5)
        row = self.series((anchor, HistoryPoint(END, 24)))
        result = HistoryResult(42, START, END, END, (row,))
        self.assertTrue(result.complete)
        self.assertEqual(row.coverage_start, anchor.measured_at)
        bad = self.series((HistoryPoint(anchor.measured_at, None, Quality.MISSING), HistoryPoint(END, 24)))
        self.assertFalse(HistoryResult(42, START, END, END, (bad,)).complete)

    def test_extra_predecessors_measurements_and_future_points_are_rejected(self):
        anchor = HistoryPoint(START.replace(microsecond=0), 23.5)
        for points, kind, received_at in (
            ((HistoryPoint(anchor.measured_at - timedelta(seconds=1), 22), anchor), SeriesKind.STEP, END),
            ((anchor,), SeriesKind.MEASUREMENT, END),
            ((anchor, HistoryPoint(END + timedelta(seconds=1), 24)), SeriesKind.STEP, END),
            ((anchor, HistoryPoint(END, 24)), SeriesKind.STEP, END - timedelta(seconds=1)),
        ):
            with self.subTest(points=points, kind=kind), self.assertRaises(InvalidArguments):
                HistoryResult(42, START, END, received_at, (self.series(points, kind=kind),))


class BoilerBoundaryRegressionTests(unittest.TestCase):
    def test_fractional_window_keeps_both_step_states_and_assesses_without_commands(self):
        start = datetime(2026, 10, 1, microsecond=665005, tzinfo=timezone.utc)
        end = start + timedelta(minutes=46)
        anchor = int(start.timestamp())
        response = {
            'z3k_temperature': {
                '7': [[anchor + minute * 60, 22] for minute in range(47)],
                '8': [[anchor + minute * 60, 40] for minute in range(47)],
            },
            'z3k_heating_circuit': {'10': {
                'target_temp': [[anchor, 23.5], [-2760, 23.5]],
                'target_sensor_id': [[anchor, 7], [-2760, 7]],
                'setpoint_temp': [[anchor, 60], [-2760, 60]],
                'worktime': [[anchor, 0], [-2760, 0]],
            }},
            'z3k_web_element': {'9': {'state': [[int((end - timedelta(hours=1)).timestamp()), 0]]}},
        }
        descriptions = []
        rows = []
        reset_start = end - timedelta(minutes=120)
        for kind, element_id, field, value_type in (
            ('sensor', 7, 'value', ValueType.NUMBER),
            ('sensor', 8, 'value', ValueType.NUMBER),
            ('circuit', 10, 'target_temp', ValueType.NUMBER),
            ('circuit', 10, 'target_sensor_id', ValueType.INTEGER),
            ('circuit', 10, 'setpoint_temp', ValueType.NUMBER),
            ('circuit', 10, 'worktime', ValueType.INTEGER),
            ('toggle_buttons', 9, 'active', ValueType.BOOLEAN),
        ):
            series_kind = SeriesKind.MEASUREMENT if kind == 'sensor' else SeriesKind.STEP
            desc = ValueDescriptor(ValueKey(42, kind, element_id, field), 'Synthetic', None,
                                   'archive', 'value', value_type, True, True, series_kind)
            descriptions.append(desc)
            lower = reset_start if kind == 'toggle_buttons' else start
            points, reasons = merge_history_points((project_history(
                response, desc, lower, end, received_at=end),))
            rows.append(HistorySeries(desc.key, value_type, None, 'archive', series_kind, points, reasons))
        telemetry = HistoryResult(42, start, end, end, tuple(rows[:-1]))
        reset = HistoryResult(42, reset_start, end, end, (rows[-1],))
        controller = Mock()
        controller.describe_values.return_value = tuple(descriptions)
        controller.read_history.side_effect = [telemetry, reset]
        module = BoilerReset(controller, wall_clock=lambda: end, reader=lambda _: {
            'device_id': 42, 'circuit_id': 10, 'air_sensor_id': 7, 'coolant_sensor_id': 8,
            'reset_control_id': 9, 'check_interval_seconds': 60})
        result = module.run_once(RunContext('boiler_reset', Event(), None))
        self.assertIs(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.value['assessment']['state_at_period_end'], 'normal')
        self.assertEqual(result.value['assessment']['valid_temperature_samples'], 45)
        self.assertFalse(result.value['reset_performed'])
        for field in ('target_temp', 'target_sensor_id'):
            row = next(row for row in telemetry.series if row.key.field == field)
            self.assertEqual(row.points[0].measured_at, start.replace(microsecond=0))
        controller.prepare_command.assert_not_called()
        controller.execute_command.assert_not_called()
        controller.read_current.assert_not_called()
