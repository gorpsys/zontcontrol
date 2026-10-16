"""Read facade contracts, with an in-memory adapter and no ZONT connection."""

import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from zontcontrol.controller.contracts import (
    DeviceUnavailable, InvalidArguments, Quality, SeriesKind, UnsupportedAction, ValueKey,
)
from zontcontrol.controller.service import ControllerService
from tests.test_inventory import inventory_device


START = datetime(2026, 10, 1, tzinfo=timezone.utc)
END = START + timedelta(minutes=3)
NOW = END + timedelta(days=5)


def device():
    raw = inventory_device()
    raw['sensors'] = [{'id': 7, 'name': 'Воздух', 'type': 'temperature',
                       'status': 'ok', 'unit': '°C', 'value': 21}]
    raw['circuits'][0]['target_temp'] = 22
    raw['circuits'].append({'id': 11, 'name': 'Котел', 'type': 'boiler',
                            'hidden': True, 'target_temp': 60})
    return raw


def key(field='value', kind='sensor', element_id=7, device_id=42):
    return ValueKey(device_id, kind, element_id, field)


def packet(rows=None):
    return {'time_truncated': False, 'z3k_temperature': {'7': rows if rows is not None else [
        [int(START.timestamp()), 21], [-60, None], [-60, 22], [-60, 23]]}}


class ControllerReadTests(unittest.TestCase):
    def setUp(self):
        self.raw = device()
        self.adapter = Mock()
        self.adapter.devices.side_effect = lambda device_id=None, **kw: [
            copy.deepcopy(self.raw if device_id in (None, 42) else {**self.raw, 'id': device_id})]
        self.adapter.full_device.return_value = {'id': 42, 'io': {'z3k-state': {
            '10': {'setpoint_temp': 45, 'worktime': 1, 'target_sensor_id': 7},
            '11': {'setpoint_temp': 70}}}}
        self.adapter.load_history.return_value = packet()
        self.service = ControllerService(self.adapter, clock=lambda: NOW)

    def test_list_and_current_are_device_scoped_and_non_atomic(self):
        self.assertEqual(self.service.list_devices()[0].model, 'SMART 2.0')
        first = self.service.read_current(42)
        second = self.service.read_current(43, (key(device_id=43),))
        self.assertFalse(first.is_atomic)
        self.assertEqual(second.values[0].key.device_id, 43)
        self.assertEqual(second.values[0].value, 21)
        self.assertEqual({x.key.device_id for x in first.values}, {42})
        self.adapter.send_command.assert_not_called()

    def test_calculated_values_keep_actual_circuit_and_missing_time(self):
        values = {x.key: x for x in self.service.read_current(42).values}
        self.assertEqual(values[key('target_temp', 'circuit', 10)].value, 22)
        self.assertEqual(values[key('setpoint_temp', 'circuit', 10)].value, 45)
        self.assertEqual(values[key('worktime', 'circuit', 10)].value, 1)
        self.assertEqual(values[key('target_sensor_id', 'circuit', 10)].value, 7)
        self.assertIs(values[key('active', 'toggle_buttons', 31)].value, False)
        self.assertIsNone(values[key()].measured_at)
        self.assertIs(values[key()].quality, Quality.UNKNOWN)
        self.assertEqual(values[key()].received_at, NOW)
        self.adapter.full_device.return_value['io']['z3k-state'].pop('10')
        value = self.service.read_current(42, (key('setpoint_temp', 'circuit', 10),)).values[0]
        self.assertIsNone(value.value)
        self.assertIs(value.quality, Quality.MISSING)

    def test_catalog_declares_history_without_inventing_it_for_current(self):
        rows = {x.key: x for x in self.service.describe_values(42)}
        self.assertTrue(rows[key()].history_available)
        self.assertTrue(rows[key('setpoint_temp', 'circuit', 10)].history_available)
        self.assertIs(rows[key('target_temp', 'circuit', 10)].history_kind, SeriesKind.STEP)
        self.assertIs(rows[key('setpoint_temp', 'circuit', 10)].history_kind, SeriesKind.STEP)
        self.assertIs(rows[key('active', 'toggle_buttons', 31)].history_kind, SeriesKind.STEP)
        self.assertFalse(rows[key('value', 'regulators', 32)].history_available)
        self.assertFalse(rows[key('actual_temp', 'circuit', 10)].history_available)

    def test_virtual_supply_sensor_does_not_hide_addressable_values(self):
        self.raw['sensors'].insert(0, {
            'id': 0, 'name': 'Питание', 'type': 'voltage', 'unit': 'В', 'value': 12.3,
        })
        self.raw['sensors'].append({
            'id': 8, 'name': 'Контроль напряжения питания', 'type': 'voltage',
            'unit': 'В', 'value': 12.3,
        })
        descriptors = self.service.describe_values(42)
        self.assertTrue(all(row.key.element_id > 0 for row in descriptors))
        readings = {row.key: row for row in self.service.read_current(42).values}
        self.assertEqual(readings[key()].value, 21)
        self.assertEqual(readings[key(element_id=8)].value, 12.3)
        self.assertEqual(readings[key(element_id=8)].unit, 'В')
        self.adapter.send_command.assert_not_called()

    def test_widget_temperature_unit_matches_circuit_and_history(self):
        self.raw['sensors'][0]['unit'] = '°'
        descriptor = next(row for row in self.service.describe_values(42) if row.key == key())
        self.assertEqual(descriptor.unit, '°C')
        readings = {row.key: row for row in self.service.read_current(42).values}
        self.assertEqual(readings[key()].unit, readings[key('target_temp', 'circuit', 10)].unit)
        history = self.service.read_history(42, (key(),), START, END)
        self.assertEqual(history.series[0].unit, '°C')

    def test_controls_include_hidden_unsupported_labels_and_limits(self):
        rows = {x.key.element_id: x for x in self.service.list_controls(42)}
        self.assertEqual(set(rows), {10, 11, 20, 30, 31, 32, 33, 34, 40, 50, 60})
        self.assertTrue(rows[11].hidden)
        self.assertEqual(rows[31].labels, ((True, 'отключен котёл'), (False, 'включен котёл')))
        self.assertEqual(rows[32].step, .1)
        self.assertEqual(rows[10].actions, ('target-temp',))
        self.assertEqual(rows[40].actions, ())
        self.assertTrue(rows[40].unavailable_reasons)

    def test_invalid_and_foreign_keys_fail_before_io(self):
        for device_id, keys in [(True, None), (42, (key(device_id=43),)),
                                (42, (key(), key())), (42, ['invalid'])]:
            with self.subTest(device_id=device_id, keys=keys), self.assertRaises(InvalidArguments):
                self.service.read_current(device_id, keys)
        self.adapter.devices.assert_not_called()

    def test_unknown_key_and_unsupported_history_never_load_archive(self):
        with self.assertRaises(InvalidArguments):
            self.service.read_current(42, (key(element_id=99),))
        with self.assertRaises(UnsupportedAction):
            self.service.read_history(42, (key('value', 'regulators', 32),), START, END)
        self.adapter.load_history.assert_not_called()

    def test_history_preserves_null_actual_times_and_no_current_values(self):
        row = self.service.read_history(42, (key(),), START, END).series[0]
        self.assertEqual([p.value for p in row.points], [21, None, 22, 23])
        self.assertEqual(row.points[1].measured_at, START + timedelta(minutes=1))
        self.assertIs(row.points[1].quality, Quality.MISSING)
        self.assertEqual((row.coverage_start, row.coverage_end), (START, END))
        self.assertIn('null_values', row.incomplete_reasons)
        self.adapter.full_device.assert_not_called()

    def test_reset_events_between_minutes_remain_step_points(self):
        self.adapter.load_history.return_value = {'z3k_web_element': {'31': {'state': [
            [int(START.timestamp()), 0], [-35, 1], [-7, 0]]}}}
        row = self.service.read_history(42, (key('active', 'toggle_buttons', 31),), START, END).series[0]
        self.assertIs(row.kind, SeriesKind.STEP)
        self.assertEqual([p.value for p in row.points], [False, True, False])
        self.assertEqual(row.points[-1].measured_at, START + timedelta(seconds=42))
        self.assertTrue(self.adapter.load_history.call_args.kwargs['include_controls'])

    def test_chunk_boundaries_duplicates_and_truncation(self):
        self.service = ControllerService(self.adapter, clock=lambda: NOW,
                                         history_chunk=timedelta(minutes=2), max_history_parts=3)
        self.adapter.load_history.side_effect = [
            packet([[int(START.timestamp()), 1], [-120, 2]]),
            {**packet([[int((START + timedelta(minutes=2)).timestamp()), 2], [-60, 3]]),
             'time_truncated': True}]
        result = self.service.read_history(42, (key(),), START, END)
        self.assertEqual([p.value for p in result.series[0].points], [1, 2, 3])
        self.assertTrue(result.time_truncated)
        self.assertFalse(result.complete)
        calls = self.adapter.load_history.call_args_list
        self.assertEqual(calls[0].args[2], calls[1].args[1])

    def test_conflicting_repeated_timestamp_is_unknown(self):
        self.adapter.load_history.return_value = packet([[int(START.timestamp()), 1],
                                                         [int(START.timestamp()), 2]])
        row = self.service.read_history(42, (key(),), START, END).series[0]
        self.assertEqual(len(row.points), 1)
        self.assertIsNone(row.points[0].value)
        self.assertIs(row.points[0].quality, Quality.UNKNOWN)
        self.assertIn('conflicting_timestamps', row.incomplete_reasons)

    def test_missing_archive_part_and_missing_fields_are_explicit(self):
        self.service = ControllerService(self.adapter, clock=lambda: NOW,
                                         history_chunk=timedelta(minutes=2))
        self.adapter.load_history.side_effect = [packet([[int(START.timestamp()), 1]]), {}]
        result = self.service.read_history(42, (key(), key('target_temp', 'circuit', 10)), START, END)
        self.assertIn('missing_part', result.series[0].incomplete_reasons)
        self.assertEqual(result.series[1].points, ())
        self.assertFalse(result.complete)
        self.adapter.full_device.assert_not_called()

    def test_history_bounds_and_limit_validate_before_io(self):
        for start, end, keys in [(START.replace(tzinfo=None), END, (key(),)),
                                  (END, START, (key(),)), (START, END, ()),
                                  (START, END, (key(device_id=43),)),
                                  (START, NOW + timedelta(seconds=1), (key(),)),
                                  (START, START + timedelta(days=33), (key(),))]:
            with self.subTest(start=start, end=end), self.assertRaises(InvalidArguments):
                self.service.read_history(42, keys, start, end)
        self.adapter.devices.assert_not_called()

    def test_malformed_upstream_values_are_safe_public_failures(self):
        for value in [True, float('nan'), 'secret-invalid-value']:
            self.raw['sensors'][0]['value'] = value
            with self.subTest(value=value), self.assertRaises(InvalidArguments) as caught:
                self.service.read_current(42)
            self.assertNotIn(str(value), str(caught.exception))

    def test_device_unavailable_propagates_without_fallback(self):
        self.adapter.devices.side_effect = DeviceUnavailable
        with self.assertRaises(DeviceUnavailable):
            self.service.read_current(42)
        self.adapter.full_device.assert_not_called()

    def test_fractional_bounds_are_preserved_and_do_not_round_in_new_points(self):
        start = START + timedelta(microseconds=100000)
        end = START + timedelta(microseconds=900000)
        result = self.service.read_history(42, (key(),), start, end)
        self.assertEqual((result.start, result.end), (start, end))
        self.assertEqual(result.series[0].points, ())
        self.assertFalse(result.complete)

    def test_missing_target_history_never_uses_hidden_boiler_or_current(self):
        self.adapter.load_history.return_value = {
            'z3k_heating_circuit': {'11': {'target_temp': [[int(START.timestamp()), 60]]}}}
        row = self.service.read_history(42, (key('target_temp', 'circuit', 10),), START, END).series[0]
        self.assertEqual(row.points, ())
        self.assertIn('missing_part', row.incomplete_reasons)

    def test_malformed_histories_and_reset_state_fail_safely(self):
        for data in [{'z3k_temperature': []}, {'time_truncated': 'false'},
                     {'z3k_temperature': {'7': [[int(START.timestamp()), True]]}},
                     {'z3k_temperature': {'7': [[int(START.timestamp()), 'private']]}}]:
            self.adapter.load_history.return_value = data
            with self.subTest(data=data), self.assertRaises(InvalidArguments) as caught:
                self.service.read_history(42, (key(),), START, END)
            self.assertNotIn('private', str(caught.exception))
        self.adapter.load_history.return_value = {
            'z3k_web_element': {'31': {'state': [[int(START.timestamp()), 2]]}}}
        with self.assertRaises(InvalidArguments):
            self.service.read_history(42, (key('active', 'toggle_buttons', 31),), START, END)

    def test_distinct_upstream_receive_times_are_not_measurement_times(self):
        self.service = ControllerService(self.adapter, clock=Mock(side_effect=[START, END]))
        snapshot = self.service.read_current(42)
        rows = {row.key: row for row in snapshot.values}
        self.assertEqual(rows[key()].received_at, START)
        self.assertEqual(rows[key('setpoint_temp', 'circuit', 10)].received_at, END)
        self.assertEqual(snapshot.received_at, END)
        self.assertTrue(all(row.measured_at is None for row in snapshot.values))

    def test_complete_real_points_and_offline_quality(self):
        self.adapter.load_history.return_value = packet([
            [int(START.timestamp()), 21], [-60, 21], [-60, 22], [-60, 22]])
        self.assertTrue(self.service.read_history(42, (key(),), START, END).complete)
        self.raw['online'] = False
        self.assertIs(self.service.read_current(42, (key(),)).values[0].quality, Quality.STALE)

    def test_history_part_limit_is_independent_of_future_validation(self):
        self.service = ControllerService(self.adapter, clock=lambda: START + timedelta(days=100))
        with self.assertRaises(InvalidArguments):
            self.service.read_history(42, (key(),), START, START + timedelta(days=32))
        self.adapter.devices.assert_not_called()
        self.adapter.load_history.assert_not_called()

    def test_mismatched_full_device_is_rejected_without_cross_device_values(self):
        for other_id in (43, True, None):
            self.adapter.full_device.return_value['id'] = other_id
            with self.subTest(other_id=other_id), self.assertRaises(InvalidArguments):
                self.service.read_current(42)

    def test_matching_circuit_ids_on_two_devices_keep_separate_full_api_targets(self):
        self.adapter.full_device.side_effect = lambda device_id: {
            'id': device_id, 'io': {'z3k-state': {'10': {'setpoint_temp': device_id}}}}
        first = self.service.read_current(42, (key('setpoint_temp', 'circuit', 10),))
        second_key = key('setpoint_temp', 'circuit', 10, device_id=43)
        second = self.service.read_current(43, (second_key,))
        self.assertEqual((first.values[0].value, second.values[0].value), (42, 43))
        self.assertEqual(second.values[0].key, second_key)


if __name__ == '__main__':
    unittest.main()
