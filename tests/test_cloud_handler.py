"""Cloud composition against synthetic controllers; no ZONT or YC calls."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from zontcontrol import __version__
from zontcontrol.application import create_application
from zontcontrol.autonomy.contracts import Mode, RunContext
from zontcontrol.controller.contracts import (
    CommandOutcomeUnknown, DeviceInfo, DeviceUnavailable, Quality, Snapshot,
    ValueDescriptor, ValueReading, ValueType,
)
from tests.test_boiler_reset import history


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / 'deploy/cloud-config'


class CloudHandlerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        logger = logging.getLogger('zontcontrol')
        logger_state = logger.handlers[:], logger.level, logger.propagate
        def restore_logger():
            for item in logger.handlers[:]:
                if item not in logger_state[0]:
                    logger.removeHandler(item)
                    item.close()
            logger.setLevel(logger_state[1])
            logger.propagate = logger_state[2]
        self.addCleanup(restore_logger)
        self.directory = self.stack.enter_context(TemporaryDirectory())
        self.root = Path(self.directory)
        (self.root / 'config').mkdir()
        for name in ('controller', 'registry', 'boiler_reset'):
            data = json.loads((PROFILE / f'{name}.json').read_text())
            if name == 'controller':
                data['contact'] = 'cloud-test@example.invalid'
            self.write_config(name, data)
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        self.stack.enter_context(patch.dict(os.environ, {'ZONT_TOKEN': 'synthetic-offline'}, clear=True))
        self.stack.enter_context(patch('socket.socket.connect', side_effect=AssertionError('network forbidden')))
        self.output = self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.events = []
        self.transport = Mock()
        self.transport.close.side_effect = lambda: self.events.append('transport_closed')
        self.transport_type = self.stack.enter_context(patch(
            'zontcontrol.application.HttpTransport', return_value=self.transport))
        self.controller = Mock()
        original = history()
        data = replace(original, device_id=700877, series=tuple(
            replace(row, key=replace(row.key, device_id=700877)) for row in original.series))
        self.controller.read_history.return_value = data
        self.controller.describe_values.return_value = tuple(
            ValueDescriptor(row.key, 'synthetic', row.unit, row.source, row.key.field,
                            row.value_type, True, True, row.kind) for row in data.series)
        self.controller.list_devices.return_value = (DeviceInfo(700877, 'synthetic', None, True),)
        reset_key = data.series[-1].key
        self.controller.read_current.return_value = Snapshot(700877, data.end, (
            ValueReading(reset_key, False, ValueType.BOOLEAN, None, 'synthetic', None,
                         data.end, Quality.UNKNOWN),))
        self.controller.prepare_command.side_effect = lambda device, key, action, args: args['target_state']
        self.controller.execute_command.side_effect = self.command
        self.stack.enter_context(patch('zontcontrol.application.ControllerService', return_value=self.controller))
        self.wait = self.stack.enter_context(patch.object(RunContext, 'wait', autospec=True,
            side_effect=lambda context, seconds: self.events.append(('wait', seconds)) or False))

    def write_config(self, name, data):
        (self.root / f'config/{name}.json').write_text(json.dumps(data), encoding='utf-8')

    def command(self, target):
        self.events.append('on' if target else 'off')

    def invoke(self, event=None, context=None):
        from zontcontrol.cloud import handler
        return handler(event, context)

    def log_rows(self, report):
        return [json.loads(line) for line in report['log'].splitlines()]

    def test_profile_has_fixed_bindings_timeouts_and_no_baked_contact(self):
        controller = json.loads((PROFILE / 'controller.json').read_text())
        self.assertEqual(controller, {'contact': '', 'http_timeout_seconds': 15})
        self.assertEqual(json.loads((PROFILE / 'boiler_reset.json').read_text()), {
            'device_id': 700877, 'circuit_id': 20496, 'air_sensor_id': 8207,
            'coolant_sensor_id': 8429, 'reset_control_id': 8832,
            'check_interval_seconds': 60, 'detection_profile': 'smart_2_heating'})
        self.assertEqual(json.loads((PROFILE / 'registry.json').read_text()), {
            'disabled_modules': [], 'autostart_dual_mode': False, 'once_timeouts': {
                'boiler_reset': {'minimum_seconds': 170, 'recommended_seconds': 300}}})

    def test_minimal_states_and_no_event_control(self):
        for state, status in (('normal', 'success'), ('unknown', 'unknown'),
                              ('suppressed', 'suppressed'), ('warning', 'success')):
            self.events.clear()
            with self.subTest(state=state), patch('zontcontrol.algorithms.boiler_reset.assess_history',
                    return_value={'state_at_period_end': state}):
                result = self.invoke({'algorithm_id': 'boiler_cli', 'mode': 'continuous',
                    'device_id': 1, 'execute': False, 'token': 'event-private'})
            self.assertEqual({key: value for key, value in result.items() if key not in ('assessment', 'log')},
                {'version': __version__, 'algorithm_id': 'boiler_reset',
                 'status': status, 'state_at_period_end': state, 'reset_performed': False, 'reason': None})
            self.assertEqual(result['assessment'], {'state_at_period_end': state})
            self.assertTrue(result['log'])
            self.assertEqual(self.events, ['transport_closed'])
            self.assertEqual(self.controller.read_history.call_args.args[0], 700877)
        self.assertNotIn('event-private', self.output.getvalue())

    def test_confirmed_sends_one_real_policy_pulse_then_closes(self):
        result = self.invoke(context=SimpleNamespace(request_id='request-123'))
        self.assertEqual(result['state_at_period_end'], 'confirmed_pattern')
        self.assertTrue(result['reset_performed'])
        self.assertEqual(self.events, ['on', ('wait', 40), 'off', 'transport_closed'])
        logs = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertTrue(any(row.get('request_id') == 'request-123' for row in logs))
        self.assertEqual(sum(row.get('event') == 'once_started' for row in logs), 1)
        self.assertEqual(result['log'], self.output.getvalue())
        self.assertEqual(result['assessment']['device_id'], 700877)
        self.assertIn('requested_period', result['assessment'])
        self.assertIn('events', result['assessment'])

    def test_warm_invocations_do_not_share_logs_and_post_response_does_not_accumulate(self):
        first = self.invoke(context=SimpleNamespace(request_id='first'))
        second = self.invoke(context=SimpleNamespace(request_id='second'))
        self.assertEqual(sum(row.get('event') == 'cloud_invocation_started' for row in self.log_rows(second)), 1)
        self.assertNotIn('first', json.dumps(second))
        snapshot = json.dumps(first)
        logging.getLogger('zontcontrol').info('after response')
        self.assertEqual(json.dumps(first), snapshot)

    def test_new_business_events_and_additional_result_fields_pass_unchanged(self):
        from zontcontrol.cloud import _response
        from zontcontrol.autonomy.contracts import AlgorithmResult, ResultStatus
        assessment = {'state_at_period_end': 'new_state', 'new_metric': 7,
            'events': [{'type': 'future_event', 'custom': {'readings': [20.1, None]}}]}
        result = AlgorithmResult(ResultStatus.SUCCESS,
            {'assessment': assessment, 'reset_performed': False, 'reason': 'new_reason',
             'extra_result': ['new', 'data']})
        report = _response(result)
        self.assertEqual(report['assessment'], assessment)
        self.assertEqual(report['reason'], 'new_reason')
        self.assertEqual(report['extra_result'], ['new', 'data'])

    def test_finished_result_survives_shutdown_failure_in_safe_error_report(self):
        with patch('zontcontrol.application._close_transport', side_effect=RuntimeError('private')):
            error = self.assert_safe_failure()
        report = json.loads(str(error))
        self.assertEqual(report['result']['state_at_period_end'], 'confirmed_pattern')
        self.assertTrue(report['result']['reset_performed'])
        self.assertEqual(json.loads(report['log'].splitlines()[-1])['event'], 'cloud_invocation_failed')
        self.assertIs(sys.stdout, self.output)

    def test_real_recovered_assessment_is_returned_without_failure_or_retry(self):
        from tests.test_monitor import heating_history, typed_point, typed_series
        from zontcontrol.monitor import assess_history
        data = heating_history(coolant=typed_series(*(
            typed_point(i * 60, 35 - .1 * i if i < 40 else 39) for i in range(46))))
        assessment = assess_history(data)
        self.assertEqual([event['type'] for event in assessment['events']],
                         ['confirmed_pattern', 'ended'])
        with patch('zontcontrol.algorithms.boiler_reset.assess_history', return_value=assessment):
            result = self.invoke()
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['assessment'], assessment)
        self.assertEqual(result['assessment']['events'][-1]['reason'], 'heating_response_returned')
        self.assertTrue(result['assessment']['events'][-1]['heat_returned'])
        self.assertEqual(self.events, ['transport_closed'])

    def test_other_logger_messages_appear_in_response_exactly_as_stdout(self):
        from zontcontrol.algorithms.boiler_reset import BoilerReset
        from zontcontrol.autonomy.contracts import AlgorithmResult, ResultStatus
        foreign = logging.getLogger('outside.application')
        previous = foreign.handlers[:], foreign.level, foreign.propagate
        handler = logging.StreamHandler(self.output)
        handler.setFormatter(logging.Formatter('PRIOR %(name)s: %(message)s'))
        foreign.handlers, foreign.level, foreign.propagate = [handler], logging.INFO, False
        self.addCleanup(setattr, foreign, 'handlers', previous[0])
        self.addCleanup(setattr, foreign, 'level', previous[1])
        self.addCleanup(setattr, foreign, 'propagate', previous[2])
        def run_once(module, context):
            foreign.info('a completely new event')
            return AlgorithmResult(ResultStatus.SUCCESS, {'assessment': {
                'state_at_period_end': 'normal'}, 'reset_performed': False, 'reason': None})
        with patch.object(BoilerReset, 'run_once', run_once):
            result = self.invoke()
        self.assertEqual(result['log'], self.output.getvalue())
        self.assertIn('PRIOR outside.application: a completely new event\n', result['log'])
        self.assertIs(handler.stream, self.output)

    def test_existing_root_handler_is_copied_without_duplicate_json_or_reformatting(self):
        from zontcontrol.algorithms.boiler_reset import BoilerReset
        from zontcontrol.autonomy.contracts import AlgorithmResult, ResultStatus
        root = logging.getLogger()
        previous = root.handlers[:], root.level
        handler = logging.StreamHandler(self.output)
        handler.setFormatter(logging.Formatter('ROOT %(message)s'))
        root.handlers, root.level = [handler], logging.INFO
        self.addCleanup(setattr, root, 'handlers', previous[0])
        self.addCleanup(setattr, root, 'level', previous[1])
        def run_once(module, context):
            logging.getLogger('vendor.root').info('ROOT_VISIBLE_RECORD')
            return AlgorithmResult(ResultStatus.SUCCESS, {'assessment': {
                'state_at_period_end': 'normal'}, 'reset_performed': False, 'reason': None})
        with patch.object(BoilerReset, 'run_once', run_once):
            result = self.invoke()
        self.assertEqual(result['log'], self.output.getvalue())
        self.assertEqual(result['log'].count('ROOT_VISIBLE_RECORD'), 1)
        self.assertIn('ROOT ROOT_VISIBLE_RECORD\n', result['log'])
        self.assertIs(handler.stream, self.output)
        self.assertEqual(root.handlers, [handler])
        self.assertEqual(root.level, logging.INFO)

    def test_stdout_stderr_and_handlers_created_during_call_are_restored_on_failure(self):
        errors = io.StringIO()
        foreign = logging.getLogger('created.inside.invocation')
        previous = foreign.handlers[:], foreign.level, foreign.propagate
        self.addCleanup(setattr, foreign, 'handlers', previous[0])
        self.addCleanup(setattr, foreign, 'level', previous[1])
        self.addCleanup(setattr, foreign, 'propagate', previous[2])
        handlers = []
        def run_once(*args, **kwargs):
            print('STDOUT RECORD')
            print('STDERR RECORD', file=sys.stderr)
            handler = logging.StreamHandler()
            handlers.append(handler)
            foreign.handlers, foreign.level, foreign.propagate = [handler], logging.INFO, False
            foreign.info('CREATED HANDLER RECORD')
            raise RuntimeError('private')
        from zontcontrol.algorithms.boiler_reset import BoilerReset
        with redirect_stderr(errors), \
                patch.object(BoilerReset, 'run_once', run_once):
            error = self.assert_safe_failure()
        report = json.loads(str(error))
        for text in ('STDOUT RECORD', 'STDERR RECORD', 'CREATED HANDLER RECORD'):
            self.assertIn(text, report['log'])
        self.assertIs(sys.stdout, self.output)
        self.assertIs(handlers[0].stream, errors)

    def test_already_on_is_explicit_suppressed_and_untouched(self):
        current = self.controller.read_current.return_value
        self.controller.read_current.return_value = replace(current, values=(replace(current.values[0], value=True),))
        result = self.invoke()
        self.assertEqual((result['status'], result['state_at_period_end'], result['reason']),
                         ('suppressed', 'suppressed', 'already_on'))
        self.assertFalse(result['reset_performed'])
        self.assertEqual(self.events, ['transport_closed'])

    def test_unknown_current_reset_stays_unknown_without_commands(self):
        current = self.controller.read_current.return_value
        self.controller.read_current.return_value = replace(
            current, values=(replace(current.values[0], value=None),))
        result = self.invoke()
        self.assertEqual((result['status'], result['state_at_period_end'], result['reason']),
                         ('unknown', 'unknown', 'current_reset_unknown'))
        self.assertFalse(result['reset_performed'])
        self.assertEqual(self.events, ['transport_closed'])
        self.controller.prepare_command.assert_not_called()

    def test_offline_device_does_not_send_commands(self):
        self.controller.list_devices.return_value = (DeviceInfo(700877, 'synthetic', None, False),)
        self.assert_safe_failure()
        self.assertEqual(self.events, ['transport_closed'])
        self.controller.prepare_command.assert_not_called()

    def test_missing_or_invalid_credentials_and_configs_never_make_requests(self):
        for token in ('', ' ', 'private\ninvalid'):
            with self.subTest(token=repr(token)), patch.dict(os.environ, {'ZONT_TOKEN': token}):
                self.assert_safe_failure()
        with patch.dict(os.environ, {}, clear=True):
            self.assert_safe_failure()
        for name in ('controller', 'registry', 'boiler_reset'):
            path = self.root / f'config/{name}.json'
            text = path.read_text()
            path.unlink()
            with self.subTest(missing=name):
                self.assert_safe_failure()
            path.write_text(text, encoding='utf-8')
            self.write_config(name, {'private': 'sensitive-config'})
            with self.subTest(invalid=name):
                self.assert_safe_failure()
            path.write_text(text, encoding='utf-8')
        self.transport.request.assert_not_called()
        self.controller.read_history.assert_not_called()
        self.assertNotIn('sensitive-config', self.output.getvalue())

    def assert_safe_failure(self):
        with self.assertRaises(RuntimeError) as caught:
            self.invoke(context=SimpleNamespace(request_id='invalid\nprivate'))
        report = json.loads(str(caught.exception))
        self.assertEqual(report['status'], 'error')
        self.assertIn(report['error'], ('timeout', 'cancelled', 'configuration_error', 'execution_failed'))
        self.assertEqual(json.loads(report['log'].splitlines()[-1])['event'], 'cloud_invocation_failed')
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        return caught.exception

    def test_on_unknown_off_failure_and_wait_exception_are_never_success(self):
        for failure in ('unknown_on', 'off', 'unknown_off', 'wait'):
            self.events.clear()
            def command(target):
                self.command(target)
                if failure == 'unknown_on' and target:
                    raise CommandOutcomeUnknown()
                if failure == 'off' and not target:
                    raise DeviceUnavailable()
                if failure == 'unknown_off' and not target:
                    raise CommandOutcomeUnknown()
            self.controller.execute_command.side_effect = command
            self.wait.side_effect = (RuntimeError('private-wait-payload') if failure == 'wait'
                                    else lambda context, seconds: False)
            with self.subTest(failure=failure):
                error = self.assert_safe_failure()
            self.assertTrue(any(row.get('event') == 'reset_off_attempt'
                                for row in self.log_rows(json.loads(str(error)))))
            self.assertEqual(self.events, ['on', 'off', 'transport_closed'])
        self.assertNotIn('private-wait-payload', self.output.getvalue())

    def test_composition_and_shutdown_errors_hide_provider_messages(self):
        for operation in ('create', 'close'):
            self.events.clear()
            target = ('zontcontrol.cloud.create_application' if operation == 'create'
                      else 'zontcontrol.application._close_transport')
            with self.subTest(operation=operation), patch(target,
                    side_effect=RuntimeError('private-provider-payload')):
                error = self.assert_safe_failure()
            self.assertNotIn('private-provider-payload', str(error))
            rendered = ''.join(traceback.format_exception(error))
            self.assertNotIn('private-provider-payload', rendered)
            self.assertNotIn('During handling', rendered)
        self.assertNotIn('private-provider-payload', self.output.getvalue())

    def test_future_reason_from_algorithm_does_not_become_platform_failure(self):
        from zontcontrol.algorithms.boiler_reset import BoilerReset
        from zontcontrol.autonomy.contracts import AlgorithmResult, ResultStatus
        result = AlgorithmResult(ResultStatus.UNKNOWN, {'assessment': {
            'state_at_period_end': 'unknown'}, 'reset_performed': False, 'reason': 'future_reason'})
        with patch.object(BoilerReset, 'run_once', return_value=result):
            response = self.invoke()
        self.assertEqual(response['reason'], 'future_reason')

    def test_each_call_has_new_application_only_once_and_explicit_catalog(self):
        apps = []
        def compose(**kwargs):
            spec, = kwargs['catalog']
            self.assertEqual(spec.algorithm_id, 'boiler_reset')
            self.assertEqual(spec.modes, frozenset({Mode.ONCE}))
            app = create_application(**kwargs)
            self.assertEqual(app.list_continuous(), ())
            apps.append(app)
            return app
        with patch('zontcontrol.cloud.create_application', side_effect=compose), \
                patch('zontcontrol.application.Application.run_once', autospec=True,
                      side_effect=lambda app, *args, **kw: app._registry.run_once(*args, **kw)) as once:
            self.invoke()
            self.invoke()
        self.assertIsNot(apps[0], apps[1])
        self.assertEqual(once.call_count, 2)
        for call in once.call_args_list:
            self.assertEqual(call.args[1:], ('boiler_reset',))
            self.assertEqual(call.kwargs, {'timeout': 300})

    def test_timeout_and_cancel_wait_for_blocked_off_before_transport_and_return(self):
        for outcome in ('timeout', 'cancel'):
            entered, release = Event(), Event()
            apps, errors = [], []
            self.events.clear()
            def command(target):
                self.command(target)
                if not target:
                    entered.set()
                    release.wait(3)
            def compose(**kwargs):
                app = create_application(**kwargs)
                apps.append(app)
                return app
            def run():
                try:
                    self.invoke()
                except Exception as error:
                    errors.append(error)
            self.controller.execute_command.side_effect = command
            with self.subTest(outcome=outcome), patch('zontcontrol.cloud.create_application', side_effect=compose):
                worker = Thread(target=run)
                worker.start()
                try:
                    self.assertTrue(entered.wait(2))
                    runtime = apps[-1]._registry._slots['boiler_reset'].runtime
                    if outcome == 'timeout':
                        with runtime._lock:
                            runtime._expire(runtime._active)
                    else:
                        runtime.stop(wait_timeout=0)
                    self.assertTrue(runtime._active.context.cancelled.is_set())
                    self.assertTrue(worker.is_alive())
                    self.assertNotIn('transport_closed', self.events)
                finally:
                    release.set()
                    worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(errors), 1)
                self.assertEqual(json.loads(str(errors[0]))['error'],
                                 outcome if outcome == 'timeout' else 'cancelled')
                events = [row.get('event') for row in self.log_rows(json.loads(str(errors[0])))]
                self.assertLess(events.index('reset_off_api_accepted'), events.index('cloud_invocation_failed'))
                self.assertEqual(self.events[-2:], ['off', 'transport_closed'])

    def test_timeout_and_cancel_interrupt_pulse_wait_then_attempt_off(self):
        for outcome in ('timeout', 'cancel'):
            entered = Event()
            apps, errors = [], []
            self.events.clear()
            def wait(context, seconds):
                self.events.append(('wait', seconds))
                entered.set()
                return context.cancelled.wait(3)
            def compose(**kwargs):
                app = create_application(**kwargs)
                apps.append(app)
                return app
            def run():
                try:
                    self.invoke()
                except Exception as error:
                    errors.append(error)
            self.wait.side_effect = wait
            with self.subTest(outcome=outcome), patch(
                    'zontcontrol.cloud.create_application', side_effect=compose):
                worker = Thread(target=run)
                worker.start()
                try:
                    self.assertTrue(entered.wait(2))
                    runtime = apps[-1]._registry._slots['boiler_reset'].runtime
                    if outcome == 'timeout':
                        with runtime._lock:
                            runtime._expire(runtime._active)
                    else:
                        runtime.stop(wait_timeout=0)
                finally:
                    if apps:
                        apps[-1].stop('boiler_reset', wait_timeout=0)
                    worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(errors), 1)
                category = 'timeout' if outcome == 'timeout' else 'cancelled'
                self.assertEqual(json.loads(str(errors[0]))['error'], category)
                self.assertEqual(self.events, ['on', ('wait', 40), 'off', 'transport_closed'])

    def test_import_is_inert_without_token_config_cli_or_network(self):
        code = '''
import logging
import sys
from unittest.mock import patch
with patch('socket.socket.connect', side_effect=AssertionError('network forbidden')), \\
        patch('pathlib.Path.read_text', side_effect=AssertionError('config read forbidden')):
    import handler
    import zontcontrol.cloud
assert handler.handler is zontcontrol.cloud.handler
assert 'zontcontrol.algorithms.boiler_cli' not in sys.modules
assert 'zontcontrol.algorithms.charts' not in sys.modules
assert not logging.getLogger('zontcontrol').handlers
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=self.root,
            env={'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': str(ROOT)},
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
