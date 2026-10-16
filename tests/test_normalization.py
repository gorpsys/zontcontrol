"""Data checks transferred from the removed prototype to the read facade."""

import copy
import unittest
from unittest.mock import Mock

from tests.test_controller_reads import device, key
from zontcontrol.controller.contracts import InvalidArguments, Quality
from zontcontrol.controller.service import ControllerService


class NormalizationTests(unittest.TestCase):
    def setUp(self):
        self.raw = device()
        self.full = {'id': 42, 'io': {'z3k-state': {'10': {'setpoint_temp': 15},
                                                    '11': {'setpoint_temp': 75}}}}
        self.adapter = Mock()
        self.adapter.devices.side_effect = lambda device_id=None: [copy.deepcopy(self.raw)]
        self.adapter.full_device.side_effect = lambda device_id: copy.deepcopy(self.full)
        self.service = ControllerService(self.adapter)

    def test_zero_null_missing_and_false_keep_distinct_meaning(self):
        self.raw['sensors'][0].update(value=0, triggered=False)
        values = self.service.read_current(42, (key(), key('triggered'))).values
        self.assertEqual(values[0].value, 0)
        self.assertIs(values[1].value, False)
        self.raw['sensors'][0]['value'] = None
        reading = self.service.read_current(42, (key(),)).values[0]
        self.assertIsNone(reading.value)
        self.assertIs(reading.quality, Quality.MISSING)
        self.assertIsNone(reading.measured_at)
        self.adapter.full_device.assert_not_called()

    def test_invalid_sensor_values_are_not_coerced(self):
        for value in (float('nan'), float('inf'), '12', True):
            self.raw['sensors'][0]['value'] = value
            with self.subTest(value=value), self.assertRaises(InvalidArguments):
                self.service.read_current(42, (key(),))

    def test_air_target_and_selected_circuit_coolant_setpoint_are_separate(self):
        values = {row.key: row.value for row in self.service.read_current(42).values}
        self.assertEqual(values[key('target_temp', 'circuit', 10)], 22)
        self.assertEqual(values[key('setpoint_temp', 'circuit', 10)], 15)
        self.assertEqual(values[key('target_temp', 'circuit', 11)], 60)
        self.assertEqual(values[key('setpoint_temp', 'circuit', 11)], 75)
        self.assertNotIn('PRIVATE', repr(values))

    def test_absent_coolant_setpoint_never_falls_back_to_boiler(self):
        self.full['io']['z3k-state'].pop('10')
        value = self.service.read_current(42, (key('setpoint_temp', 'circuit', 10),)).values[0]
        self.assertIsNone(value.value)
        self.assertIs(value.quality, Quality.MISSING)

    def test_null_and_zero_full_api_setpoint_are_preserved(self):
        for number in (None, 0):
            self.full['io']['z3k-state']['10']['setpoint_temp'] = number
            with self.subTest(number=number):
                row = self.service.read_current(42, (key('setpoint_temp', 'circuit', 10),)).values[0]
                self.assertEqual(row.value, number)

    def test_invalid_full_api_setpoint_or_container_is_rejected(self):
        for number in (True, '15', float('nan'), float('inf')):
            self.full['io']['z3k-state']['10']['setpoint_temp'] = number
            with self.subTest(number=number), self.assertRaises(InvalidArguments):
                self.service.read_current(42, (key('setpoint_temp', 'circuit', 10),))
        for value in (None, [], {'z3k-state': []}, {'z3k-state': {'10': []}}):
            self.full['io'] = value
            with self.subTest(value=value), self.assertRaises(InvalidArguments):
                self.service.read_current(42, (key('setpoint_temp', 'circuit', 10),))

    def test_invalid_circuit_scalar_and_identity_are_rejected(self):
        for field, value in [('target_temp', float('nan')), ('actual_temp', float('inf')),
                             ('target_temp', True), ('current_mode', True), ('id', '10')]:
            original = self.raw['circuits'][0].copy()
            self.raw['circuits'][0][field] = value
            with self.subTest(field=field), self.assertRaises(InvalidArguments):
                self.service.read_current(42)
            self.raw['circuits'][0] = original

    def test_empty_circuits_need_no_full_api_and_have_no_invented_values(self):
        self.raw.pop('circuits')
        values = self.service.read_current(42).values
        self.assertTrue(all(row.key.kind != 'circuit' for row in values))
        self.adapter.full_device.assert_not_called()
