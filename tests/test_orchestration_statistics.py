"""Observations are bounded data, never a command/recovery input."""

from concurrent.futures import ThreadPoolExecutor
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from zontcontrol.coordination.statistics import (
    JsonlSink, ObservationError, Statistics, analyze, read_jsonl,
)


class StatisticsTests(unittest.TestCase):
    def recorder(self, sink, **kwargs):
        return Statistics(sink, protocol_version='r1r2-v1', profile='offline', **kwargs)

    def test_concurrent_jsonl_records_and_event_counters(self):
        stream = StringIO()
        stats = self.recorder(JsonlSink(stream))
        def episode(number):
            key = f'episode-{number}'
            stats.emit('episode_started', episode_id=key, device_id=42)
            stats.emit('attempt', episode_id=key, device_id=42)
            stats.emit('grant', episode_id=key, device_id=42, wait_s=.5)
            stats.emit('episode_finished', episode_id=key, device_id=42,
                       outcome='success', coverage='complete')
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(episode, range(100)))
        records = list(read_jsonl(StringIO(stream.getvalue())))
        result = analyze(records)['groups'][0]
        self.assertEqual(result['events']['grant'], 100)
        self.assertEqual(result['episodes']['success'], 100)
        self.assertEqual(result['episodes']['complete'], 100)
        self.assertEqual(result['wait_s'], {'count': 100, 'total': 50., 'max': .5})
        self.assertEqual(stats.errors, 0)

    def test_unknown_partial_conflict_and_polls_are_separate(self):
        rows = []
        stats = self.recorder(rows.append)
        for key, outcome, coverage in [('ok', 'success', 'complete'),
                                       ('u', 'unknown', 'complete'),
                                       ('p', 'success', 'partial'),
                                       ('c', 'conflict', 'complete')]:
            stats.emit('episode_started', episode_id=key, device_id=42)
            stats.emit('episode_finished', episode_id=key, device_id=42,
                       outcome=outcome, coverage=coverage)
        for _ in range(50):
            stats.emit('poll', device_id=42)
        stats.emit('episode_started', episode_id='unfinished', device_id=42)
        group = analyze(rows)['groups'][0]
        self.assertEqual(group['episodes'], {'total': 5, 'complete': 3, 'partial': 2,
                                             'success': 1, 'conflict': 1,
                                             'unknown': 3, 'failure': 0})
        self.assertEqual(group['events']['poll'], 50)
        self.assertNotIn('reliability', group)

    def test_versions_profiles_devices_and_duplicate_finishes_do_not_mix(self):
        rows = []
        for version, profile in [('v1', 'test'), ('v2', 'test'), ('v2', 'real')]:
            stats = Statistics(rows.append, protocol_version=version, profile=profile)
            for device in (42, 43):
                stats.emit('episode_started', episode_id='same', device_id=device)
                for _ in range(2):
                    stats.emit('episode_finished', episode_id='same', device_id=device,
                               coverage='complete', outcome='success')
        groups = analyze(rows)['groups']
        self.assertEqual(len(groups), 3)
        self.assertTrue(all(g['episodes']['total'] == 2 for g in groups))
        self.assertTrue(all(g['episodes']['unknown'] == 2 for g in groups))

    def test_schema_refuses_credentials_bodies_unbounded_and_nonfinite_values(self):
        rows = []
        stats = self.recorder(rows.append)
        for fields in ({'token': 'secret'}, {'provider_body': 'secret'},
                       {'run_id': 'x' * 129}, {'rtt_s': float('inf')},
                       {'outcome': 'secret'}, {'device_id': True},
                       {'run_id': 'Authorization: secret'}):
            with self.subTest(fields=fields), self.assertRaises(ObservationError):
                stats.emit('attempt', **fields)
        self.assertEqual(rows, [])

    def test_sink_error_is_counted_and_visible_without_exception_secret(self):
        def broken(row):
            raise OSError('password=VERY_SECRET')
        stats = self.recorder(broken)
        with self.assertLogs('zontcontrol.coordination.statistics', level='WARNING') as logs:
            stats.emit('attempt', device_id=42)
        self.assertEqual(stats.errors, 1)
        self.assertNotIn('VERY_SECRET', ''.join(logs.output))

    def test_malformed_jsonl_is_explicit_and_bounded(self):
        stats = self.recorder(lambda row: None)
        for raw in ('{', '{}', '[]', '{"token":"SECRET"}', 'x' * 4097,
                    '{"schema":NaN}', '[' * 1500 + '0' + ']' * 1500):
            with self.subTest(raw=raw[:15]), self.assertRaises(ObservationError) as caught:
                list(read_jsonl(StringIO(raw + '\n')))
            self.assertNotIn('SECRET', str(caught.exception))
        self.assertEqual(stats.errors, 0)

    def test_bad_scalar_types_never_escape_as_parser_or_overflow_errors(self):
        base = {'schema': 1, 'event': 'poll', 'protocol_version': 'v1',
                'profile': 'test', 'source': 'simulation'}
        for changes in ({'event': []}, {'source': {}}, {'outcome': []},
                        {'rtt_s': 10**400}, {'coverage': {}}, {'revision': False}):
            with self.subTest(changes=changes), self.assertRaises(ObservationError):
                analyze([{**base, **changes}])

    def test_jsonl_reports_path_and_stream_write_failure_are_observations_only(self):
        with tempfile.TemporaryDirectory() as directory:
            sink = JsonlSink.in_reports('run-01.jsonl', directory=Path(directory) / 'reports')
            stats = self.recorder(sink)
            stats.emit('attempt', device_id=42)
            sink.close()
            with self.assertLogs('zontcontrol.coordination.statistics', level='WARNING'):
                stats.emit('attempt', device_id=42)
            self.assertEqual(stats.errors, 1)
            with (Path(directory) / 'reports/run-01.jsonl').open() as stream:
                self.assertEqual(len(list(read_jsonl(stream))), 1)
            for bad in ('../elsewhere.jsonl', 'report.txt', '/tmp/elsewhere.jsonl'):
                with self.assertRaises(ObservationError):
                    JsonlSink.in_reports(bad, directory=directory)

    def test_analysis_cli_only_reads_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'observations.jsonl'
            with path.open('w') as stream:
                stats = self.recorder(JsonlSink(stream))
                stats.emit('episode_started', device_id=42, episode_id='unknown')
                stats.emit('timeout', device_id=42, episode_id='unknown')
            before = path.read_bytes()
            completed = subprocess.run([sys.executable, 'scripts/analyze_orchestration.py',
                                        str(path)], capture_output=True, text=True, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result['groups'][0]['episodes']['unknown'], 1)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(directory).iterdir()), [path])
            path.write_text('{"password":"SECRET"}\n')
            failed = subprocess.run([sys.executable, 'scripts/analyze_orchestration.py', str(path)],
                                    capture_output=True, text=True, check=False)
            self.assertNotEqual(failed.returncode, 0)
            self.assertNotIn('SECRET', failed.stderr + failed.stdout)
