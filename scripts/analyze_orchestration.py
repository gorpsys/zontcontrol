#!/usr/bin/env python3
"""Offline JSONL: nearest-rank timings, incomplete windows and suggested Delta.

Recommendations require an explicit shared-profile change and are descriptive,
not a proven timing bound. Physical command effects have a separate analyzer.
"""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zontcontrol.coordination import ObservationError, analyze, read_jsonl


def analyze_calibration(path):
    """Recompute from all terminal samples; never rank incomplete live data."""
    import gzip
    import hashlib
    from scripts.calibrate_coordination import (
        request_summary, select_profile, normal_inputs, candidates, Safety,
    )
    report = json.loads(path.read_text(encoding='utf-8'))
    if report.get('matrix') == 'fault':
        return dict(status='simulation', matrix='fault',
                    cases=sum(len(p['samples']) for p in report['profiles']),
                    unexpected=sum(not s['passed'] for p in report['profiles'] for s in p['samples']))
    if 'profiles' not in report:
        return dict(status='unmeasured', source='runtime',
                    summary=request_summary(report['samples'], expected=report['started_requests']),
                    schedule_confirmed=report.get('schedule_confirmed', False),
                    physical_tail=report.get('physical_tail', {'status': 'unknown'}), selected=None)
    results = []
    expected_inputs = {json.dumps(vars(i), sort_keys=True) for i in normal_inputs()}
    expected_profiles = {tuple(vars(p).values()) for p in candidates(Safety(**report['safety']))}
    actual_profiles = [tuple(p['pacing'].values()) for p in report['profiles']]
    complete_grid = (set(actual_profiles) == expected_profiles
                     and len(actual_profiles) == len(expected_profiles))
    for profile in report['profiles']:
        name = profile['artifact']
        if Path(name).name != name:
            raise ValueError('Invalid artifact name')
        artifact = path.parent/name
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != profile['sha256']:
            raise ValueError('Artifact checksum mismatch')
        with gzip.open(artifact, 'rt', encoding='utf-8') as stream:
            series = [json.loads(line) for line in stream]
        actual_inputs = [json.dumps(row['inputs'], sort_keys=True) for row in series]
        violations = [v for row in series for v in row['violations']]
        if not complete_grid:
            violations.append('incomplete-profile-grid')
        if set(actual_inputs) != expected_inputs or len(actual_inputs) != len(expected_inputs):
            violations.append('incomplete-matrix')
        samples = []
        for row in series:
            summary = request_summary(row['samples'], expected=100)
            if summary['unknown']:
                violations.append('incomplete-series')
            samples.extend(row['samples'])
        summary = request_summary(samples, expected=9000, violations=violations)
        results.append(dict(pacing=profile['pacing'], summary=summary,
                            http_count=sum(row['http_count'] for row in series)))
    return dict(status='simulation', profiles=results, selected=select_profile(results),
                zont_timing_status='unmeasured', physical_tail={'status': 'unknown'})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', nargs='+', type=Path, help='JSONL observation files')
    parser.add_argument('--calibration', action='store_true',
                        help='Recompute calibration JSON reports from every compressed sample')
    args = parser.parse_args(argv)

    if args.calibration:
        try:
            result = {'reports': [analyze_calibration(path) for path in args.files]}
        except (OSError, UnicodeError, ValueError, KeyError, TypeError):
            parser.exit(2, 'Cannot analyze calibration: invalid/incomplete report or artifact\n')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    def observations():
        for path in args.files:
            with path.open(encoding='utf-8') as stream:
                yield from read_jsonl(stream)

    try:
        result = analyze(observations())
    except (OSError, UnicodeError, ObservationError):
        parser.exit(2, 'Cannot analyze orchestration observations: invalid or unreadable JSONL\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
