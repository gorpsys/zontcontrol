"""Offline request lifecycle, identity, deadlines and ownership race tests."""

import gc
import threading
import unittest
import weakref
from unittest.mock import patch
from uuid import uuid4

from tests.fakes.clock import FakeClock
from zontcontrol.controller.contracts import (
    AuthenticationError, CommandOutcomeUnknown, ControlOwnershipRequired, ControlRequest,
    ControlRequestErrorCode as Code, ControlRequestReason as Reason,
    ControlRequestState as State, ControlSession, DeviceControlBusy, DeviceUnavailable,
    Controller, InvalidArguments, RateLimited,
)
from zontcontrol.controller.ownership import CommandExecutor
from zontcontrol.controller.service import ControllerService


class RequestGate:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.releases = []
        self.abandoned = []
        self.action = None
        self.context = None
        self.release_action = None
        self.abandon_action = None

    def try_acquire(self, owner, device_id, *, lease_seconds, **kwargs):
        self.calls.append((owner, device_id, lease_seconds, kwargs))
        if self.action:
            self.action(device_id)
        deadline = min(self.clock.now() + lease_seconds,
                       kwargs.get('local_deadline', float('inf')))
        return ControlSession(device_id, uuid4(), deadline)

    def observation_context(self, owner, session):
        if self.context:
            self.context(session)
        return {}

    def release(self, owner, session):
        self.releases.append(session)
        if self.release_action:
            self.release_action(session)

    def abandon_request(self, owner, device_id, request_id):
        self.abandoned.append((owner, device_id, request_id))
        if self.abandon_action:
            self.abandon_action(device_id)

    def validate(self, owner, session):
        pass


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(start=100)
        self.gate = RequestGate(self.clock)
        self.executor = CommandExecutor(gate=self.gate, clock=self.clock.now)
        self.manager = self.executor._requests
        self.service = ControllerService(None, executor=self.executor)
        self.consumer = self.service.for_consumer()
        self.binding = self.consumer._control
        self.other = self.executor.bind()
        self.unblock = threading.Event()
        self.threads = []
        self.errors = []

    def tearDown(self):
        self.unblock.set()
        self.executor.begin_close()
        for thread in self.threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.join_workers()
        self.clock.advance(1000)
        self.executor.wait_idle()
        self.assertEqual(self.errors, [])

    def start(self, action):
        def run():
            try:
                action()
            except BaseException as error:
                self.errors.append(error)
        thread = threading.Thread(target=run)
        self.threads.append(thread)
        thread.start()
        return thread

    def block(self, entered):
        entered.set()
        if not self.unblock.wait(3):
            raise AssertionError('test barrier timed out')

    def request(self, binding=None, device=42, **kwargs):
        return self.manager.request(binding or self.binding, device,
                                    **({'lease_seconds': 60, 'timeout': 30,
                                        'poll_seconds': 1} | kwargs))

    def result(self, handle, binding=None):
        return self.manager.get(binding or self.binding, handle)

    def terminal(self, handle, binding=None):
        with self.executor._idle:
            self.assertTrue(self.executor._idle.wait_for(
                lambda: self.manager._lookup_locked(binding or self.binding, handle).state
                is not State.REQUESTED, timeout=2))
        return self.result(handle, binding)

    def advance(self, seconds):
        self.clock.advance(seconds)
        with self.executor._idle:
            self.executor._idle.notify_all()

    def join_workers(self):
        with self.executor._idle:
            self.assertTrue(self.executor._idle.wait_for(
                lambda: not self.manager._workers, timeout=3))
            workers = list(self.manager._threads)
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())

    def test_public_async_identity_and_local_status_cancel(self):
        entered = threading.Event()
        self.gate.action = lambda device: self.block(entered)
        consumer = self.consumer
        self.assertIsInstance(consumer, Controller)
        handle = consumer.request_control(42, lease_seconds=60, timeout=30)
        self.assertIs(handle.initial_state, State.REQUESTED)
        self.assertTrue(entered.wait(1))
        self.assertIs(consumer.request_control(42, lease_seconds=60, timeout=30), handle)
        self.assertIs(consumer.get_control_request(handle).state, State.REQUESTED)
        foreign = self.service.for_consumer()
        other_executor = CommandExecutor(gate=self.gate, clock=self.clock.now)
        other = ControllerService(None, executor=other_executor).for_consumer()
        forged = ControlRequest(handle.request_id, handle.device_id)
        for owner, request in ((consumer, forged), (foreign, handle), (other, handle),
                               (consumer, None), (consumer, handle.request_id)):
            for method in ('get_control_request', 'cancel_control_request'):
                with self.subTest(method=method), self.assertRaises(InvalidArguments):
                    getattr(owner, method)(request)
        self.assertEqual(len(self.gate.calls), 1)
        denied = consumer.cancel_control_request(handle)
        self.assertIs(denied.reason, Reason.CANCELLED)
        self.assertIs(consumer.get_control_request(handle), denied)
        self.assertIs(consumer.cancel_control_request(handle), denied)
        self.assertEqual(len(self.gate.calls), 1)

    def test_public_sync_grants_reuses_without_renew_and_returns_error_result(self):
        result = self.consumer.acquire_control(42, lease_seconds=60, timeout=30)
        self.assertIs(result.state, State.GRANTED)
        self.assertIs(self.consumer.try_acquire_control(42, lease_seconds=1), result.session)
        reused = self.consumer.acquire_control(42, lease_seconds=60, timeout=30)
        self.assertIs(reused.state, State.GRANTED)
        self.assertIs(reused.session, result.session)
        self.assertEqual(len(self.gate.calls), 1)
        self.consumer.release_control(result.session)
        self.gate.action = lambda device: (_ for _ in ()).throw(AuthenticationError())
        denied = self.consumer.acquire_control(42, lease_seconds=60, timeout=30)
        self.assertIs(denied.state, State.DENIED)
        self.assertIs(denied.reason, Reason.CONTROLLER_ERROR)
        self.assertIs(denied.error_code, Code.AUTHENTICATION_ERROR)
        self.assertIsInstance(denied.make_error(), AuthenticationError)

    def assert_forged_subclass_rejected(self, handle):
        class ForgedRequest(ControlRequest):
            def __hash__(self):
                return hash(handle)

            def __eq__(self, other):
                return True

        forged = ForgedRequest(handle.request_id, handle.device_id)
        before = len(self.gate.calls)
        for method in ('get_control_request', 'cancel_control_request'):
            with self.subTest(method=method), self.assertRaises(InvalidArguments):
                getattr(self.consumer, method)(forged)
        self.assertEqual(len(self.gate.calls), before)

    def test_forged_subclass_cannot_read_or_cancel_active_request(self):
        entered = threading.Event()
        self.gate.action = lambda device: self.block(entered)
        handle = self.consumer.request_control(42, lease_seconds=60, timeout=30)
        self.assertTrue(entered.wait(1))
        self.assert_forged_subclass_rejected(handle)
        self.assertIs(self.consumer.get_control_request(handle).state, State.REQUESTED)

    def test_forged_subclass_cannot_read_terminal_grant(self):
        handle = self.consumer.request_control(42, lease_seconds=60, timeout=30)
        granted = self.terminal(handle)
        self.join_workers()
        self.assertIs(granted.state, State.GRANTED)
        self.assert_forged_subclass_rejected(handle)
        self.assertIs(self.consumer.get_control_request(handle), granted)
        self.consumer.release_control(granted.session)

    def test_public_sync_retries_busy_without_returning_requested_or_busy(self):
        entered, done = threading.Event(), threading.Event()
        results = []
        def busy_once(device):
            if len(self.gate.calls) == 1:
                entered.set()
                raise DeviceControlBusy()
        self.gate.action = busy_once
        def acquire():
            results.append(self.consumer.acquire_control(42, lease_seconds=60, timeout=30))
            done.set()
        self.start(acquire)
        self.assertTrue(entered.wait(1))
        self.assertFalse(done.is_set())
        with self.executor._idle:
            self.assertTrue(self.executor._idle.wait_for(
                lambda: next(iter(self.manager._active.values())).next_poll > self.clock.now(),
                timeout=1))
        self.advance(1)
        self.assertTrue(done.wait(1))
        self.assertIs(results[0].state, State.GRANTED)
        self.assertEqual(len(self.gate.calls), 2)
        self.consumer.release_control(results[0].session)

    def test_public_sync_denies_during_blocked_http_without_status_polling(self):
        for cause in ('timeout', 'lease', 'event', 'explicit'):
            with self.subTest(cause=cause):
                self.unblock.clear()
                entered, done = threading.Event(), threading.Event()
                cancelled = threading.Event()
                self.gate.action = lambda device: self.block(entered)
                timeout = 60 if cause == 'lease' else 30
                handle = self.consumer.request_control(42, lease_seconds=60, timeout=timeout,
                                                       cancelled=cancelled)
                self.assertTrue(entered.wait(1))
                results = []
                def acquire():
                    results.append(self.consumer.acquire_control(
                        42, lease_seconds=60, timeout=timeout, cancelled=cancelled))
                    done.set()
                # Keep the duplicate request inside the original deadline.
                enrolled = threading.Event()
                original_request = self.manager.request
                def duplicate(*args, **kwargs):
                    request = original_request(*args, **kwargs)
                    self.assertIs(request, handle)
                    enrolled.set()
                    return request
                with patch.object(self.manager, 'request', side_effect=duplicate):
                    self.start(acquire)
                    self.assertTrue(enrolled.wait(1))
                    if cause == 'explicit':
                        expected = self.consumer.cancel_control_request(handle)
                    elif cause == 'event':
                        cancelled.set()
                    else:
                        # No notify/get: the sync waiter must check time itself.
                        self.clock.advance(timeout)
                    self.assertTrue(done.wait(1), 'sync waited for the blocked HTTP')
                result, = results
                self.assertIs(result.state, State.DENIED)
                reason = (Reason.LEASE_EXPIRED if cause == 'lease' else
                          Reason.TIMEOUT if cause == 'timeout' else Reason.CANCELLED)
                self.assertIs(result.reason, reason)
                self.assertIsNone(result.session)
                if cause == 'explicit':
                    self.assertIs(result, expected)
                self.assertIs(self.consumer.get_control_request(handle), result)
                self.assertFalse(self.unblock.is_set())
                self.unblock.set()
                self.join_workers()
                self.assertIs(self.consumer.get_control_request(handle), result)
                self.assertIs(self.consumer.cancel_control_request(handle), result)
                self.assertEqual(len(self.gate.releases), 1 + ('timeout', 'lease', 'event',
                                                              'explicit').index(cause))

    def test_public_request_invalid_and_closed_create_no_worker_or_gate_io(self):
        invalid = [dict(device_id=value) for value in (None, True, 0, -1, 1.5, '42')]
        for name in ('lease_seconds', 'timeout', 'poll_seconds'):
            invalid += [dict(**{name: value}) for value in
                        (None, True, 0, -1, float('inf'), float('nan'), 10 ** 1000, '1')]
        invalid += [dict(timeout=61), dict(cancelled=object()), dict(cancelled=True)]
        with patch('zontcontrol.controller.requests.Thread.start') as start:
            for method in (self.consumer.request_control, self.consumer.acquire_control):
                for override in invalid:
                    arguments = dict(device_id=42, lease_seconds=60, timeout=30) | override
                    with self.subTest(method=method.__name__, fields=tuple(override)), \
                            self.assertRaises(InvalidArguments):
                        method(**arguments)
            self.executor.begin_close()
            for method in (self.consumer.request_control, self.consumer.acquire_control):
                with self.assertRaises(ControlOwnershipRequired):
                    method(42, lease_seconds=60, timeout=30)
            start.assert_not_called()
        self.assertEqual(self.gate.calls, [])
        self.assertEqual(self.gate.abandoned, [])
        self.assertEqual(self.manager._active, {})
        self.assertEqual(self.manager._queues, {})
        self.assertEqual(self.manager._workers, {})

    def test_creation_is_local_duplicate_identity_and_status_has_no_io(self):
        entered = threading.Event()
        self.gate.action = lambda device: self.block(entered)
        handle = self.request()
        self.assertEqual(handle.initial_state, State.REQUESTED)
        self.assertTrue(entered.wait(1))
        self.assertIs(self.request(), handle)
        with self.assertRaises(InvalidArguments):
            self.request(timeout=20)
        count = len(self.gate.calls)
        for _ in range(3):
            self.assertEqual(self.result(handle).state, State.REQUESTED)
        self.assertEqual(len(self.gate.calls), count)
        self.unblock.set()
        result = self.terminal(handle)
        self.assertEqual(result.state, State.GRANTED)
        self.assertIs(self.result(handle), result)
        self.assertIs(self.manager.cancel(self.binding, handle), result)
        self.assertIs(self.binding.try_acquire_control(42, lease_seconds=1), result.session)
        self.binding.release_control(result.session)

    def test_autonomous_grant_without_get_and_terminal_gc(self):
        handle = self.request()
        self.join_workers()
        result = self.result(handle)
        self.assertEqual(result.state, State.GRANTED)
        self.binding.release_control(result.session)
        reference = weakref.ref(handle)
        del handle, result
        gc.collect()
        self.assertIsNone(reference())
        self.assertEqual(len(self.manager._terminal), 0)

    def test_error_gc_after_user_raise_does_not_retain_handle(self):
        self.gate.action = lambda device: (_ for _ in ()).throw(AuthenticationError())
        references = []
        def user():
            handle = self.request()
            references.append(weakref.ref(handle))
            result = self.terminal(handle)
            raise result.make_error()
        try:
            user()
        except AuthenticationError:
            pass
        self.join_workers()
        gc.collect()
        self.assertIsNone(references[0]())
        self.assertEqual(len(self.manager._terminal), 0)

    def test_unknown_devices_do_not_accumulate_workers_or_queues(self):
        def acquire(device):
            if device != 42:
                raise AuthenticationError()
        self.gate.action = acquire
        baseline = sum(t.name.startswith('control-request-') for t in threading.enumerate())
        for device in range(100, 125):
            handle = self.request(device=device)
            self.assertEqual(self.terminal(handle).state, State.DENIED)
            self.join_workers()
        gc.collect()
        self.assertEqual(self.manager._queues, {})
        self.assertEqual(self.manager._workers, {})
        self.assertLessEqual(len(self.manager._threads), 1)
        self.assertEqual(sum(t.name.startswith('control-request-')
                             for t in threading.enumerate()), baseline)
        self.assertEqual(self.terminal(self.request()).state, State.GRANTED)

    def test_busy_uncertain_transient_retries_keep_original_deadlines_and_id(self):
        for error in (DeviceControlBusy, CommandOutcomeUnknown, DeviceUnavailable, RateLimited):
            with self.subTest(error=error):
                entered = threading.Event()
                def first(device):
                    entered.set()
                    raise error()
                self.gate.action = first
                handle = self.request(device=len(self.gate.calls) + 100)
                self.assertTrue(entered.wait(1))
                with self.executor._idle:
                    self.assertTrue(self.executor._idle.wait_for(
                        lambda: not self.manager._active[handle].in_flight, timeout=1))
                first_call = self.gate.calls[-1]
                self.gate.action = None
                self.advance(1)
                result = self.terminal(handle)
                self.assertEqual(result.state, State.GRANTED)
                last_call = self.gate.calls[-1]
                self.assertEqual(first_call[3], last_call[3])
                self.assertEqual(last_call[3]['request_id'], handle.request_id)
                self.assertEqual(last_call[3]['local_deadline'], result.session.expires_at)
                self.assertEqual(self.gate.abandoned, [])

    def test_exact_timeout_and_lease_boundary_no_further_attempt(self):
        for timeout, reason in ((30, Reason.TIMEOUT), (60, Reason.LEASE_EXPIRED)):
            with self.subTest(reason=reason):
                entered = threading.Event()
                def busy(device):
                    entered.set()
                    raise DeviceControlBusy()
                self.gate.action = busy
                handle = self.request(device=int(self.clock.now()), timeout=timeout)
                self.assertTrue(entered.wait(1))
                count = len(self.gate.calls)
                self.advance(timeout)
                self.join_workers()
                self.assertEqual(self.result(handle).reason, reason)
                self.assertEqual(len(self.gate.calls), count)

    def test_cancel_and_timeout_during_http_return_immediately_late_success_cleanup(self):
        for cancel in (True, False):
            with self.subTest(cancel=cancel):
                self.unblock.clear()
                entered = threading.Event()
                self.gate.action = lambda device: self.block(entered)
                handle = self.request(device=len(self.gate.calls) + 100)
                self.assertTrue(entered.wait(1))
                if cancel:
                    denied = self.manager.cancel(self.binding, handle)
                else:
                    self.advance(30)
                    denied = self.result(handle)
                self.assertEqual(denied.reason, Reason.CANCELLED if cancel else Reason.TIMEOUT)
                self.assertFalse(self.gate.releases and self.gate.releases[-1].device_id
                                 == handle.device_id)
                self.unblock.set()
                self.join_workers()
                self.assertIs(self.result(handle), denied)
                self.assertEqual(self.gate.releases[-1].device_id, handle.device_id)
                self.assertEqual(self.gate.abandoned[-1][2], handle.request_id)

    def test_autonomous_late_denial_without_polling(self):
        entered = threading.Event()
        self.gate.action = lambda device: self.block(entered)
        handle = self.request(timeout=60)
        self.assertTrue(entered.wait(1))
        self.advance(60)
        self.unblock.set()
        self.join_workers()
        self.assertEqual(self.result(handle).reason, Reason.LEASE_EXPIRED)
        self.assertEqual(len(self.gate.calls), 1)
        self.assertEqual(len(self.gate.releases), 1)

    def test_fifo_queue_deadline_includes_queue_and_other_device_is_independent(self):
        entered = threading.Event()
        self.gate.action = lambda device: self.block(entered) if device == 42 else None
        first = self.request()
        self.assertTrue(entered.wait(1))
        second = self.request(self.other, timeout=5, lease_seconds=5)
        other_device = self.request(self.other, device=43)
        self.assertEqual(self.terminal(other_device, self.other).state, State.GRANTED)
        self.advance(5)
        self.unblock.set()
        self.join_workers()
        self.assertEqual(self.result(second, self.other).reason, Reason.LEASE_EXPIRED)
        self.assertEqual([call[1] for call in self.gate.calls], [42, 43])
        self.assertEqual(self.result(first).state, State.GRANTED)

    def test_pending_blocks_immediate_confirmation_and_cancel_event_is_autonomous(self):
        entered = threading.Event()
        def busy(device):
            entered.set()
            raise DeviceControlBusy()
        self.gate.action = busy
        cancelled = threading.Event()
        handle = self.request(cancelled=cancelled)
        self.assertTrue(entered.wait(1))
        count = len(self.gate.calls)
        with self.assertRaises(DeviceControlBusy):
            self.binding.try_acquire_control(42, lease_seconds=60)
        self.assertEqual(len(self.gate.calls), count)
        cancelled.set()
        self.join_workers()
        self.assertEqual(self.result(handle).reason, Reason.CANCELLED)

    def test_reuse_existing_issued_session_no_renew_no_cancel_revocation(self):
        session = self.binding.try_acquire_control(42, lease_seconds=60)
        with self.assertRaises(InvalidArguments):
            self.request(lease_seconds=20, timeout=10)
        handle = self.request()
        result = self.terminal(handle)
        self.assertIs(result.session, session)
        self.assertEqual(len(self.gate.calls), 1)
        self.assertIs(self.manager.cancel(self.binding, handle), result)
        self.assertEqual(self.gate.releases, [])

    def test_immediate_inflight_reuse_checks_deadline_again(self):
        for lease, expected in ((5, State.DENIED), (60, State.GRANTED)):
            with self.subTest(lease=lease):
                self.unblock.clear()
                entered = threading.Event()
                self.gate.action = lambda device: self.block(entered)
                issued = []
                device = lease + 100
                thread = self.start(lambda: issued.append(self.binding.try_acquire_control(
                    device, lease_seconds=60)))
                self.assertTrue(entered.wait(1))
                handle = self.request(device=device, lease_seconds=lease, timeout=lease)
                self.unblock.set()
                thread.join(1)
                self.advance(1)
                result = self.terminal(handle)
                self.assertEqual(result.state, expected)
                if expected is State.DENIED:
                    self.assertEqual(result.error_code, Code.INVALID_ARGUMENTS)
                else:
                    self.assertIs(result.session, issued[0])
                self.assertIs(self.executor._devices[device].session, issued[0])
                self.assertEqual(self.gate.releases, [])
                self.assertEqual(self.gate.abandoned, [])

    def test_c02_unclaimed_never_exposed_during_context_and_cleanup_once(self):
        for cancel in (True, False):
            with self.subTest(cancel=cancel):
                self.unblock.clear()
                entered = threading.Event()
                self.gate.context = lambda session: self.block(entered)
                handle = self.request(device=len(self.gate.calls) + 100)
                self.assertTrue(entered.wait(1))
                if cancel:
                    denied = self.manager.cancel(self.binding, handle)
                else:
                    self.advance(30)
                    denied = self.result(handle)
                with self.assertRaises(DeviceControlBusy):
                    self.binding.try_acquire_control(handle.device_id, lease_seconds=60)
                self.assertIsNone(denied.session)
                self.unblock.set()
                self.join_workers()
                self.assertEqual(sum(s.device_id == handle.device_id
                                     for s in self.gate.releases), 1)

    def test_baseexception_after_storing_unclaimed_found_without_returned_session(self):
        self.gate.context = lambda session: (_ for _ in ()).throw(SystemExit('private'))
        handle = self.request()
        result = self.terminal(handle)
        self.join_workers()
        self.assertEqual(result.reason, Reason.CONTROLLER_ERROR)
        self.assertEqual(result.error_code, Code.CONTROLLER_ERROR)
        self.assertEqual(len(self.gate.releases), 1)
        self.assertEqual(self.executor._devices, {})
        self.assertEqual(self.gate.abandoned[-1][2], handle.request_id)

    def test_shutdown_pending_and_inflight_waits_late_cleanup(self):
        entered = threading.Event()
        self.gate.action = lambda device: self.block(entered)
        handle = self.request()
        self.assertTrue(entered.wait(1))
        pending = self.request(self.other)
        self.executor.begin_close()
        self.assertEqual(self.result(handle).reason, Reason.SHUTDOWN)
        self.assertEqual(self.result(pending, self.other).reason, Reason.SHUTDOWN)
        with self.assertRaises(ControlOwnershipRequired):
            self.request(device=43)
        drained = threading.Event()
        self.start(lambda: (self.executor.wait_idle(), drained.set()))
        self.assertFalse(drained.is_set())
        self.unblock.set()
        self.assertTrue(drained.wait(2))
        self.assertEqual(len(self.gate.releases), 1)
        self.assertEqual(len(self.gate.calls), 1)

    def test_start_failure_denied_no_unstarted_thread_in_shutdown(self):
        with patch('zontcontrol.controller.requests.Thread.start', side_effect=RuntimeError):
            handle = self.request()
        self.assertEqual(self.result(handle).reason, Reason.CONTROLLER_ERROR)
        self.assertEqual(self.manager._workers, {})
        self.assertEqual(self.manager._queues, {})
        self.assertEqual(len(self.manager._threads), 0)
        self.assertEqual(self.gate.calls, [])

    def test_handle_identity_and_binding(self):
        handle = self.request()
        for binding, value in ((self.other, handle), (self.binding, ControlRequest(
                handle.request_id, handle.device_id))):
            with self.assertRaises(InvalidArguments):
                self.manager.get(binding, value)

    def test_worker_reuses_registration_when_new_request_arrives_before_retirement(self):
        entered = threading.Event()
        self.gate.action = lambda device: (_ for _ in ()).throw(AuthenticationError())
        self.gate.abandon_action = lambda device: self.block(entered)
        first = self.request()
        self.assertTrue(entered.wait(1))
        with self.executor._idle:
            worker = self.manager._workers[42]
        second = self.request(self.other)
        with self.executor._idle:
            self.assertIs(self.manager._workers[42], worker)
        self.unblock.set()
        self.join_workers()
        self.assertEqual(self.result(first).state, State.DENIED)
        self.assertEqual(self.result(second, self.other).state, State.DENIED)
        self.assertEqual([call[0] for call in self.gate.calls],
                         [self.binding._owner, self.other._owner])

    def test_worker_successor_after_deregistration_is_not_removed_by_old_exit(self):
        retired = threading.Event()
        run = self.manager._run
        def paused_exit(device):
            run(device)
            self.block(retired)
        self.manager._run = paused_exit
        first = self.request()
        self.assertTrue(retired.wait(1))
        self.binding.release_control(self.result(first).session)
        with self.executor._idle:
            self.assertEqual(self.manager._workers, {})
        entered = threading.Event()
        self.manager._run = run
        successor_unblock = threading.Event()
        def successor(device):
            entered.set()
            if not successor_unblock.wait(3):
                raise AssertionError('successor barrier timed out')
        self.gate.action = successor
        try:
            second = self.request(self.other)
            self.assertTrue(entered.wait(1))
            with self.executor._idle:
                new_worker = self.manager._workers[42]
            self.unblock.set()
            for worker in list(self.manager._threads):
                if worker is not new_worker:
                    worker.join(1)
            with self.executor._idle:
                self.assertIs(self.manager._workers[42], new_worker)
            self.assertEqual(len(self.gate.calls), 2)
        finally:
            successor_unblock.set()
        self.assertEqual(self.terminal(second, self.other).state, State.GRANTED)

    def test_close_joins_deregistered_thread_and_admission_stays_closed(self):
        retired = threading.Event()
        run = self.manager._run
        def paused_exit(device):
            run(device)
            self.block(retired)
        self.manager._run = paused_exit
        self.gate.action = lambda device: (_ for _ in ()).throw(AuthenticationError())
        handle = self.request()
        self.assertTrue(retired.wait(1))
        self.executor.begin_close()
        with self.assertRaises(ControlOwnershipRequired):
            self.request()
        joined = threading.Event()
        snapshot_taken = threading.Event()
        snapshot = self.manager.thread_snapshot_locked
        def observed_snapshot():
            snapshot_taken.set()
            return snapshot()
        self.manager.thread_snapshot_locked = observed_snapshot
        self.start(lambda: (self.executor.wait_idle(), joined.set()))
        self.assertTrue(snapshot_taken.wait(1))
        self.assertFalse(joined.is_set())
        self.unblock.set()
        self.assertTrue(joined.wait(1))
        self.assertEqual(self.result(handle).state, State.DENIED)

    def test_fifo_three_bindings_no_overlapping_attempts(self):
        entered = threading.Event()
        self.gate.action = lambda device: self.block(entered)
        first = self.request()
        self.assertTrue(entered.wait(1))
        second = self.request(self.other)
        third_binding = self.executor.bind()
        third = self.request(third_binding)
        self.unblock.set()
        self.binding.release_control(self.terminal(first).session)
        self.advance(1)
        self.other.release_control(self.terminal(second, self.other).session)
        self.advance(1)
        third_result = self.terminal(third, third_binding)
        self.assertEqual(third_result.state, State.GRANTED)
        self.assertEqual([call[0] for call in self.gate.calls],
                         [self.binding._owner, self.other._owner, third_binding._owner])

    def test_cancel_wins_between_acquire_return_and_manager_commit(self):
        entered = threading.Event()
        attempt = self.manager._attempt
        def returned(*args, **kwargs):
            session = attempt(*args, **kwargs)
            self.block(entered)
            return session
        self.manager._attempt = returned
        handle = self.request()
        self.assertTrue(entered.wait(1))
        denied = self.manager.cancel(self.binding, handle)
        self.advance(60)
        with self.assertRaises(DeviceControlBusy):
            self.other.try_acquire_control(42, lease_seconds=10)
        drained = threading.Event()
        self.executor.begin_close()
        self.start(lambda: (self.executor.wait_idle(), drained.set()))
        self.assertFalse(drained.is_set())
        self.unblock.set()
        self.assertTrue(drained.wait(2))
        self.assertEqual(self.gate.releases and len(self.gate.releases), 1)
        self.assertEqual(denied.reason, Reason.CANCELLED)
        self.assertIs(self.result(handle), denied)

    def test_reuse_release_race_closing_and_deleted_reservation_never_granted(self):
        for remove in (False, True):
            with self.subTest(remove=remove):
                self.unblock.clear()
                device = 100 + int(remove)
                session = self.binding.try_acquire_control(device, lease_seconds=60)
                entered = threading.Event()
                attempt = self.executor._acquire
                def returned(*args, **kwargs):
                    acquired = attempt(*args, **kwargs)
                    self.block(entered)
                    return acquired
                self.manager._attempt = returned
                handle = self.request(device=device)
                self.assertTrue(entered.wait(1))
                if remove:
                    self.binding.release_control(session)
                else:
                    # This is exactly release's locked admission before its I/O.
                    with self.executor._idle:
                        self.executor._devices[device].closing = True
                self.unblock.set()
                with self.executor._idle:
                    self.assertTrue(self.executor._idle.wait_for(
                        lambda: not self.manager._active[handle].in_flight, timeout=1))
                self.assertEqual(self.result(handle).state, State.REQUESTED)
                denied = self.manager.cancel(self.binding, handle)
                self.join_workers()
                self.assertEqual(denied.reason, Reason.CANCELLED)
                self.assertEqual(sum(s is session for s in self.gate.releases), int(remove))
                if not remove:
                    self.binding.release_control(session)

    def test_grant_wins_then_release_keeps_result_and_rejects_execute(self):
        handle = self.request()
        result = self.terminal(handle)
        self.binding.release_control(result.session)
        self.assertIs(self.result(handle), result)
        self.assertIs(self.manager.cancel(self.binding, handle), result)
        from zontcontrol.controller.contracts import ControlKey, ValidatedCommand
        command = ValidatedCommand(ControlKey(42, 'test', 1), 'test')
        with self.assertRaises(ControlOwnershipRequired):
            self.binding.execute(command, result.session, lambda: command,
                                 lambda prepared: self.fail('must not send'))

    def test_granted_result_does_not_block_immediate_reuse_before_worker_exit(self):
        entered = threading.Event()
        cleanup = self.manager._cleanup_state
        def paused(state):
            self.block(entered)
            cleanup(state)
        self.manager._cleanup_state = paused
        handle = self.request()
        self.assertTrue(entered.wait(1))
        result = self.result(handle)
        self.assertEqual(result.state, State.GRANTED)
        self.assertIs(self.binding.try_acquire_control(42, lease_seconds=60), result.session)
        self.unblock.set()

    def test_first_cleanup_attempt_cannot_be_discarded_by_expiry(self):
        entered = threading.Event()
        self.gate.context = lambda session: (_ for _ in ()).throw(SystemExit())
        release = self.executor._release_remote
        def paused(held):
            self.block(entered)
            release(held)
        self.executor._release_remote = paused
        handle = self.request()
        self.assertTrue(entered.wait(1))
        self.advance(60)
        with self.assertRaises(DeviceControlBusy):
            self.other.try_acquire_control(42, lease_seconds=10)
        self.assertEqual(self.gate.releases, [])
        self.unblock.set()
        self.join_workers()
        self.assertEqual(len(self.gate.releases), 1)
        self.assertEqual(self.result(handle).reason, Reason.CONTROLLER_ERROR)

    def test_unknown_cleanup_retains_reservation_until_expiry_without_retry(self):
        self.gate.context = lambda session: (_ for _ in ()).throw(KeyboardInterrupt())
        self.gate.release_action = lambda session: (_ for _ in ()).throw(CommandOutcomeUnknown())
        handle = self.request()
        self.join_workers()
        self.assertEqual(self.result(handle).reason, Reason.CONTROLLER_ERROR)
        self.assertEqual(len(self.gate.releases), 1)
        self.executor._release_unclaimed(self.binding, handle.request_id)
        self.assertEqual(len(self.gate.releases), 1)
        with self.assertRaises(DeviceControlBusy):
            self.other.try_acquire_control(42, lease_seconds=10)
        self.advance(60)
        self.gate.context = None
        self.gate.release_action = None
        replacement = self.other.try_acquire_control(42, lease_seconds=10)
        self.assertEqual(replacement.expires_at, self.clock.now() + 10)
        self.assertEqual(len(self.gate.releases), 1)

    def test_late_error_and_unknown_are_terminal_without_new_attempt(self):
        for error in (AuthenticationError, DeviceUnavailable, CommandOutcomeUnknown,
                      SystemExit, KeyboardInterrupt):
            with self.subTest(error=error):
                self.unblock.clear()
                entered = threading.Event()
                def late(device):
                    self.block(entered)
                    raise error()
                self.gate.action = late
                handle = self.request(device=len(self.gate.calls) + 100)
                self.assertTrue(entered.wait(1))
                count = len(self.gate.calls)
                self.advance(30)
                self.unblock.set()
                self.join_workers()
                self.assertEqual(self.result(handle).reason, Reason.TIMEOUT)
                self.assertEqual(len(self.gate.calls), count)
                self.assertEqual(self.gate.releases, [])

    def test_all_fatal_error_codes_and_worker_interruptions_terminalize(self):
        from zontcontrol.controller.contracts import (
            ControllerError, IncompleteHistory, UnsupportedAction,
        )
        errors = (AuthenticationError, UnsupportedAction, InvalidArguments,
                  IncompleteHistory, ControlOwnershipRequired, ControllerError,
                  SystemExit, KeyboardInterrupt, RuntimeError)
        for error in errors:
            with self.subTest(error=error):
                self.gate.action = lambda device: (_ for _ in ()).throw(error())
                handle = self.request(device=len(self.gate.calls) + 100)
                result = self.terminal(handle)
                self.join_workers()
                self.assertEqual(result.reason, Reason.CONTROLLER_ERROR)
                self.assertEqual(result.error_code, Code.from_error(error()))
                self.assertEqual(self.gate.abandoned[-1][2], handle.request_id)

    def test_denial_priority_shutdown_cancel_lease_timeout_and_identity_cleanup(self):
        entered = threading.Event()
        self.gate.context = lambda session: self.block(entered)
        cancelled = threading.Event()
        handle = self.request(timeout=60, cancelled=cancelled)
        self.assertTrue(entered.wait(1))
        session = self.executor._devices[42].session
        self.executor._release_unclaimed(self.other, handle.request_id)
        self.executor._release_unclaimed(self.binding, uuid4())
        self.executor._release_unclaimed(self.binding, handle.request_id,
                                         ControlSession(42, session.session_id, session.expires_at))
        self.assertEqual(self.gate.releases, [])
        cancelled.set()
        self.advance(60)
        self.executor.begin_close()
        self.assertEqual(self.result(handle).reason, Reason.SHUTDOWN)
        self.unblock.set()
        self.join_workers()
        self.assertEqual(self.gate.releases, [session])


if __name__ == '__main__':
    unittest.main()
