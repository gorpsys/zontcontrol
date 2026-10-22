"""Boiler policy against virtual history, clocks and commands only."""

from dataclasses import replace
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import Mock, patch

from zontcontrol.algorithms.boiler_reset import (
    BoilerReset, CONFIG_PATH, MINIMUM_SECONDS, PULSE_SECONDS, load_config,
)
from zontcontrol.autonomy.catalog import BOILER_RESET_SPEC
from zontcontrol.autonomy.contracts import Mode, ResultStatus, RunContext
from zontcontrol.autonomy.registry import AlgorithmRegistry
from zontcontrol.config import ConfigurationError, OnceTimeout, RegistryConfig
from zontcontrol.controller.contracts import (
    CommandOutcomeUnknown, ControlKey, DeviceUnavailable, DeviceInfo, DeviceUnavailable,
    HistoryPoint, HistoryResult, HistorySeries, Quality, SeriesKind, Snapshot,
    ValidatedCommand, ValueDescriptor, ValueKey, ValueReading, ValueType,
)
from zontcontrol.detection_profile import SMART_2_HEATING_CONFIG
from zontcontrol.heating import HeatingHistory
from zontcontrol.heating_adapter import from_controller_history
from zontcontrol.monitor import assess_history, history_samples
from tests.fakes.clock import FakeClock
from tests.test_heating_regressions import canonical_dates, load_cases


SETTINGS = dict(device_id=42, circuit_id=20496, air_sensor_id=8207,
                coolant_sensor_id=8429, reset_control_id=8832, check_interval_seconds=60)


def history(data=None):
    data = data or next(case['packet'] for case in load_cases() if case['name'] == 'confirmed')
    device = data['devices'][0]
    rows = []
    for sensor in device['temperature_sensors']:
        rows.append((ValueKey(42, 'sensor', sensor['id'], 'value'),
                     ValueType.NUMBER, SeriesKind.MEASUREMENT, sensor['points']))
    for circuit in device['circuits']:
        for field, points in circuit['series'].items():
            rows.append((ValueKey(42, 'circuit', circuit['id'], field),
                         ValueType.INTEGER if field in ('worktime', 'target_sensor_id')
                         else ValueType.NUMBER, SeriesKind.STEP, points))
    for reset in device['web_elements']:
        rows.append((ValueKey(42, 'toggle_buttons', reset['id'], 'active'),
                     ValueType.BOOLEAN, SeriesKind.STEP,
                     [{**p, 'value': bool(p['value']) if p['value'] is not None else None}
                      for p in reset['series']['state']]))
    return HistoryResult(42, datetime.fromisoformat(data['requested_period']['start']),
                         datetime.fromisoformat(data['requested_period']['end']),
                         datetime.fromisoformat(data['fetched_at']), tuple(
        HistorySeries(key, kind, None, 'virtual', series_kind,
                      tuple(HistoryPoint(datetime.fromisoformat(p['time']), p['value'])
                            for p in points))
        for key, kind, series_kind, points in rows), device['time_truncated'])


def history_response(device_id, keys, start, end, *, data=None):
    """Return only requested bindings and period, with a fresh synthetic end."""
    data = data or history()
    if device_id != data.device_id:
        raise AssertionError('unexpected history device')
    offset = end - data.end
    return replace(data, start=start, end=end, received_at=data.received_at + offset,
        series=tuple(replace(row, points=tuple(
            replace(point, measured_at=point.measured_at + offset) for point in row.points
            if start <= point.measured_at + offset <= end))
            for row in data.series if row.key in keys))


class BoilerResetTests(unittest.TestCase):
    def test_broken_diagnostics_cannot_prevent_reset_or_finally_off(self):
        class BrokenHandler(logging.Handler):
            def emit(self, record):
                raise RuntimeError('synthetic output failure')
        handler = BrokenHandler()
        logger = logging.getLogger('zontcontrol.algorithms.boiler_reset')
        level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        self.addCleanup(logger.setLevel, level)
        self.addCleanup(logger.removeHandler, handler)
        result = self.module().run_once(self.context)
        self.assertTrue(result.value['reset_performed'])
        self.assertEqual(self.events, ['on', ('wait', 40), 'off'])

    def test_logging_records_attempts_and_api_acceptance_without_physical_success(self):
        with self.assertLogs('zontcontrol', level='INFO') as captured:
            self.module().run_once(self.context)
        records = captured.records
        self.assertEqual([record.event for record in records], ['once_started',
            'reset_on_sending', 'reset_off_attempt', 'reset_off_api_accepted', 'once_finished'])
        self.assertTrue(records[-1].reset_performed)
        self.assertEqual(records[-1].state, 'confirmed_pattern')
        self.assertEqual(records[-1].elapsed_ms, 40000)
        self.assertTrue(all(record.device_id == 42 for record in records))
        self.assertNotIn('recovered', ' '.join(record.getMessage() for record in records))

    def test_logging_failures_never_claim_success_or_include_exception_payload(self):
        for failed in ('on', 'off', 'both'):
            def command(command):
                self.execute(command)
                target = dict(command.arguments)['target_state']
                if failed == 'both' or target == (failed == 'on'):
                    raise RuntimeError('private-exception-payload')
            self.events.clear()
            self.controller.execute_command.side_effect = command
            with self.subTest(failed=failed), self.assertLogs('zontcontrol', level='INFO') as captured:
                with self.assertRaises(RuntimeError):
                    self.module().run_once(self.context)
            names = [record.event for record in captured.records]
            self.assertIn('once_failed', names)
            self.assertNotIn('once_finished', names)
            self.assertEqual('reset_off_api_accepted' in names, failed == 'on')
            self.assertEqual(self.events.count('on'), 1)
            self.assertEqual(self.events.count('off'), 1)
            self.assertNotIn('private', ' '.join(captured.output))
            self.assertTrue(all(not record.exc_info for record in captured.records))

    def setUp(self):
        self.clock = FakeClock(wall_start=history().end)
        self.context = RunContext('boiler_reset', Event(), None)
        self.events = []
        self.controller = Mock()
        self.controller.read_history.side_effect = history_response
        self.controller.describe_values.return_value = tuple(
            ValueDescriptor(row.key, 'virtual', row.unit, row.source, row.key.field,
                            row.value_type, True, True, row.kind)
            for row in history().series)
        self.controller.list_devices.return_value = (DeviceInfo(42, 'virtual', None, True),)
        self.reset = ValueKey(42, 'toggle_buttons', 8832, 'active')
        self.controller.read_current.return_value = Snapshot(42, self.clock.wall_now(), (
            ValueReading(self.reset, False, ValueType.BOOLEAN, None, 'virtual', None,
                         self.clock.wall_now(), Quality.UNKNOWN),))
        self.controller.prepare_command.side_effect = lambda device, key, action, arguments: (
            ValidatedCommand(key, action, tuple(arguments.items())))
        self.controller.execute_command.side_effect = self.execute
        self.reader = Mock(return_value=dict(SETTINGS))


    def execute(self, command):
        self.assertEqual(command.control_key, ControlKey(42, 'toggle_buttons', 8832))
        self.events.append('on' if dict(command.arguments)['target_state'] else 'off')

    def wait(self, context, seconds):
        self.events.append(('wait', seconds))
        return self.clock.wait(context.cancelled, seconds)

    def module(self, **kwargs):
        return BoilerReset(self.controller, reader=self.reader, clock=self.clock.now,
                           wall_clock=self.clock.wall_now, wait=kwargs.pop('wait', self.wait),
                           **kwargs)

    def test_confirmed_current_pattern_runs_whole_pulse_and_once_has_no_pause(self):
        result = self.module().run_once(self.context)
        self.assertIs(result.status, ResultStatus.SUCCESS)
        self.assertTrue(result.value['reset_performed'])
        self.assertEqual(self.events, ['on', ('wait', 40), 'off'])
        self.reader.assert_called_once_with(CONFIG_PATH)
        self.assert_history_requests()

    def assert_history_requests(self):
        calls = self.controller.read_history.call_args_list
        self.assertEqual(len(calls), 2)
        telemetry, reset = (call.args for call in calls)
        end = history().end
        self.assertEqual(telemetry, (42, tuple(row.key for row in history().series
            if row.key != self.reset), end - timedelta(minutes=46), end))
        self.assertEqual(reset, (42, (self.reset,), telemetry[3] - timedelta(minutes=120),
                                 telemetry[3]))

    def split_history(self, points, *, telemetry=None, reset_series=True, **metadata):
        original = history()
        end = original.end
        telemetry = telemetry or history_response(42, tuple(row.key for row in original.series
            if row.key != self.reset), end - timedelta(minutes=46), end)
        row = replace(original.series[-1], points=tuple(points))
        # A deliberately wider valid DTO can contain a point after the requested
        # T; assembly must still retain telemetry's end and causal cutoff.
        reset = replace(original, start=end - timedelta(minutes=120),
                        end=max((end, *(point.measured_at for point in points))),
                        series=(row,) if reset_series else (), **{
                            'received_at': max((original.received_at,
                                *(point.measured_at for point in points))), **metadata})
        self.controller.read_history.side_effect = [telemetry, reset]
        return telemetry, reset

    def test_older_off_keeps_telemetry_window_and_all_45_minutes(self):
        end = history().end
        for age in (60, 120):
            with self.subTest(age=age):
                self.clock = FakeClock(wall_start=end)
                self.events.clear()
                self.controller.read_history.reset_mock()
                telemetry, reset = self.split_history((HistoryPoint(
                    end - timedelta(minutes=age), False),))
                wall_clock = Mock(return_value=end)
                with patch('zontcontrol.algorithms.boiler_reset.assess_history',
                           wraps=assess_history) as assess:
                    module = self.module()
                    module._wall_clock = wall_clock
                    result = module.run_once(self.context)
                wall_clock.assert_called_once_with()
                self.assert_history_requests()
                self.assertTrue(result.value['reset_performed'])
                typed = assess.call_args.args[0]
                expected = from_controller_history(telemetry, device_id=42, circuit_id=20496,
                    air_id=8207, coolant_id=8429, reset_id=8832)
                self.assertEqual(replace(typed, reset=None), expected)
                self.assertEqual(typed.reset.points[0].measured_at, reset.series[0].points[0].measured_at)
                self.assertEqual(len(list(history_samples(typed))), 45)
                self.assertEqual(result.value['assessment']['valid_temperature_samples'], 45)

    def test_reset_series_unknown_transitions_and_future_point(self):
        end = history().end
        def point(minutes, value, quality=Quality.GOOD):
            return HistoryPoint(end - timedelta(minutes=minutes), value, quality)
        cases = (
            ('absent', None, 'unknown', 'reset_state_unknown'),
            ('empty', (), 'unknown', 'reset_state_unknown'),
            ('null', (point(60, False), point(1, None)), 'unknown', 'reset_state_unknown'),
            ('stale', (point(60, False), point(1, False, Quality.STALE)), 'unknown', 'reset_state_unknown'),
            ('unknown', (point(60, False, Quality.UNKNOWN),), 'unknown', 'reset_state_unknown'),
            ('missing', (point(60, False, Quality.MISSING),), 'unknown', 'reset_state_unknown'),
            ('on', (point(60, False), point(1, True)), 'suppressed', 'intentional_shutdown'),
            ('off', (point(60, False), point(3, True), point(2, False), point(1, False)),
             'suppressed', 'restart_grace'),
            ('future_only', (point(-1, False),), 'unknown', 'reset_state_unknown'),
            ('future_on', (point(60, False), point(-1, True)), 'confirmed_pattern',
             'current_no_heating_response'),
            ('old_off', (point(60, False), point(10, True), point(9, False), point(1, False)),
             'normal', 'no_current_pattern'),
        )
        for name, points, state, reason in cases:
            with self.subTest(name=name):
                self.events.clear()
                self.clock = FakeClock(wall_start=end)
                self.split_history(points or (), reset_series=points is not None)
                with patch('zontcontrol.algorithms.boiler_reset.assess_history',
                           wraps=assess_history) as assess:
                    result = self.module().run_once(self.context)
                decision = result.value['assessment']
                self.assertEqual((decision['state_at_period_end'], decision['decision_reason']),
                                 (state, reason))
                typed = assess.call_args.args[0]
                self.assertEqual(None if typed.reset is None else tuple(
                    (p.measured_at, p.value, p.quality) for p in typed.reset.points),
                    None if points is None else tuple(
                        (p.measured_at, p.value, p.quality.value) for p in points))
                self.assertEqual(self.events, ['on', ('wait', 40), 'off']
                                 if state == 'confirmed_pattern' else [])

    def test_both_histories_preserve_quality_metadata_and_truncation(self):
        end = history().end
        for truncated in ('telemetry', 'reset', None):
            for newer in ('telemetry', 'reset'):
                with self.subTest(truncated=truncated, newer=newer):
                    telemetry, reset = self.split_history((HistoryPoint(
                        end - timedelta(minutes=60), False, Quality.STALE),))
                    telemetry = replace(telemetry, received_at=end + timedelta(
                        seconds=2 if newer == 'telemetry' else 1),
                        series=(replace(telemetry.series[0], incomplete_reasons=('air_reason',),
                            points=tuple(replace(point, quality=Quality.UNKNOWN)
                                         for point in telemetry.series[0].points)),
                            *telemetry.series[1:]),
                        time_truncated=truncated == 'telemetry',
                        incomplete_reasons=('telemetry_reason', 'shared'))
                    reset = replace(reset, received_at=end + timedelta(
                        seconds=2 if newer == 'reset' else 1),
                        series=(replace(reset.series[0], incomplete_reasons=('reset_series_reason',)),),
                        time_truncated=truncated == 'reset',
                        incomplete_reasons=('shared', 'reset_reason'))
                    self.controller.read_history.side_effect = [telemetry, reset]
                    with patch('zontcontrol.algorithms.boiler_reset.assess_history',
                               wraps=assess_history) as assess:
                        result = self.module().run_once(self.context)
                    typed = assess.call_args.args[0]
                    self.assertEqual(typed.received_at, end + timedelta(seconds=2))
                    self.assertEqual(typed.time_truncated, truncated is not None)
                    self.assertEqual(typed.incomplete_reasons,
                                     ('telemetry_reason', 'shared', 'reset_reason'))
                    self.assertEqual(typed.reset.incomplete_reasons, ('reset_series_reason',))
                    self.assertEqual(typed.reset.points[0].quality, 'stale')
                    self.assertIs(typed.reset.points[0].value, False)
                    self.assertEqual(typed.air.incomplete_reasons, ('air_reason',))
                    self.assertEqual(tuple((p.value, p.quality) for p in typed.air.points),
                                     tuple((p.value, 'unknown') for p in telemetry.series[0].points))
                    self.assertEqual(result.value['assessment']['decision_reason'],
                        'truncated_history' if truncated else 'reset_state_unknown')
                    self.assertEqual(self.events, [])

    def test_cancel_after_either_history_never_assesses_or_commands(self):
        for cancel_after in (1, 2):
            with self.subTest(cancel_after=cancel_after):
                self.context.cancelled.clear()
                self.controller.read_history.reset_mock()
                calls = []
                def read(*args):
                    calls.append(args)
                    result = history_response(*args)
                    if len(calls) == cancel_after:
                        self.context.cancelled.set()
                    return result
                self.controller.read_history.side_effect = read
                with patch('zontcontrol.algorithms.boiler_reset.assess_history') as assess:
                    result = self.module().run_once(self.context)
                self.assertEqual(result.value['reason'], 'cancelled')
                self.assertEqual(len(calls), cancel_after)
                assess.assert_not_called()
                self.assertEqual(self.events, [])

    def test_second_history_error_cannot_assess_or_send_commands(self):
        self.controller.read_history.side_effect = [history(), DeviceUnavailable()]
        with patch('zontcontrol.algorithms.boiler_reset.assess_history') as assess:
            with self.assertRaises(DeviceUnavailable):
                self.module().run_once(self.context)
        self.assertEqual(self.controller.read_history.call_count, 2)
        assess.assert_not_called()
        self.controller.read_current.assert_not_called()
        self.assertEqual(self.events, [])

    def test_reset_binding_is_still_required_before_both_reads(self):
        rows = self.controller.describe_values.return_value
        self.controller.describe_values.return_value = tuple(row for row in rows
                                                            if row.key != self.reset)
        with self.assertRaises(ConfigurationError):
            self.module().run_once(self.context)
        self.controller.read_history.assert_not_called()

    def test_adapter_decision_equals_existing_fixture_without_recalculation(self):
        selected = {'confirmed', 'old_incident_current_normal', 'missing_sensor',
                    'subminute_pulse', 'truncated', 'old_gap_recovered'}
        for case in load_cases():
            if case['name'] not in selected:
                continue
            with self.subTest(case=case['name']):
                converted = from_controller_history(history(case['packet']), device_id=42,
                    circuit_id=20496, air_id=8207, coolant_id=8429, reset_id=8832)
                self.assertEqual(canonical_dates(assess_history(converted)),
                                 canonical_dates(case['expected']))

    def test_unknown_warning_and_old_events_do_not_send_commands(self):
        for state in ('unknown', 'warning', 'normal', 'suppressed'):
            with self.subTest(state=state), patch(
                    'zontcontrol.algorithms.boiler_reset.assess_history', return_value={
                        'state_at_period_end': state, 'anomaly': True,
                        'events': [{'type': 'confirmed_pattern'}]}):
                self.events.clear()
                result = self.module().run_once(self.context)
                self.assertFalse(result.value['reset_performed'])
                self.assertEqual(self.events, [])
                if state == 'unknown':
                    self.assertIs(result.status, ResultStatus.UNKNOWN)

    def test_explicit_ids_and_configured_profile_are_passed_to_detector(self):
        with patch('zontcontrol.algorithms.boiler_reset.assess_history', wraps=assess_history) as assess:
            self.module().run_once(self.context)
        self.assertEqual(assess.call_args.kwargs, dict(config=SMART_2_HEATING_CONFIG))
        typed = assess.call_args.args[0]
        self.assertIsInstance(typed, HeatingHistory)
        self.assertEqual((typed.device_id, typed.circuit_id, typed.air_sensor_id,
                          typed.coolant_sensor_id, typed.reset_control_id), (42, 20496, 8207, 8429, 8832))

    def test_history_quality_is_not_promoted_to_good_and_metadata_is_retained(self):
        original = history()
        row = original.series[0]
        altered = replace(row, points=tuple(replace(p, quality=Quality.STALE)
                                            for p in row.points), incomplete_reasons=('missing_part',))
        data = replace(original, series=(altered, *original.series[1:]),
                       incomplete_reasons=('source_incomplete',))
        converted = from_controller_history(data, device_id=42, circuit_id=20496,
                                            air_id=8207, coolant_id=8429, reset_id=8832)
        self.assertEqual(tuple(p.value for p in converted.air.points), tuple(p.value for p in row.points))
        self.assertTrue(all(p.quality == 'stale' for p in converted.air.points))
        self.assertEqual(converted.incomplete_reasons, ('source_incomplete',))
        self.assertEqual(converted.air.incomplete_reasons, ('missing_part',))
        self.assertEqual(assess_history(converted)['state_at_period_end'], 'unknown')

    def test_missing_and_invalid_config_fail_before_controller_calls(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigurationError, 'config/boiler_reset.json'):
                BoilerReset(self.controller, root=directory)
        for changes in ({'device_id': True}, {'circuit_id': 0}, {'air_sensor_id': 8429},
                        {'check_interval_seconds': 0}, {'check_interval_seconds': float('inf')},
                        {'check_interval_seconds': True}, {'grant_wait_seconds': -1},
                        {'grant_poll_seconds': 31}, {'detection_profile': 'new'},
                        {'token': 'private'}, {'pulse_seconds': 1}):
            with self.subTest(changes=changes), self.assertRaises(ConfigurationError):
                BoilerReset(self.controller, reader=lambda path: {**SETTINGS, **changes})
        self.controller.assert_not_called()
        self.assertEqual(self.controller.mock_calls, [])

    def test_module_factory_probes_configuration_without_network(self):
        self.module()
        self.assertEqual(self.controller.mock_calls, [])

    def test_missing_explicit_binding_is_error_without_history_or_commands(self):
        self.controller.describe_values.return_value = ()
        with self.assertRaises(ConfigurationError):
            self.module().run_once(self.context)
        self.controller.read_history.assert_not_called()
        self.assertEqual(self.events, [])

    def test_already_on_is_never_switched_off(self):
        current = self.controller.read_current.return_value
        self.controller.read_current.return_value = replace(current, values=(
            replace(current.values[0], value=True),))
        result = self.module().run_once(self.context)
        self.assertIs(result.status, ResultStatus.SUPPRESSED)
        self.assertEqual(self.events, [])

    def test_offline_or_unknown_reset_never_attempt_on_or_off(self):
        self.controller.list_devices.return_value = (DeviceInfo(42, 'virtual', None, False),)
        with self.assertRaises(DeviceUnavailable):
            self.module().run_once(self.context)
        self.assertEqual(self.events, [])
        self.events.clear()
        self.controller.list_devices.return_value = (DeviceInfo(42, 'virtual', None, True),)
        current = self.controller.read_current.return_value
        self.controller.read_current.return_value = replace(current, values=(
            replace(current.values[0], value=None, quality=Quality.MISSING),))
        self.assertIs(self.module().run_once(self.context).status, ResultStatus.UNKNOWN)
        self.assertEqual(self.events, [])

    def test_stop_or_exception_during_wait_attempts_immediate_off(self):
        for fail in (False, True):
            def interrupted(context, seconds):
                if fail:
                    raise RuntimeError('virtual wait failure')
                context.cancelled.set()
                return True
            with self.subTest(fail=fail):
                self.events.clear()
                self.context.cancelled.clear()
                if fail:
                    with self.assertRaises(RuntimeError):
                        self.module(wait=interrupted).run_once(self.context)
                else:
                    self.module(wait=interrupted).run_once(self.context)
                self.assertEqual(self.events, ['on', 'off'])
                self.assertEqual(self.clock.now(), 0)

    def test_unknown_on_is_not_retried_and_off_error_is_visible(self):
        for off_error in (None, DeviceUnavailable()):
            def command(command):
                self.execute(command)
                if dict(command.arguments)['target_state']:
                    raise CommandOutcomeUnknown()
                if off_error:
                    raise off_error
            self.events.clear()
            self.controller.execute_command.side_effect = command
            with self.subTest(off_error=off_error), self.assertRaises(
                    DeviceUnavailable if off_error else CommandOutcomeUnknown):
                self.module().run_once(self.context)
            self.assertEqual(self.events, ['on', 'off'])

    def test_preparation_failure_and_stop_before_on_have_no_off(self):
        self.controller.prepare_command.side_effect = DeviceUnavailable()
        with self.assertRaises(DeviceUnavailable):
            self.module().run_once(self.context)
        self.assertEqual(self.events, [])

    def test_long_wait_still_attempts_off_without_a_lease_deadline(self):
        def delayed(context, seconds):
            self.clock.advance(1000)
            context.cancelled.set()
            return True
        self.module(wait=delayed).run_once(self.context)
        self.assertEqual(self.events, ['on', 'off'])

    def test_cancel_during_on_still_attempts_off(self):
        def send(command):
            self.execute(command)
            if dict(command.arguments)['target_state']:
                self.context.cancelled.set()
        self.controller.execute_command.side_effect = send
        self.module().run_once(self.context)
        self.assertEqual(self.events, ['on', ('wait', 40), 'off'])
        self.events.clear()
        self.context.cancelled.set()
        self.module().run_once(self.context)
        self.assertEqual(self.events, [])


    def test_continuous_pause_starts_after_off_without_ownership_or_once_deadline(self):
        calls = []
        def wait(context, seconds):
            calls.append(seconds)
            if seconds == 300:
                self.assertEqual(self.events[-1], 'off')
                context.cancelled.set()
                return True
            return self.clock.wait(context.cancelled, seconds)
        self.module(wait=wait).run_continuous(self.context)
        self.assertEqual(calls, [40, 300])
        self.assertEqual(self.events, ['on', 'off'])

    def test_continuous_uses_monotonic_slots_skips_overruns_and_has_no_extra_pause(self):
        times = []
        module = self.module()
        def check(context):
            times.append(self.clock.now())
            if len(times) == 1:
                self.clock.advance(130)
            elif len(times) == 3:
                context.cancelled.set()
            return type('Result', (), {'value': {'reset_performed': False}})()
        module.run_once = check
        module.run_continuous(self.context)
        self.assertEqual(times, [0, 180, 240])

    def test_registrar_continuous_outlives_once_budget_and_pause_only_follows_reset(self):
        spec = BOILER_RESET_SPEC
        waits, contexts = [], []
        def wait(context, seconds):
            contexts.append(context)
            waits.append(seconds)
            if len(waits) == 4:
                context.cancelled.set()
                return True
            return self.clock.wait(context.cancelled, seconds)
        spec = replace(spec, factory=lambda controller: self.module(wait=wait))
        config = RegistryConfig(frozenset(), False, {'boiler_reset': OnceTimeout(
            MINIMUM_SECONDS, MINIMUM_SECONDS)})
        registry = AlgorithmRegistry(config, self.controller, clock=self.clock.now)
        self.addCleanup(lambda: registry.close(wait_timeout=2))
        registry.register_all([spec])
        handle = registry.start('boiler_reset')
        handle.worker.join(2)
        self.assertFalse(handle.worker.is_alive())
        self.assertIsNone(handle.failure)
        self.assertEqual(waits, [40, 300, 40, 300])
        self.assertGreater(self.clock.now(), MINIMUM_SECONDS)
        self.assertTrue(all(context.deadline is None for context in contexts))
        self.assertEqual(self.events, ['on', 'off'] * 2)
        calls = self.controller.read_history.call_args_list
        self.assertEqual(len(calls), 4)
        for first, second in (calls[:2], calls[2:]):
            telemetry, reset = first.args, second.args
            self.assertEqual(telemetry[0], 42)
            self.assertEqual(set(telemetry[1]), set(self.module().config.history_keys) - {self.reset})
            self.assertEqual(telemetry[3] - telemetry[2], timedelta(minutes=46))
            self.assertEqual(reset, (42, (self.reset,), telemetry[3] - timedelta(minutes=120),
                                     telemetry[3]))

    def test_registrar_once_uses_builtin_module_and_returns_after_single_pulse(self):
        spec = replace(BOILER_RESET_SPEC, factory=lambda controller: self.module())
        config = RegistryConfig(frozenset(), False, {'boiler_reset': OnceTimeout(
            MINIMUM_SECONDS, MINIMUM_SECONDS)})
        registry = AlgorithmRegistry(config, self.controller, clock=self.clock.now)
        self.addCleanup(lambda: registry.close(wait_timeout=2))
        registry.register_all([spec])
        result = registry.run_once('boiler_reset')
        self.assertTrue(result.value['reset_performed'])
        self.assertEqual(self.clock.now(), 40)

    def test_builtin_spec_is_explicit_dual_mode_and_minimum_includes_cleanup(self):
        spec = BOILER_RESET_SPEC
        self.assertEqual(spec.modes, frozenset({Mode.ONCE, Mode.CONTINUOUS}))
        self.assertEqual(spec.minimum_seconds, MINIMUM_SECONDS)
        self.assertGreater(spec.minimum_seconds, PULSE_SECONDS + 30)
        with patch('zontcontrol.algorithms.boiler_reset.read_json', return_value=SETTINGS):
            self.assertIsInstance(spec.factory(self.controller), BoilerReset)

    def test_example_is_module_owned_and_valid(self):
        example = json.loads(Path('config/boiler_reset.example.json').read_text())
        load_config(reader=lambda path: example)


if __name__ == '__main__':
    unittest.main()
