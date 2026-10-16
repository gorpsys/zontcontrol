"""Offline registrar lifecycle and constructor DI contracts."""

from pathlib import Path
from threading import Barrier, Event, Thread
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from zontcontrol.autonomy.catalog import AlgorithmCatalog
from zontcontrol.autonomy.contracts import (
    AlgorithmBusy, AlgorithmFailed, AlgorithmResult, AlgorithmSpec, AlgorithmState,
    AlgorithmTimeout, Mode, ResultStatus,
)
from zontcontrol.autonomy.registry import AlgorithmRegistry
from zontcontrol.config import ConfigurationError, OnceTimeout, RegistryConfig, read_json
from zontcontrol.controller.contracts import ControlKey
from zontcontrol.controller.ownership import CommandExecutor
from zontcontrol.controller.service import ControllerService
from tests.fakes.clock import FakeClock
from tests.test_control_ownership import LocalTestGate
from tests.test_inventory import inventory_device


SUCCESS = AlgorithmResult(ResultStatus.SUCCESS, 'ok')
DUAL = frozenset({Mode.ONCE, Mode.CONTINUOUS})


class Module:
    def __init__(self, controller):
        self.controller = controller
        self.entered = Event()

    def run_once(self, context):
        return SUCCESS

    def run_continuous(self, context):
        self.entered.set()
        context.wait()


def spec(name='reset', modes=DUAL, factory=Module, minimum=0, requirements=frozenset()):
    return AlgorithmSpec(name, 'Test algorithm', modes, factory, requirements, minimum)


class RegistryTests(unittest.TestCase):
    def registry(self, specs, *, disabled=(), autostart=False, pairs=None,
                 bind=None, capabilities=None, close=None, begin_close=None):
        if pairs is None:
            pairs = {s.algorithm_id: OnceTimeout(.01, 1) for s in specs if Mode.ONCE in s.modes}
        config = RegistryConfig(frozenset(disabled), autostart, pairs)
        registry = AlgorithmRegistry(config, bind or (lambda: object()),
                                     begin_close_controller=begin_close,
                                     close_controller=close,
                                     **({} if capabilities is None else
                                        {'available_capabilities': capabilities}))
        self.addCleanup(lambda: registry.close(wait_timeout=2))
        report = registry.register_all(AlgorithmCatalog(specs))
        return registry, {item.spec.algorithm_id: item for item in report}

    def test_explicit_catalog_lists_every_enabled_spec_and_filters_before_factory(self):
        forbidden = Mock(side_effect=AssertionError('disabled config read'))
        registry, report = self.registry([spec(), spec('denied', factory=forbidden)],
                                         disabled=['denied'])
        self.assertEqual(set(report), {'reset', 'denied'})
        self.assertTrue(report['denied'].disabled)
        self.assertEqual([x.spec.algorithm_id for x in registry.list_algorithms()], ['reset'])
        forbidden.assert_not_called()
        with self.assertRaises(KeyError):
            registry.run_once('denied')
        with self.assertRaises(ValueError):
            registry.register_all(AlgorithmCatalog([]))

    def test_factory_owns_config_and_error_does_not_hide_other_registrations(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'config/broken.json'
            broken = Mock(side_effect=lambda controller: read_json(path))
            registry, report = self.registry([spec('broken', factory=broken), spec('healthy')])
            self.assertIsInstance(report['broken'].error, ConfigurationError)
            self.assertIn(str(path), str(report['broken'].error))
            self.assertEqual(report['broken'].state, AlgorithmState.FAILED)
            self.assertIs(registry.run_once('healthy'), SUCCESS)
            self.assertEqual(set(x.spec.algorithm_id for x in registry.list_algorithms()),
                             {'healthy', 'broken'})

    def test_registration_checks_factory_methods_and_capabilities(self):
        unsupported = Mock()
        registry, report = self.registry([
            spec('broken', factory=lambda _: object()),
            spec('missing', factory=unsupported, requirements=frozenset({'read_history'})),
            spec('healthy')], capabilities=frozenset({'read_current'}))
        self.assertIsInstance(report['broken'].error, AlgorithmFailed)
        self.assertEqual(report['missing'].state, AlgorithmState.FAILED)
        unsupported.assert_not_called()
        self.assertIs(registry.run_once('healthy'), SUCCESS)

    def test_autostart_continuous_only_and_dual_explicit_start_busy_and_restart(self):
        made = []

        def factory(controller):
            module = Module(controller)
            made.append(module)
            return module

        registry, _ = self.registry([spec('ui', frozenset({Mode.CONTINUOUS}), factory),
                                     spec(factory=factory), spec('once', frozenset({Mode.ONCE}))])
        self.assertTrue(any(module.entered.wait(.1) for module in made))
        states = {x.spec.algorithm_id: x.state for x in registry.list_algorithms()}
        self.assertEqual(states['reset'], AlgorithmState.REGISTERED)
        self.assertEqual(states['ui'], AlgorithmState.RUNNING_CONTINUOUS)
        handle = registry.start('reset')
        self.assertIs(registry.start('reset'), handle)
        self.assertIsNone(handle.context.deadline)
        with self.assertRaises(AlgorithmBusy) as caught:
            registry.run_once('reset')
        self.assertEqual(caught.exception.reason, 'already_running_continuous')
        self.assertEqual(len(registry.list_algorithms(Mode.ONCE)), 2)
        self.assertEqual(len(registry.list_continuous()), 2)
        with self.assertRaises(ValueError):
            registry.start('once')
        with self.assertRaises(ValueError):
            registry.run_once('ui')
        self.assertTrue(registry.stop('reset', wait_timeout=1))
        again = registry.start('reset')
        self.assertNotEqual(again.context.instance_id, handle.context.instance_id)
        self.assertIsNot(made[-1], made[1])

    def test_config_true_autostarts_dual_mode_without_once_deadline(self):
        registry, _ = self.registry([spec()], autostart=True)
        handle = registry.start('reset')
        self.assertIsNone(handle.context.deadline)
        self.assertEqual(registry.list_algorithms()[0].state, AlgorithmState.RUNNING_CONTINUOUS)

    def test_minimum_and_missing_pairs_fail_before_factory_and_worker(self):
        calls = Mock(return_value=Module(object()))
        registry, report = self.registry([spec('short', factory=calls, minimum=2),
                                         spec('absent', factory=calls), spec('good')],
                                        pairs={'short': OnceTimeout(.01, 1),
                                               'good': OnceTimeout(.01, 1)})
        calls.assert_not_called()
        for name in ('short', 'absent'):
            self.assertIsInstance(report[name].error, ConfigurationError)
            with self.assertRaises(ConfigurationError):
                registry.run_once(name)
        with self.assertRaises(ConfigurationError):
            registry.run_once('good', timeout=.001)
        self.assertIs(registry.run_once('good'), SUCCESS)

    def test_run_all_once_starts_all_before_collecting_independent_outcomes(self):
        entered, release = Event(), Event()
        self.addCleanup(release.set)

        class Blocked(Module):
            def run_once(self, context):
                entered.set()
                release.wait()
                return SUCCESS

        class Good(Module):
            def run_once(self, context):
                release.set()
                return SUCCESS

        registry, _ = self.registry([spec('blocked', factory=Blocked), spec('good', factory=Good),
                                     spec('busy'), spec('timeout', factory=Blocked)])
        registry.start('busy')
        # A separate permanently blocked worker demonstrates Timeout independently.
        forever = Event()
        self.addCleanup(forever.set)
        registry2, _ = self.registry([spec('timeout', factory=lambda c: Mock(
            run_once=lambda ctx: (forever.wait(), SUCCESS)[1]))])
        self.addCleanup(forever.set)
        result = registry.run_all_once()
        self.assertTrue(entered.is_set())
        self.assertIs(result['good'], SUCCESS)
        self.assertIs(result['blocked'], SUCCESS)
        self.assertIsInstance(result['busy'], AlgorithmBusy)
        self.assertIsInstance(registry2.run_all_once({'timeout': .02})['timeout'], AlgorithmTimeout)
        with self.assertRaises(KeyError):
            registry.run_all_once({'typo': 1})

    def test_failed_start_is_visible_and_explicit_restart_uses_new_instance(self):
        class Broken(Module):
            def run_continuous(self, context):
                raise RuntimeError('private-value')

        restarted = Module(object())
        factory = Mock(side_effect=[Module(object()), Broken(object()), restarted])
        registry, _ = self.registry([spec(factory=factory)])
        handle = registry.start('reset')
        with self.assertRaises(AlgorithmFailed):
            handle.result(1)
        handle.worker.join(1)
        self.assertEqual(registry.list_algorithms()[0].state, AlgorithmState.FAILED)
        registry.start('reset')
        self.assertTrue(restarted.entered.wait(1))
        self.assertEqual(factory.call_count, 3)

    def test_stop_and_close_keep_stopping_until_all_finally_before_shared_close(self):
        entered, cleanup, release = Event(), Event(), Event()
        effects = []
        self.addCleanup(release.set)

        class Slow(Module):
            def run_continuous(self, context):
                try:
                    entered.set()
                    context.wait()
                finally:
                    cleanup.set()
                    release.wait()
                    effects.append('off')

        registry, _ = self.registry([spec(factory=Slow), spec('other')],
                                    close=lambda: effects.append('closed'))
        registry.start('reset')
        self.assertTrue(entered.wait(1))
        self.assertFalse(registry.stop('reset'))
        self.assertTrue(cleanup.wait(1))
        self.assertEqual(registry.list_algorithms()[0].state, AlgorithmState.STOPPING)
        with self.assertRaises(AlgorithmBusy) as caught:
            registry.start('reset')
        self.assertEqual(caught.exception.reason, 'stopping')
        self.assertFalse(registry.close(wait_timeout=0))
        self.assertEqual(effects, [])
        with self.assertRaises(AlgorithmBusy):
            registry.run_once('other')
        release.set()
        self.assertTrue(registry.close(wait_timeout=1))
        self.assertTrue(registry.close(wait_timeout=0))
        self.assertEqual(effects, ['off', 'closed'])

    def test_constructor_di_is_per_instance_without_registry_or_element_id_dispatch(self):
        controllers = [object(), object(), object(), object()]
        bind = Mock(side_effect=controllers)
        made = []

        def factory(controller):
            module = Module(controller)
            made.append(module)
            return module

        registry, _ = self.registry([spec('device_one', factory=factory),
                                     spec('device_two', factory=factory)], bind=bind)
        self.assertIs(registry.run_once('device_one'), SUCCESS)
        self.assertIs(registry.run_once('device_two'), SUCCESS)
        self.assertEqual([x.controller for x in made], controllers)
        self.assertEqual(bind.call_args_list, [unittest.mock.call()] * 4)
        self.assertFalse(hasattr(made[0], 'registry'))

    def test_registration_constructs_without_running_and_launch_bootstrap_is_timed(self):
        for blocked_step in ('di', 'factory'):
            with self.subTest(blocked_step=blocked_step):
                entered, release = Event(), Event()
                effects = Mock()
                module = Mock(run_once=Mock(return_value=SUCCESS),
                              run_continuous=effects)
                count = {'di': 0, 'factory': 0}

                def bootstrap(step):
                    count[step] += 1
                    if step == blocked_step and count[step] == 2:
                        entered.set()
                        release.wait()

                def bind():
                    bootstrap('di')
                    return object()

                def factory(controller):
                    bootstrap('factory')
                    return module

                registry, report = self.registry([spec(factory=factory)], bind=bind)
                self.addCleanup(release.set)
                self.assertEqual(report['reset'].state, AlgorithmState.REGISTERED)
                effects.assert_not_called()
                module.run_once.assert_not_called()
                with self.assertRaises(AlgorithmTimeout):
                    registry.run_once('reset', timeout=.02)
                self.assertTrue(entered.is_set())
                self.assertEqual(registry.list_algorithms()[0].state, AlgorithmState.STOPPING)
                module.run_once.assert_not_called()
                release.set()
                self.assertTrue(registry.stop('reset', wait_timeout=1))

    def test_failed_factory_isolated_and_explicit_launch_retries_construction(self):
        factory = Mock(side_effect=[ValueError('private-value'), Module(object())])
        registry, report = self.registry([spec('bad', factory=factory), spec('good')])
        self.assertIsInstance(report['bad'].error, AlgorithmFailed)
        self.assertNotIn('private-value', str(report['bad'].error))
        self.assertIs(registry.run_all_once()['bad'], SUCCESS)
        self.assertEqual(factory.call_count, 2)

    def test_same_element_id_on_different_devices_uses_independent_consumer_bindings(self):
        clock = FakeClock(start=100)
        gate = LocalTestGate(clock)
        executor = CommandExecutor(gate=gate, clock=clock.now)
        adapter = Mock()
        adapter.devices.side_effect = lambda device_id=None: [
            {**inventory_device(), 'id': device_id}]
        service = ControllerService(adapter, executor=executor, clock=clock.wall_now)
        both = Barrier(2)

        class Reader(Module):
            def __init__(self, controller, device_id):
                super().__init__(controller)
                self.device_id = device_id

            def run_once(self, context):
                session = self.controller.try_acquire_control(self.device_id, lease_seconds=60)
                try:
                    command = self.controller.prepare_command(
                        self.device_id, ControlKey(self.device_id, 'toggle_buttons', 31),
                        'trigger', {'target_state': True})
                    both.wait(1)
                    return AlgorithmResult(ResultStatus.SUCCESS, command.control_key)
                finally:
                    self.controller.release_control(session)

        registry, _ = self.registry([
            spec('one', factory=lambda c: Reader(c, 42)),
            spec('two', factory=lambda c: Reader(c, 43))], bind=service.for_consumer)
        self.assertEqual(gate.acquisitions, 0)
        adapter.assert_not_called()
        adapter.devices.assert_not_called()
        result = registry.run_all_once()
        self.assertEqual(result['one'].value, ControlKey(42, 'toggle_buttons', 31))
        self.assertEqual(result['two'].value, ControlKey(43, 'toggle_buttons', 31))
        self.assertEqual(gate.acquisitions, 2)

    def test_registry_configuration_is_snapshot_not_mutable_service_locator(self):
        pairs = {'reset': OnceTimeout(.01, 1)}
        config = RegistryConfig(frozenset(), False, pairs)
        pairs.clear()
        self.assertIn('reset', config.once_timeouts)
        with self.assertRaises(TypeError):
            config.once_timeouts['other'] = OnceTimeout(.01, 1)

    def test_close_signals_every_worker_before_waiting_and_closes_after_all_cleanup(self):
        release, first_cleanup, second_cleanup = Event(), Event(), Event()
        effects = []

        class Slow(Module):
            def __init__(self, controller, cleanup):
                super().__init__(controller)
                self.cleanup = cleanup

            def run_continuous(self, context):
                try:
                    self.entered.set()
                    context.wait()
                finally:
                    self.cleanup.set()
                    release.wait()
                    effects.append(self.cleanup)

        registry, _ = self.registry([
            spec('one', factory=lambda c: Slow(c, first_cleanup)),
            spec('two', factory=lambda c: Slow(c, second_cleanup))],
            close=lambda: effects.append('closed'))
        self.addCleanup(release.set)
        handles = [registry.start(name) for name in ('one', 'two')]
        self.assertFalse(registry.close(wait_timeout=0))
        self.assertTrue(all(h.context.cancelled.is_set() for h in handles))
        self.assertTrue(first_cleanup.wait(1))
        self.assertTrue(second_cleanup.wait(1))
        self.assertEqual(effects, [])
        release.set()
        self.assertTrue(registry.close(wait_timeout=1))
        self.assertEqual(set(effects[:2]), {first_cleanup, second_cleanup})
        self.assertEqual(effects[-1], 'closed')

    def test_registration_rejects_global_catalog_errors_before_any_construction(self):
        factory = Mock()
        for specs, config in (
                ([spec(factory=factory), spec(factory=factory)], RegistryConfig(frozenset(), False, {})),
                ([spec(factory=factory)], RegistryConfig(frozenset({'typo'}), False, {}))):
            registry = AlgorithmRegistry(config, Mock())
            with self.assertRaises(ValueError):
                registry.register_all(specs)
            self.assertTrue(registry.close())
        factory.assert_not_called()

    def test_list_while_registration_waits_and_no_launch_before_probe_finishes(self):
        entered, release = Event(), Event()

        def factory(controller):
            entered.set()
            release.wait()
            return Module(controller)

        registry = AlgorithmRegistry(RegistryConfig(frozenset(), False,
                                                   {'reset': OnceTimeout(.01, 1)}), object)
        self.addCleanup(registry.close)
        self.addCleanup(release.set)
        thread = Thread(target=lambda: registry.register_all([spec(factory=factory)]))
        thread.start()
        self.addCleanup(lambda: thread.join(1))
        self.assertTrue(entered.wait(1))
        self.assertEqual(registry.list_algorithms()[0].state, AlgorithmState.REGISTERED)
        with self.assertRaises(AlgorithmBusy) as caught:
            registry.start('reset')
        self.assertEqual(caught.exception.reason, 'registering')
        release.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())

    def test_run_all_invalid_timeout_is_local_and_unknown_result_is_unchanged(self):
        unknown = AlgorithmResult(ResultStatus.UNKNOWN, {'reason': 'missing history'})
        registry, _ = self.registry([
            spec('short'), spec('unknown', factory=lambda _: Mock(run_once=lambda ctx: unknown)),
            spec('healthy')])
        outcomes = registry.run_all_once({'short': .001})
        self.assertIsInstance(outcomes['short'], ConfigurationError)
        self.assertIs(outcomes['unknown'], unknown)
        self.assertIs(outcomes['healthy'], SUCCESS)

    def test_shared_close_failure_is_safe_and_not_retried(self):
        close = Mock(side_effect=RuntimeError('private-value'))
        registry = AlgorithmRegistry(RegistryConfig(frozenset(), False, {}), object,
                                     close_controller=close)
        registry.register_all(AlgorithmCatalog())
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, '^Shared controller shutdown failed$'):
                registry.close()
        close.assert_called_once_with()

    def test_early_close_hook_precedes_cancellation_and_all_finally(self):
        effects, handles = [], []
        entered = Barrier(3)
        finish = Event()

        class Worker(Module):
            def run_continuous(self, context):
                try:
                    entered.wait(1)
                    context.wait()
                finally:
                    finish.wait()
                    effects.append('finally')

        def begin_close():
            self.assertTrue(all(not h.context.cancelled.is_set() for h in handles))
            effects.append('admission')

        registry, _ = self.registry([spec('one', factory=Worker), spec('two', factory=Worker)],
                                    begin_close=begin_close,
                                    close=lambda: effects.append('resources'))
        self.addCleanup(finish.set)
        handles.extend(registry.start(name) for name in ('one', 'two'))
        entered.wait(1)
        self.assertFalse(registry.close(wait_timeout=0))
        self.assertTrue(all(h.context.cancelled.is_set() for h in handles))
        self.assertEqual(effects, ['admission'])
        with self.assertRaises(AlgorithmBusy):
            registry.start('one')
        finish.set()
        self.assertTrue(registry.close(wait_timeout=1))
        self.assertTrue(registry.close(wait_timeout=0))
        self.assertEqual(effects, ['admission', 'finally', 'finally', 'resources'])

    def test_invalid_close_timeout_does_not_begin_shutdown(self):
        begin, close = Mock(), Mock()
        registry, _ = self.registry([spec()], begin_close=begin, close=close)
        for timeout in (-1, True, '1', float('inf'), float('nan')):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                registry.close(wait_timeout=timeout)
        begin.assert_not_called()
        close.assert_not_called()
        self.assertFalse(registry._closed)
        self.assertFalse(registry._close_started)
        self.assertIs(registry.run_once('reset'), SUCCESS)
        self.assertTrue(registry.close())
        begin.assert_called_once_with()
        close.assert_called_once_with()

    def test_concurrent_close_uses_one_hook_and_one_shutdown_worker(self):
        entered, finish = Event(), Event()
        begin = Mock()
        def close():
            entered.set()
            finish.wait()
        final = Mock(side_effect=close)
        registry, _ = self.registry([], begin_close=begin, close=final)
        self.addCleanup(finish.set)
        results = []
        thread = Thread(target=lambda: results.append(registry.close()))
        thread.start()
        self.addCleanup(lambda: thread.join(1))
        self.assertTrue(entered.wait(1))
        shutdown = registry._shutdown
        self.assertFalse(registry.close(wait_timeout=0))
        self.assertIs(registry._shutdown, shutdown)
        finish.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [True])
        self.assertTrue(registry.close(wait_timeout=0))
        begin.assert_called_once_with()
        final.assert_called_once_with()

    def test_early_hook_failure_is_terminal_for_concurrent_and_repeated_close(self):
        hook_entered, hook_finish = Event(), Event()
        worker_entered, worker_finish = Event(), Event()
        errors = []

        class Worker(Module):
            def run_continuous(self, context):
                worker_entered.set()
                try:
                    context.wait()
                finally:
                    worker_finish.wait()

        def fail():
            hook_entered.set()
            hook_finish.wait()
            raise RuntimeError('private-value')

        begin, final = Mock(side_effect=fail), Mock()
        registry = AlgorithmRegistry(RegistryConfig(frozenset(), False,
            {'reset': OnceTimeout(.01, 1)}), object,
            begin_close_controller=begin, close_controller=final)
        registry.register_all([spec(factory=Worker)])
        handle = registry.start('reset')
        self.addCleanup(worker_finish.set)
        self.addCleanup(hook_finish.set)
        self.assertTrue(worker_entered.wait(1))

        def close(wait_timeout=0):
            try:
                registry.close(wait_timeout=wait_timeout)
                errors.append('unexpected return')
            except RuntimeError as error:
                errors.append((str(error), error.__suppress_context__,
                               handle.context.cancelled.is_set()))

        threads = [Thread(target=close) for _ in range(2)]
        threads[0].start()
        self.assertTrue(hook_entered.wait(1))
        threads[1].start()
        hook_finish.set()
        for thread in threads:
            thread.join(1)
            self.assertFalse(thread.is_alive())
        close()
        close(None)
        self.assertEqual(errors, [('Shared controller shutdown failed', True, True)] * 4)
        self.assertTrue(registry._shutdown_done.is_set())
        self.assertTrue(registry._close_started)
        self.assertIsNone(registry._shutdown)
        begin.assert_called_once_with()
        final.assert_not_called()
        with self.assertRaises(AlgorithmBusy):
            registry.start('reset')
        worker_finish.set()
        self.assertTrue(registry.stop('reset', wait_timeout=1))

    def test_hook_failure_during_registration_never_autostarts_late_runtime(self):
        entered, resume = Event(), Event()
        ran, close = Mock(), Mock()
        def factory(controller):
            entered.set()
            resume.wait()
            return Mock(run_continuous=ran)
        begin = Mock(side_effect=RuntimeError('private-value'))
        registry = AlgorithmRegistry(RegistryConfig(frozenset(), True, {}), object,
            begin_close_controller=begin, close_controller=close)
        self.addCleanup(resume.set)
        thread = Thread(target=lambda: registry.register_all([
            spec(modes=frozenset({Mode.CONTINUOUS}), factory=factory),
            spec('late', modes=frozenset({Mode.CONTINUOUS}),
                 factory=lambda controller: Mock(run_continuous=ran))]))
        thread.start()
        self.assertTrue(entered.wait(1))
        with self.assertRaisesRegex(RuntimeError, '^Shared controller shutdown failed$'):
            registry.close(wait_timeout=0)
        self.assertTrue(registry._shutdown_done.is_set())
        resume.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        ran.assert_not_called()
        close.assert_not_called()
        begin.assert_called_once_with()

    def test_early_close_hook_dependency_must_be_callable(self):
        with self.assertRaises(ValueError):
            AlgorithmRegistry(RegistryConfig(frozenset(), False, {}), object,
                              begin_close_controller=object())


if __name__ == '__main__':
    unittest.main()
