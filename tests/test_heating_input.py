from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timedelta, timezone
import unittest

from zontcontrol.controller.contracts import (
    HistoryPoint, HistoryResult, HistorySeries, Quality, SeriesKind, ValueKey, ValueType,
)
from zontcontrol.heating import HeatingHistory, HeatingPoint, HeatingSeries
from zontcontrol.heating_adapter import from_controller_history


START = datetime(2026, 10, 1, tzinfo=timezone.utc)
END = START + timedelta(minutes=46)
BINDINGS = dict(device_id=42, circuit_id=7, air_id=7, coolant_id=8, reset_id=7)


def history(**changes):
    return HeatingHistory(device_id=42, circuit_id=7, air_sensor_id=7,
                          coolant_sensor_id=8, reset_control_id=7,
                          start=START, end=END, received_at=END, **changes)


def controller_series(kind, element_id, field, value, *, quality=Quality.GOOD,
                      value_type=ValueType.NUMBER, points=None):
    return HistorySeries(ValueKey(42, kind, element_id, field), value_type,
                         '°C' if kind == 'sensor' else None, 'archive-api',
                         SeriesKind.MEASUREMENT if kind == 'sensor' else SeriesKind.STEP,
                         (HistoryPoint(START, value, quality),) if points is None else points,
                         ('gap',))


class HeatingModelTests(unittest.TestCase):
    def test_exact_fields_frozen_values_and_utc_precision(self):
        self.assertEqual([f.name for f in fields(HeatingPoint)], ['measured_at', 'value', 'quality'])
        self.assertEqual([f.name for f in fields(HeatingSeries)], [
            'points', 'source', 'unit', 'value_type', 'kind', 'incomplete_reasons'])
        self.assertEqual([f.name for f in fields(HeatingHistory)], [
            'device_id', 'circuit_id', 'air_sensor_id', 'coolant_sensor_id', 'reset_control_id',
            'start', 'end', 'received_at', 'air', 'coolant', 'air_target', 'coolant_target',
            'worktime', 'target_sensor_id', 'reset', 'time_truncated', 'incomplete_reasons'])
        local = datetime(2026, 10, 1, 3, 0, 0, 123456, timezone(timedelta(hours=3)))
        point = HeatingPoint(local, 23.5, 'stale')
        self.assertEqual(point.measured_at, START.replace(microsecond=123456))
        self.assertIs(point.measured_at.tzinfo, timezone.utc)
        row = HeatingSeries((point,), 'sensor-api', '°C', 'number', 'measurement', ('gap',))
        data = history(air=row)
        for obj, field, value in ((point, 'value', None), (row, 'points', ()),
                                  (data, 'air', None)):
            with self.subTest(obj=type(obj).__name__), self.assertRaises(FrozenInstanceError):
                setattr(obj, field, value)
        self.assertEqual((point.value, point.quality), (23.5, 'stale'))
        offset = timezone(timedelta(hours=3))
        normalized = replace(data, start=START.astimezone(offset), end=END.astimezone(offset),
                             received_at=END.astimezone(offset))
        for name in ('start', 'end', 'received_at'):
            self.assertEqual(getattr(normalized, name), getattr(data, name))
            self.assertIs(getattr(normalized, name).tzinfo, timezone.utc)

    def test_missing_empty_initial_and_duplicate_points_are_distinct(self):
        before = HeatingPoint(START - timedelta(days=1), 12, 'good')
        first = HeatingPoint(START, 20, 'good')
        last = HeatingPoint(START, None, 'missing')
        row = HeatingSeries((before, first, last))
        data = history(air=row, reset=HeatingSeries(()))
        self.assertEqual(data.air.points, (before, first, last))
        self.assertIsNone(data.coolant)
        self.assertEqual(data.reset.points, ())
        self.assertIsNone(history().reset)
        self.assertEqual((row.source, row.unit, row.value_type, row.kind), (None,) * 4)

    def test_scalar_and_quality_are_preserved_without_numeric_policy(self):
        for value in (None, False, True, 0, -1, 61, 1.5, 'unknown', float('inf')):
            for quality in ('good', 'unknown', 'missing', 'stale'):
                with self.subTest(value=value, quality=quality):
                    point = HeatingPoint(START, value, quality)
                    self.assertIs(point.value, value)
                    self.assertEqual(point.quality, quality)

    def test_invalid_point_structure_is_value_error(self):
        for changes in ({'measured_at': START.replace(tzinfo=None)},
                        {'measured_at': START.isoformat()}, {'value': []},
                        {'value': {}}, {'quality': 'bad'}, {'quality': Quality.GOOD}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                HeatingPoint(**{'measured_at': START, 'value': 20, 'quality': 'good', **changes})

    def test_invalid_series_structure_is_value_error(self):
        point = HeatingPoint(START, 20, 'good')
        later = HeatingPoint(START + timedelta(microseconds=1), 21, 'good')
        for changes in ({'points': [point]}, {'points': (None,)}, {'points': (later, point)},
                        {'source': 1}, {'unit': []}, {'value_type': ValueType.NUMBER},
                        {'kind': SeriesKind.STEP}, {'incomplete_reasons': ['gap']},
                        {'incomplete_reasons': (1,)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                HeatingSeries(**{'points': (point,), **changes})

    def test_invalid_history_structure_is_value_error(self):
        valid = history()
        changes = [{name: value} for name in ('device_id', 'circuit_id', 'air_sensor_id',
                   'coolant_sensor_id', 'reset_control_id') for value in (False, 0, -1, 1.5, '7')]
        changes += [{'coolant_sensor_id': valid.air_sensor_id}, {'end': START},
                    {'start': END}, {'time_truncated': 1}, {'air': ()},
                    {'incomplete_reasons': ['gap']}, {'incomplete_reasons': (None,)}]
        changes += [{name: START.replace(tzinfo=None)} for name in ('start', 'end', 'received_at')]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(valid, **change)


class ControllerHeatingAdapterTests(unittest.TestCase):
    def test_all_bindings_values_quality_and_metadata_survive(self):
        rows = (
            controller_series('sensor', 7, 'value', 21.125, quality=Quality.STALE),
            controller_series('sensor', 8, 'value', None, quality=Quality.MISSING),
            controller_series('circuit', 7, 'target_temp', 23),
            controller_series('circuit', 7, 'setpoint_temp', 82),
            controller_series('circuit', 7, 'worktime', 60, value_type=ValueType.INTEGER),
            controller_series('circuit', 7, 'target_sensor_id', 7, value_type=ValueType.INTEGER),
            controller_series('toggle_buttons', 7, 'active', False, value_type=ValueType.BOOLEAN),
        )
        original = HistoryResult(42, START, END, END + timedelta(seconds=1), rows, True, ('partial',))
        adapted = from_controller_history(original, **BINDINGS)
        self.assertEqual((adapted.device_id, adapted.circuit_id, adapted.air_sensor_id,
                          adapted.coolant_sensor_id, adapted.reset_control_id), (42, 7, 7, 8, 7))
        self.assertEqual((adapted.start, adapted.end, adapted.received_at),
                         (original.start, original.end, original.received_at))
        self.assertTrue(adapted.time_truncated)
        self.assertEqual(adapted.incomplete_reasons, ('partial',))
        for name, source in zip(('air', 'coolant', 'air_target', 'coolant_target',
                                 'worktime', 'target_sensor_id', 'reset'), rows):
            with self.subTest(name=name):
                target = getattr(adapted, name)
                self.assertEqual(target, HeatingSeries(
                    points=tuple(HeatingPoint(p.measured_at, p.value, p.quality.value)
                                 for p in source.points),
                    source=source.source, unit=source.unit, value_type=source.value_type.value,
                    kind=source.kind.value, incomplete_reasons=source.incomplete_reasons))
        self.assertEqual(original.series, rows)
        self.assertEqual(original.series[0].points[0].value, 21.125)

    def test_missing_bindings_do_not_select_hidden_circuit_or_wrong_field(self):
        rows = (controller_series('circuit', 6, 'target_temp', 99),
                controller_series('sensor', 7, 'other', 99),
                controller_series('other', 8, 'value', 99),
                controller_series('toggle_buttons', 7, 'other', 1))
        adapted = from_controller_history(HistoryResult(42, START, END, END, rows), **BINDINGS)
        for name in ('air', 'coolant', 'air_target', 'coolant_target', 'worktime',
                     'target_sensor_id', 'reset'):
            self.assertIsNone(getattr(adapted, name), name)

    def test_existing_empty_reset_and_all_quality_values(self):
        for quality in Quality:
            rows = (controller_series('sensor', 7, 'value', 20, quality=quality),
                    controller_series('toggle_buttons', 7, 'active', None,
                                      value_type=ValueType.BOOLEAN, points=()))
            adapted = from_controller_history(HistoryResult(42, START, END, END, rows), **BINDINGS)
            self.assertEqual((adapted.air.points[0].value, adapted.air.points[0].quality),
                             (20, quality.value))
            self.assertEqual(adapted.reset.points, ())

    def test_controller_timestamps_keep_exact_precision(self):
        measured_at = START + timedelta(microseconds=123456)
        row = controller_series('sensor', 7, 'value', None, points=(
            HistoryPoint(measured_at, 21.125, Quality.UNKNOWN),))
        original = HistoryResult(42, START, END, END + timedelta(microseconds=987654), (row,))
        adapted = from_controller_history(original, **BINDINGS)
        self.assertEqual(adapted.air.points[0].measured_at, measured_at)
        self.assertEqual(adapted.received_at, original.received_at)
        self.assertEqual((adapted.air.points[0].value, adapted.air.points[0].quality),
                         (21.125, 'unknown'))

    def test_foreign_device_and_invalid_bindings_are_rejected(self):
        original = HistoryResult(43, START, END, END, ())
        with self.assertRaises(ValueError):
            from_controller_history(original, **BINDINGS)
        original = HistoryResult(42, START, END, END, ())
        for binding in ({'device_id': True}, {'air_id': 0}, {'air_id': []}, {'coolant_id': 7}):
            with self.subTest(binding=binding), self.assertRaises(ValueError):
                from_controller_history(original, **{**BINDINGS, **binding})
