"""Frozen complete assessments from the monitor before the typed-input change."""

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import unittest

from zontcontrol.monitor import assess_history
from scripts.heating_archive import from_archive_packet


FIXTURE = Path(__file__).parent / 'fixtures/heating-assessment-cases.json'


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


class HeatingRegressionTests(unittest.TestCase):
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
                self.assertEqual(canonical_dates(actual), canonical_dates(normalized_expected(case)))
                self.assertEqual(set(actual), set(case['expected']))
                self.assertEqual(packet, original)
