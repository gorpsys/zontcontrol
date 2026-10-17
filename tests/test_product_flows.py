"""Product boundaries through the actual composition root, with virtual HTTP only."""

from copy import deepcopy
from dataclasses import replace
import json
from threading import Event, Thread
from time import monotonic
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from tests.fakes.zont import FakeZontServer
from tests.test_boiler_cli import Terminal
from tests.test_boiler_reset import SETTINGS, history
from tests.test_inventory import inventory_device
from zontcontrol.algorithms.boiler_cli import BoilerCLI
from zontcontrol.algorithms.boiler_reset import BoilerReset
from zontcontrol.application import create_application
from zontcontrol.autonomy.contracts import (
    AlgorithmBusy, AlgorithmFailed, AlgorithmSpec, AlgorithmState, AlgorithmTimeout, Mode,
    ResultStatus,
)
from zontcontrol.controller.contracts import (
    CommandOutcomeUnknown, ControlKey, ControlOwnershipRequired, DeviceControlBusy,
    ValueDescriptor,
)
from zontcontrol.controller.transport import FailureKind, TransportError
from zontcontrol.coordination.models import decode_r2
from zontcontrol.coordination.statistics import analyze


PREFIX = '  Дом 🏠  '


class VirtualHTTP:
    """Only the documented HTTP requests the composed application actually sends."""

    def __init__(self, server):
        self.server = server
        self.commands = []
        self.unknown_on = False
        self.on_entered = Event()
        self.on_resume = Event()
        self.on_resume.set()
        self.off_entered = Event()
        self.off_resume = Event()
        self.off_resume.set()
        self.closed = False

    def request(self, request):
        if self.closed:
            raise AssertionError('request after transport close')
        url = urlsplit(request.full_url)
        body = json.loads(request.data) if request.data else None
        if url.path.endswith('/update_device'):
            changes = dict(body)
            device_id = changes.pop('device_id')
            sent = self.server.send_update(device_id, changes)
            self.server.apply(sent)
            return {}
        if '/actions/' in url.path:
            device_id = int(url.path.split('/devices/')[1].split('/')[0])
            self.commands.append((device_id, body))
            if body.get('target_state') is True:
                self.on_entered.set()
                if not self.on_resume.wait(2):
                    raise AssertionError('command barrier timed out')
            if body.get('target_state') is False:
                self.off_entered.set()
                if not self.off_resume.wait(2):
                    raise AssertionError('cleanup barrier timed out')
            if body.get('target_state') is True and self.unknown_on:
                raise TransportError(FailureKind.UNCONFIRMED)
            return {}
        if url.path.endswith('/devices'):
            device_ids = parse_qs(url.query).get('device_ids')
            ids = [int(device_ids[0])] if device_ids else [42, 43]
            return {'devices': [self.server.read_device(device_id) for device_id in ids]}
        raise AssertionError('unexpected virtual endpoint')

    def close(self):
        self.closed = True


class ProductFlowTests(unittest.TestCase):
    def setUp(self):
        devices = {device_id: {**deepcopy(inventory_device()), 'id': device_id,
                               'name': PREFIX, 'notes': None} for device_id in (42, 43)}
        devices[42]['controls']['toggle_buttons'][0]['id'] = SETTINGS['reset_control_id']
        self.server = FakeZontServer(devices)
        self.apps = []
        self.transports = []
        self.rows = []
        self.sessions = []
        self.external_threads = []
        self.external_resumes = []
        self.addCleanup(self.cleanup)
        self.network = patch('socket.socket.connect', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def cleanup(self):
        for transport in self.transports:
            transport.on_resume.set()
            transport.off_resume.set()
        for resume in self.external_resumes:
            resume.set()
        for thread in self.external_threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        # Failed assertions must not leave external owners blocking app.close().
        for controller, session in reversed(self.sessions):
            try:
                controller.release_control(session)
            except ControlOwnershipRequired:
                pass  # The scenario already released this exact session.
        for app in reversed(self.apps):
            self.assertTrue(app.close(wait_timeout=2))

    def build(self, catalog=()):
        data = {
            'config/controller.json': {'contact': 'offline@example.invalid',
                'http_timeout_seconds': 1, 'coordination': {
                    'delta_seconds': .02, 'leader_period_seconds': 10},
                'statistics': {'path': 'reports/unused.jsonl', 'profile': 'product-offline'}},
            'config/registry.json': {'once_timeouts': {
                spec.algorithm_id: {'minimum_seconds': .01, 'recommended_seconds': .5}
                for spec in catalog if Mode.ONCE in spec.modes}},
        }
        transport = VirtualHTTP(self.server)
        self.transports.append(transport)
        app = create_application(environ={'ZONT_TOKEN': 'synthetic-offline'}, catalog=catalog,
            reader=lambda path: data[str(path)], transport_factory=lambda **_: transport,
            statistics_sink=self.rows.append)
        self.apps.append(app)
        return app, transport

    def acquire(self, controller, device_id=42, *, track=True):
        deadline = monotonic() + 2
        while True:
            try:
                session = controller.try_acquire_control(device_id, lease_seconds=60)
                if track:
                    self.sessions.append((controller, session))
                return session
            except DeviceControlBusy:
                if monotonic() >= deadline:
                    self.fail('virtual coordinator did not grant within test guard')
                Event().wait(.005)

    def external(self, action):
        results, errors = [], []
        def run():
            try:
                results.append(action())
            except BaseException as error:
                errors.append(error)
        thread = Thread(target=run)
        self.external_threads.append(thread)
        thread.start()
        return thread, results, errors

    def command(self, controller, device_id=42, state=True):
        return controller.prepare_command(device_id,
            ControlKey(device_id, 'toggle_buttons', 8832 if device_id == 42 else 31),
            'trigger', {'target_state': state})

    def test_independent_applications_two_devices_and_no_ownership_bypass(self):
        first, first_http = self.build()
        second, second_http = self.build()
        owner, rival = first.controller(), second.controller()
        command = self.command(owner)
        with self.assertRaises(ControlOwnershipRequired):
            owner.execute_command(command, None)
        session = self.acquire(owner)
        raw = self.server.read_device(42)['name']
        self.assertTrue(raw.startswith(PREFIX))
        self.assertIn('управление занято', raw)
        self.assertIsNotNone(decode_r2(raw, PREFIX))
        # Reading and preparing on both roots remain available while occupied.
        self.assertEqual([row.device_id for row in rival.list_devices()], [42, 43])
        self.command(rival)
        with self.assertRaises(DeviceControlBusy):
            rival.try_acquire_control(42, lease_seconds=60)
        with self.assertRaises(ControlOwnershipRequired):
            rival.execute_command(command, session)
        other = self.acquire(rival, 43)
        rival.execute_command(self.command(rival, 43), other)
        owner.execute_command(command, session)
        self.assertEqual(first_http.commands, [(42, {'target_state': True})])
        self.assertEqual(second_http.commands, [(43, {'target_state': True})])
        owner.release_control(session)
        rival.release_control(other)
        self.assertTrue(any(row['event'] == 'grant' for row in self.rows))
        self.assertIn('groups', analyze(self.rows))

    def test_close_waits_for_external_off_accepted_by_http_and_session_release(self):
        app, transport = self.build()
        owner = app.controller()
        session = self.acquire(owner)
        off = self.command(owner, state=False)
        transport.off_resume.clear()
        thread, results, errors = self.external(lambda: owner.execute_command(off, session))
        self.assertTrue(transport.off_entered.wait(2))
        self.assertFalse(app.close(wait_timeout=.01))
        self.assertFalse(transport.closed)
        self.assertIsNotNone(decode_r2(self.server.read_device(42)['name'], PREFIX))
        with self.assertRaises(ControlOwnershipRequired):
            app.controller()
        with self.assertRaises(ControlOwnershipRequired):
            owner.try_acquire_control(43, lease_seconds=60)
        transport.off_resume.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(results[0].api_accepted)
        # A completed command does not end the caller's critical section.
        self.assertFalse(app.close(wait_timeout=.01))
        self.assertFalse(transport.closed)
        owner.release_control(session)
        self.assertTrue(app.close(wait_timeout=2))
        self.assertTrue(transport.closed)
        self.assertEqual(transport.commands, [(42, {'target_state': False})])
        self.assertEqual(self.server.read_device(42)['name'], PREFIX)

    def test_close_between_external_on_and_finally_keeps_cleanup_available(self):
        app, transport = self.build()
        owner = app.controller()
        on_done, finish = Event(), Event()
        self.external_resumes.append(finish)
        def pulse():
            session = self.acquire(owner)
            try:
                owner.execute_command(self.command(owner), session)
                on_done.set()
                if not finish.wait(2):
                    raise AssertionError('external pulse barrier timed out')
            finally:
                try:
                    owner.execute_command(self.command(owner, state=False), session)
                finally:
                    owner.release_control(session)
        thread, _, errors = self.external(pulse)
        self.assertTrue(on_done.wait(2))
        self.assertEqual(transport.commands, [(42, {'target_state': True})])
        self.assertFalse(app.close(wait_timeout=.01))
        self.assertFalse(transport.closed)
        finish.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(app.close(wait_timeout=2))
        self.assertTrue(transport.closed)
        self.assertEqual(transport.commands,
                         [(42, {'target_state': True}), (42, {'target_state': False})])
        self.assertEqual(self.server.read_device(42)['name'], PREFIX)

    def test_close_waits_for_external_device_after_registered_finally(self):
        self.assert_shared_shutdown('registered')

    def test_close_waits_for_registered_finally_after_external_device(self):
        self.assert_shared_shutdown('external')

    def assert_shared_shutdown(self, first):
        pulse, cleaned = Event(), Event()
        test = self
        class RegisteredPulse:
            def __init__(self, controller):
                self.controller = controller
            def run_continuous(self, context):
                session = test.acquire(self.controller, track=False)
                try:
                    self.controller.execute_command(test.command(self.controller), session)
                    pulse.set()
                    context.cancelled.wait(2)
                finally:
                    try:
                        self.controller.execute_command(
                            test.command(self.controller, state=False), session)
                    finally:
                        self.controller.release_control(session)
                        cleaned.set()
        spec = AlgorithmSpec('registered_pulse', 'Offline lifecycle pulse',
                             frozenset({Mode.CONTINUOUS}), RegisteredPulse)
        app, transport = self.build((spec,))
        self.assertTrue(pulse.wait(2))
        owner = app.controller()
        session = self.acquire(owner, 43)
        transport.off_resume.clear()
        self.assertFalse(app.close(wait_timeout=.01))
        self.assertTrue(transport.off_entered.wait(2))
        if first == 'registered':
            transport.off_resume.set()
            self.assertTrue(cleaned.wait(2))
        else:
            owner.release_control(session)
        self.assertFalse(app.close(wait_timeout=.01))
        self.assertFalse(transport.closed)
        if first == 'registered':
            owner.release_control(session)
        else:
            transport.off_resume.set()
        self.assertTrue(app.close(wait_timeout=2))
        self.assertTrue(cleaned.is_set())
        self.assertTrue(transport.closed)
        self.assertEqual(transport.commands,
                         [(42, {'target_state': True}), (42, {'target_state': False})])
        self.assertEqual(self.server.read_device(42)['name'], PREFIX)
        self.assertEqual(self.server.read_device(43)['name'], PREFIX)

    def test_close_during_external_unknown_on_preserves_outcome_and_finally_without_retry(self):
        app, transport = self.build()
        owner = app.controller()
        session = self.acquire(owner)
        transport.unknown_on = True
        transport.on_resume.clear()
        cleanup_results = []
        def pulse():
            try:
                return owner.execute_command(self.command(owner), session)
            finally:
                try:
                    cleanup_results.append(owner.execute_command(
                        self.command(owner, state=False), session))
                finally:
                    owner.release_control(session)
        thread, results, errors = self.external(pulse)
        self.assertTrue(transport.on_entered.wait(2))
        self.assertFalse(app.close(wait_timeout=.01))
        self.assertFalse(transport.closed)
        transport.on_resume.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], CommandOutcomeUnknown)
        self.assertTrue(cleanup_results[0].api_accepted)
        self.assertTrue(app.close(wait_timeout=2))
        self.assertTrue(transport.closed)
        self.assertEqual(transport.commands,
                         [(42, {'target_state': True}), (42, {'target_state': False})])
        self.assertEqual(self.server.read_device(42)['name'], PREFIX)
        self.assertTrue(any(row.get('outcome') == 'unknown' for row in self.rows))

    def reset_catalog(self, wait):
        return (AlgorithmSpec('boiler_reset', 'Offline reset', frozenset({Mode.ONCE}),
            lambda controller: BoilerReset(controller, reader=lambda _: {
                **SETTINGS, 'grant_poll_seconds': .005},
                wall_clock=lambda: history().end, wait=wait)),)

    def history_patches(self):
        rows = tuple(ValueDescriptor(row.key, 'virtual', row.unit, row.source,
            row.key.field, row.value_type, True, True, row.kind) for row in history().series)
        return (patch('zontcontrol.controller.service.ControllerService.describe_values',
                      return_value=rows),
                patch('zontcontrol.controller.service.ControllerService.read_history',
                      return_value=history()))

    def test_registered_reset_timeout_keeps_stopping_and_finally_while_cli_competes(self):
        pulse = Event()
        def wait(context, seconds):
            if seconds != 40:
                return context.cancelled.wait(seconds)
            pulse.set()
            return context.cancelled.wait(2)
        terminal = Terminal()
        cli_data = {'devices': [42, 43], 'controls': [{'device_id': device_id,
            'kind': 'toggle_buttons', 'element_id': element, 'actions': ['trigger']}
            for device_id, element in ((42, 8832), (43, 31))], 'graphs': [],
            'grant_wait_seconds': .05}
        cli = AlgorithmSpec('boiler_cli', 'Offline terminal', frozenset({Mode.CONTINUOUS}),
            lambda controller: BoilerCLI(controller, reader=lambda _: cli_data, terminal=terminal))
        descriptions, archive = self.history_patches()
        with descriptions, archive:
            app, transport = self.build((*self.reset_catalog(wait), cli))
            transport.off_resume.clear()
            results = []
            def run():
                try:
                    app.run_once('boiler_reset')
                except Exception as error:
                    results.append(error)
            thread = Thread(target=run)
            thread.start()
            self.addCleanup(thread.join, 2)
            self.assertTrue(pulse.wait(2))
            self.assertTrue(transport.off_entered.wait(2))
            thread.join(1)
            self.assertIsInstance(results[0], AlgorithmTimeout)
            reset = next(row for row in app.list_algorithms()
                         if row.spec.algorithm_id == 'boiler_reset')
            self.assertIs(reset.state, AlgorithmState.STOPPING)
            with self.assertRaises(AlgorithmBusy):
                app.run_once('boiler_reset')
            preview = "command 42 toggle_buttons 8832 trigger '{\"target_state\":false}'"
            terminal.lines.put(preview)
            terminal.lines.put(preview + ' --execute')
            deadline = monotonic() + 1
            while not any('busy' in line.lower() for line in terminal.output):
                self.assertLess(monotonic(), deadline)
                Event().wait(.005)
            self.assertEqual([body for _, body in transport.commands],
                             [{'target_state': True}, {'target_state': False}])
            self.assertFalse(app.close(wait_timeout=0))
            self.assertFalse(transport.closed)
            transport.off_resume.set()
            self.assertTrue(app.close(wait_timeout=2))
            self.assertTrue(transport.closed)
            self.assertEqual(self.server.read_device(42)['name'], PREFIX)

    def test_unknown_on_is_not_retried_and_registered_failure_still_sends_off(self):
        descriptions, archive = self.history_patches()
        with descriptions, archive:
            app, transport = self.build(self.reset_catalog(lambda *_: False))
            transport.unknown_on = True
            with self.assertRaises(AlgorithmFailed):
                app.run_once('boiler_reset', timeout=1)
            self.assertTrue(app.close(wait_timeout=2))
        self.assertEqual(transport.commands,
                         [(42, {'target_state': True}), (42, {'target_state': False})])
        self.assertEqual(self.server.read_device(42)['name'], PREFIX)
        self.assertTrue(any(row.get('outcome') == 'unknown' for row in self.rows))

    def test_facade_unknown_foreign_forged_and_expired_sessions_never_repeat_effects(self):
        app, transport = self.build()
        owner, stranger = app.controller(), app.controller()
        session = self.acquire(owner)
        command = self.command(owner)
        for controller, supplied in ((stranger, session), (owner, replace(session)),
                                     (owner, None)):
            with self.subTest(controller=controller, supplied=supplied), \
                    self.assertRaises(ControlOwnershipRequired):
                controller.execute_command(command, supplied)
        transport.unknown_on = True
        with self.assertRaises(CommandOutcomeUnknown):
            owner.execute_command(command, session)
        self.assertEqual(transport.commands, [(42, {'target_state': True})])
        self.assertIsNotNone(decode_r2(self.server.read_device(42)['name'], PREFIX))
        # Expiry is local admission policy; retaining the mutex does not extend R2.
        with patch.object(app._controller._executor, '_clock', return_value=session.expires_at):
            with self.assertRaises(ControlOwnershipRequired):
                owner.execute_command(self.command(owner, state=False), session)
        self.assertEqual(transport.commands, [(42, {'target_state': True})])
        owner.release_control(session)

    def test_all_once_preserves_detector_unknown_and_isolates_failure_from_continuous(self):
        class Broken:
            def __init__(self, controller):
                pass
            def run_once(self, context):
                raise RuntimeError('private module failure')
        terminal = Terminal()
        cli = AlgorithmSpec('boiler_cli', 'Offline terminal', frozenset({Mode.CONTINUOUS}),
            lambda controller: BoilerCLI(controller, terminal=terminal,
                reader=lambda _: {'devices': [42], 'controls': [], 'graphs': []}))
        broken = AlgorithmSpec('broken', 'Failure isolation', frozenset({Mode.ONCE}), Broken)
        descriptions, archive = self.history_patches()
        incomplete = replace(history(), series=tuple(replace(row, points=())
                                                   for row in history().series))
        with descriptions, archive as read:
            read.return_value = incomplete
            app, transport = self.build((*self.reset_catalog(lambda *_: False), cli, broken))
            result = app.run_all_once()
            self.assertIs(result['boiler_reset'].status, ResultStatus.UNKNOWN)
            self.assertFalse(result['boiler_reset'].value['reset_performed'])
            self.assertIsInstance(result['broken'], AlgorithmFailed)
            self.assertNotIn('private', str(result['broken']))
            self.assertEqual(transport.commands, [])
            rows = {row.spec.algorithm_id: row for row in app.list_algorithms()}
            self.assertIs(rows['boiler_cli'].state, AlgorithmState.RUNNING_CONTINUOUS)
            self.assertTrue(app.stop('boiler_cli', wait_timeout=1))


if __name__ == '__main__':
    unittest.main()
