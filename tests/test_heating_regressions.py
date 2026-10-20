"""Frozen complete assessments from the monitor before the typed-input change."""

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import unittest

from zontcontrol.monitor import assess_history


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


class HeatingRegressionTests(unittest.TestCase):
    def test_complete_assessments_match_frozen_results(self):
        for case in load_cases():
            with self.subTest(case=case['name']):
                packet = case['packet']
                self.assertGreaterEqual(datetime.fromisoformat(packet['fetched_at']),
                                        datetime.fromisoformat(packet['requested_period']['end']))
                original = deepcopy(packet)
                actual = assess_history(packet, circuit_id=20496)
                self.assertEqual(canonical_dates(actual), canonical_dates(case['expected']))
                self.assertEqual(packet, original)
