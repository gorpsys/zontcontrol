"""Frozen complete assessments from the monitor before the typed-input change."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import unittest

from zontcontrol.monitor import assess_history
from scripts.heating_archive import from_archive_packet
from zontcontrol.controller.contracts import (
    HistoryPoint, HistoryResult, HistorySeries, Quality, SeriesKind, ValueKey, ValueType,
)
from zontcontrol.heating_adapter import from_controller_history


FIXTURE = Path(__file__).parent / 'fixtures/heating-assessment-cases.json'
BINDINGS = dict(device_id=42, circuit_id=20496, air_id=8207, coolant_id=8429, reset_id=8832)


def load_cases():
    """Expand stored input replacements without running the detector."""
    fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
    cases = []
    for case in fixture['cases']:
        packet = deepcopy(fixture['base_packet'])
        for change in case['changes']:
            parent = packet
            for key in change['path'][:-1]:
                parent = parent[key]
            parent[change['path'][-1]] = deepcopy(change['value'])
        cases.append({**case, 'packet': packet})
    return cases


def controller_input(packet):
    """Express shared fixture values directly as DTOs, without either adapter."""
    device = packet['devices'][0]
    rows = []

    def row(kind, element_id, field, points):
        rows.append(HistorySeries(
            ValueKey(device['id'], kind, element_id, field), ValueType.NUMBER,
            '°C' if kind == 'sensor' else None, 'fixture-controller',
            SeriesKind.MEASUREMENT if kind == 'sensor' else SeriesKind.STEP,
            tuple(HistoryPoint(datetime.fromisoformat(p['time']), p['value']) for p in points)))

    for sensor in device['temperature_sensors']:
        row('sensor', sensor['id'], 'value', sensor['points'])
    for circuit in device['circuits']:
        for field, points in circuit['series'].items():
            row('circuit', circuit['id'], field, points)
    for control in device.get('web_elements', []):
        row('toggle_buttons', control['id'], 'active', control['series']['state'])
    return HistoryResult(device['id'], datetime.fromisoformat(packet['requested_period']['start']),
                         datetime.fromisoformat(packet['requested_period']['end']),
                         datetime.fromisoformat(packet['fetched_at']), tuple(rows),
                         device.get('time_truncated', False))


def canonical_dates(value):
    """Compare equivalent ISO offsets as instants, keeping every result field."""
    if isinstance(value, dict):
        return {key: canonical_dates(item) for key, item in value.items()}
    if isinstance(value, list):
        return [canonical_dates(item) for item in value]
    if isinstance(value, str) and 'T' in value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return value


def normalized_expected(case):
    """Apply only hand-calculated consequences of the agreed input policy.

    The immutable fixture still contains the complete OLD monitor results.
    No expectations are captured from the new implementation.
    """
    expected = deepcopy(case['expected'])
    name = case['name']
    if name == 'bad_current_sensor':
        # 44 finite exact readings plus a nonnumeric value at minute 45.
        expected['valid_temperature_samples'] = 44
    elif name == 'stale_temperature':
        # Last actual reading is minute 39. Minute 40 has no right support,
        # so the former 90-second carry-forward vanishes and ends evidence.
        expected['valid_temperature_samples'] = 39
        expected['events'][-1]['time'] = '2026-10-01T00:40:00+00:00'
    elif name in ('temperature_age_90_seconds', 'temperature_age_120_seconds',
                  'temperature_age_over_120_seconds'):
        # Last reading is 43:30. E shifts from 45 to 44 once; minutes 1..43
        # are known, minute 44 has no right support and ends the old incident.
        expected['valid_temperature_samples'] = 43
        expected['events'].append({
            'type': 'ended', 'time': '2026-10-01T00:44:00+00:00',
            'reason': 'telemetry_gap_or_invalid', 'heat_returned': False})
        expected['state_at_period_end'] = 'unknown'
        expected['anomaly'] = None
        expected['decision_reason'] = 'insufficient_or_invalid_telemetry'
    return expected


def frozen_projection(assessment):
    """Keep every legacy field; additive diagnostics have dedicated regressions."""
    added = {'usable_samples', 'detector_buffer_size', 'latest_valid_sample_age_seconds',
             'time_truncated', 'reset_latest_measured_at', 'reset_latest_quality'}
    return {key: value for key, value in assessment.items() if key not in added}


class HeatingRegressionTests(unittest.TestCase):
    def test_controller_and_archive_match_complete_frozen_assessments(self):
        # These cases have values/timestamps representable at BOTH boundaries.
        # Archive-only duplicates, prior points and invalid scalar types are
        # tested separately: Controller DTO intentionally rejects those shapes.
        selected = {
            'confirmed', 'warning_hot_coolant', 'normal_heating', 'recovery',
            'old_incident_current_normal', 'intentional_shutdown', 'subminute_pulse',
            'restart_grace', 'reset_unknown_null', 'reset_missing', 'reset_empty',
            'missing_sensor', 'wrong_target_sensor', 'invalid_worktime', 'truncated',
            'old_gap_recovered', 'stale_temperature', 'temperature_age_90_seconds',
            'temperature_age_120_seconds', 'temperature_age_over_120_seconds',
        }
        cases = [case for case in load_cases() if case['name'] in selected]
        self.assertEqual({case['name'] for case in cases}, selected)
        for case in cases:
            with self.subTest(case=case['name']):
                packet = case['packet']
                dto = controller_input(packet)
                controller = from_controller_history(dto, **BINDINGS)
                archive = from_archive_packet(packet, **BINDINGS)
                expected = canonical_dates(normalized_expected(case))
                self.assertEqual(canonical_dates(frozen_projection(assess_history(controller))), expected)
                self.assertEqual(canonical_dates(frozen_projection(assess_history(archive))), expected)

    def test_absent_and_explicit_empty_reset_have_separate_full_results(self):
        cases = {case['name']: case for case in load_cases()}
        actual = {}
        for name, available in (('reset_missing', False), ('reset_empty', True)):
            case = cases[name]
            for history in (from_archive_packet(case['packet'], **BINDINGS),
                            from_controller_history(controller_input(case['packet']), **BINDINGS)):
                result = assess_history(history)
                self.assertEqual(canonical_dates(frozen_projection(result)), canonical_dates(case['expected']))
                self.assertIs(result['reset_history_available'], available)
                actual[name] = result
        self.assertEqual({**actual['reset_missing'], 'reset_history_available': True}, actual['reset_empty'])

    def test_controller_quality_remains_separate_from_archive_value_projection(self):
        case = next(case for case in load_cases() if case['name'] == 'confirmed')
        dto = controller_input(case['packet'])
        for quality in (Quality.UNKNOWN, Quality.MISSING, Quality.STALE):
            air = dto.series[0]
            altered = replace(air, points=tuple(replace(p, quality=quality) for p in air.points))
            source = replace(dto, series=(altered, *dto.series[1:]))
            typed = from_controller_history(source, **BINDINGS)
            result = assess_history(typed)
            expected = {**case['expected'], 'state_at_period_end': 'unknown', 'anomaly': None,
                        'decision_reason': 'insufficient_or_invalid_telemetry',
                        'valid_temperature_samples': 0, 'latest_valid_sample': None,
                        'events': [], 'incident_pending': False}
            with self.subTest(quality=quality):
                self.assertEqual(canonical_dates(frozen_projection(result)), canonical_dates(expected))
                self.assertEqual(typed.air.points[0].value, 23.5)
                self.assertEqual(typed.air.points[0].quality, quality.value)
                self.assertEqual(source.series[0], altered)

    def test_reordered_independent_calls_keep_full_events_and_pending_state(self):
        cases = {case['name']: case for case in load_cases()}
        order = ('confirmed', 'recovery', 'reset_missing', 'warning_hot_coolant',
                 'normal_heating', 'confirmed', 'warning_hot_coolant', 'recovery')
        for name in order:
            case = cases[name]
            history = from_archive_packet(case['packet'], **BINDINGS)
            self.assertEqual(canonical_dates(frozen_projection(assess_history(history))),
                             canonical_dates(normalized_expected(case)), name)

    def test_complete_assessments_match_frozen_results(self):
        for case in load_cases():
            with self.subTest(case=case['name']):
                packet = case['packet']
                self.assertGreaterEqual(datetime.fromisoformat(packet['fetched_at']),
                                        datetime.fromisoformat(packet['requested_period']['end']))
                original = deepcopy(packet)
                history = from_archive_packet(packet, device_id=42, circuit_id=20496,
                                              air_id=8207, coolant_id=8429, reset_id=8832)
                actual = assess_history(history)
                self.assertEqual(canonical_dates(frozen_projection(actual)),
                                 canonical_dates(normalized_expected(case)))
                self.assertEqual(set(frozen_projection(actual)), set(case['expected']))
                self.assertEqual(packet, original)
