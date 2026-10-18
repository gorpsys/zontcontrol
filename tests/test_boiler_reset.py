"""Boiler policy against virtual history, clocks and commands only."""

from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import Mock, patch
from tests.fakes.uuid import uuid_generator

from zontcontrol.algorithms.boiler_reset import (
    BoilerReset, CONFIG_PATH, MINIMUM_SECONDS, PULSE_SECONDS, load_config, history_packet,
)
from zontcontrol.autonomy.catalog import BUILTIN_SPECS
from zontcontrol.autonomy.contracts import Mode, ResultStatus, RunContext
from zontcontrol.autonomy.registry import AlgorithmRegistry
from zontcontrol.config import ConfigurationError, OnceTimeout, RegistryConfig
from zontcontrol.controller.contracts import (
    CommandOutcomeUnknown, ControlKey, ControlOwnershipRequired, ControlSession,
    ControlRequestResult, ControlRequestState, ControlRequestReason, ControlRequestErrorCode,
    DeviceControlBusy, DeviceInfo, DeviceUnavailable, HistoryPoint, HistoryResult,
    HistorySeries, Quality, RateLimited, SeriesKind, Snapshot, ValidatedCommand, ValueDescriptor,
    ValueKey, ValueReading, ValueType,
)
from zontcontrol.detection_profile import SMART_2_HEATING_CONFIG
from zontcontrol.controller.ownership import CommandExecutor
from zontcontrol.coordination.statistics import Statistics
from zontcontrol.monitor import assess_history
from tests.fakes.clock import FakeClock
from tests.test_monitor import packet
from tests.test_control_ownership import LocalTestGate


SETTINGS = dict(device_id=42, circuit_id=20496, air_sensor_id=8207,
                coolant_sensor_id=8429, reset_control_id=8832, check_interval_seconds=60)


def history(data=None):
    data = data or packet()
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


class BoilerResetTests(unittest.TestCase):
    def setUp(self):
        self.uuid_generator = uuid_generator()
        self.clock = FakeClock(wall_start=history().end)
        self.context = RunContext('boiler_reset', self.uuid_generator(), self.uuid_generator(), Event(), None)
        self.events = []
        self.controller = Mock()
        self.controller.read_history.return_value = history()
        self.controller.describe_values.return_value = tuple(
            ValueDescriptor(row.key, 'virtual', row.unit, row.source, row.key.field,
                            row.value_type, True, True, row.kind)
            for row in history().series)
        self.controller.list_devices.return_value = (DeviceInfo(42, 'virtual', None, True),)
        self.reset = ValueKey(42, 'toggle_buttons', 8832, 'active')
        self.controller.read_current.return_value = Snapshot(42, self.clock.wall_now(), (
            ValueReading(self.reset, False, ValueType.BOOLEAN, None, 'virtual', None,
                         self.clock.wall_now(), Quality.UNKNOWN),))
        self.session = ControlSession(42, self.uuid_generator(), 1000)
        self.controller.acquire_control.side_effect = self.acquire_result
        self.controller.release_control.side_effect = lambda session: self.events.append('release')
        self.controller.prepare_command.side_effect = lambda device, key, action, arguments: (
            ValidatedCommand(key, action, tuple(arguments.items())))
        self.controller.execute_command.side_effect = self.execute
        self.reader = Mock(return_value=dict(SETTINGS))

    def acquire(self, device, *, lease_seconds):
        self.events.append('acquire')
        self.assertEqual(device, 42)
        self.assertGreater(lease_seconds, 40 + 30)
        return self.session

    def acquire_result(self, device, *, lease_seconds, timeout, poll_seconds, cancelled):
        self.assertEqual(timeout, self.reader.return_value.get('grant_wait_seconds', 30))
        self.assertEqual(poll_seconds, self.reader.return_value.get('grant_poll_seconds', 1))
        self.assertIsInstance(cancelled, Event)
        return ControlRequestResult(self.uuid_generator(), device, ControlRequestState.GRANTED,
                                    session=self.acquire(device, lease_seconds=lease_seconds))

    def test_acquire_uses_one_sync_request_with_module_budget(self):
        self.reader.return_value.update(grant_wait_seconds=8, grant_poll_seconds=.5)
        module = self.module()
        module.run_once(self.context)
        self.controller.acquire_control.assert_called_once_with(
            42, lease_seconds=118, timeout=8, poll_seconds=.5, cancelled=self.context.cancelled)
        self.controller.try_acquire_control.assert_not_called()
        self.assertEqual(self.events, ['acquire', 'on', ('wait', 40), 'off', 'release'])

    def test_denied_acquire_has_no_impulse_and_keeps_refusal_semantics(self):
        for reason in (ControlRequestReason.CANCELLED, ControlRequestReason.SHUTDOWN,
                       ControlRequestReason.TIMEOUT, ControlRequestReason.LEASE_EXPIRED):
            with self.subTest(reason=reason):
                self.events.clear()
                self.controller.acquire_control.side_effect = None
                self.controller.acquire_control.return_value = ControlRequestResult(
                    self.uuid_generator(), 42, ControlRequestState.DENIED, reason=reason)
                if reason in (ControlRequestReason.CANCELLED, ControlRequestReason.SHUTDOWN):
                    result = self.module().run_once(self.context)
                    self.assertFalse(result.value['reset_performed'])
                    self.assertEqual(result.value['reason'], 'cancelled')
                    self.assertEqual(result.value['assessment']['state_at_period_end'],
                                     'confirmed_pattern')
                else:
                    with self.assertRaises(DeviceControlBusy):
                        self.module().run_once(self.context)
                self.controller.execute_command.assert_not_called()
                self.controller.release_control.assert_not_called()
                self.assertEqual(self.events, [])
        self.assertEqual(self.controller.acquire_control.call_count, 4)
        self.controller.try_acquire_control.assert_not_called()

    def test_controller_error_raises_fresh_typed_error_without_normal_or_command(self):
        result = ControlRequestResult(self.uuid_generator(), 42, ControlRequestState.DENIED,
            reason=ControlRequestReason.CONTROLLER_ERROR,
            error_code=ControlRequestErrorCode.RATE_LIMITED)
        self.controller.acquire_control.side_effect = None
        self.controller.acquire_control.return_value = result
        errors = []
        for _ in range(2):
            with self.assertRaises(RateLimited) as caught:
                self.module().run_once(self.context)
            errors.append(caught.exception)
        self.assertIsNot(errors[0], errors[1])
        self.assertEqual(result.error_code, ControlRequestErrorCode.RATE_LIMITED)
        self.assertFalse(hasattr(result, 'error'))
        self.controller.execute_command.assert_not_called()
        self.controller.release_control.assert_not_called()

    def execute(self, command, session):
        self.assertIs(session, self.session)
        self.assertEqual(command.control_key, ControlKey(42, 'toggle_buttons', 8832))
        self.events.append('on' if dict(command.arguments)['target_state'] else 'off')

    def wait(self, context, seconds):
        self.events.append(('wait', seconds))
        return self.clock.wait(context.cancelled, seconds)

    def module(self, **kwargs):
        return BoilerReset(self.controller, reader=self.reader, clock=self.clock.now,
                           wall_clock=self.clock.wall_now, wait=kwargs.pop('wait', self.wait),
                           **kwargs)

    def test_confirmed_current_pattern_owns_whole_pulse_and_once_has_no_pause(self):
        result = self.module().run_once(self.context)
        self.assertIs(result.status, ResultStatus.SUCCESS)
        self.assertTrue(result.value['reset_performed'])
        self.assertEqual(self.events, ['acquire', 'on', ('wait', 40), 'off', 'release'])
        self.reader.assert_called_once_with(CONFIG_PATH)
        args = self.controller.read_history.call_args.args
        self.assertEqual(args[0], 42)
        self.assertEqual(set(args[1]), {r.key for r in history().series})
        self.assertEqual(args[3] - args[2], timedelta(minutes=45))
        self.controller.read_history.assert_called_once()

    def test_adapter_decision_equals_existing_fixture_without_recalculation(self):
        for case in ('pattern', 'old', 'missing', 'reset', 'truncated', 'old_gap'):
            with self.subTest(case=case):
                data = packet()
                device = data['devices'][0]
                if case == 'old':
                    device['circuits'][0]['series']['worktime'].append({
                        'time': (history().end - timedelta(minutes=5)).isoformat(), 'value': 0})
                elif case == 'missing':
                    device['temperature_sensors'][0]['points'] = []
                elif case == 'reset':
                    device['web_elements'][0]['series']['state'] += [
                        {'time': (history().end - timedelta(seconds=40)).isoformat(), 'value': 1},
                        {'time': (history().end - timedelta(seconds=10)).isoformat(), 'value': 0}]
                elif case == 'truncated':
                    device['time_truncated'] = True
                elif case == 'old_gap':
                    device['temperature_sensors'][0]['points'][5]['value'] = None
                converted = history_packet(history(data), load_config(reader=self.reader))
                self.assertEqual(assess_history(converted, circuit_id=20496),
                                 assess_history(data, circuit_id=20496))

    def test_unknown_warning_and_old_events_do_not_acquire(self):
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
        self.assertEqual(assess.call_args.kwargs, dict(circuit_id=20496, air_id=8207,
                         coolant_id=8429, reset_id=8832, config=SMART_2_HEATING_CONFIG))

    def test_history_quality_is_not_promoted_to_good_and_metadata_is_retained(self):
        original = history()
        row = original.series[0]
        altered = replace(row, points=tuple(replace(p, quality=Quality.STALE)
                                            for p in row.points), incomplete_reasons=('missing_part',))
        data = replace(original, series=(altered, *original.series[1:]),
                       incomplete_reasons=('source_incomplete',))
        converted = history_packet(data, load_config(reader=self.reader))
        points = converted['devices'][0]['temperature_sensors'][0]['points']
        self.assertTrue(all(p['value'] is None for p in points))
        self.assertTrue(all(p['quality'] == 'stale' for p in points))
        self.assertEqual(converted['incomplete_reasons'], ['source_incomplete'])
        self.assertEqual(converted['devices'][0]['temperature_sensors'][0]['incomplete_reasons'],
                         ['missing_part'])
        self.assertEqual(assess_history(converted, circuit_id=20496)['state_at_period_end'], 'unknown')

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
        self.assertEqual(self.events, ['acquire', 'release'])

    def test_offline_or_unknown_reset_never_attempt_on_or_off(self):
        self.controller.list_devices.return_value = (DeviceInfo(42, 'virtual', None, False),)
        with self.assertRaises(DeviceUnavailable):
            self.module().run_once(self.context)
        self.assertEqual(self.events, ['acquire', 'release'])
        self.events.clear()
        self.controller.list_devices.return_value = (DeviceInfo(42, 'virtual', None, True),)
        current = self.controller.read_current.return_value
        self.controller.read_current.return_value = replace(current, values=(
            replace(current.values[0], value=None, quality=Quality.MISSING),))
        self.assertIs(self.module().run_once(self.context).status, ResultStatus.UNKNOWN)
        self.assertEqual(self.events, ['acquire', 'release'])

    def test_stop_or_exception_during_wait_attempts_immediate_off_and_releases(self):
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
                self.assertEqual(self.events, ['acquire', 'on', 'off', 'release'])
                self.assertEqual(self.clock.now(), 0)

    def test_unknown_on_is_not_retried_and_off_error_is_visible_then_release(self):
        for off_error in (None, ControlOwnershipRequired()):
            def command(command, session):
                self.execute(command, session)
                if dict(command.arguments)['target_state']:
                    raise CommandOutcomeUnknown()
                if off_error:
                    raise off_error
            self.events.clear()
            self.controller.execute_command.side_effect = command
            with self.subTest(off_error=off_error), self.assertRaises(
                    ControlOwnershipRequired if off_error else CommandOutcomeUnknown):
                self.module().run_once(self.context)
            self.assertEqual(self.events, ['acquire', 'on', 'off', 'release'])

    def test_preparation_failure_and_stop_before_on_have_no_off(self):
        self.controller.prepare_command.side_effect = DeviceUnavailable()
        with self.assertRaises(DeviceUnavailable):
            self.module().run_once(self.context)
        self.assertEqual(self.events, ['acquire', 'release'])
        self.events.clear()
        self.context.cancelled.set()
        self.module().run_once(self.context)
        self.assertEqual(self.events, [])

    def test_pending_grant_poll_is_interruptible_and_never_sends_on(self):
        def stop(device, *, lease_seconds, timeout, poll_seconds, cancelled):
            self.assertIs(cancelled, self.context.cancelled)
            cancelled.set()
            return ControlRequestResult(self.uuid_generator(), device, ControlRequestState.DENIED,
                                        reason=ControlRequestReason.CANCELLED)
        self.controller.acquire_control.side_effect = stop
        self.module().run_once(self.context)
        self.controller.acquire_control.assert_called_once()
        self.controller.try_acquire_control.assert_not_called()
        self.controller.execute_command.assert_not_called()
        self.controller.release_control.assert_not_called()

    def test_pending_grant_has_own_deadline_and_unchanged_requested_lease(self):
        def timeout(device, *, lease_seconds, timeout, poll_seconds, cancelled):
            self.clock.advance(timeout)
            return ControlRequestResult(self.uuid_generator(), device, ControlRequestState.DENIED,
                                        reason=ControlRequestReason.TIMEOUT)
        self.controller.acquire_control.side_effect = timeout
        with self.assertRaises(DeviceControlBusy):
            self.module().run_once(self.context)
        self.assertEqual(self.clock.now(), 30)
        self.controller.acquire_control.assert_called_once_with(
            42, lease_seconds=140, timeout=30, poll_seconds=1, cancelled=self.context.cancelled)
        self.assertEqual(self.events, [])
        self.controller.execute_command.assert_not_called()

    def test_pending_grant_can_arrive_then_only_one_pulse_is_sent(self):
        def grant(device, **kwargs):
            self.clock.advance(1)
            return self.acquire_result(device, **kwargs)
        self.controller.acquire_control.side_effect = grant
        self.module().run_once(self.context)
        self.assertEqual(self.events, ['acquire', 'on', ('wait', 40), 'off', 'release'])
        self.assertEqual(self.clock.now(), 41)
        self.controller.acquire_control.assert_called_once()

    def test_pending_grant_timeout_is_not_retried_by_module(self):
        self.controller.acquire_control.side_effect = [
            ControlRequestResult(self.uuid_generator(), 42, ControlRequestState.DENIED,
                                 reason=ControlRequestReason.TIMEOUT),
            ControlRequestResult(self.uuid_generator(), 42, ControlRequestState.GRANTED, session=self.session)]
        with self.assertRaises(DeviceControlBusy):
            self.module().run_once(self.context)
        self.controller.acquire_control.assert_called_once()
        self.controller.execute_command.assert_not_called()

    def test_stop_during_acquisition_releases_immediately_without_further_reads(self):
        def acquire(device, **kwargs):
            self.context.cancelled.set()
            return ControlRequestResult(self.uuid_generator(), device, ControlRequestState.GRANTED,
                                        session=self.session)
        self.controller.acquire_control.side_effect = acquire
        self.module().run_once(self.context)
        self.controller.list_devices.assert_not_called()
        self.controller.read_current.assert_not_called()
        self.assertEqual(self.events, ['release'])

    def test_off_uses_real_executor_admission_and_expired_or_lost_grant_is_error(self):
        for cause in ('expiry', 'grant_lost'):
            with self.subTest(cause=cause):
                gate = LocalTestGate(self.clock, uuid_factory=self.uuid_generator)
                executor = CommandExecutor(gate=gate, clock=self.clock.now, uuid_factory=self.uuid_generator)
                binding = executor.bind()
                self.controller.acquire_control.side_effect = binding.acquire_control
                self.controller.release_control.side_effect = binding.release_control
                sends = []
                self.controller.execute_command.side_effect = lambda command, session: binding.execute(
                    command, session, lambda: command,
                    lambda prepared: sends.append(dict(prepared.arguments)['target_state']))
                def disrupt(context, seconds):
                    if cause == 'expiry':
                        self.clock.advance(200)
                    else:
                        gate.allowed = False
                    context.cancelled.set()
                    return True
                self.context.cancelled.clear()
                with self.assertRaises(ControlOwnershipRequired):
                    self.module(wait=disrupt).run_once(self.context)
                self.assertEqual(sends, [True])
                self.assertEqual(len(gate.releases), 1)
                self.assertEqual(gate.sessions, {})

    def test_cancel_during_accepted_on_completes_it_and_off_uses_same_owned_session(self):
        gate = LocalTestGate(self.clock, uuid_factory=self.uuid_generator)
        executor = CommandExecutor(gate=gate, clock=self.clock.now, uuid_factory=self.uuid_generator)
        binding = executor.bind()
        self.controller.acquire_control.side_effect = binding.acquire_control
        self.controller.release_control.side_effect = binding.release_control
        sends = []
        def send(command):
            sends.append(dict(command.arguments)['target_state'])
            self.assertTrue(gate.sessions)
            if sends == [True]:
                self.context.cancelled.set()
            return True
        self.controller.execute_command.side_effect = lambda command, session: binding.execute(
            command, session, lambda: command, send)
        self.module().run_once(self.context)
        self.assertEqual(sends, [True, False])
        self.assertEqual(len(gate.releases), 1)
        self.assertEqual(gate.sessions, {})

    def test_new_reset_binding_after_unknown_release_waits_for_expiry(self):
        gate = LocalTestGate(self.clock, uuid_factory=self.uuid_generator)
        executor = CommandExecutor(gate=gate, clock=self.clock.now, uuid_factory=self.uuid_generator)
        gate.release = Mock(side_effect=[CommandOutcomeUnknown(), None])
        sends = []
        def bind():
            binding = executor.bind()
            self.controller.acquire_control.side_effect = binding.acquire_control
            self.controller.release_control.side_effect = binding.release_control
            self.controller.execute_command.side_effect = lambda command, session: binding.execute(
                command, session, lambda: command,
                lambda command: sends.append(dict(command.arguments)['target_state']))
            return binding
        bind()
        with self.assertRaises(CommandOutcomeUnknown):
            self.module().run_once(self.context)
        replacement = bind()
        with self.assertRaises(DeviceControlBusy):
            replacement.try_acquire_control(42, lease_seconds=140)
        self.assertEqual(gate.release.call_count, 1)
        self.clock.advance(140)
        self.module().run_once(self.context)
        self.assertEqual(sends, [True, False, True, False])
        self.assertEqual(gate.release.call_count, 2)

    def test_finally_off_failure_is_observed_and_never_retried(self):
        gate = LocalTestGate(self.clock, uuid_factory=self.uuid_generator)
        rows = []
        executor = CommandExecutor(gate=gate, clock=self.clock.now,
            statistics=Statistics(rows.append, protocol_version='1', profile='offline'),
            uuid_factory=self.uuid_generator)
        binding = executor.bind()
        self.controller.acquire_control.side_effect = binding.acquire_control
        self.controller.release_control.side_effect = binding.release_control
        sends = []
        def send(command):
            sends.append(dict(command.arguments)['target_state'])
            if sends[-1] is False:
                raise RateLimited()
        self.controller.execute_command.side_effect = lambda command, session: binding.execute(
            command, session, lambda: command, send)
        with self.assertRaises(RateLimited):
            self.module().run_once(self.context)
        self.assertEqual(sends, [True, False])
        self.assertEqual(len(gate.releases), 1)
        cleanup = [row for row in rows if row['event'] == 'cleanup' and row['phase'] == 'command']
        self.assertEqual(len(cleanup), 1)
        self.assertEqual(cleanup[0]['outcome'], 'failure')

    def test_continuous_pause_starts_after_off_without_ownership_or_once_deadline(self):
        calls = []
        def wait(context, seconds):
            calls.append(seconds)
            if seconds == 300:
                self.assertEqual(self.events[-1], 'release')
                context.cancelled.set()
                return True
            return self.clock.wait(context.cancelled, seconds)
        self.module(wait=wait).run_continuous(self.context)
        self.assertEqual(calls, [40, 300])
        self.assertEqual(self.events, ['acquire', 'on', 'off', 'release'])

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
        spec = next(s for s in BUILTIN_SPECS if s.algorithm_id == 'boiler_reset')
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
        registry = AlgorithmRegistry(config, lambda: self.controller, clock=self.clock.now,
        uuid_factory=self.uuid_generator)
        self.addCleanup(lambda: registry.close(wait_timeout=2))
        registry.register_all([spec])
        handle = registry.start('boiler_reset')
        handle.worker.join(2)
        self.assertFalse(handle.worker.is_alive())
        self.assertIsNone(handle.failure)
        self.assertEqual(waits, [40, 300, 40, 300])
        self.assertGreater(self.clock.now(), MINIMUM_SECONDS)
        self.assertTrue(all(context.deadline is None for context in contexts))
        self.assertEqual(self.events, ['acquire', 'on', 'off', 'release'] * 2)

    def test_registrar_once_uses_builtin_module_and_returns_after_single_pulse(self):
        spec = replace(BUILTIN_SPECS[0], factory=lambda controller: self.module())
        config = RegistryConfig(frozenset(), False, {'boiler_reset': OnceTimeout(
            MINIMUM_SECONDS, MINIMUM_SECONDS)})
        registry = AlgorithmRegistry(config, lambda: self.controller, clock=self.clock.now,
        uuid_factory=self.uuid_generator)
        self.addCleanup(lambda: registry.close(wait_timeout=2))
        registry.register_all([spec])
        result = registry.run_once('boiler_reset')
        self.assertTrue(result.value['reset_performed'])
        self.assertEqual(self.clock.now(), 40)

    def test_builtin_spec_is_explicit_dual_mode_and_minimum_includes_cleanup(self):
        spec = next(s for s in BUILTIN_SPECS if s.algorithm_id == 'boiler_reset')
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
