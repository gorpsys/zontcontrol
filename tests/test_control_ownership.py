"""Local command ownership with explicit, test-only coordination permission."""

import copy
import threading
import unittest
from queue import Queue
from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

from tests.fakes.clock import FakeClock
from tests.test_inventory import inventory_device
from zontcontrol.controller.contracts import (
    CommandOutcomeUnknown, ControlKey, ControlOwnershipRequired, ControlSession,
    Controller, DeviceControlBusy, DeviceUnavailable, InvalidArguments, RateLimited, UnsupportedAction,
    ValidatedCommand,
)
from zontcontrol.controller.ownership import CommandExecutor
from zontcontrol.controller.service import ControllerService
from zontcontrol.coordination.statistics import Statistics


class LocalTestGate:
    """Permission for offline tests only; real applications must check R1/R2."""

    def __init__(self, clock):
        self.clock = clock
        self.allowed = True
        self.sessions = {}
        self.acquisitions = 0
        self.releases = []

    def try_acquire(self, owner, device_id, *, lease_seconds):
        self.acquisitions += 1
        session = ControlSession(device_id, uuid4(), self.clock.now() + lease_seconds)
        self.sessions[owner, session.session_id] = session
        return session

    def validate(self, owner, session):
        if not self.allowed or self.sessions.get((owner, session.session_id)) is not session:
            raise ControlOwnershipRequired()

    def release(self, owner, session):
        self.releases.append(session)
        del self.sessions[owner, session.session_id]


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(start=100)
        self.gate = LocalTestGate(self.clock)
        self.adapter = Mock()
        self.raw = inventory_device()
        self.adapter.devices.side_effect = lambda device_id=None: [
            copy.deepcopy({**self.raw, 'id': device_id or 42})]
        self.adapter.full_device.return_value = {'id': 42, 'io': {}}
        self.executor = CommandExecutor(gate=self.gate, clock=self.clock.now)
        self.service = ControllerService(self.adapter, executor=self.executor,
                                         clock=self.clock.wall_now)
        self.owner = self.service.for_consumer()
        self.other = self.service.for_consumer()
        self.threads = []
        self.errors = []
        self.unblock = threading.Event()

    def tearDown(self):
        self.unblock.set()
        for thread in self.threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(self.errors, [])

    def start(self, operation):
        def run():
            try:
                operation()
            except BaseException as error:
                self.errors.append(error)
        thread = threading.Thread(target=run)
        self.threads.append(thread)
        thread.start()
        return thread

    def command(self, owner=None, device_id=42, state=True):
        return (owner or self.owner).prepare_command(
            device_id, ControlKey(device_id, 'toggle_buttons', 31), 'trigger',
            {'target_state': state})

    def acquire(self, owner=None, device_id=42, seconds=60):
        return (owner or self.owner).try_acquire_control(device_id, lease_seconds=seconds)

    def test_bound_facade_and_reacquire_without_nesting_or_extension(self):
        self.assertIsInstance(self.owner, Controller)
        session = self.acquire()
        self.clock.advance(1)
        self.assertIs(self.acquire(seconds=600), session)
        self.assertEqual(self.gate.acquisitions, 1)
        self.owner.release_control(session)
        self.assertEqual(self.gate.releases, [session])
        self.acquire(self.other)

    def test_failed_release_blocks_until_expiry_then_allows_new_binding(self):
        session = self.acquire()
        self.gate.release = Mock(side_effect=CommandOutcomeUnknown())
        with self.assertRaises(CommandOutcomeUnknown):
            self.owner.release_control(session)
        with self.assertRaises(DeviceControlBusy):
            self.acquire(self.other)
        self.clock.advance(60)
        self.acquire(self.other)

    def test_expired_release_blocks_reacquisition_until_first_attempt_finishes(self):
        session = self.acquire()
        entered = threading.Event()
        release = self.executor._release_remote
        def delayed(held):
            entered.set()
            self.assertTrue(self.unblock.wait(2))
            release(held)
        self.executor._release_remote = delayed
        thread = self.start(lambda: self.owner.release_control(session))
        self.assertTrue(entered.wait(1))
        self.clock.advance(60)
        with self.assertRaises(DeviceControlBusy):
            self.acquire(self.other)
        self.unblock.set()
        thread.join(1)
        self.assertEqual(self.gate.releases, [session])
        replacement = self.acquire(self.other)
        self.other.execute_command(self.command(self.other), replacement)
        with self.assertRaises(DeviceControlBusy):
            self.acquire(self.owner)

    def test_command_errors_and_cleanup_are_observed_without_changing_outcomes(self):
        for error in (CommandOutcomeUnknown(), RateLimited()):
            with self.subTest(error=type(error).__name__):
                rows = []
                self.executor._statistics = Statistics(rows.append, protocol_version='1', profile='test')
                session = self.acquire()
                self.adapter.send_command.side_effect = error
                with self.assertRaises(type(error)):
                    self.owner.execute_command(self.command(state=False), session)
                self.owner.release_control(session)
                outcome = 'unknown' if isinstance(error, CommandOutcomeUnknown) else 'failure'
                self.assertTrue(any(row['event'] == 'command' and row['outcome'] == outcome
                                    for row in rows))
                self.assertTrue(any(row['event'] == 'cleanup' and row['phase'] == 'session'
                                    and row['outcome'] == outcome for row in rows))
                self.assertTrue(any(row['event'] == ('timeout' if outcome == 'unknown'
                                                     else 'rate_limited') for row in rows))
        broken = Mock()
        broken.emit.side_effect = RuntimeError('private sink error')
        self.executor._statistics = broken
        session = self.acquire()
        self.adapter.send_command.side_effect = None
        self.owner.execute_command(self.command(state=False), session)
        self.owner.release_control(session)
        self.acquire(self.other)

    def test_whole_device_immediate_busy_from_another_thread(self):
        self.acquire()
        done = threading.Event()
        def compete():
            with self.assertRaises(DeviceControlBusy):
                self.acquire(self.other)
            done.set()
        self.start(compete)
        self.assertTrue(done.wait(1))
        self.assertEqual(self.gate.acquisitions, 1)

    def test_simultaneous_acquisition_has_exactly_one_owner(self):
        barrier = threading.Barrier(3)
        results = []
        def compete(owner):
            barrier.wait(1)
            try:
                results.append(self.acquire(owner))
            except DeviceControlBusy:
                results.append('busy')
        self.start(lambda: compete(self.owner))
        self.start(lambda: compete(self.other))
        barrier.wait(1)
        for thread in self.threads:
            thread.join(1)
        self.assertEqual(results.count('busy'), 1)
        self.assertEqual(self.gate.acquisitions, 1)

    def test_foreign_forged_released_and_wrong_device_sessions_never_send(self):
        session = self.acquire()
        command = self.command()
        for owner, value, cmd in [
                (self.other, session, command), (self.owner, replace(session), command),
                (self.owner, None, command), (self.owner, session, self.command(device_id=43))]:
            with self.subTest(owner=owner, session=value), self.assertRaises(ControlOwnershipRequired):
                owner.execute_command(cmd, value)
        with self.assertRaises(ControlOwnershipRequired):
            self.other.release_control(session)
        self.owner.release_control(session)
        with self.assertRaises(ControlOwnershipRequired):
            self.owner.execute_command(command, session)
        self.adapter.send_command.assert_not_called()

    def test_reads_and_preview_while_busy_do_not_acquire(self):
        self.acquire()
        self.assertEqual(self.other.read_current(42).device_id, 42)
        self.command(self.other)
        self.assertTrue(self.other.list_controls(42))
        self.assertEqual(self.gate.acquisitions, 1)
        self.adapter.send_command.assert_not_called()

    def test_slow_command_does_not_block_another_device_or_reads(self):
        accepted = threading.Event()
        session = self.acquire()
        def send(command):
            if command.device_id == 42:
                accepted.set()
                self.assertTrue(self.unblock.wait(2))
        self.adapter.send_command.side_effect = send
        self.start(lambda: self.owner.execute_command(self.command(), session))
        self.assertTrue(accepted.wait(1))
        done = threading.Event()
        def other_device():
            other_session = self.acquire(self.other, 43)
            self.other.execute_command(self.command(self.other, 43), other_session)
            self.other.read_current(42)
            done.set()
        self.start(other_device)
        self.assertTrue(done.wait(1))

    def test_release_retains_inflight_and_new_owner_cannot_overtake(self):
        accepted = threading.Event()
        session = self.acquire()
        command = self.command()
        def send(prepared):
            accepted.set()
            self.assertTrue(self.unblock.wait(2))
        self.adapter.send_command.side_effect = send
        thread = self.start(lambda: self.owner.execute_command(command, session))
        self.assertTrue(accepted.wait(1))
        self.owner.release_control(session)
        self.assertEqual(self.gate.releases, [])
        self.clock.advance(61)
        with self.assertRaises(DeviceControlBusy):
            self.acquire(self.other)
        with self.assertRaises(ControlOwnershipRequired):
            self.owner.execute_command(command, session)
        self.unblock.set()
        thread.join(1)
        self.assertEqual(self.gate.releases, [session])
        self.acquire(self.other)

    def test_release_waits_for_every_accepted_command(self):
        session = self.acquire()
        command = self.command()
        accepted = threading.Barrier(3)
        first_can_finish = threading.Event()
        def send(prepared):
            accepted.wait(1)
            if prepared.body['target_state']:
                self.assertTrue(first_can_finish.wait(1))
            else:
                self.assertTrue(self.unblock.wait(2))
        self.adapter.send_command.side_effect = send
        first = self.start(lambda: self.owner.execute_command(command, session))
        self.start(lambda: self.owner.execute_command(self.command(state=False), session))
        accepted.wait(1)
        self.owner.release_control(session)
        first_can_finish.set()
        first.join(1)
        self.assertFalse(first.is_alive())
        self.assertEqual(self.gate.releases, [])
        with self.assertRaises(DeviceControlBusy):
            self.acquire(self.other)
        self.unblock.set()
        for thread in self.threads:
            thread.join(1)
        self.assertEqual(self.gate.releases, [session])

    def test_unconfirmed_completion_retains_ownership_without_retry(self):
        session = self.acquire()
        self.gate.release = Mock(side_effect=CommandOutcomeUnknown)
        with self.assertRaises(CommandOutcomeUnknown):
            self.owner.release_control(session)
        with self.assertRaises(DeviceControlBusy):
            self.acquire(self.other)
        with self.assertRaises(ControlOwnershipRequired):
            self.owner.execute_command(self.command(), session)
        self.gate.release.assert_called_once()
        self.adapter.send_command.assert_not_called()

    def test_timeout_stop_and_expiry_after_acceptance_do_not_cancel_command(self):
        accepted = threading.Event()
        stopped = threading.Event()
        results = []
        session = self.acquire(seconds=1)
        def send(prepared):
            accepted.set()
            self.assertTrue(self.unblock.wait(2))
        self.adapter.send_command.side_effect = send
        thread = self.start(lambda: results.append(self.owner.execute_command(self.command(), session)))
        self.assertTrue(accepted.wait(1))
        # A registrar returning Timeout only signals cancellation to its module.
        stopped.set()
        self.clock.advance(2)
        with self.assertRaises(DeviceControlBusy):
            self.acquire(self.other)
        self.unblock.set()
        thread.join(1)
        self.assertTrue(stopped.is_set())
        self.assertTrue(results[0].api_accepted)
        with self.assertRaises(ControlOwnershipRequired):
            self.owner.execute_command(self.command(state=False), session)

    def test_finally_uses_ordinary_api_even_after_module_stop_or_error(self):
        session = self.acquire()
        try:
            self.owner.execute_command(self.command(), session)
            raise RuntimeError('module stopped')
        except RuntimeError:
            pass
        finally:
            self.owner.execute_command(self.command(state=False), session)
            self.owner.release_control(session)
        self.assertEqual([call.args[0].body for call in self.adapter.send_command.call_args_list],
                         [{'target_state': True}, {'target_state': False}])

    def test_exact_expiry_boundary_and_no_command_time_reserve(self):
        session = self.acquire(seconds=10)
        self.clock.advance(9.999)
        self.owner.execute_command(self.command(), session)
        self.clock.advance(.001)
        with self.assertRaises(ControlOwnershipRequired):
            self.owner.execute_command(self.command(state=False), session)
        self.assertEqual(self.adapter.send_command.call_count, 1)
        self.assertIs(self.acquire(), session)

    def test_fresh_technical_checks_gate_and_expiry_precede_acceptance(self):
        session = self.acquire()
        command = self.owner.prepare_command(42, ControlKey(42, 'circuit', 10),
                                             'target-temp', {'target_temp': 25})
        self.raw['circuits'][0]['max'] = 24
        with self.assertRaises(InvalidArguments):
            self.owner.execute_command(command, session)
        self.raw['circuits'][0]['max'] = 30
        self.raw['circuits'][0]['is_dependent'] = True
        with self.assertRaises(UnsupportedAction):
            self.owner.execute_command(command, session)
        self.gate.allowed = False
        with self.assertRaises(ControlOwnershipRequired):
            self.owner.execute_command(self.command(), session)
        self.gate.allowed = True
        self.adapter.devices.side_effect = lambda device_id: (self.clock.advance(60) or [self.raw])
        with self.assertRaises(ControlOwnershipRequired):
            self.owner.execute_command(self.command(), session)
        self.adapter.send_command.assert_not_called()

    def test_unknown_or_presend_failure_is_not_retried_and_reservation_is_released(self):
        for error in (CommandOutcomeUnknown, DeviceUnavailable):
            with self.subTest(error=error):
                session = self.acquire()
                self.adapter.send_command.side_effect = error
                with self.assertRaises(error):
                    self.owner.execute_command(self.command(), session)
                self.owner.release_control(session)
        self.assertEqual(self.adapter.send_command.call_count, 2)

    def test_unconfigured_service_cannot_issue_an_effective_facade(self):
        service = ControllerService(self.adapter)
        with self.assertRaises(ControlOwnershipRequired):
            service.for_consumer()
        with self.assertRaises(TypeError):
            CommandExecutor(clock=self.clock.now)
        self.adapter.send_command.assert_not_called()

    def test_invalid_lease_and_previews_rejected_without_gate_or_send(self):
        for lease in (0, -1, True, float('nan'), float('inf'), '60'):
            with self.subTest(lease=lease), self.assertRaises(InvalidArguments):
                self.acquire(seconds=lease)
        for key, action, arguments, error in [
                (ControlKey(43, 'toggle_buttons', 31), 'trigger', {}, InvalidArguments),
                (ControlKey(42, 'toggle_buttons', 31), 'trigger', {'target_state': 1}, InvalidArguments),
                (ControlKey(42, 'toggle_buttons', 31), 'activate', {}, UnsupportedAction),
                (ControlKey(42, 'statuses', 33), 'trigger', {}, UnsupportedAction),
                (ControlKey(42, 'buttons', 999), 'trigger', {}, InvalidArguments)]:
            with self.subTest(key=key), self.assertRaises(error):
                self.owner.prepare_command(42, key, action, arguments)
        self.assertEqual(self.gate.acquisitions, 0)
        self.adapter.send_command.assert_not_called()

    def test_constructing_command_dto_does_not_skip_technical_checks(self):
        session = self.acquire()
        forged = ValidatedCommand(ControlKey(42, 'regulators', 32), 'set-voltage',
                                  (('target_value', 100),))
        with self.assertRaises(InvalidArguments):
            self.owner.execute_command(forged, session)
        self.adapter.send_command.assert_not_called()

    def test_offline_device_rejects_execution_but_allows_preview(self):
        session = self.acquire()
        self.raw['online'] = False
        command = self.command()
        with self.assertRaises(DeviceUnavailable):
            self.owner.execute_command(command, session)
        self.adapter.send_command.assert_not_called()

    def test_gate_pending_and_failed_acquisition_do_not_leave_local_owner(self):
        original = self.gate.try_acquire
        self.gate.try_acquire = Mock(side_effect=DeviceControlBusy)
        with self.assertRaises(DeviceControlBusy):
            self.acquire()
        self.gate.try_acquire = original
        self.acquire(self.other)

    def test_gate_checks_current_permission_on_each_command(self):
        session = self.acquire()
        command = self.command()
        self.owner.execute_command(command, session)
        self.gate.allowed = False
        with self.assertRaises(ControlOwnershipRequired):
            self.owner.execute_command(command, session)
        self.assertEqual(self.adapter.send_command.call_count, 1)
        self.owner.release_control(session)

    def test_gate_io_never_holds_global_lock_and_release_blocks_only_its_device(self):
        entered = threading.Event()
        original_acquire = self.gate.try_acquire
        def acquire(owner, device_id, *, lease_seconds):
            if device_id == 42:
                entered.set()
                self.assertTrue(self.unblock.wait(2))
            return original_acquire(owner, device_id, lease_seconds=lease_seconds)
        self.gate.try_acquire = acquire
        first = self.start(lambda: self.acquire())
        self.assertTrue(entered.wait(1))
        done = threading.Event()
        def independent():
            with self.assertRaises(DeviceControlBusy):
                self.acquire(self.other)
            session = self.acquire(self.other, 43)
            self.other.release_control(session)
            self.other.read_current(42)
            done.set()
        self.start(independent)
        self.assertTrue(done.wait(1))
        self.unblock.set()
        first.join(1)
        self.unblock.clear()
        entered.clear()
        original_release = self.gate.release
        def release(owner, session):
            entered.set()
            self.assertTrue(self.unblock.wait(2))
            return original_release(owner, session)
        self.gate.release = release
        self.start(lambda: self.owner.release_control(self.acquire()))
        self.assertTrue(entered.wait(1))
        done.clear()
        def independent_during_release():
            with self.assertRaises(DeviceControlBusy):
                self.acquire(self.other)
            self.acquire(self.other, 43)
            self.other.read_current(42)
            done.set()
        self.start(independent_during_release)
        self.assertTrue(done.wait(1))
        self.unblock.set()

    def test_release_during_validation_prevents_send_and_retains_reservation(self):
        session = self.acquire()
        command = self.command()
        entered = threading.Event()
        def validate(owner, value):
            entered.set()
            self.assertTrue(self.unblock.wait(2))
        self.gate.validate = validate
        rejected = threading.Event()
        def execute():
            with self.assertRaises(ControlOwnershipRequired):
                self.owner.execute_command(command, session)
            rejected.set()
        thread = self.start(execute)
        self.assertTrue(entered.wait(1))
        self.owner.release_control(session)
        self.other.read_current(42)
        with self.assertRaises(DeviceControlBusy):
            self.acquire(self.other)
        self.unblock.set()
        thread.join(1)
        self.assertTrue(rejected.is_set())
        self.acquire(self.other)
        self.adapter.send_command.assert_not_called()

    def test_public_action_mapping_and_success_do_not_imply_observed_state(self):
        session = self.acquire()
        for kind, element_id, action, arguments, expected in [
                ('circuit', 10, 'target-temp', {'target_temp': 22}, 'set-temp'),
                ('mode', 20, 'activate', {'duration': 10}, 'activate-mode'),
                ('buttons', 30, 'trigger', {}, 'press-button'),
                ('toggle_buttons', 31, 'trigger', {'target_state': False}, 'set-switch'),
                ('regulators', 32, 'set-voltage', {'target_value': 3}, 'set-regulator'),
                ('scenarios', 50, 'activate', {}, 'run-scenario'),
                ('guard_zones', 60, 'activate', {'enable': True}, 'set-guard')]:
            command = self.owner.prepare_command(42, ControlKey(42, kind, element_id),
                                                 action, arguments)
            with self.subTest(kind=kind):
                result = self.owner.execute_command(command, session)
                sent = self.adapter.send_command.call_args.args[0]
                self.assertEqual((sent.action, sent.body), (expected, arguments))
                self.assertTrue(result.api_accepted)
                self.assertIsNone(result.observed_state)
                self.assertEqual(result.received_at, self.clock.wall_now())


class ExecutorShutdownTests(unittest.TestCase):
    def setUp(self):
        OwnershipTests.setUp(self)
        self.stop_waiting = threading.Event()

    start = OwnershipTests.start
    command = OwnershipTests.command
    acquire = OwnershipTests.acquire

    def tearDown(self):
        self.unblock.set()
        for event in getattr(self, 'extra_unblocks', []):
            event.set()
        for thread in self.threads:
            thread.join(2)
        # Also clean up after a failed assertion, before joining drain waiters.
        with self.executor._lock:
            held_sessions = list(self.executor._devices.values())
        for held in held_sessions:
            if held.session is not None and not held.closing:
                held.binding.release_control(held.session)
        self.clock.advance(10000)
        self.stop_waiting.set()
        if hasattr(self.executor, '_idle'):
            with self.executor._idle:
                self.executor._idle.notify_all()
        for thread in getattr(self, 'waiters', []):
            thread.join(2)
            self.assertFalse(thread.is_alive())
        for thread in self.threads:
            self.assertFalse(thread.is_alive())
        self.assertEqual(self.errors, [])

    def wait_for_idle(self):
        """Observe actual Condition waits, without guessing thread scheduling."""
        waiting = Queue()
        done = threading.Event()
        original = self.executor._idle.wait
        def wait(timeout=None):
            waiting.put(timeout)
            result = original(timeout)
            if self.stop_waiting.is_set():
                raise InterruptedError('test teardown')
            return result
        self.executor._idle.wait = wait
        def drain():
            try:
                self.executor.wait_idle()
                done.set()
            except InterruptedError:
                if not self.stop_waiting.is_set():
                    raise
            except BaseException as error:
                self.errors.append(error)
        thread = threading.Thread(target=drain)
        self.waiters = getattr(self, 'waiters', []) + [thread]
        thread.start()
        waiting.get(timeout=1)
        self.assertFalse(done.is_set())
        return done, waiting

    def test_close_rejects_new_consumers_and_acquisitions_but_allows_cleanup(self):
        session = self.acquire()
        self.executor.begin_close()
        self.executor.begin_close()
        with self.assertRaises(ControlOwnershipRequired):
            self.service.for_consumer()
        for owner, device in ((self.owner, 42), (self.other, 42), (self.other, 43)):
            with self.subTest(device=device), self.assertRaises(ControlOwnershipRequired):
                self.acquire(owner, device)
        self.assertEqual(self.gate.acquisitions, 1)
        self.owner.read_current(42)
        self.owner.execute_command(self.command(state=False), session)
        self.owner.release_control(session)
        self.executor.wait_idle()
        self.assertEqual(self.gate.releases, [session])
        self.adapter.send_command.assert_called_once()

    def test_empty_bindings_do_not_delay_idle(self):
        self.executor.begin_close()
        self.executor.wait_idle()
        self.assertEqual(self.gate.acquisitions, 0)

    def test_idle_waits_for_session_between_commands(self):
        session = self.acquire()
        self.executor.begin_close()
        done, _ = self.wait_for_idle()
        self.owner.execute_command(self.command(state=False), session)
        self.assertFalse(done.is_set())
        self.owner.release_control(session)
        self.assertTrue(done.wait(1))

    def assert_phase_drained(self, phase):
        session = self.acquire()
        command = self.command()
        entered = threading.Event()
        target, method = {
            'prepare': (self.service, '_prepare'),
            'validate': (self.gate, 'validate'),
            'send': (self.adapter, 'send_command'),
            'release': (self.gate, 'release'),
        }[phase]
        original = getattr(target, method)
        def block(*args, **kwargs):
            entered.set()
            self.assertTrue(self.unblock.wait(2))
            return original(*args, **kwargs)
        setattr(target, method, block)
        def operation():
            if phase == 'release':
                self.owner.release_control(session)
            else:
                try:
                    self.owner.execute_command(command, session)
                except ControlOwnershipRequired:
                    if phase not in ('prepare', 'validate'):
                        raise
        thread = self.start(operation)
        self.assertTrue(entered.wait(1))
        self.executor.begin_close()
        done, _ = self.wait_for_idle()
        if phase != 'release':
            self.owner.release_control(session)
        self.clock.advance(60)
        self.assertFalse(done.is_set())
        self.unblock.set()
        thread.join(1)
        self.assertTrue(done.wait(1))
        self.assertEqual(self.gate.releases, [session])

    def test_idle_waits_through_technical_validation(self):
        self.assert_phase_drained('prepare')

    def test_idle_waits_through_gate_validation(self):
        self.assert_phase_drained('validate')

    def test_idle_waits_through_send(self):
        self.assert_phase_drained('send')

    def test_idle_waits_through_remote_release_even_after_expiry(self):
        self.assert_phase_drained('release')

    def assert_pending_release_drained(self, *, deferred):
        session = self.acquire(seconds=1)
        release_pending = threading.Event()
        original_release = self.executor._release_remote
        self.gate.release = Mock(wraps=self.gate.release)
        rows = []
        self.executor._statistics = Statistics(rows.append, protocol_version='1', profile='test')
        def release(held):
            release_pending.set()
            self.assertTrue(self.unblock.wait(2))
            original_release(held)
        self.executor._release_remote = release
        if deferred:
            command_entered = threading.Event()
            command_can_finish = threading.Event()
            self.extra_unblocks = [command_can_finish]
            def send(prepared):
                command_entered.set()
                self.assertTrue(command_can_finish.wait(2))
            self.adapter.send_command.side_effect = send
            thread = self.start(lambda: self.owner.execute_command(self.command(), session))
            self.assertTrue(command_entered.wait(1))
            self.owner.release_control(session)
            self.clock.advance(1)
            command_can_finish.set()
        else:
            self.clock.advance(1)
            thread = self.start(lambda: self.owner.release_control(session))
        self.assertTrue(release_pending.wait(1))
        self.executor.begin_close()
        self.gate.release.assert_not_called()
        done, _ = self.wait_for_idle()
        self.unblock.set()
        thread.join(1)
        self.assertTrue(done.wait(1))
        self.gate.release.assert_called_once()
        self.assertEqual(self.gate.releases, [session])
        self.assertEqual(len([row for row in rows if row['event'] == 'cleanup'
                              and row['phase'] == 'session']), 1)

    def test_idle_waits_before_first_release_attempt_for_expired_session(self):
        self.assert_pending_release_drained(deferred=False)

    def test_idle_waits_before_deferred_release_attempt_after_expired_command(self):
        self.assert_pending_release_drained(deferred=True)

    def test_idle_waits_for_both_commands_and_every_device(self):
        session = self.acquire()
        other = self.acquire(self.other, 43)
        command = self.command()
        commands_entered = threading.Barrier(3)
        first_can_finish = threading.Event()
        self.extra_unblocks = [first_can_finish]
        def send(prepared):
            commands_entered.wait(1)
            event = first_can_finish if prepared.body['target_state'] else self.unblock
            self.assertTrue(event.wait(2))
        self.adapter.send_command.side_effect = send
        first = self.start(lambda: self.owner.execute_command(command, session))
        second = self.start(lambda: self.owner.execute_command(self.command(state=False), session))
        commands_entered.wait(1)
        self.executor.begin_close()
        done, _ = self.wait_for_idle()
        self.owner.release_control(session)
        first_can_finish.set()
        first.join(1)
        self.assertEqual(self.gate.releases, [])
        self.assertFalse(done.is_set())
        self.unblock.set()
        second.join(1)
        self.assertEqual(self.gate.releases, [session])
        self.assertFalse(done.is_set())
        self.other.release_control(other)
        self.assertTrue(done.wait(1))

    def test_late_gate_session_is_only_released_and_never_issued(self):
        gate_entered = threading.Event()
        gate_can_return = threading.Event()
        release_entered = threading.Event()
        rejected = threading.Event()
        self.extra_unblocks = [gate_can_return]
        original_acquire = self.gate.try_acquire
        original_release = self.gate.release
        def acquire(*args, **kwargs):
            session = original_acquire(*args, **kwargs)
            gate_entered.set()
            self.assertTrue(gate_can_return.wait(2))
            return session
        def release(*args):
            release_entered.set()
            self.assertTrue(self.unblock.wait(2))
            return original_release(*args)
        self.gate.try_acquire = acquire
        self.gate.release = release
        def acquire_late():
            with self.assertRaises(ControlOwnershipRequired):
                self.acquire()
            rejected.set()
        thread = self.start(acquire_late)
        self.assertTrue(gate_entered.wait(1))
        self.executor.begin_close()
        done, _ = self.wait_for_idle()
        gate_can_return.set()
        self.assertTrue(release_entered.wait(1))
        self.assertFalse(done.is_set())
        # A second shutdown enters the lock while release I/O is blocked.
        lock_free = threading.Event()
        self.start(lambda: (self.executor.begin_close(), lock_free.set()))
        self.assertTrue(lock_free.wait(1))
        self.assertFalse(rejected.is_set())
        self.unblock.set()
        thread.join(1)
        self.assertTrue(rejected.is_set())
        self.assertTrue(done.wait(1))
        self.assertEqual(len(self.gate.releases), 1)

    def assert_gate_failure_drained(self, error):
        entered = threading.Event()
        def acquire(*args, **kwargs):
            entered.set()
            self.assertTrue(self.unblock.wait(2))
            raise error
        self.gate.try_acquire = acquire
        def fail():
            with self.assertRaises(type(error)):
                self.acquire()
        thread = self.start(fail)
        self.assertTrue(entered.wait(1))
        self.executor.begin_close()
        done, _ = self.wait_for_idle()
        self.unblock.set()
        thread.join(1)
        self.assertTrue(done.wait(1))
        self.assertEqual(self.gate.releases, [])

    def test_pending_gate_busy_releases_local_reservation_during_shutdown(self):
        self.assert_gate_failure_drained(DeviceControlBusy())

    def test_pending_gate_error_releases_local_reservation_during_shutdown(self):
        self.assert_gate_failure_drained(DeviceUnavailable())

    def test_acquisition_committed_before_close_survives_delayed_return(self):
        committed = threading.Event()
        sessions = []
        def observation_context(*args):
            committed.set()
            self.assertTrue(self.unblock.wait(2))
            return {}
        self.gate.observation_context = observation_context
        thread = self.start(lambda: sessions.append(self.acquire()))
        self.assertTrue(committed.wait(1))
        self.executor.begin_close()
        done, _ = self.wait_for_idle()
        self.unblock.set()
        thread.join(1)
        self.assertEqual(len(sessions), 1)
        self.assertFalse(done.is_set())
        self.assertEqual(self.gate.releases, [])
        self.owner.execute_command(self.command(state=False), sessions[0])
        self.owner.release_control(sessions[0])
        self.assertTrue(done.wait(1))

    def assert_failed_release_expires(self, error):
        session = self.acquire()
        self.gate.release = Mock(side_effect=error)
        with self.assertRaises(type(error)):
            self.owner.release_control(session)
        self.executor.begin_close()
        done, waits = self.wait_for_idle()
        with self.executor._lock:
            self.assertIs(self.executor._devices[42].session, session)
        with self.assertRaises(ControlOwnershipRequired):
            self.owner.execute_command(self.command(state=False), session)
        # Advance the injected lease clock; no acquire/notify should be needed
        # to recheck expiry. The Condition itself must use a bounded wait.
        self.clock.advance(60)
        self.assertTrue(done.wait(1))
        self.gate.release.assert_called_once()
        self.adapter.send_command.assert_not_called()

    def test_unknown_release_is_retained_until_expiry_without_retry(self):
        self.assert_failed_release_expires(CommandOutcomeUnknown())

    def test_failed_release_is_retained_until_expiry_without_retry(self):
        self.assert_failed_release_expires(DeviceUnavailable())

    def test_expiry_does_not_discard_session_before_owner_release(self):
        session = self.acquire()
        self.executor.begin_close()
        done, waits = self.wait_for_idle()
        self.clock.advance(60)
        # Explicit notification models a spurious wakeup, not a release.
        with self.executor._idle:
            self.executor._idle.notify_all()
        waits.get(timeout=1)
        self.assertFalse(done.is_set())
        with self.assertRaises(ControlOwnershipRequired):
            self.owner.execute_command(self.command(state=False), session)
        self.assertEqual(self.gate.releases, [])
        self.owner.release_control(session)
        self.assertTrue(done.wait(1))
        self.assertEqual(self.gate.releases, [session])

    def test_late_acquisition_unknown_release_drains_only_at_local_expiry(self):
        entered = threading.Event()
        rejected = threading.Event()
        original = self.gate.try_acquire
        def acquire(*args, **kwargs):
            session = original(*args, **kwargs)
            entered.set()
            self.assertTrue(self.unblock.wait(2))
            return session
        self.gate.try_acquire = acquire
        self.gate.release = Mock(side_effect=CommandOutcomeUnknown())
        def acquire_late():
            with self.assertRaises(CommandOutcomeUnknown):
                self.acquire()
            rejected.set()
        thread = self.start(acquire_late)
        self.assertTrue(entered.wait(1))
        self.executor.begin_close()
        done, _ = self.wait_for_idle()
        self.unblock.set()
        thread.join(1)
        self.assertTrue(rejected.is_set())
        self.assertFalse(done.is_set())
        with self.executor._lock:
            held = self.executor._devices[42]
            self.assertTrue(held.closing)
            self.assertFalse(held.releasing)
        self.gate.release.assert_called_once()
        self.clock.advance(60)
        self.assertTrue(done.wait(1))
        self.gate.release.assert_called_once()

    def test_failed_release_after_last_accepted_command_drains_at_expiry(self):
        entered = threading.Event()
        session = self.acquire()
        def send(prepared):
            entered.set()
            self.assertTrue(self.unblock.wait(2))
        self.adapter.send_command.side_effect = send
        self.gate.release = Mock(side_effect=CommandOutcomeUnknown())
        def execute():
            with self.assertRaises(CommandOutcomeUnknown):
                self.owner.execute_command(self.command(), session)
        thread = self.start(execute)
        self.assertTrue(entered.wait(1))
        self.executor.begin_close()
        done, _ = self.wait_for_idle()
        self.owner.release_control(session)
        self.clock.advance(60)
        self.assertFalse(done.is_set())
        self.gate.release.assert_not_called()
        self.unblock.set()
        thread.join(1)
        self.assertTrue(done.wait(1))
        self.gate.release.assert_called_once()

    def test_late_expired_gate_session_still_completes_cleanup(self):
        entered = threading.Event()
        original = self.gate.try_acquire
        def acquire(*args, **kwargs):
            session = original(*args, **kwargs)
            entered.set()
            self.assertTrue(self.unblock.wait(2))
            return session
        self.gate.try_acquire = acquire
        def acquire_late():
            with self.assertRaises(ControlOwnershipRequired):
                self.acquire()
        thread = self.start(acquire_late)
        self.assertTrue(entered.wait(1))
        self.executor.begin_close()
        done, _ = self.wait_for_idle()
        self.clock.advance(60)
        self.unblock.set()
        thread.join(1)
        self.assertTrue(done.wait(1))
        self.assertEqual(len(self.gate.releases), 1)
        self.assertEqual(self.gate.sessions, {})


if __name__ == '__main__':
    unittest.main()
