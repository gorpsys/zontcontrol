"""Real application and API adaptation with offline HTTP and no synchronization."""

from copy import deepcopy
from dataclasses import replace
import json
from threading import Event, Thread
from urllib.parse import urlsplit
import unittest
from unittest.mock import patch

from tests.test_boiler_cli import Terminal
from tests.test_boiler_reset import SETTINGS, history
from tests.test_inventory import inventory_device
from zontcontrol.algorithms.boiler_cli import BoilerCLI
from zontcontrol.algorithms.boiler_reset import BoilerReset
from zontcontrol.application import create_application
from zontcontrol.autonomy.contracts import (
    AlgorithmFailed, AlgorithmSpec, AlgorithmState, AlgorithmTimeout, Mode, ResultStatus,
)
from zontcontrol.controller.contracts import ControlKey, ValueDescriptor
from zontcontrol.controller.transport import FailureKind, TransportError


class VirtualHTTP:
    def __init__(self):
        self.raw = inventory_device()
        self.raw['controls']['toggle_buttons'][0]['id'] = SETTINGS['reset_control_id']
        self.raw.update(name='Original device name', notes='Existing user notes')
        self.commands = []
        self.paths = []
        self.closed = False
        self.unknown_on = False
        self.off_entered, self.off_resume = Event(), Event()
        self.off_resume.set()

    def request(self, request, **kwargs):
        if self.closed:
            raise AssertionError('request after transport close')
        path = urlsplit(request.full_url).path
        self.paths.append(path)
        if '/actions/' in path:
            body = json.loads(request.data)
            self.commands.append(body)
            if body.get('target_state') is False:
                self.off_entered.set()
                if not self.off_resume.wait(2):
                    raise AssertionError('cleanup barrier timed out')
            if body.get('target_state') is True and self.unknown_on:
                raise TransportError(FailureKind.UNCONFIRMED)
            return {'ok': True}
        if path == '/api/widget/v3/devices':
            return {'ok': True, 'devices': [deepcopy(self.raw)]}
        if path == '/api/update_device':
            body = json.loads(request.data)
            if set(body) != {'device_id', 'name'} or body['device_id'] != self.raw['id']:
                raise AssertionError('unexpected metadata update')
            self.raw['name'] = body['name']
            return {'ok': True}
        raise AssertionError('unexpected endpoint: '+path)

    def close(self):
        self.closed = True


class DirectProductFlows(unittest.TestCase):
    def setUp(self):
        self.apps, self.transports = [], []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for transport in self.transports:
            transport.off_resume.set()
        for app in self.apps:
            self.assertTrue(app.close(wait_timeout=2))

    def build(self, specs=()):
        data = {'config/controller.json': {'contact': 'offline@example.invalid',
                    'http_timeout_seconds': 5},
                'config/registry.json': {'once_timeouts': {
                    s.algorithm_id: {'minimum_seconds': .01, 'recommended_seconds': .2}
                    for s in specs if Mode.ONCE in s.modes}}}
        transport = VirtualHTTP()
        self.transports.append(transport)
        with patch('socket.socket.connect', side_effect=AssertionError('network forbidden')):
            app = create_application(environ={'ZONT_TOKEN': 'synthetic-offline'},
                catalog=specs, reader=lambda path: data[str(path)],
                transport_factory=lambda **_: transport)
        self.apps.append(app)
        return app, transport

    def test_cli_sends_one_command_without_background_protocol_traffic(self):
        terminal = Terminal()
        data = {'devices': [42], 'controls': [{'device_id': 42, 'kind': 'mode',
                'element_id': 20, 'actions': ['activate']}], 'graphs': []}
        app, transport = self.build()
        cli = BoilerCLI(app.controller(), terminal=terminal, reader=lambda _: data)
        context = type('Context', (), {'cancelled': Event()})()
        cli.handle_line("command 42 mode 20 activate '{}'", context)
        self.assertEqual(transport.commands, [])
        cli.handle_line("command 42 mode 20 activate '{}' --execute", context)
        self.assertEqual(transport.commands, [{}])
        self.assertTrue(all('/api/widget/v3/' in path or path == '/api/update_device'
                            for path in transport.paths))
        self.assertEqual(transport.paths.count('/api/update_device'), 1)
        self.assertTrue(transport.raw['name'].startswith('Original device name [zontcontrol connected '))
        self.assertEqual(transport.raw['notes'], 'Existing user notes')

    def test_independent_applications_can_command_the_same_device(self):
        for _ in range(2):
            app, transport = self.build()
            controller = app.controller()
            command = controller.prepare_command(42, ControlKey(42, 'mode', 20), 'activate', {})
            self.assertTrue(controller.execute_command(command).api_accepted)
            self.assertEqual(transport.commands, [{}])
            self.assertEqual(transport.paths.count('/api/update_device'), 1)

    def reset_spec(self, wait):
        return AlgorithmSpec('boiler_reset', 'Offline reset', frozenset({Mode.ONCE}),
            lambda controller: BoilerReset(controller, reader=lambda _: SETTINGS,
                                           wall_clock=lambda: history().end, wait=wait))

    def history_patches(self):
        rows = tuple(ValueDescriptor(row.key, 'virtual', row.unit, row.source,
            row.key.field, row.value_type, True, True, row.kind) for row in history().series)
        return (patch('zontcontrol.controller.service.ControllerService.describe_values', return_value=rows),
                patch('zontcontrol.controller.service.ControllerService.read_history', return_value=history()))

    def test_unknown_on_still_attempts_off_once(self):
        descriptions, archive = self.history_patches()
        with descriptions, archive:
            app, transport = self.build((self.reset_spec(lambda *_: False),))
            transport.unknown_on = True
            with self.assertRaises(AlgorithmFailed):
                app.run_once('boiler_reset')
        self.assertEqual(transport.commands, [{'target_state': True}, {'target_state': False}])

    def test_once_timeout_and_shutdown_wait_for_finally_off(self):
        pulse = Event()
        def wait(context, seconds):
            pulse.set()
            return context.wait(2)
        descriptions, archive = self.history_patches()
        with descriptions, archive:
            app, transport = self.build((self.reset_spec(wait),))
            transport.off_resume.clear()
            results = []
            def run():
                try:
                    app.run_once('boiler_reset', timeout=.05)
                except Exception as error:
                    results.append(error)
            thread = Thread(target=run)
            thread.start()
            self.addCleanup(lambda: thread.join(2))
            self.assertTrue(pulse.wait(1))
            self.assertTrue(transport.off_entered.wait(1))
            thread.join(1)
            self.assertIsInstance(results[0], AlgorithmTimeout)
            self.assertIs(app.list_algorithms()[0].state, AlgorithmState.STOPPING)
            self.assertFalse(app.close(wait_timeout=0))
            self.assertFalse(transport.closed)
            transport.off_resume.set()
            self.assertTrue(app.close(wait_timeout=2))
            self.assertTrue(transport.closed)
        self.assertEqual(transport.commands, [{'target_state': True}, {'target_state': False}])

    def test_incomplete_history_stays_unknown_and_sends_nothing(self):
        descriptions, archive = self.history_patches()
        incomplete = replace(history(), series=tuple(replace(row, points=()) for row in history().series))
        with descriptions, archive as read:
            read.return_value = incomplete
            app, transport = self.build((self.reset_spec(lambda *_: False),))
            result = app.run_once('boiler_reset')
        self.assertIs(result.status, ResultStatus.UNKNOWN)
        self.assertEqual(transport.commands, [])
