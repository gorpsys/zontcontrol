"""Thread lifecycle tests use explicit gates; no ZONT or real control effects."""

from threading import Barrier, Event, Thread, current_thread
from time import monotonic
import unittest
from unittest.mock import Mock
from uuid import UUID

from zontcontrol.autonomy.contracts import (
    AlgorithmBusy, AlgorithmCancelled, AlgorithmFailed, AlgorithmResult,
    AlgorithmSpec, AlgorithmState, AlgorithmTimeout, Mode, ResultStatus,
)
from zontcontrol.autonomy.runtime import AlgorithmRuntime
from zontcontrol.controller.contracts import ControlKey, DeviceControlBusy, ValidatedCommand
from zontcontrol.controller.ownership import CommandExecutor
from zontcontrol.config import OnceTimeout
from tests.fakes.uuid import uuid_generator
from tests.fakes.clock import FakeClock
from tests.test_control_ownership import LocalTestGate


SUCCESS = AlgorithmResult(ResultStatus.SUCCESS, {'value': 1})


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.uuid_generator = uuid_generator()
        self.gates = []

    def tearDown(self):
        for event in self.gates:
            event.set()

    def runtime(self, algorithm=None, *, factory=None, controller_factory=None,
                algorithm_id='test', close_controller=None, clock=monotonic):
        module = algorithm or Mock()
        spec = AlgorithmSpec(algorithm_id, 'Test algorithm', frozenset(Mode),
                             factory or Mock(return_value=module))
        runtime = AlgorithmRuntime(spec, controller_factory or Mock(return_value=object()),
                                   close_controller=close_controller, clock=clock,
                                   uuid_factory=self.uuid_generator)
        self.addCleanup(runtime.close)
        return runtime

    def gate(self):
        event = Event()
        self.gates.append(event)
        return event

    def wait(self, event):
        self.assertTrue(event.wait(2), 'Worker did not reach test gate')

    def test_once_result_and_ordinary_di_run_in_worker(self):
        controller = object()
        threads = []
        contexts = []

        def run(context):
            threads.append(current_thread())
            contexts.append(context)
            return SUCCESS

        module = Mock(run_once=run)
        factory = Mock(side_effect=lambda injected: (threads.append(current_thread()), module)[1])
        runtime = self.runtime(factory=factory, controller_factory=lambda: controller)
        before = monotonic()
        self.assertIs(runtime.run_once(1), SUCCESS)
        self.assertTrue(runtime.wait(2))
        factory.assert_called_once_with(controller)
        self.assertTrue(all(thread is not current_thread() for thread in threads))
        context = contexts[0]
        self.assertEqual(context.algorithm_id, 'test')
        self.assertEqual((context.instance_id, context.run_id), (UUID(int=1), UUID(int=2)))
        self.assertEqual(self.uuid_generator.call_count, 2)
        self.assertLessEqual(before + 1, context.deadline)
        self.assertFalse(context.cancelled.is_set())
        self.assertEqual(runtime.state, AlgorithmState.STOPPED)

    def test_restart_consumes_fresh_injected_instance_and_run_ids(self):
        module = Mock()
        module.run_once.return_value = SUCCESS
        runtime = self.runtime(module)
        for expected in ((1, 2), (3, 4)):
            self.assertIs(runtime.run_once(1), SUCCESS)
            self.assertTrue(runtime.wait(2))
            context = module.run_once.call_args.args[0]
            self.assertEqual((context.instance_id, context.run_id),
                             tuple(UUID(int=value) for value in expected))
        self.assertEqual(self.uuid_generator.call_count, 4)

    def test_exception_and_invalid_result_are_failed_without_raw_error_text(self):
        for effect in (ValueError('private-value'), None):
            with self.subTest(effect=type(effect)):
                module = Mock()
                if effect is None:
                    module.run_once.return_value = 'invalid'
                else:
                    module.run_once.side_effect = effect
                runtime = self.runtime(module)
                with self.assertRaises(AlgorithmFailed) as caught:
                    runtime.run_once(1)
                self.assertNotIn('private-value', str(caught.exception))
                self.assertTrue(runtime.wait(2))
                self.assertEqual(runtime.state, AlgorithmState.FAILED)

    def test_once_timeout_covers_blocked_bootstrap_and_never_runs_it_on_supervisor(self):
        entered, release = Event(), self.gate()
        threads = []
        module = Mock()

        def factory(controller):
            threads.append(current_thread().name)
            entered.set()
            release.wait()
            return module

        runtime = self.runtime(factory=factory)
        handle = runtime.start(Mode.ONCE, timeout=.03)
        self.wait(entered)
        with self.assertRaises(AlgorithmTimeout):
            handle.result(1)
        self.assertTrue(handle.context.cancelled.is_set())
        self.assertEqual(runtime.state, AlgorithmState.STOPPING)
        self.assertIn('worker', threads[0])
        release.set()
        self.assertTrue(runtime.wait(2))
        module.run_once.assert_not_called()

    def test_controller_binding_bootstrap_is_in_once_budget(self):
        entered, release = Event(), self.gate()
        factory = Mock()

        def bind():
            entered.set()
            release.wait()
            return object()

        runtime = self.runtime(factory=factory, controller_factory=bind)
        handle = runtime.start(Mode.ONCE, timeout=.03)
        self.wait(entered)
        with self.assertRaises(AlgorithmTimeout):
            handle.result(1)
        release.set()
        self.assertTrue(runtime.wait(2))
        factory.assert_not_called()

    def test_bootstrap_failure_is_failed_and_other_modules_are_unaffected(self):
        for stage in ('binding', 'factory'):
            with self.subTest(stage=stage):
                failure = Mock(side_effect=ValueError('private-value'))
                runtime = self.runtime(**({'controller_factory': failure} if stage == 'binding'
                                          else {'factory': failure}))
                with self.assertRaises(AlgorithmFailed):
                    runtime.run_once(1)
                self.assertTrue(runtime.wait(2))
                self.assertEqual(runtime.state, AlgorithmState.FAILED)

    def test_timeout_result_is_prompt_during_cleanup_and_late_success_cannot_replace_it(self):
        cleanup, release = Event(), self.gate()

        def run(context):
            try:
                context.wait(60)
            finally:
                cleanup.set()
                release.wait()
            return SUCCESS

        runtime = self.runtime(Mock(run_once=run))
        handle = runtime.start(Mode.ONCE, timeout=.03)
        with self.assertRaises(AlgorithmTimeout) as first:
            handle.result(1)
        self.wait(cleanup)
        self.assertEqual(runtime.state, AlgorithmState.STOPPING)
        with self.assertRaisesRegex(AlgorithmBusy, 'stopping'):
            runtime.start(Mode.ONCE, timeout=1)
        self.assertFalse(runtime.wait(0))
        release.set()
        self.assertTrue(runtime.wait(2))
        with self.assertRaises(AlgorithmTimeout) as late:
            handle.result()
        self.assertIs(first.exception, late.exception)
        self.assertEqual(runtime.state, AlgorithmState.STOPPED)

    def test_cleanup_error_after_timeout_is_observable_without_replacing_timeout(self):
        def run(context):
            context.wait(60)
            raise ValueError('off failed private-value')

        runtime = self.runtime(Mock(run_once=run))
        handle = runtime.start(Mode.ONCE, timeout=.03)
        with self.assertRaises(AlgorithmTimeout):
            handle.result(1)
        self.assertTrue(runtime.wait(2))
        self.assertEqual(runtime.state, AlgorithmState.FAILED)
        self.assertIsInstance(handle.failure, AlgorithmFailed)
        self.assertNotIn('private-value', str(handle.failure))

    def test_stop_interrupts_long_wait_and_preserves_finally_until_exit(self):
        entered, cleanup, release = Event(), Event(), self.gate()

        def run(context):
            try:
                entered.set()
                self.assertTrue(context.wait(60))
            finally:
                cleanup.set()
                release.wait()

        runtime = self.runtime(Mock(run_continuous=run))
        handle = runtime.start(Mode.CONTINUOUS)
        self.wait(entered)
        self.assertFalse(runtime.stop(wait_timeout=0))
        self.wait(cleanup)
        self.assertEqual(runtime.state, AlgorithmState.STOPPING)
        self.assertIsNone(handle.context.deadline)
        with self.assertRaises(AlgorithmBusy):
            runtime.start(Mode.CONTINUOUS)
        release.set()
        self.assertTrue(runtime.wait(2))
        self.assertIsNone(handle.result())

    def test_continuous_has_no_once_budget_or_supervisor_and_can_restart(self):
        entered = Barrier(2)
        contexts = []

        def run(context):
            contexts.append(context)
            entered.wait(2)
            context.wait(60)

        factory = Mock(return_value=Mock(run_continuous=run))
        runtime = self.runtime(factory=factory)
        first = runtime.start(Mode.CONTINUOUS)
        entered.wait(2)
        self.assertIs(runtime.start(Mode.CONTINUOUS), first)
        configured_once = OnceTimeout(.01, .02)
        self.assertFalse(first.context.cancelled.wait(configured_once.resolve() * 3))
        self.assertEqual(runtime.state, AlgorithmState.RUNNING_CONTINUOUS)
        self.assertIsNone(first.context.deadline)
        self.assertIsNone(first.supervisor)
        self.assertTrue(runtime.stop(wait_timeout=2))
        second = runtime.start(Mode.CONTINUOUS)
        entered.wait(2)
        self.assertNotEqual(first.context.run_id, second.context.run_id)
        self.assertNotEqual(first.context.instance_id, second.context.instance_id)
        self.assertTrue(runtime.stop(wait_timeout=2))
        self.assertEqual(factory.call_count, 2)

    def test_simultaneous_once_admission_starts_exactly_one_worker(self):
        barrier = Barrier(3)
        entered, release = Event(), self.gate()
        outcomes = []

        def run(context):
            entered.set()
            release.wait()
            return SUCCESS

        factory = Mock(return_value=Mock(run_once=run))
        runtime = self.runtime(factory=factory)

        def compete():
            barrier.wait(2)
            try:
                outcomes.append(runtime.start(Mode.ONCE, timeout=2))
            except AlgorithmBusy as error:
                outcomes.append(error)

        threads = [Thread(target=compete) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(2)
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.wait(entered)
        self.assertEqual(sum(isinstance(value, AlgorithmBusy) for value in outcomes), 1)
        self.assertEqual(factory.call_count, 1)
        release.set()
        self.assertTrue(runtime.wait(2))

    def test_continuous_failure_does_not_respawn_and_restart_is_explicit(self):
        factory = Mock(return_value=Mock(run_continuous=Mock(side_effect=RuntimeError())))
        runtime = self.runtime(factory=factory)
        first = runtime.start(Mode.CONTINUOUS)
        with self.assertRaises(AlgorithmFailed):
            first.result(1)
        self.assertTrue(runtime.wait(2))
        self.assertEqual(runtime.state, AlgorithmState.FAILED)
        self.assertEqual(factory.call_count, 1)
        second = runtime.start(Mode.CONTINUOUS)
        with self.assertRaises(AlgorithmFailed):
            second.result(1)
        self.assertTrue(runtime.wait(2))
        self.assertEqual(factory.call_count, 2)

    def test_once_during_continuous_is_busy_with_contract_reason(self):
        entered = Event()

        def run(context):
            entered.set()
            context.wait(60)

        runtime = self.runtime(Mock(run_continuous=run))
        runtime.start(Mode.CONTINUOUS)
        self.wait(entered)
        with self.assertRaises(AlgorithmBusy) as caught:
            runtime.run_once(1)
        self.assertEqual(caught.exception.reason, 'already_running_continuous')
        self.assertEqual(caught.exception.algorithm_id, 'test')
        self.assertIn('уже работает в постоянном режиме', str(caught.exception))

    def test_stop_once_returns_cancelled_before_noncooperative_worker_exits(self):
        entered, release = Event(), self.gate()

        def run(context):
            entered.set()
            release.wait()
            return SUCCESS

        runtime = self.runtime(Mock(run_once=run))
        handle = runtime.start(Mode.ONCE, timeout=5)
        self.wait(entered)
        self.assertFalse(runtime.stop(wait_timeout=0))
        with self.assertRaises(AlgorithmCancelled):
            handle.result(1)
        self.assertEqual(runtime.state, AlgorithmState.STOPPING)
        self.assertFalse(handle.worker.daemon)
        self.assertTrue(handle.worker.is_alive())
        release.set()
        self.assertTrue(runtime.wait(2))

    def test_independent_algorithms_make_progress_while_another_is_blocked(self):
        entered, release = Event(), self.gate()

        def run(context):
            entered.set()
            release.wait()
            return SUCCESS

        blocked = self.runtime(Mock(run_once=run))
        handle = blocked.start(Mode.ONCE, timeout=.03)
        self.wait(entered)
        other = self.runtime(Mock(run_once=Mock(return_value=SUCCESS)), algorithm_id='other')
        self.assertIs(other.run_once(1), SUCCESS)
        with self.assertRaises(AlgorithmTimeout):
            handle.result(1)

    def test_close_defers_controller_shutdown_until_finally_and_rejects_new_work(self):
        entered, cleanup, release = Event(), Event(), self.gate()
        controller = Mock()

        def run(context):
            try:
                entered.set()
                context.wait(60)
            finally:
                cleanup.set()
                release.wait()
                controller.off()

        runtime = self.runtime(Mock(run_continuous=run), close_controller=controller.close)
        runtime.start(Mode.CONTINUOUS)
        self.wait(entered)
        self.assertFalse(runtime.close(wait_timeout=0))
        self.wait(cleanup)
        controller.close.assert_not_called()
        with self.assertRaisesRegex(AlgorithmBusy, 'closed'):
            runtime.start(Mode.CONTINUOUS)
        release.set()
        self.assertTrue(runtime.close(wait_timeout=2))
        self.assertEqual(controller.mock_calls, [unittest.mock.call.off(), unittest.mock.call.close()])
        self.assertTrue(runtime.close(wait_timeout=0))
        controller.close.assert_called_once()

    def test_timeout_preserves_real_executor_ownership_and_late_finally_command(self):
        self.check_executor_cleanup(inflight=False)

    def test_command_accepted_before_timeout_finishes_later_and_finally_still_executes(self):
        self.check_executor_cleanup(inflight=True)

    def check_executor_cleanup(self, *, inflight):
        clock = FakeClock(start=100)
        gate = LocalTestGate(clock, uuid_factory=self.uuid_generator)
        executor = CommandExecutor(gate=gate, clock=clock.now, uuid_factory=self.uuid_generator)
        owner, competitor = executor.bind(), executor.bind()
        entered, send_entered, release = Event(), Event(), self.gate()
        effects = []
        command = ValidatedCommand(ControlKey(42, 'toggle_buttons', 31), 'trigger',
                                   (('target_state', False),))
        on = ValidatedCommand(command.control_key, 'trigger', (('target_state', True),))

        def send(prepared):
            # The accepted synchronous request remains in flight past close.
            send_entered.set()
            release.wait()
            effects.append(prepared)

        def run(context):
            session = owner.try_acquire_control(42, lease_seconds=60)
            try:
                entered.set()
                if inflight:
                    owner.execute(on, session, lambda: on, send)
                else:
                    context.wait(60)
            finally:
                owner.execute(command, session, lambda: command,
                              effects.append if inflight else send)
                owner.release_control(session)
            return SUCCESS

        close = Mock()
        runtime = self.runtime(Mock(run_once=run), close_controller=close)
        handle = runtime.start(Mode.ONCE, timeout=.03)
        self.wait(entered)
        with self.assertRaises(AlgorithmTimeout):
            handle.result(1)
        self.wait(send_entered)
        self.assertFalse(runtime.close(wait_timeout=0))
        self.assertEqual(runtime.state, AlgorithmState.STOPPING)
        with self.assertRaises(DeviceControlBusy):
            competitor.try_acquire_control(42, lease_seconds=60)
        close.assert_not_called()
        self.assertEqual(gate.releases, [])
        release.set()
        self.assertTrue(runtime.close(wait_timeout=2))
        self.assertEqual(effects, [on, command] if inflight else [command])
        self.assertEqual(len(gate.releases), 1)
        competitor.try_acquire_control(42, lease_seconds=60)
        close.assert_called_once()

    def test_invalid_mode_and_timeout_fail_before_bootstrap(self):
        bootstrap = Mock()
        runtime = self.runtime(controller_factory=bootstrap)
        for value in (0, -1, True, float('inf'), float('nan'), None, '1'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                runtime.start(Mode.ONCE, timeout=value)
        with self.assertRaises(ValueError):
            runtime.start('once', timeout=1)
        with self.assertRaises(ValueError):
            runtime.start(Mode.CONTINUOUS, timeout=1)
        bootstrap.assert_not_called()

    def test_abandoning_result_wait_does_not_cancel_the_algorithm(self):
        entered, release = Event(), self.gate()

        def run(context):
            entered.set()
            release.wait()
            return SUCCESS

        runtime = self.runtime(Mock(run_once=run))
        handle = runtime.start(Mode.ONCE, timeout=2)
        self.wait(entered)
        with self.assertRaises(TimeoutError):
            handle.result(0)
        self.assertFalse(handle.context.cancelled.is_set())
        self.assertEqual(runtime.state, AlgorithmState.RUNNING_ONCE)
        release.set()
        self.assertIs(handle.result(1), SUCCESS)

    def test_late_worker_cannot_publish_success_even_if_timer_has_not_run_yet(self):
        clock = FakeClock(start=100)
        entered, release = Event(), self.gate()

        def run(context):
            entered.set()
            release.wait()
            return SUCCESS

        runtime = self.runtime(Mock(run_once=run), clock=clock.now)
        handle = runtime.start(Mode.ONCE, timeout=10)
        self.wait(entered)
        clock.advance(11)
        release.set()
        with self.assertRaises(AlgorithmTimeout):
            handle.result(1)
        self.assertTrue(runtime.wait(2))


if __name__ == '__main__':
    unittest.main()
