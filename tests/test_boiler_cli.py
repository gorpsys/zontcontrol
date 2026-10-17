"""Persistent UI behavior with virtual terminals/controllers; no network/viewer."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import pty
from queue import Empty, Queue
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from zontcontrol.algorithms.boiler_cli import BoilerCLI, Terminal as RealTerminal, load_config, UPDATE_SECONDS
from zontcontrol.autonomy.catalog import BUILTIN_SPECS
from zontcontrol.autonomy.contracts import AlgorithmBusy, AlgorithmState, Mode, RunContext
from zontcontrol.autonomy.registry import AlgorithmRegistry
from zontcontrol.config import ConfigurationError, RegistryConfig
from zontcontrol.controller.service import ControllerService
from tests.test_inventory import inventory_device
from zontcontrol.controller.contracts import (
    CommandOutcomeUnknown, ControlDescriptor, ControlKey, ControlSession, DeviceControlBusy,
    ControlRequestResult, ControlRequestState, ControlRequestReason, ControlRequestErrorCode,
    DeviceInfo, DeviceUnavailable, HistoryPoint, HistoryResult, HistorySeries, Quality,
    SeriesKind, Snapshot, ValidatedCommand, ValueDescriptor, ValueKey, ValueReading, ValueType,
)


NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
KEY = ValueKey(42, 'sensor', 7, 'value')
CONTROL = ControlKey(42, 'circuit', 8)
SETTINGS = dict(devices=[42], controls=[dict(device_id=42, kind=CONTROL.kind,
    element_id=8, actions=['target-temp'])], graphs=[dict(graph_id='air', title='Air',
    series=[dict(key=dict(device_id=42, kind='sensor', element_id=7, field='value'))])])


class Terminal:
    def __init__(self, identity=None):
        self.identity = identity or uuid4()
        self.active = True
        self.lines = Queue()
        self.output = []
        self.written = Event()

    def available(self):
        return self.active

    def poll(self, timeout):
        try:
            return self.lines.get(timeout=timeout)
        except Empty:
            return None

    def write(self, text):
        self.output.append(text)
        self.written.set()


class BoilerCLITests(unittest.TestCase):
    def setUp(self):
        self.terminal = Terminal()
        self.context = RunContext('boiler_cli', uuid4(), uuid4(), Event(), None)
        self.controller = Mock()
        self.controller.list_devices.return_value = (DeviceInfo(42, 'House', 'SMART', True),)
        self.descriptor = ValueDescriptor(KEY, 'Air', '°C', 'virtual', 'air', ValueType.NUMBER,
                                          True, True, SeriesKind.MEASUREMENT)
        self.controller.describe_values.return_value = (self.descriptor,)
        self.control = ControlDescriptor(CONTROL, 'Target', ('target-temp',), minimum=5, maximum=30)
        self.controller.list_controls.return_value = (self.control,)
        self.snapshot = Snapshot(42, NOW, (ValueReading(KEY, 20, ValueType.NUMBER, '°C',
            'virtual', NOW - timedelta(seconds=12), NOW, Quality.UNKNOWN),))
        self.controller.read_current.return_value = self.snapshot
        self.controller.prepare_command.side_effect = lambda device, key, action, args: (
            ValidatedCommand(key, action, tuple(args.items())))
        self.session = ControlSession(42, uuid4(), 200)
        self.granted = ControlRequestResult(uuid4(), 42, ControlRequestState.GRANTED,
                                            session=self.session)
        self.controller.acquire_control.return_value = self.granted
        self.reader = Mock(side_effect=lambda _: json.loads(json.dumps(SETTINGS)))

    def module(self, **kwargs):
        return BoilerCLI(self.controller, reader=self.reader, terminal=self.terminal,
                         wall_clock=lambda: NOW, **kwargs)

    def launch(self, module=None):
        module = module or self.module()
        errors = []
        def run():
            try:
                module.run_continuous(self.context)
            except Exception as error:
                errors.append(error)
        thread = Thread(target=run)
        thread.start()
        self.addCleanup(lambda: (self.context.cancelled.set(), thread.join(2)))
        return thread, errors

    def command(self, tail='', module=None, action='target-temp', args='{"target_temp":20}'):
        (module or self.module()).handle_line(
            f"command 42 {CONTROL.kind} 8 {action} '{args}' {tail}", self.context)

    def test_fixed_config_loaded_by_module_and_invalid_before_facade(self):
        self.module()
        self.reader.assert_called_once_with(Path('config/boiler_cli.json'))
        self.assertEqual(self.controller.mock_calls, [])
        with TemporaryDirectory() as directory:
            with self.assertRaises(ConfigurationError):
                BoilerCLI(self.controller, root=directory, terminal=self.terminal)
        for updates in (dict(devices=[]), dict(devices=[True]), dict(devices=[42, 42]),
                        dict(devices=[3]), dict(lease_seconds=0), dict(lease_seconds=float('inf')),
                        dict(display_timezone='nonesuch'), dict(graphs=[{}]),
                        dict(controls=[dict(device_id=42, kind='x', element_id=0, actions=[])]),
                        dict(update_seconds=1), dict(output_directory='')):
            with self.subTest(updates=updates), self.assertRaises(ConfigurationError):
                load_config(reader=lambda _: {**SETTINGS, **updates})
        self.assertEqual(self.controller.mock_calls, [])

    def test_grant_wait_optional_default_and_validation_before_facade(self):
        for lease, expected in ((60, 30), (10, 5), (1200, 30), (.1, .05)):
            with self.subTest(lease=lease):
                config = load_config(reader=lambda _: {**SETTINGS, 'lease_seconds': lease})
                self.assertEqual(config.grant_wait_seconds, expected)
        for value in (0, -1, True, False, float('inf'), float('nan'), '3', None, 61):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                load_config(reader=lambda _: {**SETTINGS, 'grant_wait_seconds': value})
        for value in (.1, 30, 60):
            self.assertEqual(load_config(reader=lambda _: {
                **SETTINGS, 'grant_wait_seconds': value}).grant_wait_seconds, value)
        self.assertEqual(self.controller.mock_calls, [])

    def test_first_execute_waits_for_grant_while_reader_remains_available(self):
        entered, grant = Event(), Event()
        self.addCleanup(grant.set)
        def acquire(device, *, lease_seconds, timeout, cancelled):
            self.assertEqual((device, lease_seconds, timeout), (42, 60, 30))
            self.assertIs(cancelled, self.context.cancelled)
            entered.set()
            self.assertTrue(grant.wait(2))
            return self.granted
        self.controller.acquire_control.side_effect = acquire
        self.terminal.lines.put('command 42 circuit 8 target-temp \'{"target_temp":20}\' --execute')
        thread, errors = self.launch()
        self.assertTrue(entered.wait(1))
        self.controller.execute_command.assert_not_called()
        reads = self.controller.read_current.call_count
        # The independent reader remains usable throughout the synchronous wait.
        self.module().refresh()
        self.assertGreater(self.controller.read_current.call_count, reads)
        grant.set()
        self.terminal.lines.put('quit')
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.controller.acquire_control.assert_called_once()
        self.controller.try_acquire_control.assert_not_called()
        self.controller.execute_command.assert_called_once()
        self.controller.release_control.assert_called_once_with(self.session)

    def test_denied_grant_never_executes_or_releases(self):
        for reason in ControlRequestReason:
            with self.subTest(reason=reason):
                self.controller.reset_mock()
                self.controller.acquire_control.return_value = ControlRequestResult(
                    uuid4(), 42, ControlRequestState.DENIED, reason=reason,
                    error_code=ControlRequestErrorCode.AUTHENTICATION_ERROR
                    if reason is ControlRequestReason.CONTROLLER_ERROR else None)
                self.command('--execute')
                self.controller.acquire_control.assert_called_once()
                self.controller.try_acquire_control.assert_not_called()
                self.controller.execute_command.assert_not_called()
                self.controller.release_control.assert_not_called()
                if reason is ControlRequestReason.CONTROLLER_ERROR:
                    self.assertEqual(self.terminal.output[-1], 'Authentication failed.')

    def test_stop_cancels_wait_without_sending_command(self):
        entered = Event()
        def acquire(device, *, lease_seconds, timeout, cancelled):
            entered.set()
            self.assertTrue(cancelled.wait(2))
            return ControlRequestResult(uuid4(), device, ControlRequestState.DENIED,
                                        reason=ControlRequestReason.CANCELLED)
        self.controller.acquire_control.side_effect = acquire
        self.terminal.lines.put('command 42 circuit 8 target-temp \'{"target_temp":20}\' --execute')
        thread, errors = self.launch()
        self.assertTrue(entered.wait(1))
        self.context.cancelled.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.controller.execute_command.assert_not_called()
        self.controller.release_control.assert_not_called()

    def test_controller_error_uses_new_safe_exception_for_each_attempt(self):
        result = ControlRequestResult(uuid4(), 42, ControlRequestState.DENIED,
            reason=ControlRequestReason.CONTROLLER_ERROR,
            error_code=ControlRequestErrorCode.DEVICE_UNAVAILABLE)
        self.controller.acquire_control.return_value = result
        made = []
        original = ControlRequestResult.make_error
        def make_error(outcome):
            error = original(outcome)
            made.append(error)
            return error
        with patch.object(ControlRequestResult, 'make_error', make_error):
            self.command('--execute')
            self.command('--execute')
        self.assertEqual(len(made), 2)
        self.assertIsNot(made[0], made[1])
        self.assertEqual(self.terminal.output[-1], str(DeviceUnavailable()))
        self.controller.execute_command.assert_not_called()
        self.controller.release_control.assert_not_called()

    def test_headless_registration_failed_and_disabled_never_reads_config(self):
        self.terminal.active = False
        spec = next(s for s in BUILTIN_SPECS if s.algorithm_id == 'boiler_cli')
        self.assertEqual(spec.modes, frozenset({Mode.CONTINUOUS}))
        self.assertTrue(RegistryConfig(frozenset(), False, {}).should_autostart(spec))
        registry = AlgorithmRegistry(RegistryConfig(frozenset(), False, {}), lambda: self.controller)
        self.addCleanup(lambda: registry.close(wait_timeout=1))
        report = registry.register_all((replace(spec, factory=lambda c: self.module()),))
        self.assertIs(report[0].state, AlgorithmState.FAILED)
        self.controller.execute_command.assert_not_called()
        self.reader.reset_mock()
        disabled = AlgorithmRegistry(RegistryConfig(frozenset({'boiler_cli'}), False, {}),
                                     lambda: self.controller)
        self.addCleanup(lambda: disabled.close(wait_timeout=1))
        report = disabled.register_all((replace(spec, factory=lambda c: self.module()),))
        self.assertTrue(report[0].disabled)
        self.reader.assert_not_called()

    def test_registration_autostarts_stop_restart_and_once_is_unavailable(self):
        spec = next(s for s in BUILTIN_SPECS if s.algorithm_id == 'boiler_cli')
        made = []
        def factory(controller):
            module = self.module()
            made.append(module)
            return module
        registry = AlgorithmRegistry(RegistryConfig(frozenset(), False, {}), lambda: self.controller)
        self.addCleanup(lambda: registry.close(wait_timeout=1))
        registry.register_all((replace(spec, factory=factory),))
        self.assertTrue(self.terminal.written.wait(1))
        self.assertIs(registry.list_algorithms()[0].state, AlgorithmState.RUNNING_CONTINUOUS)
        first = registry.start('boiler_cli')
        self.assertIsNone(first.context.deadline)
        with self.assertRaises(ValueError):
            registry.run_once('boiler_cli')
        self.assertTrue(registry.stop('boiler_cli', wait_timeout=1))
        again = registry.start('boiler_cli')
        self.assertNotEqual(first.context.instance_id, again.context.instance_id)
        self.assertEqual(len(made), 3)  # registration probe and two fresh launches
        self.assertTrue(registry.stop('boiler_cli', wait_timeout=1))
        self.controller.execute_command.assert_not_called()

    def test_actual_terminal_poll_buffers_lines_and_reports_eof_without_blocking(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        with os.fdopen(slave, 'r', encoding='utf-8') as stream:
            terminal = RealTerminal(stream, stream)
            self.assertTrue(terminal.available())
            self.assertEqual(terminal.identity, os.ttyname(slave))
            self.assertIsNone(terminal.poll(0))
            os.write(master, b'help\nquit\n')
            self.assertEqual(terminal.poll(.1), 'help')
            self.assertEqual(terminal.poll(.1), 'quit')

    def test_stop_keeps_stopping_and_terminal_until_pending_read_finishes(self):
        entered, release = Event(), Event()
        def slow(device):
            entered.set()
            release.wait(2)
            return self.snapshot
        self.controller.read_current.side_effect = slow
        spec = replace(next(s for s in BUILTIN_SPECS if s.algorithm_id == 'boiler_cli'),
                       factory=lambda c: self.module())
        registry = AlgorithmRegistry(RegistryConfig(frozenset(), False, {}), lambda: self.controller)
        self.addCleanup(lambda: registry.close(wait_timeout=2))
        self.addCleanup(release.set)
        registry.register_all((spec,))
        self.assertTrue(entered.wait(1))
        self.assertFalse(registry.stop('boiler_cli', wait_timeout=0))
        self.assertIs(registry.list_algorithms()[0].state, AlgorithmState.STOPPING)
        with self.assertRaises(AlgorithmBusy):
            registry.start('boiler_cli')
        with self.assertRaises(ValueError):
            self.module().run_continuous(self.context)
        release.set()
        self.assertTrue(registry.stop('boiler_cli', wait_timeout=1))

    def test_example_is_accepted_without_effects_and_unavailable_device_is_visible(self):
        example = json.loads(Path('config/boiler_cli.example.json').read_text())
        config = load_config(reader=lambda _: example)
        self.assertEqual(config.devices, (42,))
        self.assertEqual(config.display_timezone, 'Europe/Moscow')
        module = self.module()
        self.controller.list_devices.return_value = ()
        self.command('--execute', module=module)
        self.controller.acquire_control.assert_not_called()
        self.controller.prepare_command.assert_not_called()

    def test_preview_and_explicit_execute_acquire_release_only_during_command(self):
        self.command()
        self.controller.acquire_control.assert_not_called()
        self.assertIn('preview', '\n'.join(self.terminal.output))
        self.command('--execute')
        self.controller.acquire_control.assert_called_once_with(
            42, lease_seconds=60, timeout=30, cancelled=self.context.cancelled)
        self.controller.execute_command.assert_called_once_with(
            ValidatedCommand(CONTROL, 'target-temp', (('target_temp', 20),)), self.session)
        self.controller.release_control.assert_called_once_with(self.session)
        text = '\n'.join(self.terminal.output)
        self.assertIn('42', text)
        self.assertIn('API accepted', text)
        self.assertNotIn('recovered', text)

    def test_busy_unknown_and_error_do_not_retry_and_release_on_sent_error(self):
        for error in (DeviceControlBusy(), CommandOutcomeUnknown(), DeviceUnavailable()):
            with self.subTest(error=error):
                self.controller.reset_mock()
                self.controller.acquire_control.return_value = (
                    ControlRequestResult(uuid4(), 42, ControlRequestState.DENIED,
                                         reason=ControlRequestReason.TIMEOUT)
                    if isinstance(error, DeviceControlBusy) else self.granted)
                self.controller.execute_command.side_effect = error
                self.command('--execute')
                self.assertIn(str(error), self.terminal.output[-1])
                self.assertEqual(self.controller.execute_command.call_count,
                                 0 if isinstance(error, DeviceControlBusy) else 1)
                self.assertEqual(self.controller.release_control.call_count,
                                 0 if isinstance(error, DeviceControlBusy) else 1)

    def test_allowlist_and_current_availability_checked_before_preparation(self):
        for control in (None, replace(self.control, unavailable_reasons=('offline',)),
                        replace(self.control, actions=('other',))):
            self.controller.list_controls.return_value = () if control is None else (control,)
            self.command('--execute')
        self.command('--execute', action='reset')
        self.module().handle_line('command 43 circuit 8 target-temp {} --execute', self.context)
        self.controller.prepare_command.assert_not_called()
        self.controller.acquire_control.assert_not_called()

    def test_temperature_bounds_and_invalid_json_never_acquire(self):
        raw = inventory_device()
        raw['circuits'][0].update(id=8, min=5, max=30)
        adapter = Mock()
        adapter.devices.return_value = [raw]
        self.controller.prepare_command.side_effect = ControllerService(adapter).prepare_command
        for args in ('{"target_temp":4}', '{"target_temp":31}', '{"target_temp":NaN}',
                     '{"target_temp":true}', 'oops'):
            self.command('--execute', args=args)
        self.controller.acquire_control.assert_not_called()

    def test_reset_uses_same_facade_and_does_not_schedule_automatic_off(self):
        key = ControlKey(42, 'toggle_buttons', 8)
        settings = {**SETTINGS, 'controls':[dict(device_id=42, kind=key.kind,
                    element_id=8, actions=['trigger'])]}
        self.reader.side_effect = lambda _: settings
        self.controller.list_controls.return_value = (replace(self.control, key=key, actions=('trigger',)),)
        self.module().handle_line('command 42 toggle_buttons 8 trigger \'{"target_state":true}\' --execute',
                                  self.context)
        self.controller.execute_command.assert_called_once()
        self.controller.release_control.assert_called_once()

    def test_current_quality_age_missing_and_read_error_are_visible(self):
        module = self.module()
        module.refresh()
        text = '\n'.join(self.terminal.output)
        self.assertIn('unknown', text)
        self.assertIn('12', text)
        self.controller.read_current.side_effect = DeviceUnavailable()
        module.refresh()
        self.assertIn('stale', self.terminal.output[-1])
        self.assertIn('12', self.terminal.output[-1])
        self.controller.read_current.side_effect = None
        self.controller.read_current.return_value = Snapshot(42, NOW, ())
        module.refresh()
        self.assertIn('missing', self.terminal.output[-1])

    def test_input_during_slow_read_and_stop_waits_for_reader_and_terminal(self):
        entered, release = Event(), Event()
        def slow(*args):
            entered.set()
            release.wait(2)
            return self.snapshot
        self.controller.read_current.side_effect = slow
        thread, errors = self.launch()
        self.assertTrue(entered.wait(1))
        self.terminal.written.clear()
        self.terminal.lines.put('help')
        self.assertTrue(self.terminal.written.wait(1))
        self.assertIn('command', '\n'.join(self.terminal.output))
        self.context.cancelled.set()
        self.assertTrue(thread.is_alive())
        release.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_tty_loss_stops_and_releases_single_terminal_owner(self):
        thread, errors = self.launch()
        self.assertTrue(self.terminal.written.wait(1))
        with self.assertRaises(ValueError):
            self.module().run_continuous(replace(self.context, cancelled=Event()))
        self.terminal.active = False
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.terminal.active = True
        self.context.cancelled.set()
        self.module().run_continuous(self.context)

    def test_schedule_is_minutely_and_skips_missed_slots(self):
        self.assertEqual(UPDATE_SECONDS, 60)
        now = [0]
        waits = []
        module = self.module(clock=lambda: now[0])
        def refresh():
            now[0] = 130
        module.refresh = refresh
        stop = Event()
        def wait(seconds):
            waits.append(seconds)
            stop.set()
            return True
        module.update_loop(stop, wait=wait)
        self.assertEqual(waits, [50])

    def test_graph_fetches_history_writes_svg_and_view_is_explicit(self):
        start = NOW - timedelta(hours=1)
        self.controller.read_history.return_value = HistoryResult(42, start, NOW, NOW, (
            HistorySeries(KEY, ValueType.NUMBER, '°C', 'virtual', SeriesKind.MEASUREMENT,
                          (HistoryPoint(start, 18), HistoryPoint(NOW, 20))),))
        with TemporaryDirectory() as directory:
            self.reader.side_effect = lambda _: {**SETTINGS, 'output_directory':directory}
            viewer = Mock()
            module = self.module(viewer=viewer)
            command = f'graph 42 air {start.isoformat()} {NOW.isoformat()}'
            module.handle_line(command, self.context)
            path = Path(directory) / 'device-42-air.svg'
            self.assertIn('<svg', path.read_text())
            self.assertIn(str(path), self.terminal.output[-1])
            viewer.assert_not_called()
            module.handle_line(command + ' --view', self.context)
            viewed = Path(self.terminal.output[-1])
            viewer.assert_called_once_with(viewed)
            self.assertTrue(viewed.is_file())
            self.controller.read_history.assert_called_with(42, (KEY,), start, NOW)
            self.controller.acquire_control.assert_not_called()

    def test_mixed_graph_units_and_unavailable_control_fail_without_history_or_command(self):
        target = ValueKey(42, 'circuit', 8, 'target_temp')
        settings = json.loads(json.dumps(SETTINGS))
        settings['graphs'][0]['series'].append(dict(key=dict(device_id=42, kind='circuit',
                                                            element_id=8, field='target_temp')))
        self.reader.side_effect = lambda _: settings
        self.controller.describe_values.return_value = (self.descriptor,
            replace(self.descriptor, key=target, unit='bar'))
        module = self.module()
        module.handle_line(f'graph 42 air {(NOW-timedelta(hours=1)).isoformat()} {NOW.isoformat()}',
                           self.context)
        self.controller.read_history.assert_not_called()

    def test_quit_and_eof_cooperatively_release_terminal_for_restart(self):
        self.terminal.lines.put('quit')
        thread, errors = self.launch()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.terminal.poll = Mock(side_effect=EOFError())
        thread, errors = self.launch()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], EOFError)
        self.context.cancelled.set()
        self.module().run_continuous(self.context)

    def test_invalid_graph_dates_timezone_or_binding_do_not_fetch_or_write(self):
        module = self.module()
        for command in ('graph 42 air 2026-01-01 2026-01-02',
                        f'graph 42 air {NOW.isoformat()} {NOW.isoformat()}',
                        f'graph 43 air {(NOW-timedelta(hours=1)).isoformat()} {NOW.isoformat()}'):
            module.handle_line(command, self.context)
        self.controller.describe_values.return_value = (replace(self.descriptor, history_available=False,
                                                                 history_kind=None),)
        module.handle_line(f'graph 42 air {(NOW-timedelta(hours=1)).isoformat()} {NOW.isoformat()}', self.context)
        self.controller.read_history.assert_not_called()

    def test_no_hidden_control_of_other_algorithms_or_unknown_commands(self):
        module = self.module()
        for line in ('stop boiler_reset', 'start boiler_reset', 'reset', 'quit now'):
            module.handle_line(line, self.context)
        self.assertEqual(self.controller.mock_calls, [])
        self.assertFalse(self.context.cancelled.is_set())


if __name__ == '__main__':
    unittest.main()
