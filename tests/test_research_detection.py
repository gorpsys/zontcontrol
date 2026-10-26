"""Research callers use the same causal typed history, without network access."""

from copy import deepcopy
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from dataclasses import asdict
import gzip
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.analyze_3weeks import frames, load_series
from scripts import backtest_profile, tune_anomaly, validate_one_shot, validate_recalibration
from zontcontrol.detection_profile import SMART_2_HEATING_CONFIG
from zontcontrol.anomaly import DetectionConfig
from zontcontrol.heating import HeatingHistory
from zontcontrol.monitor import assess_history, history_samples
from tests.test_heating_regressions import load_cases, canonical_dates, frozen_projection


END = datetime(2026, 10, 1, tzinfo=timezone.utc)
T = int(END.timestamp())


def small_archive(root):
    """60 minutes, plus a future point which must never reach an earlier poll."""
    start = END - timedelta(minutes=60)

    def points(values):
        return [{'time': (END + timedelta(seconds=offset)).isoformat(), 'value': value}
                for offset, value in values]

    # At T legacy frames can carry the final air measurement forward for 90s.
    # At E=T-60 the typed policy has no right support and returns unknown.
    air = [(i * 60, 23.5) for i in range(-60, -1)] + [(-90, 23.5), (10, 99)]
    device = {
        'id': 700877, 'time_truncated': False,
        'temperature_sensors': [
            {'id': 8207, 'points': points(air)},
            {'id': 8429, 'points': points([(i * 60, 40 - .2 * (i + 60)) for i in range(-60, 1)] + [(10, 99)])}],
        'circuits': [{'id': 20496, 'series': {
            name: points([(-3600, value), (10, 999)]) for name, value in (
                ('target_temp', 23.5), ('setpoint_temp', 60), ('worktime', 60),
                ('target_sensor_id', 8207), ('mode_id', 1), ('status', 1))}}],
        'web_elements': [{'id': 8832, 'series': {'state': points([(-3600, 0), (10, 1)])}}]}
    manifest = {'start': start.isoformat(), 'end': END.isoformat(), 'chunks': [{
        'file': 'chunk.json.gz', 'start': start.isoformat(), 'end': END.isoformat(), 'time_truncated': False}]}
    root.mkdir()
    (root / 'manifest.json').write_text(json.dumps(manifest))
    with gzip.open(root / 'chunk.json.gz', 'wt') as stream:
        json.dump({'devices': [device]}, stream)
    (root.parent / 'anomalies-3w.json').write_text(json.dumps({'incidents': []}))
    return load_series(root, 20496, 8207, 8429, 8832)


class ResearchDetectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'archive'
        self.manifest, self.series = small_archive(self.root)
        self.indexes = {key: [time for time, value in points] for key, points in self.series.items()}
        self.rows = frames(self.manifest, self.series, 8207)

    def test_loader_keeps_existing_quality_and_fractional_second_losses(self):
        case = next(case for case in load_cases() if case['name'] == 'confirmed')
        device = deepcopy(case['packet']['devices'][0])
        for sensor in device['temperature_sensors']:
            for point in sensor['points']:
                point['quality'] = 'stale'
                point['time'] = (datetime.fromisoformat(point['time'])
                                 + timedelta(microseconds=123456)).isoformat()
        circuit = device['circuits'][0]['series']
        circuit['status'] = circuit['worktime']
        circuit['mode_id'] = circuit['worktime']
        with gzip.open(self.root / 'chunk.json.gz', 'wt') as stream:
            json.dump({'devices': [device]}, stream)
        _, series = load_series(self.root, 20496, 8207, 8429, 8832)
        indexes = {key: [time for time, value in points] for key, points in series.items()}
        end = datetime.fromisoformat(case['packet']['requested_period']['end'])
        history = validate_one_shot.heating_window(series, indexes, int(end.timestamp()))
        self.assertEqual(series['air'][0], (T, 23.5))
        self.assertEqual(history.air.points[0].measured_at, END)
        self.assertTrue(all(p.quality == 'good' for p in history.air.points))
        expected = deepcopy(case['expected'])
        expected['device_id'] = 700877
        expected['requested_period']['start'] = (end - timedelta(minutes=46)).isoformat()
        self.assertEqual(canonical_dates(frozen_projection(assess_history(history))), canonical_dates(expected))

    def test_initial_measurement_age_is_not_relabelled_to_window_start(self):
        values = {**self.series, 'air': [(T - 7200, 23.5)], 'coolant': [(T - 7200, 40)]}
        indexes = {key: [time for time, value in points] for key, points in values.items()}
        history = validate_one_shot.heating_window(values, indexes, T)
        self.assertEqual(history.air.points[0].measured_at, END - timedelta(hours=2))
        self.assertEqual(assess_history(history), {
            'fetched_at': END.isoformat(),
            'requested_period': {'start': (END - timedelta(minutes=46)).isoformat(), 'end': END.isoformat()},
            'device_id': 700877, 'circuit_id': 20496, 'state_at_period_end': 'unknown',
            'anomaly': None, 'decision_reason': 'insufficient_or_invalid_telemetry',
            'valid_temperature_samples': 0, 'latest_valid_sample': (END - timedelta(hours=2)).isoformat(),
            'usable_samples': 0, 'detector_buffer_size': 0, 'latest_valid_sample_age_seconds': 7200,
            'time_truncated': False, 'reset_latest_measured_at': (END - timedelta(hours=1)).isoformat(),
            'reset_latest_quality': 'good',
            'reset_history_available': True, 'reset_active_at_period_end': False,
            'events': [], 'automatic_control': False, 'incident_pending': False})

    def test_each_poll_cuts_all_seven_source_rows_before_normalization(self):
        # The future air point at T+10 provides support for minute T only when
        # the later poll actually has it. It cannot leak into the T assessment.
        past = validate_one_shot.heating_window(self.series, self.indexes, T)
        past_result = assess_history(past)
        later = validate_one_shot.heating_window(self.series, self.indexes, T + 10)
        self.assertEqual(list(history_samples(past))[-1].time, END - timedelta(minutes=1))
        self.assertEqual(list(history_samples(later))[-1].time, END)
        self.assertIsNotNone(list(history_samples(later))[-1].air)
        self.assertEqual(assess_history(later)['decision_reason'], 'intentional_shutdown')
        self.assertEqual(assess_history(past), past_result)
        for name in ('air', 'coolant', 'air_target', 'coolant_target', 'worktime', 'target_sensor_id', 'reset'):
            self.assertTrue(all(p.measured_at <= END for p in getattr(past, name).points), name)
            self.assertEqual(getattr(later, name).points[-1].measured_at, END + timedelta(seconds=10), name)

    def test_heating_window_builds_seven_typed_rows_with_real_initial_age(self):
        original = deepcopy(self.series)
        history = validate_one_shot.heating_window(self.series, self.indexes, T)
        self.assertIsInstance(history, HeatingHistory)
        self.assertEqual((history.start, history.end, history.received_at),
                         (END - timedelta(minutes=46), END, END))
        self.assertEqual((history.device_id, history.circuit_id, history.air_sensor_id,
                          history.coolant_sensor_id, history.reset_control_id), (700877, 20496, 8207, 8429, 8832))
        self.assertEqual(history.air.points[0].measured_at, END - timedelta(minutes=47))
        self.assertEqual(history.air_target.points[0].measured_at, END - timedelta(minutes=60))
        for name in ('air', 'coolant', 'air_target', 'coolant_target', 'worktime', 'target_sensor_id', 'reset'):
            row = getattr(history, name)
            self.assertTrue(all(p.measured_at <= END and p.quality == 'good' for p in row.points))
            self.assertEqual((row.source, row.unit, row.value_type, row.kind), (None,) * 4)
        self.assertEqual(self.series, original)

    def test_empty_reset_slice_is_absent_and_an_old_initial_point_is_retained(self):
        values = {**self.series, 'reset': [(T + 1, 1)]}
        indexes = {key: [time for time, value in points] for key, points in values.items()}
        history = validate_one_shot.heating_window(values, indexes, T)
        self.assertIsNone(history.reset)
        self.assertFalse(assess_history(history)['reset_history_available'])
        history = validate_one_shot.heating_window(self.series, self.indexes, T)
        self.assertEqual(history.reset.points[0].measured_at, END - timedelta(minutes=60))

    def test_one_shot_evaluate_uses_typed_causal_history(self):
        episode = {'id': 'test', 'kind': 'manual', 'start': T}
        with patch.object(validate_one_shot, 'OFFSETS', (0,)), patch.object(
                validate_one_shot, 'assess_history', wraps=assess_history) as assess:
            checks = validate_one_shot.evaluate([episode], self.series, self.rows)
        self.assertIsInstance(assess.call_args.args[0], HeatingHistory)
        self.assertEqual(assess.call_args.kwargs, {})
        self.assertEqual(checks[0]['state'], 'unknown')
        self.assertEqual(checks[0]['valid_samples'], 44)

    def test_tuning_loads_history_and_samples_then_verifies_same_typed_input(self):
        reference = {'reset_assumption': {'before_exclusive': self.manifest['start']},
                     'offsets_minutes': [0], 'episodes': [{'id': 'test', 'kind': 'manual', 'start': T}]}
        _, _, cases = tune_anomaly.load_cases(self.root, reference)
        self.assertEqual(len(cases), 1)
        self.assertNotIn('packet', cases[0])
        self.assertIsInstance(cases[0]['history'], HeatingHistory)
        self.assertEqual(cases[0]['samples'], list(history_samples(cases[0]['history'])))
        config = DetectionConfig()
        with patch.object(tune_anomaly, 'assess_history', wraps=assess_history) as assess:
            verified = tune_anomaly.verify(cases, {'config': asdict(config)})
        self.assertIs(assess.call_args.args[0], cases[0]['history'])
        self.assertEqual(assess.call_args.kwargs, {'config': config})
        self.assertEqual(verified['checks'][0]['state'], 'unknown')

    def test_backtest_reports_input_differences_and_passes_truncation_by_replace(self):
        with patch.object(backtest_profile, 'assess_history', wraps=assess_history) as assess:
            report = backtest_profile.run(self.manifest, self.series, T - 60, T,
                                          Path(self.directory.name) / 'backtest.json')
        self.assertEqual(report['minute_input_policy'], 'legacy_frames')
        self.assertEqual(report['minute_decisions'], {'confirmed_pattern': 1})
        self.assertEqual(report['input_policy_comparisons'], 1)
        self.assertEqual(report['input_policy_differences'], [{
            'time': END.isoformat(), 'frames_state': 'confirmed_pattern', 'normalized_history_state': 'unknown'}])
        typed = assess.call_args.args[0]
        self.assertIsInstance(typed, HeatingHistory)
        self.assertFalse(typed.time_truncated)
        manifest = deepcopy(self.manifest)
        manifest['chunks'][0]['time_truncated'] = True
        with patch.object(backtest_profile, 'assess_history', wraps=assess_history) as assess:
            report = backtest_profile.run(manifest, self.series, T - 60, T,
                                          Path(self.directory.name) / 'truncated.json')
        self.assertTrue(assess.call_args.args[0].time_truncated)
        self.assertEqual(report['minute_decisions'], {'unknown': 1})

    def test_backtest_main_writes_reports_and_prints_input_policy_summary(self):
        output = Path(self.directory.name) / 'backtest-main.json'
        stdout = StringIO()
        argv = ['backtest_profile', '--input', str(self.root),
                '--from', (END - timedelta(minutes=1)).isoformat(), '--to', END.isoformat(),
                '--reset-inactive-before', self.manifest['start'], '--output', str(output)]
        with patch('sys.argv', argv), redirect_stdout(stdout):
            backtest_profile.main()
        report = json.loads(output.read_text())
        summary = json.loads(stdout.getvalue().splitlines()[-1])
        self.assertEqual(summary, {
            'period': report['period'], 'minute_decisions': {'confirmed_pattern': 1},
            'confirmed_segment_count': 1, 'segment_length_counts': {'1-5': 1},
            'input_policy_comparisons': 1,
            'input_policy_differences': [{'time': END.isoformat(), 'frames_state': 'confirmed_pattern',
                                          'normalized_history_state': 'unknown'}]})
        self.assertEqual(report['input_policy_differences'], summary['input_policy_differences'])
        self.assertTrue(output.with_suffix('.md').is_file())
        with gzip.open(output.with_name(output.stem + '-minutes.csv.gz'), 'rt') as stream:
            self.assertEqual(len(stream.read().splitlines()), 2)

    def test_recalibration_reports_different_policies_without_claiming_equivalence(self):
        with patch.object(validate_recalibration, 'assess_history', wraps=assess_history) as assess:
            compared, differences = validate_recalibration.compare_input_policies(
                self.series, {T: 'confirmed_pattern'}, {T, T + 60}, SMART_2_HEATING_CONFIG)
        self.assertEqual(compared, 1)
        self.assertEqual(differences, [{'time': END.isoformat(), 'frames_state': 'confirmed_pattern',
                                      'normalized_history_state': 'unknown'}])
        self.assertIsInstance(assess.call_args.args[0], HeatingHistory)
        self.assertEqual(assess.call_args.kwargs, {'config': SMART_2_HEATING_CONFIG})
