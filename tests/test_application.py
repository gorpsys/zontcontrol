"""Offline composition and lifecycle; never uses an actual ZONT connection."""

import unittest
from threading import Event
from unittest.mock import Mock, patch

from zontcontrol.application import create_application, create_read_controller
from zontcontrol.autonomy.contracts import AlgorithmResult, AlgorithmSpec, Mode, ResultStatus
from zontcontrol.config import ConfigurationError
from zontcontrol.controller.contracts import ControlOwnershipRequired, ControlKey
from tests.test_inventory import inventory_device
from zontcontrol.autonomy.contracts import AlgorithmState


def settings(disabled=()):
    return {
        'config/controller.json': {'contact': 'owner@example.invalid', 'http_timeout_seconds': 15,
            'coordination': {'delta_seconds': .01, 'leader_period_seconds': 1},
            'statistics': {'path': 'reports/test.jsonl', 'profile': 'offline'}},
        'config/registry.json': {'disabled_modules': list(disabled),
            'once_timeouts': {'sample': {'minimum_seconds': .1, 'recommended_seconds': 1}}},
    }


class ApplicationTests(unittest.TestCase):
    def build(self, *, catalog=(), config=None, **kwargs):
        data = settings() if config is None else config
        if not catalog:
            data['config/registry.json']['once_timeouts'] = {}
        return create_application(environ={'ZONT_TOKEN': 'fake-secret'},
            reader=lambda path: data[str(path)], catalog=catalog,
            statistics_sink=lambda row: None, **kwargs)

    def test_config_errors_before_transport_and_no_token_diagnostics(self):
        with patch('zontcontrol.application.HttpTransport') as transport:
            with self.assertRaises(ConfigurationError):
                create_application(environ={'ZONT_TOKEN': 'fake-secret'}, reader=lambda p: {})
            transport.assert_not_called()
        for value in (None, '', '   '):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'ZONT_TOKEN') as caught:
                create_application(environ={} if value is None else {'ZONT_TOKEN': value})
            self.assertIn('zontcontrol.get_token', str(caught.exception))

    def test_unknown_catalog_id_and_bad_statistics_profile_fail_before_transport(self):
        for kind in ('unknown', 'statistics'):
            data = settings()
            if kind == 'unknown':
                data['config/registry.json']['disabled_modules'] = ['unknown']
            else:
                data['config/registry.json']['once_timeouts'] = {}
                data['config/controller.json']['statistics']['profile'] = 'bad secret profile'
            with self.subTest(kind=kind), patch('zontcontrol.application.HttpTransport') as transport:
                with self.assertRaises(ConfigurationError):
                    create_application(environ={'ZONT_TOKEN': 'fake-secret'}, catalog=(),
                        reader=lambda path: data[str(path)])
                transport.assert_not_called()

    def test_disabled_builtins_read_no_module_files_and_perform_no_requests(self):
        data = settings(disabled=('boiler_cli', 'boiler_reset'))
        data['config/registry.json']['once_timeouts'] = {}
        paths = []
        def read(path):
            paths.append(str(path))
            return data[str(path)]
        with patch('zontcontrol.algorithms.boiler_cli.read_json') as cli_config, \
                patch('zontcontrol.algorithms.boiler_reset.read_json') as reset_config, \
                patch('zontcontrol.application.HttpTransport') as transport:
            with create_application(environ={'ZONT_TOKEN': 'fake-secret'}, reader=read,
                                    statistics_sink=lambda row: None) as app:
                self.assertEqual(app.list_algorithms(), ())
                self.assertEqual(app.run_all_once(), {})
            cli_config.assert_not_called()
            reset_config.assert_not_called()
            transport.return_value.request.assert_not_called()
        self.assertEqual(paths, ['config/registry.json', 'config/controller.json'])

    def test_headless_cli_failure_is_safe_and_never_contacts_zont(self):
        data = settings(disabled=('boiler_reset',))
        data['config/registry.json']['once_timeouts'] = {}
        cli_config = {'devices': [42], 'controls': [], 'graphs': []}
        with patch('zontcontrol.algorithms.boiler_cli.read_json', return_value=cli_config) as config, \
                patch('zontcontrol.algorithms.boiler_cli.Terminal.available', return_value=False), \
                patch('zontcontrol.application.HttpTransport') as transport:
            with create_application(environ={'ZONT_TOKEN': 'fake-secret'},
                    reader=lambda path: data[str(path)], statistics_sink=lambda row: None) as app:
                row, = app.list_algorithms()
                self.assertIs(row.state, AlgorithmState.FAILED)
                self.assertNotIn('fake-secret', str(row.error))
            config.assert_called_once()
            self.assertEqual(str(config.call_args.args[0]), 'config/boiler_cli.json')
            transport.return_value.request.assert_not_called()

    def test_module_config_error_is_local_and_shows_fixed_module_path(self):
        class Module:
            def __init__(self, controller):
                raise ConfigurationError('config/sample.json', 'Missing or invalid configuration')
        spec = AlgorithmSpec('sample', 'Sample', frozenset({Mode.ONCE}), Module)
        with self.build(catalog=(spec,)) as app:
            row, = app.list_algorithms()
            self.assertIs(row.state, AlgorithmState.FAILED)
            self.assertIn('config/sample.json', str(row.error))

    def test_registry_probe_fresh_launch_and_dual_mode_explicit_start(self):
        constructed, contexts = [], []
        continuous_entered = Event()
        class Module:
            def __init__(self, controller):
                constructed.append(controller)
            def run_once(self, context):
                contexts.append(context)
                return AlgorithmResult(ResultStatus.SUCCESS, 'done')
            def run_continuous(self, context):
                contexts.append(context)
                continuous_entered.set()
                context.cancelled.wait()
        spec = AlgorithmSpec('sample', 'Sample', frozenset({Mode.ONCE, Mode.CONTINUOUS}), Module)
        with self.build(catalog=(spec,)) as app:
            self.assertEqual(len(constructed), 1)
            self.assertEqual(contexts, [])
            self.assertEqual(app.list_algorithms()[0].spec.algorithm_id, 'sample')
            self.assertEqual(app.run_once('sample').value, 'done')
            self.assertTrue(app._registry._slots['sample'].runtime.wait(1))
            self.assertEqual(set(app.run_all_once()), {'sample'})
            self.assertTrue(app._registry._slots['sample'].runtime.wait(1))
            handle = app.start('sample')
            self.assertIs(app.start('sample'), handle)
            self.assertTrue(continuous_entered.wait(1))
            self.assertTrue(app.stop('sample', wait_timeout=1))
            self.assertEqual(len(constructed), 4)

    def test_read_root_has_no_registry_or_control_capability(self):
        data = settings()
        with patch('zontcontrol.application.HttpTransport') as transport:
            service = create_read_controller(environ={'ZONT_TOKEN': 'fake-secret'},
                reader=lambda path: data[str(path)])
            self.assertFalse(hasattr(service, 'run_once'))
            with self.assertRaises(ControlOwnershipRequired):
                service.for_consumer()
            transport.return_value.request.assert_not_called()

    def test_failed_bootstrap_closes_owned_transport_before_returning_error(self):
        data = settings()
        data['config/registry.json']['once_timeouts'] = {}
        transport = Mock()
        with patch('zontcontrol.application.BootClock', side_effect=RuntimeError('clock unavailable')):
            with self.assertRaises(RuntimeError):
                create_application(environ={'ZONT_TOKEN': 'fake-secret'}, catalog=(),
                    reader=lambda path: data[str(path)], transport_factory=lambda **kw: transport)
        transport.close.assert_called_once_with()
        transport.request.assert_not_called()

    def test_public_consumer_cannot_send_without_own_session(self):
        with self.build() as app:
            adapter = app._controller._adapter
            with patch.object(adapter, 'devices', return_value=[inventory_device()]), \
                    patch.object(adapter, 'send_command') as send:
                consumer = app.controller()
                command = consumer.prepare_command(42, ControlKey(42, 'circuit', 10),
                                                   'target-temp', {'target_temp': 22})
                with self.assertRaises(ControlOwnershipRequired):
                    consumer.execute_command(command, None)
                send.assert_not_called()

    def test_close_waits_for_worker_finally_before_resources(self):
        entered, finish = Event(), Event()
        events = []
        class Module:
            def __init__(self, controller):
                pass
            def run_continuous(self, context):
                entered.set()
                try:
                    context.cancelled.wait()
                finally:
                    finish.wait()
                    events.append('finally')
        spec = AlgorithmSpec('sample', 'Sample', frozenset({Mode.CONTINUOUS}), Module)
        app = self.build(catalog=(spec,))
        def cleanup():
            finish.set()
            self.assertTrue(app.close(wait_timeout=2))
            runtime = app._registry._slots['sample'].runtime
            self.assertTrue(runtime.wait(1))
        self.addCleanup(cleanup)
        self.assertTrue(entered.wait(1))
        with patch.object(app._coordination, 'close', side_effect=lambda: events.append('resources')):
            self.assertFalse(app.close(wait_timeout=0))
            self.assertEqual(events, [])
            finish.set()
            self.assertTrue(app.close(wait_timeout=1))
        self.assertEqual(events, ['finally', 'resources'])

    def test_host_event_is_an_in_process_wait_and_stop_is_python_api(self):
        cancelled = Event()
        cancelled.set()
        with self.build() as app:
            app.host(cancelled)
            self.assertEqual(app.list_continuous(), ())
