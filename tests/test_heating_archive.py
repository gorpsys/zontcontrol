from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from scripts.heating_archive import from_archive_packet


START = datetime(2026, 10, 1, tzinfo=timezone.utc)
BINDINGS = dict(device_id=42, circuit_id=7, air_id=7, coolant_id=8, reset_id=7)


def point(time, value, **extra):
    return {'time': time.isoformat(), 'value': value, **extra}


def packet():
    return dict(fetched_at=(START + timedelta(minutes=46, seconds=1)).isoformat(),
                requested_period=dict(start=START.isoformat(),
                                      end=(START + timedelta(minutes=46)).isoformat()),
                incomplete_reasons=['partial'], devices=[dict(
                    id=42, time_truncated=True,
                    temperature_sensors=[dict(id=7, points=[point(START, 20)]),
                                         dict(id=8, points=[point(START, None)])],
                    circuits=[dict(id=6, series={'target_temp': [point(START, 99)]}),
                              dict(id=7, series={
                                  'target_temp': [point(START, 23)],
                                  'setpoint_temp': [point(START, 82)],
                                  'worktime': [point(START, 60)],
                                  'target_sensor_id': [point(START, 7)]})],
                    web_elements=[dict(id=7, series={'state': [point(START, False)]})])])


class ArchiveHeatingAdapterTests(unittest.TestCase):
    def test_explicit_bindings_all_values_and_missing_metadata(self):
        data = packet()
        original = deepcopy(data)
        result = from_archive_packet(data, **BINDINGS)
        self.assertEqual((result.device_id, result.circuit_id, result.air_sensor_id,
                          result.coolant_sensor_id, result.reset_control_id), (42, 7, 7, 8, 7))
        self.assertEqual((result.start, result.end, result.received_at),
                         (START, START + timedelta(minutes=46), START + timedelta(minutes=46, seconds=1)))
        self.assertTrue(result.time_truncated)
        self.assertEqual(result.incomplete_reasons, ('partial',))
        for name, value in zip(('air', 'coolant', 'air_target', 'coolant_target', 'worktime',
                                'target_sensor_id', 'reset'), (20, None, 23, 82, 60, 7, False)):
            row = getattr(result, name)
            self.assertEqual((row.points[0].measured_at, row.points[0].value,
                              row.points[0].quality), (START, value, 'good'))
            self.assertEqual((row.source, row.unit, row.value_type, row.kind), (None,) * 4)
            self.assertEqual(row.incomplete_reasons, ())
        self.assertEqual(data, original)

    def test_exact_stable_order_duplicates_and_original_initial_time(self):
        data = packet()
        early = START - timedelta(days=1)
        later = START + timedelta(microseconds=900000)
        middle = START + timedelta(microseconds=100000)
        data['devices'][0]['temperature_sensors'][0]['points'] = [
            point(later, 22), point(middle, 20), point(early, 1), point(middle, None)]
        original = deepcopy(data)
        row = from_archive_packet(data, **BINDINGS).air
        self.assertEqual([(p.measured_at, p.value) for p in row.points],
                         [(early, 1), (middle, 20), (middle, None), (later, 22)])
        self.assertEqual(data, original)

    def test_archive_quality_is_not_a_new_filter_or_source_of_errors(self):
        for quality in ('stale', 'missing', 'unknown', 'bad', {'old': 'format'}, None):
            data = packet()
            data['devices'][0]['temperature_sensors'][0]['points'] = [
                point(START, 21.125, quality=quality, original_value=99)]
            row = from_archive_packet(data, **BINDINGS).air
            self.assertEqual((row.points[0].value, row.points[0].quality), (21.125, 'good'))

    def test_available_series_metadata_is_preserved(self):
        data = packet()
        metadata = dict(source='api', unit='°C', value_type='number',
                        history_kind='measurement', incomplete_reasons=['gap'])
        data['devices'][0]['temperature_sensors'][0].update(metadata)
        data['devices'][0]['circuits'][1]['series_metadata'] = {'target_temp': metadata.copy()}
        data['devices'][0]['web_elements'][0].update(metadata)
        result = from_archive_packet(data, **BINDINGS)
        for row in (result.air, result.air_target, result.reset):
            self.assertEqual((row.source, row.unit, row.value_type, row.kind,
                              row.incomplete_reasons), ('api', '°C', 'number', 'measurement', ('gap',)))

    def test_absent_and_explicit_empty_rows_are_distinct(self):
        data = packet()
        device = data['devices'][0]
        device['temperature_sensors'][0]['points'] = []
        device['temperature_sensors'].pop()
        device['circuits'][1]['series'].pop('worktime')
        device['web_elements'][0]['series']['state'] = []
        result = from_archive_packet(data, **BINDINGS)
        self.assertEqual(result.air.points, ())
        self.assertIsNone(result.coolant)
        self.assertIsNone(result.worktime)
        self.assertEqual(result.reset.points, ())
        device['web_elements'] = []
        self.assertIsNone(from_archive_packet(data, **BINDINGS).reset)

    def test_no_first_device_or_hidden_circuit_fallback(self):
        for devices in ([], [dict(id=43)], [dict(id=43), packet()['devices'][0]]):
            data = packet()
            data['devices'] = devices
            with self.subTest(devices=devices), self.assertRaises(ValueError):
                from_archive_packet(data, **BINDINGS)
        data = packet()
        data['devices'][0]['circuits'].pop()
        with self.assertRaises(ValueError):
            from_archive_packet(data, **BINDINGS)

    def test_duplicate_selected_elements_are_rejected(self):
        for collection, index in (('temperature_sensors', 0), ('circuits', 1), ('web_elements', 0)):
            data = packet()
            rows = data['devices'][0][collection]
            rows.append(deepcopy(rows[index]))
            with self.subTest(collection=collection), self.assertRaises(ValueError):
                from_archive_packet(data, **BINDINGS)

    def test_aware_offset_timestamps_are_normalized_once(self):
        data = packet()
        data['devices'][0]['temperature_sensors'][0]['points'] = [
            {'time': '2026-10-01T03:00:00.123456+03:00', 'value': 20}]
        time = from_archive_packet(data, **BINDINGS).air.points[0].measured_at
        self.assertEqual(time, START.replace(microsecond=123456))
        self.assertIs(time.tzinfo, timezone.utc)

    def test_invalid_structure_naive_dates_and_bindings_are_value_errors(self):
        invalid = []
        for key, value in (('devices', {}), ('requested_period', []), ('fetched_at', 'invalid'),
                           ('fetched_at', '2026-10-01T00:46:00'), ('incomplete_reasons', 'gap')):
            data = packet()
            data[key] = value
            invalid.append(data)
        for key in ('start', 'end'):
            data = packet()
            data['requested_period'][key] = '2026-10-01T00:00:00'
            invalid.append(data)
        for points in (None, {}, [None], [{'time': '2026-10-01T00:00:00', 'value': 1}],
                       [{'time': START.isoformat(), 'value': []}], [{'time': START.isoformat()}]):
            data = packet()
            data['devices'][0]['temperature_sensors'][0]['points'] = points
            invalid.append(data)
        for data in (None, [], {}, *invalid):
            with self.subTest(data=data), self.assertRaises(ValueError):
                from_archive_packet(data, **BINDINGS)
        for binding in ({'device_id': True}, {'air_id': 0}, {'coolant_id': 7}):
            with self.subTest(binding=binding), self.assertRaises(ValueError):
                from_archive_packet(packet(), **{**BINDINGS, **binding})
