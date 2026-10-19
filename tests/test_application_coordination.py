"""Full composition over fake HTTP and manually released clock waits."""

from copy import deepcopy
from dataclasses import replace
import io
import json
from queue import Queue
from threading import Event, Lock, Thread, current_thread
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit
from uuid import UUID
from tests.fakes.uuid import uuid_generator

from tests.test_application import settings
from tests.test_inventory import inventory_device
from zontcontrol.application import create_application
from zontcontrol.autonomy.contracts import AlgorithmSpec, Mode
from zontcontrol.controller.contracts import (
    ControlKey, ControlOwnershipRequired, DeviceControlBusy,
    ControlRequestReason as Reason, ControlRequestState as State,
)
from zontcontrol.controller.transport import HttpTransport
from zontcontrol.coordination.models import (
    RecordError, decode_r1, decode_r2, encode_r1, encode_r2, original_name,
)
from zontcontrol.coordination.store import ExecutorStore


def first_grant(raw):
    record = decode_r1(raw, 'Дом')
    return record.grants[0] if record is not None and record.grants else None


class StepClock:
    """Protocol time moves only when a test releases a scheduled wait."""

    def __init__(self):
        self.time = 100
        self.waits = Queue()
        self.lock = Lock()

    def now(self):
        with self.lock:
            return self.time

    def wait(self, cancelled, seconds):
        resume = Event()
        self.waits.put((resume, seconds))
        while not cancelled.is_set():
            if resume.wait(.01):
                return cancelled.is_set()
        return True

    def advance_wait(self):
        resume, seconds = self.waits.get(timeout=1)
        with self.lock:
            self.time += seconds + 1e-8
        resume.set()


class MemoryTransport:
    def __init__(self):
        self.raw = inventory_device()
        self.raw['notes'] = ''
        self.lock = Lock()
        self.granted, self.cleared = Event(), Event()
        self.commands = []
        self.closed = False
        self.requests = []
        self.request_trace = []
        self.after = lambda request, response: None

    def request(self, request, *, timeout_seconds=None):
        if self.closed:
            raise AssertionError('request after transport close')
        self.requests.append(request)
        self.request_trace.append((request, current_thread().name))
        response = self.respond(request)
        self.after(request, response)
        return response

    def respond(self, request):
        body = json.loads(request.data) if request.data is not None else None
        path = urlsplit(request.full_url).path
        with self.lock:
            if path.endswith('/update_device'):
                self.raw.update({key: value for key, value in body.items() if key != 'device_id'})
                if 'name' in body:
                    record = decode_r1(body['name'], 'Дом')
                    if record is not None and record.grants:
                        self.granted.set()
                    if record is not None and record.handled_request is not None and (
                            record.handled_request.outcome == 'finished'):
                        self.cleared.set()
                return {'ok': True}
            if '/actions/' in path:
                self.commands.append((path, body))
                return {'ok': True}
            if path.endswith('/devices'):
                return {'ok': True, 'devices': [deepcopy(self.raw)]}
            raise AssertionError('Unexpected offline endpoint')

    def close(self):
        self.closed = True


class ComposedCoordinationTests(unittest.TestCase):
    def setUp(self):
        self.uuid_generator = uuid_generator()
        self.clock = StepClock()
        self.transport = MemoryTransport()
        self.observations = []
        self.on_observation = lambda row: None
        data = settings()
        data['config/registry.json']['once_timeouts'] = {}
        data['config/controller.json']['coordination']['poll_seconds'] = .1
        entered, contexts = Event(), []
        class Sentinel:
            def __init__(self, controller):
                pass
            def run_continuous(self, context):
                contexts.append(context)
                entered.set()
                context.cancelled.wait()
        spec = AlgorithmSpec('sentinel', 'Offline lifecycle observer',
                             frozenset({Mode.CONTINUOUS}), Sentinel)
        self.app = create_application(environ={'ZONT_TOKEN': 'fake-secret'},
            catalog=(spec,), reader=lambda path: data[str(path)], clock=self.clock,
            transport_factory=lambda **kw: self.transport,
            statistics_sink=self.observe,
            uuid_factory=self.uuid_generator)
        self.owner = self.app.controller()
        self.other = self.app.controller()
        self.addCleanup(self.app.close)
        def expire_after_test():
            with self.clock.lock:
                self.clock.time += 2000
        self.addCleanup(expire_after_test)
        self.assertTrue(entered.wait(1))
        self.algorithm_context = contexts[0]

    def observe(self, row):
        self.observations.append(row)
        self.on_observation(row)

    def test_injected_uuid_sequence_reaches_leader_grant_and_session(self):
        before = self.uuid_generator.call_count
        read = ExecutorStore.read_r1
        reads = 0
        def ordered_read(store, **kwargs):
            nonlocal reads
            reads += 1
            if reads == 2:  # Bootstrap read is first; then wait for candidate WRITE.
                scheduled = self.clock.waits.get(timeout=1)
                self.clock.waits.put(scheduled)
            return read(store, **kwargs)
        with patch.object(ExecutorStore, 'read_r1', ordered_read):
            handle = self.request()
            result = self.wait_result(handle, pump=True)
        self.assertIs(result.state, State.GRANTED)
        gate, _ = self.app._coordination._devices[42]
        store = gate._stores[42]
        leader, request = store.read_r1().leader, store.read_r2().request
        grant = store.read_r1().grants[0]
        self.assertEqual(handle.request_id, UUID(int=before + 1))
        self.assertEqual(leader.owner_id, str(UUID(int=before + 2)))
        self.assertEqual(request.request_id, str(handle.request_id))
        self.assertEqual(request.nonce, str(UUID(int=before + 5)))
        self.assertEqual(grant.grant_id, str(UUID(int=before + 7)))
        self.assertEqual(grant.request_id, str(handle.request_id))
        self.assertEqual(result.session.session_id, UUID(int=before + 9))
        self.owner.release_control(result.session)
        self.assertEqual(self.uuid_generator.call_count, before + 10)
        self.assertEqual(self.transport.commands, [])

    def test_role_writers_use_actual_name_and_notes_http_fields(self):
        handle = self.request()
        result = self.wait_result(handle, pump=True)
        self.assertIs(result.state, State.GRANTED)
        try:
            fields = set()
            for request, thread in self.transport.request_trace:
                if not request.full_url.endswith('/update_device'):
                    continue
                body = json.loads(request.data)
                keys = set(body)-{'device_id'}
                self.assertEqual(len(keys), 1)
                field, = keys
                fields.add(field)
                if thread.startswith('coordinator-'):
                    self.assertEqual(field, 'name')
                    self.assertIsNotNone(decode_r1(body[field], 'Дом').leader)
                else:
                    self.assertEqual(thread, 'control-request-42')
                    self.assertEqual(field, 'notes')
                    self.assertEqual(decode_r2(body[field]).request.request_id, str(handle.request_id))
            self.assertEqual(fields, {'name', 'notes'})
        finally:
            self.owner.release_control(result.session)

    def test_future_grant_remains_requested_until_usable_without_status_io(self):
        first = self.acquire()
        self.owner.release_control(first)
        published = Event()
        def observe_request(row):
            if row['event'] == 'request' and row.get('run_id') == str(self.other._control._owner):
                published.set()
        self.on_observation = observe_request
        handle = self.request(self.other, lease=60, timeout=30)
        executor = self.app._controller._executor
        def wait_request_poll():
            # The request worker uses its own Condition, not StepClock.wait.
            # Finish its due attempt before advancing the coordinator again.
            with executor._idle:
                executor._idle.notify_all()
                self.assertTrue(executor._idle.wait_for(lambda:
                    handle in executor._requests._terminal or (
                        not executor._requests._active[handle].in_flight
                        and executor._requests._active[handle].next_poll > self.clock.now()),
                    timeout=1))
        self.assertTrue(published.wait(1))
        wait_request_poll()
        self.clock.advance_wait()
        scheduled = self.clock.waits.get(timeout=1)
        self.clock.waits.put(scheduled)
        wait_request_poll()
        grants = decode_r1(self.transport.raw['name'], 'Дом').grants
        self.assertEqual(len(grants), 2)
        self.assertGreater(grants[1].valid_from, grants[0].valid_until)
        before = len(self.transport.requests)
        pending = self.other.get_control_request(handle)
        self.assertIs(pending.state, State.REQUESTED)
        self.assertIsNone(pending.session)
        self.assertIs(self.other.get_control_request(handle), pending)
        self.assertEqual(len(self.transport.requests), before)
        for _ in range(200):
            with executor._idle:
                done = handle in executor._requests._terminal
            if done:
                break
            self.clock.advance_wait()
            scheduled = self.clock.waits.get(timeout=1)
            self.clock.waits.put(scheduled)
            wait_request_poll()
        granted = self.other.get_control_request(handle)
        self.assertIs(granted.state, State.GRANTED)
        try:
            command = self.other.prepare_command(42, ControlKey(42, 'circuit', 10),
                                                'target-temp', {'target_temp': 22})
            self.assertTrue(self.other.execute_command(command, granted.session).api_accepted)
        finally:
            self.other.release_control(granted.session)

    def request(self, controller=None, *, lease=60, timeout=10):
        return (controller or self.owner).request_control(
            42, lease_seconds=lease, timeout=timeout, poll_seconds=.005)

    def wait_result(self, handle, *, pump=False, controller=None):
        executor = self.app._controller._executor
        for _ in range(20 if pump else 1):
            with executor._idle:
                done = executor._idle.wait_for(
                    lambda: handle in executor._requests._terminal, timeout=.1 if pump else 1)
            if done:
                return (controller or self.owner).get_control_request(handle)
            if pump:
                self.clock.advance_wait()
        self.fail('request did not settle without status polling')

    def join_requests(self):
        executor = self.app._controller._executor
        with executor._idle:
            workers = executor._requests.thread_snapshot_locked()
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())

    def wait_ack_barrier(self, entered):
        for _ in range(10):
            if entered.wait(.03):
                return
            self.clock.advance_wait()
        self.fail('real lower gate did not reach grant_ack')

    def assert_late_grant_denied(self, reason):
        entered, resume = Event(), Event()
        self.addCleanup(resume.set)
        def pause(row):
            if row['event'] == 'grant_ack' and not entered.is_set():
                entered.set()
                self.assertTrue(resume.wait(2))
        self.on_observation = pause
        handle = self.request(timeout=1)
        self.wait_ack_barrier(entered)
        old = first_grant(self.transport.raw['name'])
        self.assertEqual(old.request_id, str(handle.request_id))
        if reason is Reason.CANCELLED:
            denied = self.owner.cancel_control_request(handle)
        else:
            with self.clock.lock:
                self.clock.time += 1
            denied = self.owner.get_control_request(handle)
        self.assertIs(denied.state, State.DENIED)
        self.assertIs(denied.reason, reason)
        self.assertIsNone(denied.session)
        # Replace R2 while accepted local work is still completing. Cleanup of
        # the old request must not announce completion of this foreign grant.
        foreign = replace(old, owner_id=str(self.uuid_generator()), request_id=str(self.uuid_generator()),
                          grant_id=str(self.uuid_generator()), nonce=str(self.uuid_generator()))
        with self.transport.lock:
            self.transport.raw['name'] = encode_r1('Дом', replace(
                decode_r1(self.transport.raw['name'], 'Дом'), grants=(foreign,)))
        resume.set()
        self.join_requests()
        self.assertEqual(first_grant(self.transport.raw['name']), foreign)
        self.assertEqual(decode_r2(self.transport.raw['notes']).request.operation, 'acquire')
        gate = self.app._coordination._devices[42][0]
        self.assertTrue(all(held.finished for held in gate._held.values()))
        # A new public request gets a fresh lower request/nonce, never old ack.
        next_handle = self.request(timeout=1)
        self.assertNotEqual(next_handle.request_id, handle.request_id)
        executor = self.app._controller._executor
        with executor._idle:
            self.assertTrue(executor._idle.wait_for(lambda:
                decode_r2(self.transport.raw['notes']).request.request_id
                    == str(next_handle.request_id), timeout=1))
        next_record = decode_r2(self.transport.raw['notes']).request
        self.assertNotEqual(next_record.nonce, old.nonce)
        self.assertIs(self.owner.get_control_request(next_handle).state, State.REQUESTED)
        self.owner.cancel_control_request(next_handle)
        self.join_requests()
        self.assertIs(self.owner.get_control_request(handle), denied)
        command = self.owner.prepare_command(42, ControlKey(42, 'circuit', 10),
            'target-temp', {'target_temp': 22})
        with self.assertRaises(ControlOwnershipRequired):
            self.owner.execute_command(command, denied.session)
        self.assertEqual(self.transport.commands, [])

    def test_cancel_during_ack_cleans_only_late_owned_session(self):
        self.assert_late_grant_denied(Reason.CANCELLED)

    def test_timeout_during_ack_never_sends_or_reuses_late_grant(self):
        self.assert_late_grant_denied(Reason.TIMEOUT)

    def test_shutdown_waits_for_pending_http_and_late_release_before_closing_transport(self):
        entered, resume, releasing, released = Event(), Event(), Event(), Event()
        finished = []
        self.addCleanup(resume.set)
        self.addCleanup(released.set)
        def pause_ack(row):
            if row['event'] == 'grant_ack':
                entered.set()
                self.assertTrue(resume.wait(2))
        self.on_observation = pause_ack
        def pause_finished(request, response):
            if current_thread().name == 'control-request-42' and request.data:
                body = json.loads(request.data)
                if 'notes' in body and decode_r2(body['notes']).request.operation == 'finish':
                    finished.append(request)
                    releasing.set()
                    self.assertTrue(released.wait(2))
        self.transport.after = pause_finished
        handle = self.request(timeout=10)
        self.wait_ack_barrier(entered)
        self.assertFalse(self.app.close(wait_timeout=.01))
        denied = self.owner.get_control_request(handle)
        self.assertIs(denied.reason, Reason.SHUTDOWN)
        self.assertFalse(self.transport.closed)
        resume.set()
        self.assertTrue(releasing.wait(1))
        self.assertFalse(self.app.close(wait_timeout=.01))
        self.assertFalse(self.transport.closed)
        released.set()
        self.assertTrue(self.app.close(wait_timeout=1))
        self.assertTrue(self.transport.closed)
        self.assertIs(self.owner.get_control_request(handle), denied)
        self.assertEqual(self.transport.commands, [])
        self.assertEqual(len(finished), 1)

    def test_async_late_first_metadata_does_not_start_more_stages_or_coordinator(self):
        entered, resume = Event(), Event()
        def pause(request, response):
            if current_thread().name == 'control-request-42' and not entered.is_set():
                entered.set()
                self.assertTrue(resume.wait(2))
        self.transport.after = pause
        self.addCleanup(resume.set)
        handle = self.request(timeout=1)
        self.assertTrue(entered.wait(1))
        self.assertEqual(len(self.transport.requests), 1)
        with self.clock.lock:
            self.clock.time += 1
        result = self.owner.get_control_request(handle)
        self.assertIs(result.reason, Reason.TIMEOUT)
        before = len(self.transport.requests)
        self.assertIs(self.owner.cancel_control_request(handle), result)
        self.assertEqual(len(self.transport.requests), before)
        resume.set()
        self.join_requests()
        self.assertEqual(len(self.transport.requests), 1)
        self.assertEqual(self.app._coordination._devices, {})
        self.assertEqual(self.transport.commands, [])

    def test_async_timeout_does_not_stop_existing_coordinator_or_foreign_grant(self):
        session = self.acquire()
        worker = self.app._coordination._devices[42][1]
        grant = first_grant(self.transport.raw['name'])
        handle = self.request(self.other, lease=5, timeout=1)
        with self.clock.lock:
            self.clock.time += 1
        result = self.wait_result(handle, controller=self.other)
        self.assertIs(result.reason, Reason.TIMEOUT)
        self.join_requests()
        self.assertFalse(worker.cancelled.is_set())
        self.assertTrue(worker.thread.is_alive())
        self.assertEqual(first_grant(self.transport.raw['name']), grant)
        command = self.owner.prepare_command(42, ControlKey(42, 'circuit', 10),
            'target-temp', {'target_temp': 22})
        self.owner.execute_command(command, session)
        self.assertEqual(len(self.transport.commands), 1)
        self.owner.release_control(session)


    def test_shutdown_keeps_async_granted_until_local_expiry_without_new_rights(self):
        handle = self.request(lease=5, timeout=4)
        granted = self.wait_result(handle, pump=True)
        session = granted.session
        if session is not None:
            def expire_session():
                with self.clock.lock:
                    self.clock.time = max(self.clock.time, session.expires_at)
            self.addCleanup(expire_session)
        self.assertIs(granted.state, State.GRANTED)
        grant = first_grant(self.transport.raw['name'])
        self.assertFalse(self.app.close(wait_timeout=.01))
        self.assertFalse(self.transport.closed)
        self.assertIs(self.owner.cancel_control_request(handle), granted)
        command = self.owner.prepare_command(42, ControlKey(42, 'circuit', 10),
            'target-temp', {'target_temp': 22})
        self.assertTrue(self.owner.execute_command(command, session).api_accepted)
        with self.clock.lock:
            self.clock.time = session.expires_at
        with self.assertRaises(ControlOwnershipRequired):
            self.owner.execute_command(command, session)
        self.assertTrue(self.app.close(wait_timeout=1))
        self.assertTrue(self.transport.closed)
        self.assertEqual(len(self.transport.commands), 1)
        self.assertIs(self.owner.get_control_request(handle), granted)
        self.assertEqual(first_grant(self.transport.raw['name']), grant)

    def test_system_exit_at_lower_ack_does_not_leak_long_session_to_short_request(self):
        ack = []
        def interrupt(row):
            if row['event'] == 'grant_ack' and not ack:
                ack.append(first_grant(self.transport.raw['name']))
                raise SystemExit()
        self.on_observation = interrupt
        first = self.request()
        denied = self.wait_result(first, pump=True)
        self.assertIs(denied.state, State.DENIED)
        self.assertIs(denied.reason, Reason.CONTROLLER_ERROR)
        self.join_requests()
        gate, _ = self.app._coordination._devices[42]
        self.assertEqual(gate._held, {})
        self.assertEqual(gate._pending, {})
        started = self.clock.now()
        second = self.request(lease=5, timeout=4)
        # A distinct short request publishes its own nonce while old R2 is busy.
        executor = self.app._controller._executor
        with executor._idle:
            self.assertTrue(executor._idle.wait_for(lambda:
                decode_r2(self.transport.raw['notes']).request.request_id == str(second.request_id),
                timeout=1))
        pending = decode_r2(self.transport.raw['notes']).request
        self.assertNotEqual(pending.nonce, ack[0].nonce)
        self.assertEqual(first_grant(self.transport.raw['name']), ack[0])
        self.assertIs(self.owner.get_control_request(second).state, State.REQUESTED)
        while self.clock.now() < started + 5:
            self.clock.advance_wait()
            scheduled = self.clock.waits.get(timeout=1)
            self.clock.waits.put(scheduled)
        short = self.wait_result(second)
        self.assertIs(short.state, State.DENIED)
        self.assertIsNone(short.session)
        self.join_requests()
        self.assertEqual(gate._held, {})
        # Expire the old remote grant. A fresh request can then be acknowledged,
        # with a new ID/nonce and the original five-second local deadline.
        # Advance ordinary polls, keeping the leader's renewals continuous.
        # A fresh leader would have to observe the full remote lease anew.
        for _ in range(1000):
            self.clock.advance_wait()
            scheduled = self.clock.waits.get(timeout=1)
            self.clock.waits.put(scheduled)
            if first_grant(self.transport.raw['name']) is None:
                break
        self.assertIsNone(first_grant(self.transport.raw['name']))
        third_start = self.clock.now()
        third = self.request(lease=5, timeout=4)
        result = self.wait_result(third, pump=True)
        self.assertIs(result.state, State.GRANTED)
        try:
            new = first_grant(self.transport.raw['name'])
            self.assertEqual(new.request_id, str(third.request_id))
            self.assertNotIn(new.request_id, {str(first.request_id), str(second.request_id)})
            self.assertNotEqual(new.nonce, ack[0].nonce)
            self.assertLessEqual(result.session.expires_at, third_start + 5)
            self.assertIs(self.owner.get_control_request(first), denied)
            self.assertEqual(self.transport.commands, [])
        finally:
            self.owner.release_control(result.session)

    def acquire(self):
        with self.assertRaises(DeviceControlBusy):
            self.owner.try_acquire_control(42, lease_seconds=10)
        self.clock.advance_wait()  # candidate's Fischer barrier
        # Synchronize on the next poll: its preceding step/readback is complete.
        # R1 is deliberately RMW, so the first request may race the candidate's
        # initial write. The executor's next explicit attempt republishes it.
        for _ in range(12):
            scheduled = self.clock.waits.get(timeout=1)
            self.clock.waits.put(scheduled)
            try:
                session = self.owner.try_acquire_control(42, lease_seconds=10)
                def release():
                    try:
                        self.owner.release_control(session)
                    except ControlOwnershipRequired:
                        pass
                self.addCleanup(release)
                return session
            except DeviceControlBusy:
                self.clock.advance_wait()
        self.fail('Offline grant was not acknowledged after explicit reconciliation')

    def test_lazy_initialization_checks_deadlines_and_close_between_stages(self):
        runtime = self.app._coordination
        adapter = runtime.adapter
        for stage in ('metadata', 'r1', 'r2', 'lock'):
            for reason in ('attempt', 'lease', 'closed'):
                with self.subTest(stage=stage, reason=reason):
                    entered, resume = Event(), Event()
                    errors, calls = [], []
                    deadline = self.clock.now() + 1
                    def pause():
                        entered.set()
                        self.assertTrue(resume.wait(2))
                    metadata = adapter.read_metadata
                    read_r1, read_r2 = ExecutorStore.read_r1, ExecutorStore.read_r2
                    def initial(device_id, **kwargs):
                        calls.append('metadata')
                        result = metadata(device_id, **kwargs)
                        if stage == 'metadata':
                            pause()
                        return result
                    def r1(store, **kwargs):
                        calls.append('r1')
                        result = read_r1(store, **kwargs)
                        if stage == 'r1':
                            pause()
                        return result
                    def r2(store, **kwargs):
                        calls.append('r2')
                        result = read_r2(store, **kwargs)
                        if stage == 'r2':
                            pause()
                        return result
                    class WaitingLock:
                        def __enter__(self):
                            entered.set()
                            blocked_lock.acquire()
                        def __exit__(self, *_):
                            blocked_lock.release()
                    if stage == 'lock':
                        blocked_lock = Lock()
                        blocked_lock.acquire()
                        runtime._device_locks[42] = WaitingLock()
                    def attempt():
                        try:
                            runtime.try_acquire(self.uuid_generator(), 42, lease_seconds=10,
                                request_id=self.uuid_generator(),
                                local_deadline=deadline if reason == 'lease' else deadline + 10,
                                attempt_deadline=deadline if reason == 'attempt' else deadline + 10)
                        except BaseException as error:
                            errors.append(error)
                    with patch.object(adapter, 'read_metadata', side_effect=initial), \
                            patch.object(ExecutorStore, 'read_r1', r1), \
                            patch.object(ExecutorStore, 'read_r2', r2), \
                            patch('zontcontrol.application._DeviceCoordinator') as worker:
                        thread = Thread(target=attempt)
                        thread.start()
                        try:
                            self.assertTrue(entered.wait(1))
                            if reason == 'closed':
                                with runtime._lock:
                                    runtime._closed = True
                            else:
                                with self.clock.lock:
                                    self.clock.time = deadline
                        finally:
                            if stage == 'lock':
                                blocked_lock.release()
                            resume.set()
                            thread.join(2)
                            with runtime._lock:
                                runtime._closed = False
                            runtime._device_locks.pop(42, None)
                        self.assertFalse(thread.is_alive())
                        self.assertEqual(len(errors), 1)
                        self.assertIsInstance(errors[0], ControlOwnershipRequired)
                        worker.return_value.thread.start.assert_not_called()
                    self.assertEqual(calls, {'metadata': ['metadata'],
                        'r1': ['metadata', 'r1', 'metadata'],
                        'r2': ['metadata', 'r1', 'metadata', 'r2', 'metadata'],
                        'lock': []}[stage])
                    self.assertNotIn(42, runtime._devices)
                    self.assertEqual(self.transport.commands, [])

    def test_runtime_abandon_never_initializes_missing_device(self):
        runtime = self.app._coordination
        with patch.object(runtime.adapter, 'read_metadata') as metadata:
            runtime.abandon_request(self.uuid_generator(), 42, self.uuid_generator())
            metadata.assert_not_called()
        self.assertEqual(runtime._devices, {})

    def test_expired_attempt_preserves_existing_shared_coordinator(self):
        session = self.acquire()
        runtime = self.app._coordination
        gate, worker = runtime._devices[42]
        deadline = self.clock.now()
        with patch.object(gate, 'try_acquire') as attempt:
            with self.assertRaises(ControlOwnershipRequired):
                runtime.try_acquire(self.uuid_generator(), 42, lease_seconds=5,
                    request_id=self.uuid_generator(), local_deadline=deadline + 5,
                    attempt_deadline=deadline)
            attempt.assert_not_called()
        self.assertIs(runtime._devices[42][1], worker)
        self.assertFalse(worker.cancelled.is_set())
        self.assertTrue(worker.thread.is_alive())
        self.owner.release_control(session)

    def test_shutdown_does_not_extend_expired_r2_or_authorize_cleanup(self):
        session = self.acquire()
        key = ControlKey(42, 'toggle_buttons', 31)
        on = self.owner.prepare_command(42, key, 'trigger', {'target_state': True})
        off = self.owner.prepare_command(42, key, 'trigger', {'target_state': False})
        self.assertTrue(self.owner.execute_command(on, session).api_accepted)
        with self.transport.lock:
            grant = first_grant(self.transport.raw['name'])
        self.assertFalse(self.app.close(wait_timeout=.01))
        self.assertFalse(self.transport.closed)
        # Expire the same injected clock used by the real R1/R2 gate and executor.
        with self.clock.lock:
            self.clock.time = session.expires_at
        with self.assertRaises(ControlOwnershipRequired):
            self.owner.execute_command(off, session)
        self.assertEqual(len(self.transport.commands), 1)
        with self.transport.lock:
            self.assertEqual(first_grant(self.transport.raw['name']), grant)
        # The algorithm's lease also bounds idle local ownership during shutdown.
        self.assertTrue(self.app.close(wait_timeout=1))
        self.assertTrue(self.transport.closed)
        self.assertEqual(len(self.transport.commands), 1)

    def test_interrupted_public_acquisition_closes_after_its_local_lease(self):
        # Prepare the real grant, then interrupt instrumentation before the
        # public facade can return its session to the caller.
        session = self.acquire()
        self.owner.release_control(session)
        self.clock.advance_wait()
        self.assertTrue(self.transport.cleared.wait(1))
        for _ in range(200):
            if first_grant(self.transport.raw['name']) is None:
                break
            self.clock.advance_wait()
            scheduled = self.clock.waits.get(timeout=1)
            self.clock.waits.put(scheduled)
        self.assertIsNone(first_grant(self.transport.raw['name']))
        gate = self.app._coordination._devices[42][0]
        with patch.object(gate, 'observation_context', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.acquire()
        self.assertFalse(self.app.close(wait_timeout=.01))
        self.assertFalse(self.transport.closed)
        with self.clock.lock:
            self.clock.time += 10
        self.assertTrue(self.app.close(wait_timeout=1))
        self.assertTrue(self.transport.closed)
        self.assertEqual(self.transport.commands, [])

    def test_real_gate_requires_confirmed_r1_r2_before_synchronous_command(self):
        session = self.acquire()
        with self.transport.lock:
            grant = first_grant(self.transport.raw['name'])
            leader = decode_r1(self.transport.raw['name'], 'Дом').leader
        self.assertEqual(grant.issuer_term_id, leader.term_id)
        command = self.owner.prepare_command(42, ControlKey(42, 'circuit', 10),
                                             'target-temp', {'target_temp': 22})
        with self.assertRaises(ControlOwnershipRequired):
            self.other.execute_command(command, session)
        with self.assertRaises(DeviceControlBusy):
            self.other.try_acquire_control(42, lease_seconds=10)
        self.assertTrue(self.owner.execute_command(command, session).api_accepted)
        self.assertEqual(len(self.transport.commands), 1)
        self.owner.release_control(session)
        self.clock.advance_wait()  # coordinator consumes exact finished request
        self.assertTrue(self.transport.cleared.wait(1))
        self.app.close()
        self.assertTrue(self.transport.closed)
        self.assertTrue(any(row['event'] == 'grant_ack' for row in self.observations))
        self.assertNotIn('fake-secret', repr(self.observations))
        command_rows = [row for row in self.observations if row['event'] == 'command']
        self.assertEqual(len(command_rows), 1)
        self.assertEqual(command_rows[0]['grant_id'], grant.grant_id)
        self.assertEqual(command_rows[0]['episode_id'], grant.request_id)

    def test_shutdown_interrupts_fischer_barrier_without_waiting_for_leader_lock(self):
        with self.assertRaises(DeviceControlBusy):
            self.owner.try_acquire_control(42, lease_seconds=10)
        resume, seconds = self.clock.waits.get(timeout=1)
        done = Event()
        thread = Thread(target=lambda: (self.app.close(), done.set()))
        def cleanup():
            with self.clock.lock:
                self.clock.time += seconds + 1e-8
            resume.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.addCleanup(cleanup)
        thread.start()
        self.assertTrue(done.wait(1), 'close must cancel the barrier immediately')
        self.assertTrue(self.transport.closed)

    def test_shutdown_rejects_initialization_that_finishes_after_close(self):
        entered, resume = Event(), Event()
        errors = []
        adapter = self.app._controller._adapter
        read_metadata = adapter.read_metadata
        def blocked(device_id, **kwargs):
            entered.set()
            resume.wait()
            return read_metadata(device_id, **kwargs)
        def acquire():
            try:
                self.owner.try_acquire_control(42, lease_seconds=10)
            except Exception as error:
                errors.append(error)
        thread = Thread(target=acquire)
        def cleanup():
            resume.set()
            thread.join(2)
            self.app.close(wait_timeout=2)
            self.assertFalse(thread.is_alive())
        self.addCleanup(cleanup)
        with patch.object(adapter, 'read_metadata', side_effect=blocked):
            thread.start()
            self.assertTrue(entered.wait(1))
            self.assertFalse(self.app.close(wait_timeout=.05))
            self.assertFalse(self.transport.closed)
            resume.set()
            thread.join(1)
            self.assertTrue(self.app.close(wait_timeout=1))
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], (ControlOwnershipRequired, DeviceControlBusy))
        self.assertTrue(self.transport.closed)
        self.assertTrue(all(not worker.thread.is_alive()
                            for _, worker in self.app._coordination._devices.values()))

    def test_deep_metadata_response_does_not_kill_coordinator_worker(self):
        session = self.acquire()
        opener = Mock()
        def response(request, **kwargs):
            return io.BytesIO(json.dumps(self.transport.request(request)).encode())
        opener.open.side_effect = response
        adapter = self.app._controller._adapter
        adapter._transport = HttpTransport(opener=opener)
        malformed = b'{"ok":true,"nested":' + b'[' * 20000 + b']' * 20000 + b'}'
        def fail_once(request, **kwargs):
            opener.open.side_effect = response
            return io.BytesIO(malformed)
        opener.open.side_effect = fail_once
        self.clock.advance_wait()
        scheduled = self.clock.waits.get(timeout=1)
        self.clock.waits.put(scheduled)
        worker = self.app._coordination._devices[42][1]
        self.assertTrue(worker.thread.is_alive())
        self.assertTrue(any(row['event'] == 'error' for row in self.observations))
        self.clock.advance_wait()
        scheduled = self.clock.waits.get(timeout=1)
        self.clock.waits.put(scheduled)
        self.assertTrue(worker.thread.is_alive())
        self.owner.release_control(session)

    def test_changed_leader_preserves_cached_grant_and_local_owner(self):
        session = self.acquire()
        with self.transport.lock:
            record = decode_r1(self.transport.raw['name'], 'Дом')
            changed = replace(record.leader, term_id=str(self.uuid_generator()))
            self.transport.raw['name'] = encode_r1('Дом', replace(record, leader=changed, grants=()))
        self.clock.advance_wait()
        scheduled = self.clock.waits.get(timeout=1)
        self.clock.waits.put(scheduled)
        self.assertTrue(any(row['event'] == 'leader_lost' for row in self.observations))
        self.assertFalse(self.algorithm_context.cancelled.is_set())
        command = self.owner.prepare_command(42, ControlKey(42, 'circuit', 10),
                                             'target-temp', {'target_temp': 22})
        self.assertTrue(self.owner.execute_command(command, session).api_accepted)
        with self.assertRaises(DeviceControlBusy):
            self.other.try_acquire_control(42, lease_seconds=10)
        self.assertEqual(len(self.transport.commands), 1)
        self.owner.release_control(session)

    def test_device_name_recovery_validates_exact_suffix_before_preserving_prefix(self):
        session = self.acquire()
        self.assertEqual(original_name(self.transport.raw['name']), 'Дом')
        self.assertEqual(original_name('Дом'), 'Дом')
        with self.assertRaises(RecordError):
            original_name('Дом [zc:v1 управление занято malformed]')
        self.owner.release_control(session)

    def test_invalid_bootstrap_metadata_starts_no_worker_and_sends_no_write(self):
        from tests.fakes.protocol import PROFILE
        from zontcontrol.coordination.models import R1Record, R2Record

        foreign = replace(PROFILE, profile_id=str(self.uuid_generator()))
        cases = (
            ('name', 'Дом [zc:v1 malformed]'),
            ('notes', '{"v":1}'),
            ('name', encode_r1('Дом', R1Record(foreign, str(self.uuid_generator())))),
            ('notes', encode_r2(R2Record(foreign.profile_id))),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                with self.transport.lock:
                    self.transport.raw['name'], self.transport.raw['notes'] = 'Дом', ''
                    self.transport.raw[field] = value
                    original = deepcopy(self.transport.raw)
                before = len(self.transport.requests)
                with self.assertRaises(RecordError):
                    self.owner.try_acquire_control(42, lease_seconds=10)
                self.assertEqual(self.app._coordination._devices, {})
                self.assertEqual(self.transport.commands, [])
                self.assertEqual(self.transport.raw, original)
                self.assertTrue(all(request.full_url.endswith('/devices')
                                    for request in self.transport.requests[before:]))

    def test_host_services_leader_renewals_without_extending_executor_session(self):
        session = self.acquire()
        self.app._coordination._devices[42][1].leader.renewal_fraction = .0003
        initial = decode_r1(self.transport.raw['name'], 'Дом').leader
        expires_at = session.expires_at
        for _ in range(3):
            self.clock.advance_wait()
            scheduled = self.clock.waits.get(timeout=1)
            self.clock.waits.put(scheduled)
        current = decode_r1(self.transport.raw['name'], 'Дом').leader
        self.assertEqual(current.term_id, initial.term_id)
        self.assertGreater(current.revision, initial.revision)
        self.assertIs(self.owner.try_acquire_control(42, lease_seconds=100), session)
        self.assertEqual(session.expires_at, expires_at)
        self.owner.release_control(session)

class ProfileTransitionTests(unittest.TestCase):
    """Known A→B transitions use ordinary role writers and fixed API budgets."""

    def setup_profiles(self):
        from tests.fakes.protocol import Scenario
        from zontcontrol.coordination.models import ProtocolProfile
        from zontcontrol.coordination.store import LeaderStore, ExecutorStore
        from zontcontrol.coordination.leader import LeaderElection
        from zontcontrol.coordination.grants import ExecutorGate, GrantCoordinator
        from zontcontrol.coordination.timing import LeaseTiming
        self.s = Scenario(period=60)
        s = self.s
        self.a = s.profile
        self.b = ProtocolProfile(str(s.uid()), 2, 3)
        self.store = s.make_store(LeaderStore, profile=self.b, previous=self.a)
        self.executor = s.make_store(ExecutorStore, profile=self.b, previous=self.a)
        self.leader = LeaderElection(self.store, str(s.uid()), LeaseTiming(300, 2, 3),
            clock=s.clock, uuid_factory=s.uid, epoch_sampler=s.leader.timeline.now)
        self.gate = ExecutorGate({42: self.executor}, clock=s.clock, delta=2, uuid_factory=s.uid)
        self.coordinator = GrantCoordinator(self.store, self.leader, clock=s.clock, uuid_factory=s.uid)

    def test_known_profile_waits_for_old_lease_and_tail_before_r1_then_r2(self):
        from zontcontrol.coordination.clock import clock_factor
        self.setup_profiles()
        s = self.s
        s.acquire(seconds=10)
        original = s.store.read_r1()
        s.leader.stop()  # All A processes stopped before this configured switch.
        rid, owner = s.uid(), s.uid()
        started = s.clock.now()
        with self.assertRaises(DeviceControlBusy):
            self.gate.try_acquire(owner, 42, lease_seconds=140, request_id=rid,
                                  local_deadline=started+140, attempt_deadline=started+120)
        pending = self.gate._pending[owner, 42]
        old_nonce = pending.request.nonce
        self.assertEqual(s.store.read_r2().profile_id, self.a.profile_id)
        self.assertFalse(self.leader.try_acquire())
        safe_after = (original.leader.leader_until + max(self.a.delta_seconds, self.b.delta_seconds)
                      + self.a.delta_cmd_seconds*clock_factor(self.a.delta_cmd_seconds))
        wait = safe_after-self.leader.timeline.now()
        s.advance(wait-.01, self.leader)
        self.assertFalse(self.leader.try_acquire())
        self.assertEqual(s.store.read_r1(), original)
        s.advance(.02, self.leader)
        self.assertTrue(self.leader.try_acquire())
        self.assertEqual(self.store.read_r1().profile, self.b)
        self.assertEqual(self.store.read_r2().profile_id, self.a.profile_id)
        with self.assertRaises(DeviceControlBusy):
            self.gate.try_acquire(owner, 42, lease_seconds=140, request_id=rid,
                                  local_deadline=started+1000, attempt_deadline=started+1000)
        self.assertEqual(pending.local_deadline, started+140)
        self.assertEqual(pending.request.request_id, str(rid))
        self.assertNotEqual(pending.request.nonce, old_nonce)
        self.assertEqual(self.store.read_r2().profile_id, self.b.profile_id)
        self.coordinator.step()
        with self.assertRaises(DeviceControlBusy):
            self.gate.try_acquire(owner, 42, lease_seconds=140, request_id=rid)
        s.advance(pending.interval.usable_from-s.clock.now()+.01, self.leader)
        session = self.gate.try_acquire(owner, 42, lease_seconds=140, request_id=rid)
        self.assertLessEqual(session.expires_at, started+140)
        self.gate.validate(owner, session)

    def test_interruption_between_r1_b_and_r2_b_resumes_without_erasing(self):
        from zontcontrol.coordination.clock import clock_factor
        from zontcontrol.coordination.grants import ExecutorGate
        self.setup_profiles()
        s = self.s
        s.acquire(seconds=10)
        s.leader.stop()
        self.assertFalse(self.leader.try_acquire())
        safe_after = s.leader.record.leader_until + 2 + clock_factor(1)
        s.advance(safe_after-self.leader.timeline.now()+.01, self.leader)
        self.assertTrue(self.leader.try_acquire())
        before = self.store.read_r1()
        self.leader.stop()
        restarted = ExecutorGate({42: self.executor}, clock=s.clock, delta=2, uuid_factory=s.uid)
        with self.assertRaises(DeviceControlBusy):
            restarted.try_acquire(s.uid(), 42, lease_seconds=60)
        self.assertEqual(self.store.read_r1(), before)
        self.assertEqual(self.store.read_r2().profile_id, self.b.profile_id)
        self.assertEqual(restarted._held, {})

    def test_unknown_profile_is_rejected_without_any_write(self):
        from zontcontrol.coordination.models import ProtocolProfile
        from zontcontrol.coordination.store import ExecutorStore
        self.setup_profiles()
        unknown = ProtocolProfile(str(self.s.uid()), 1, 1)
        store = self.s.make_store(ExecutorStore, profile=unknown)
        before = len(self.s.adapter.writes)
        with self.assertRaises(RecordError):
            store.read_r1()
        self.assertEqual(len(self.s.adapter.writes), before)

    def test_previous_r1_current_r2_pair_starts_no_worker_and_never_transitions(self):
        from unittest.mock import Mock
        from zontcontrol.config import CoordinationConfig
        from zontcontrol.coordination.runtime import CoordinationRuntime
        from zontcontrol.coordination.leader import LeaseLost
        from zontcontrol.coordination.models import R2Record
        for free in (False, True):
            with self.subTest(free=free):
                self.setup_profiles()
                s = self.s
                if free:
                    s.inject_r1(replace(s.store.read_r1(), record_id=str(s.uid()),
                                        leader=None, grants=()))
                s.server.apply(s.server.send_update(42, {'notes': encode_r2(R2Record(self.b.profile_id))}))
                original = s.server.read_device(42)
                port = Mock(wraps=s.adapter)
                port.read_metadata = Mock(side_effect=lambda *a, **kw: s.server.read_device(42))
                worker = Mock()
                config = CoordinationConfig(2, 300, 3, self.b.profile_id,
                                            1, 1, .1, .25, self.a)
                runtime = CoordinationRuntime(port, config, s.clock, None,
                                               worker_factory=worker, uuid_factory=s.uid)
                before = len(s.adapter.writes)
                with self.assertRaises(RecordError):
                    runtime.try_acquire(s.uid(), 42, lease_seconds=60)
                worker.assert_not_called()
                self.assertEqual(runtime._devices, {})
                if not free:
                    self.assertFalse(self.leader.try_acquire())
                    s.advance(100, self.leader)
                with self.assertRaises(LeaseLost):
                    self.leader.try_acquire()
                self.assertEqual(len(s.adapter.writes), before)
                self.assertEqual(s.server.read_device(42), original)
