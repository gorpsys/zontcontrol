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
from tests.fakes.clock import FakeClock
from tests.fakes.protocol import PROFILE
from tests.fakes.uuid import uuid_generator
from tests.fakes.zont import FakeMetadataAdapter, FakeZontServer
from tests.test_coordination_store import PREFIX, r1
from zontcontrol.coordination.store import LeaderStore
from zontcontrol.coordination.timing import FreezeGuard, OperationWindow, TimingViolation
from zontcontrol.control_types import CommandOutcomeUnknown


class StatisticsTests(unittest.TestCase):
    def test_calibration_analyzer_rejects_incomplete_normal_grid(self):
        from scripts.analyze_orchestration import analyze_calibration
        from scripts.calibrate_coordination import Pacing, Safety, normal_inputs, run_series
        from dataclasses import asdict, replace
        import gzip
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'report.json'
            artifact = path.parent/'one.jsonl.gz'
            series = run_series(Pacing(1, 1, 0, .2),
                                replace(next(normal_inputs()), requests=1), Safety())
            with gzip.open(artifact, 'wt') as stream:
                stream.write(json.dumps(series)+'\n')
            path.write_text(json.dumps(dict(status='simulation', safety=asdict(Safety()),
                profiles=[dict(pacing=Pacing(1, 1, 0, .2).as_dict(), artifact=artifact.name,
                    sha256=hashlib.sha256(artifact.read_bytes()).hexdigest())])))
            result = analyze_calibration(path)
            self.assertIsNone(result['selected'])
            self.assertEqual(result['profiles'][0]['summary']['started_requests'], 9000)
            self.assertIn('incomplete-profile-grid', result['profiles'][0]['summary']['violations'])

    def test_live_calibration_keeps_missing_requests_unknown_without_winner(self):
        from scripts.analyze_orchestration import analyze_calibration
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'live.json'
            path.write_text(json.dumps(dict(status='calibrated', started_requests=2,
                samples=[dict(outcome='GRANTED', wait_s=1, timeout_s=30)],
                schedule_confirmed=True)))
            result = analyze_calibration(path)
            self.assertEqual(result['status'], 'unmeasured')
            self.assertEqual(result['summary']['started_requests'], 2)
            self.assertEqual(result['summary']['unknown'], 1)
            self.assertFalse(result['summary']['eligible'])
            self.assertIsNone(result['selected'])

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


class SynchronizationMeasurementsTests(unittest.TestCase):
    def setUp(self):
        self.rows = []
        self.stats = Statistics(self.rows.append, protocol_version='r1r2-v2', profile='profile')
        self.clock = FakeClock(start=100)
        self.uid = uuid_generator()

    def test_nearest_rank_percentiles_exceedances_and_recommendation(self):
        for number in range(1, 1001):
            self.stats.emit('sync_window', device_id=42, role='leader', stage='confirmation',
                            sync_window_s=number / 100, delta_s=5, outcome='success')
        group = analyze(self.rows)['groups'][0]
        metric = group['timings'][0]
        self.assertEqual(metric['count'], 1000)
        self.assertEqual(metric['p99'], 9.9)
        self.assertEqual(metric['p99_9'], 9.99)
        self.assertEqual(metric['max'], 10)
        self.assertEqual(metric['exceedances'], 500)
        self.assertEqual(metric['exceedance_fraction'], .5)
        self.assertEqual(group['delta_recommendation_s'], 99.9)

    def test_timing_groups_keep_device_role_stage_and_protocol_separate(self):
        for device, role, stage in [(42, 'leader', 'write'), (43, 'leader', 'write'),
                                     (42, 'executor', 'write'), (42, 'leader', 'confirmation')]:
            self.stats.emit('sync_window', device_id=device, role=role, stage=stage,
                            sync_window_s=1, delta_s=5, outcome='success')
        self.assertEqual(len(analyze(self.rows)['groups'][0]['timings']), 4)
        self.assertIsNone(analyze([self.stats._base])['groups'][0]['delta_recommendation_s'])

    def store(self, statistics=None):
        adapter = FakeMetadataAdapter(FakeZontServer({42: {'name': PREFIX, 'notes': None}},
                                                    clock=self.clock))
        store = LeaderStore(adapter, 42, original_name=PREFIX, notes_capacity=10000,
                            name_capacity=10000, profile=PROFILE,
                            statistics=self.stats if statistics is None else statistics)
        return store, adapter

    def test_real_store_observes_original_window_and_read_write_confirmation(self):
        store, adapter = self.store()
        adapter.read_delay = .1
        adapter.write_delay = .2
        window = OperationWindow(self.clock, 5)
        self.clock.advance(.5)  # Includes scheduling before the first store call.
        store.write_r1(r1(record_id=str(self.uid())), window=window, role_deadline=200)
        events = [row['event'] for row in self.rows]
        self.assertIn('sync_read', events)
        self.assertIn('sync_write', events)
        self.assertIn('sync_confirmation', events)
        finished = next(row for row in self.rows if row['event'] == 'sync_window')
        written = next(row for row in self.rows if row['event'] == 'sync_write')
        self.assertAlmostEqual(written['read_write_s'], .9)
        self.assertIn('sync_write_started', events)
        self.assertAlmostEqual(finished['sync_window_s'], 1)
        self.assertEqual(finished['started_at'], 100)
        self.assertAlmostEqual(finished['finished_at'], 101)
        self.assertEqual(analyze(self.rows)['groups'][0]['windows']['incomplete'], 0)

    def test_old_presend_samples_cannot_be_completed_read_write_percentiles(self):
        self.stats.emit('sync_write', device_id=42, role='leader', stage='write',
                        read_write_s=.7, delta_s=5)
        group = analyze(self.rows)['groups'][0]
        metric = next(row for row in group['timings'] if row['metric'] == 'read_write_s')
        self.assertEqual(metric['count'], 0)
        self.assertIsNone(metric['max'])
        self.assertIsNone(metric['p99'])
        self.assertEqual(metric['unknown'], 1)
        self.assertEqual(metric['censored_elapsed']['max'], .7)
        self.assertEqual(group['delta_recommendation_status'], 'incomplete')

    def test_lost_finish_and_timeout_samples_remain_unknown(self):
        store, adapter = self.store()
        adapter.write_delay = 6
        update = adapter.update_metadata
        def late_response(*args, **kwargs):
            try:
                return update(*args, **kwargs)
            finally:
                self.clock.advance(1)  # Scheduler resumes after the socket timeout.
        adapter.update_metadata = late_response
        with self.assertRaises(CommandOutcomeUnknown):
            store.write_r1(r1(record_id=str(self.uid())),
                           window=OperationWindow(self.clock, 5), role_deadline=200)
        group = analyze(self.rows)['groups'][0]
        self.assertEqual(group['windows']['unknown'], 1)
        timing = next(row for row in group['timings'] if row['metric'] == 'sync_window_s')
        self.assertEqual(timing['count'], 0)
        self.assertIsNone(timing['p99_9'])
        self.assertEqual(timing['censored_elapsed']['max'], 6)
        self.assertEqual(group['delta_recommendation_status'], 'incomplete')
        summary = next(row for row in self.rows if row['event'] == 'sync_window')
        self.assertEqual(summary['outcome'], 'unknown')
        written = next(row for row in self.rows if row['event'] == 'sync_write')
        self.assertEqual(written['outcome'], 'unknown')
        self.assertEqual(written['finished_at'], self.clock.now())
        metric = next(row for row in group['timings'] if row['metric'] == 'read_write_s')
        self.assertEqual(metric['count'], 0)
        self.assertEqual(metric['censored_elapsed']['max'], 6)
        self.assertGreater(summary['sync_window_s'], 5)
        lost = analyze([row for row in self.rows if row['event'] != 'sync_window'])['groups'][0]
        self.assertEqual(lost['windows']['incomplete'], 1)

    def test_freeze_records_expected_wakeup_and_preserves_rejection(self):
        guard = FreezeGuard(self.clock, 5, expected_resume_at=110,
                            statistics=self.stats, device_id=42, role='leader')
        self.clock.advance(12)
        self.assertEqual(guard.check(), 2)
        self.clock.advance(4)
        with self.assertRaises(TimingViolation):
            guard.check()
        self.assertEqual([row['freeze_gap_s'] for row in self.rows], [2, 6])
        self.assertEqual([row['exceeded'] for row in self.rows], [0, 1])

    def test_failed_observer_never_changes_write_or_guard_outcome(self):
        class Broken:
            def emit(self, *args, **kwargs):
                raise RuntimeError('private')
        store, adapter = self.store(Broken())
        with self.assertLogs('zontcontrol.coordination.statistics', level='WARNING'):
            store.write_r1(r1(record_id=str(self.uid())),
                           window=OperationWindow(self.clock, 5), role_deadline=200)
        self.assertEqual(len(adapter.writes), 1)
        self.clock.advance(6)
        window = OperationWindow(self.clock, 5)
        self.clock.advance(6)
        with self.assertLogs('zontcontrol.coordination.statistics', level='WARNING'):
            with self.assertRaises(TimingViolation):
                store.write_r1(r1(record_id=str(self.uid())), window=window, role_deadline=200)
        self.assertEqual(len(adapter.writes), 1)
