"""Full composition over fake HTTP and manually released clock waits."""

from copy import deepcopy
from dataclasses import replace
import io
import json
from queue import Queue
from threading import Event, Lock, Thread
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from tests.test_application import settings
from tests.test_inventory import inventory_device
from zontcontrol.application import create_application
from zontcontrol.autonomy.contracts import AlgorithmSpec, Mode
from zontcontrol.controller.contracts import ControlKey, ControlOwnershipRequired, DeviceControlBusy
from zontcontrol.controller.transport import HttpTransport
from zontcontrol.coordination.models import RecordError, decode_r1, decode_r2, encode_r1, original_name


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

    def request(self, request):
        body = json.loads(request.data) if request.data is not None else None
        path = urlsplit(request.full_url).path
        with self.lock:
            if path.endswith('/update_device'):
                self.raw.update({key: value for key, value in body.items() if key != 'device_id'})
                if 'name' in body:
                    (self.cleared if body['name'] == 'Дом' else self.granted).set()
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
        self.clock = StepClock()
        self.transport = MemoryTransport()
        self.observations = []
        data = settings()
        data['config/registry.json']['once_timeouts'] = {}
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
            statistics_sink=self.observations.append)
        self.owner = self.app.controller()
        self.other = self.app.controller()
        self.addCleanup(self.app.close)
        self.assertTrue(entered.wait(1))
        self.algorithm_context = contexts[0]

    def acquire(self):
        with self.assertRaises(DeviceControlBusy):
            self.owner.try_acquire_control(42, lease_seconds=10)
        self.clock.advance_wait()  # candidate's Fischer barrier
        # Synchronize on the next poll: its preceding step/readback is complete.
        # R1 is deliberately RMW, so the first request may race the candidate's
        # initial write. The executor's next explicit attempt republishes it.
        for _ in range(2):
            scheduled = self.clock.waits.get(timeout=1)
            self.clock.waits.put(scheduled)
            try:
                return self.owner.try_acquire_control(42, lease_seconds=10)
            except DeviceControlBusy:
                self.clock.advance_wait()
        self.fail('Offline grant was not acknowledged after explicit reconciliation')

    def test_real_gate_requires_confirmed_r1_r2_before_synchronous_command(self):
        session = self.acquire()
        with self.transport.lock:
            grant = decode_r2(self.transport.raw['name'], 'Дом')
            leader = decode_r1(self.transport.raw['notes']).leader
        self.assertEqual(grant.leader_token, leader.token)
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
        def blocked(device_id):
            entered.set()
            resume.wait()
            return read_metadata(device_id)
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

    def test_changed_leader_rejects_new_send_but_does_not_release_local_owner(self):
        session = self.acquire()
        with self.transport.lock:
            record = decode_r1(self.transport.raw['notes'])
            changed = replace(record.leader, token='a' * 32)
            self.transport.raw['notes'] = encode_r1(replace(record, leader=changed))
        self.clock.advance_wait()
        scheduled = self.clock.waits.get(timeout=1)
        self.clock.waits.put(scheduled)
        self.assertTrue(any(row['event'] == 'leader_lost' for row in self.observations))
        self.assertFalse(self.algorithm_context.cancelled.is_set())
        command = self.owner.prepare_command(42, ControlKey(42, 'circuit', 10),
                                             'target-temp', {'target_temp': 22})
        with self.assertRaises(ControlOwnershipRequired):
            self.owner.execute_command(command, session)
        with self.assertRaises(DeviceControlBusy):
            self.other.try_acquire_control(42, lease_seconds=10)
        self.assertEqual(self.transport.commands, [])
        self.owner.release_control(session)

    def test_device_name_recovery_validates_exact_suffix_before_preserving_prefix(self):
        session = self.acquire()
        self.assertEqual(original_name(self.transport.raw['name']), 'Дом')
        self.assertEqual(original_name('Дом'), 'Дом')
        with self.assertRaises(RecordError):
            original_name('Дом [zc:v1 управление занято malformed]')
        self.owner.release_control(session)

    def test_host_services_leader_renewals_without_extending_executor_session(self):
        session = self.acquire()
        initial = decode_r1(self.transport.raw['notes']).leader
        expires_at = session.expires_at
        for _ in range(3):
            self.clock.advance_wait()
            scheduled = self.clock.waits.get(timeout=1)
            self.clock.waits.put(scheduled)
        current = decode_r1(self.transport.raw['notes']).leader
        self.assertEqual(current.token, initial.token)
        self.assertGreater(current.seq, initial.seq)
        self.assertIs(self.owner.try_acquire_control(42, lease_seconds=100), session)
        self.assertEqual(session.expires_at, expires_at)
        self.owner.release_control(session)
