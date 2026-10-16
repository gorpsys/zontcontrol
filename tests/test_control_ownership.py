"""Local command ownership with explicit, test-only coordination permission."""

import copy
import threading
import unittest
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

    def test_expired_release_racing_reacquisition_cannot_delete_new_owner(self):
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
        replacement = self.acquire(self.other)
        self.unblock.set()
        thread.join(1)
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


if __name__ == '__main__':
    unittest.main()
