"""Executor leases on offline, independent participants and relative clocks."""

from dataclasses import replace
import unittest
from unittest.mock import patch
from uuid import uuid4

from tests.fakes.clock import FakeClock
from tests.fakes.zont import FakeMetadataAdapter, FakeZontServer
from tests.test_coordination_store import OWNER, PREFIX
from zontcontrol.controller.contracts import (
    CommandOutcomeUnknown, ControlOwnershipRequired, DeviceControlBusy,
)
from zontcontrol.controller.ownership import CommandExecutor
from zontcontrol.coordination.clock import LeaseTiming, clock_factor, clock_rho
from zontcontrol.coordination.grants import ExecutorGate, GrantCoordinator
from zontcontrol.coordination.leader import LeaderElection, LeaseLost
from zontcontrol.coordination.models import R1Record
from zontcontrol.coordination.store import ExecutorStore, LeaderStore


class GrantTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(start=100)
        self.server = FakeZontServer({42: {'name': PREFIX, 'notes': None},
                                      43: {'name': PREFIX, 'notes': None}}, clock=self.clock)
        self.adapter = FakeMetadataAdapter(self.server)
        self.store = self.make_store(LeaderStore)
        self.leader = LeaderElection(self.store, OWNER, LeaseTiming(1000, 1), clock=self.clock)
        self.assertTrue(self.leader.try_acquire())
        self.coordinator = GrantCoordinator(self.store, self.leader, clock=self.clock)
        self.owner = uuid4()
        self.gate = self.make_gate()

    def make_store(self, cls, adapter=None, device_id=42):
        return cls(adapter or self.adapter, device_id, original_name=PREFIX,
                   notes_capacity=10000, name_capacity=10000)

    def make_gate(self, clock=None, adapter=None):
        return ExecutorGate({42: self.make_store(ExecutorStore, adapter)},
                            clock=clock or self.clock, delta=1)

    def pending(self, gate=None, owner=None, seconds=60):
        with self.assertRaises(DeviceControlBusy):
            (gate or self.gate).try_acquire(owner or self.owner, 42, lease_seconds=seconds)

    def acquire(self, gate=None, owner=None, seconds=60):
        gate, owner = gate or self.gate, owner or self.owner
        self.pending(gate, owner, seconds)
        self.coordinator.step()
        return gate.try_acquire(owner, 42, lease_seconds=seconds)

    def test_request_grant_heartbeat_finished_and_release(self):
        session = self.acquire()
        first = self.store.read_r2()
        self.assertEqual(first.owner, str(self.owner))
        self.assertEqual(first.nonce, self.store.read_r1().request.nonce)
        self.gate.validate(self.owner, session)
        self.clock.advance(10)
        with self.assertRaises(DeviceControlBusy):
            self.gate.renew(self.owner, session, lease_seconds=60)
        self.assertEqual(self.store.read_r1().request.state, 'heartbeat')
        self.coordinator.step()
        renewed = self.gate.renew(self.owner, session, lease_seconds=60)
        current = self.store.read_r2()
        self.assertEqual(current.grant_id, first.grant_id)
        self.assertEqual(current.revision, first.revision + 1)
        self.assertNotEqual(current.nonce, first.nonce)
        self.assertGreater(renewed.expires_at, session.expires_at)
        self.gate.validate(self.owner, renewed)
        with self.assertRaises(ControlOwnershipRequired):
            self.gate.validate(self.owner, session)
        self.gate.release(self.owner, renewed)
        self.assertEqual(self.store.read_r1().request.state, 'finished')
        self.coordinator.step()
        self.assertIsNone(self.store.read_r2())
        self.assertEqual(self.server.read_device(42)['name'], PREFIX)

    def test_shutdown_settles_completion_without_new_grant_or_renewal(self):
        self.pending()
        self.coordinator.step(accepting=False)
        self.assertIsNone(self.store.read_r2())
        self.coordinator.step()
        session = self.gate.try_acquire(self.owner, 42, lease_seconds=60)
        granted = self.store.read_r2()
        with self.assertRaises(DeviceControlBusy):
            self.gate.renew(self.owner, session, lease_seconds=60)
        self.coordinator.step(accepting=False)
        self.assertEqual(self.store.read_r2(), granted)
        self.gate.release(self.owner, session)
        self.coordinator.step(accepting=False)
        self.assertIsNone(self.store.read_r2())

    def test_expired_request_is_consumed_and_never_regranted(self):
        self.pending()
        grant = self.coordinator.step()
        self.coordinator.step()  # start conservative observation
        self.clock.advance(grant.lease_s)
        for _ in range(4):
            self.assertIsNone(self.coordinator.step())
        self.assertIsNone(self.store.read_r1().request)

    def test_old_owner_records_are_pruned_without_losing_active_or_uncertain_release(self):
        session = self.acquire()
        self.pending(owner=uuid4(), seconds=10)
        self.assertIn((self.owner, 42), self.gate._held)
        self.adapter.write_mode = 'lose_response'
        with self.assertRaises(CommandOutcomeUnknown):
            self.gate.release(self.owner, session)
        self.adapter.write_mode = 'apply'
        abandoned = uuid4()
        self.pending(owner=abandoned, seconds=10)
        self.assertIn((self.owner, 42), self.gate._held)
        self.clock.advance(61)
        self.pending(owner=uuid4())
        self.assertNotIn((self.owner, 42), self.gate._held)
        self.assertNotIn((abandoned, 42), self.gate._pending)

    def test_independent_applications_busy_explicit_pending_retry_no_queue(self):
        session = self.acquire()
        other, owner = self.make_gate(), uuid4()
        self.pending(other, owner)
        waiting = self.store.read_r1().request
        self.coordinator.step()
        self.assertEqual(self.store.read_r2().owner, str(self.owner))
        # A request overwritten in R1 is republished only by an explicit call.
        self.gate.release(self.owner, session)
        self.coordinator.step()
        self.assertIsNone(self.store.read_r2())
        self.pending(other, owner)
        self.assertEqual(self.store.read_r1().request, waiting)
        self.coordinator.step()
        acquired = other.try_acquire(owner, 42, lease_seconds=60)
        other.validate(owner, acquired)

    def test_rereading_and_reacquiring_do_not_extend_nonce_send_deadline(self):
        sent = self.clock.now()
        session = self.acquire()
        self.assertAlmostEqual(session.expires_at, sent + 60)
        self.clock.advance(20)
        self.assertIs(self.gate.try_acquire(self.owner, 42, lease_seconds=600), session)
        self.gate.validate(self.owner, session)
        self.assertEqual(session.expires_at, sent + 60)

    def test_same_owner_can_request_again_after_unreleased_session_expires(self):
        previous = self.acquire(seconds=10)
        self.coordinator.step()  # Start the conservative remote expiry interval.
        self.clock.advance(20)
        replacement = self.acquire()
        self.assertIsNot(replacement, previous)
        self.gate.validate(self.owner, replacement)
        with self.assertRaises(ControlOwnershipRequired):
            self.gate.validate(self.owner, previous)

    def test_unknown_nonce_or_wrong_owner_request_grant_token_revision_never_authorizes(self):
        for changes in ({'owner': str(uuid4())}, {'request_id': str(uuid4())},
                        {'grant_id': str(uuid4())}, {'leader_token': 'd' * 32},
                        {'nonce': str(uuid4())}, {'revision': 2}):
            self.setUp()
            session = self.acquire()
            self.store.write_r2(replace(self.store.read_r2(), **changes))
            with self.subTest(changes=changes), self.assertRaises(ControlOwnershipRequired):
                self.gate.validate(self.owner, session)
        with self.assertRaises(ControlOwnershipRequired):
            self.gate.validate(uuid4(), session)

    def test_missing_heartbeat_does_not_revoke_or_replace_grant(self):
        session = self.acquire()
        granted = self.store.read_r2()
        self.store.write_r1(R1Record(self.leader.record))
        self.coordinator.step()
        self.assertEqual(self.store.read_r2(), granted)
        self.gate.validate(self.owner, session)

    def test_late_ack_and_process_restart_never_create_right(self):
        self.pending()
        self.coordinator.step()
        self.clock.advance(60)
        self.pending()
        restarted = self.make_gate()
        self.pending(restarted, uuid4())
        self.assertEqual(self.store.read_r2().owner, str(self.owner))

    def test_all_drift_boundaries_use_original_period_not_scaled_lease(self):
        for period in (59.99, 60, 60.01, 299.99, 300, 300.01, 599.99, 600, 600.01):
            self.setUp()
            sent = self.clock.now()
            session = self.acquire(seconds=period)
            grant = self.store.read_r2()
            with self.subTest(period=period):
                self.assertEqual(grant.clock_rho, clock_rho(period))
                self.assertAlmostEqual(grant.lease_s, period * clock_factor(period))
                self.assertAlmostEqual(session.expires_at, sent + period)
                self.assertEqual(self.coordinator.window, 1)  # independent short Delta

    def test_opposite_drift_bands_hold_old_owner_until_observer_wait(self):
        for period, rho in ((60, .1), (300, .05), (600, .01), (700, .005)):
            for sign in (-1, 1):
                self.setUp()
                holder = FakeClock(start=5000, rate=1 + sign * rho)
                observer = FakeClock(start=20, rate=1 - sign * rho)
                gate = self.make_gate(holder)
                session = self.acquire(gate, seconds=period)
                coordinator = GrantCoordinator(self.store, self.leader, clock=observer)
                coordinator.step()
                other, owner = self.make_gate(), uuid4()
                self.pending(other, owner, seconds=900)
                elapsed = period * clock_factor(period) / (1 - sign * rho)
                holder.advance(elapsed)
                observer.advance(elapsed)
                self.clock.advance(elapsed)
                self.assertGreaterEqual(holder.now() + 1e-9, session.expires_at)
                self.assertAlmostEqual(observer.now() - 20, period * clock_factor(period))
                coordinator.step()
                self.assertEqual(self.store.read_r2().owner, str(owner))
                with self.assertRaises(ControlOwnershipRequired):
                    gate.validate(self.owner, session)

    def test_observer_interval_first_response_to_final_read_send_and_revision_reset(self):
        self.acquire(seconds=10)
        self.coordinator.step()  # begin observation of the whole grant
        first = self.clock.now()
        lease = self.store.read_r2().lease_s
        other, owner = self.make_gate(), uuid4()
        self.pending(other, owner, seconds=30)
        self.clock.advance(lease - .05)
        read_r2 = self.store.read_r2
        def delayed_r2():
            self.clock.advance(.1)
            return read_r2()
        with patch.object(self.store, 'read_r2', side_effect=delayed_r2):
            self.coordinator.step()
        self.assertGreater(self.clock.now() - first, lease)
        self.assertNotEqual(self.store.read_r2().owner, str(owner))
        self.coordinator.step()
        self.assertEqual(self.store.read_r2().owner, str(owner))
        # Full-value/revision change restarts observation, even after old L.
        grant = self.store.read_r2()
        self.coordinator.step()
        self.clock.advance(grant.lease_s)
        self.store.write_r2(replace(grant, revision=grant.revision + 1))
        self.coordinator.step()
        self.assertIsNotNone(self.store.read_r2())

    def test_finished_is_bound_to_exact_request_grant_revision_and_nonce(self):
        session = self.acquire()
        self.gate.release(self.owner, session)
        finished = self.store.read_r1().request
        granted = self.store.read_r2()
        for changes in ({'grant_id': str(uuid4())}, {'request_id': str(uuid4())},
                        {'grant_revision': 123}, {'nonce': str(uuid4())}):
            self.store.write_r1(R1Record(self.leader.record, replace(finished, **changes)))
            self.coordinator.step()
            self.assertEqual(self.store.read_r2(), granted)
        self.store.write_r1(R1Record(self.leader.record, finished))
        self.coordinator.step()
        self.assertIsNone(self.store.read_r2())

    def test_new_leader_observes_existing_grant_and_checks_current_token(self):
        session = self.acquire()
        granted = self.store.read_r2()
        self.leader.release()
        other = LeaderElection(self.store, str(uuid4()), LeaseTiming(1000, 1), clock=self.clock)
        self.assertTrue(other.try_acquire())
        new = GrantCoordinator(self.store, other, clock=self.clock)
        self.pending(self.make_gate(), uuid4())
        new.step()
        self.assertEqual(self.store.read_r2(), granted)
        with self.assertRaises(ControlOwnershipRequired):
            self.gate.validate(self.owner, session)
        # Losing the coordinator alone does not cancel a still matching grant.
        self.setUp()
        session = self.acquire()
        self.leader.stop()
        self.gate.validate(self.owner, session)

    def test_unknown_renewal_is_not_blindly_retried_and_old_ack_is_not_renewal(self):
        session = self.acquire()
        self.adapter.write_mode = 'lose_response'
        before = len(self.adapter.writes)
        with self.assertRaises(CommandOutcomeUnknown):
            self.gate.renew(self.owner, session, lease_seconds=60)
        self.assertEqual(len(self.adapter.writes), before + 1)
        self.adapter.write_mode = 'apply'
        with self.assertRaises(DeviceControlBusy):
            self.gate.renew(self.owner, session, lease_seconds=60)
        self.assertEqual(len(self.adapter.writes), before + 1)
        self.coordinator.step()
        renewed = self.gate.renew(self.owner, session, lease_seconds=60)
        self.assertIsNot(renewed, session)

    def test_replayed_heartbeat_cannot_renew_newer_revision(self):
        session = self.acquire()
        with self.assertRaises(DeviceControlBusy):
            self.gate.renew(self.owner, session, lease_seconds=60)
        old_heartbeat = self.store.read_r1().request
        self.coordinator.step()
        renewed = self.gate.renew(self.owner, session, lease_seconds=60)
        current = self.store.read_r2()
        self.store.write_r1(R1Record(self.leader.record, old_heartbeat))
        self.coordinator.step()
        self.assertEqual(self.store.read_r2(), current)
        self.gate.validate(self.owner, renewed)

    def test_leader_checks_r1_and_full_window_before_r2_writes(self):
        self.pending()
        self.store.write_r1(R1Record(replace(self.leader.record, token='c' * 32),
                                    self.store.read_r1().request))
        before = len(self.adapter.writes)
        with self.assertRaises(LeaseLost):
            self.coordinator.step()
        self.assertEqual(len(self.adapter.writes), before)
        self.assertIsNone(self.store.read_r2())

    def test_unknown_finished_retry_reads_exact_grant_and_never_erases_new_owner(self):
        session = self.acquire()
        self.adapter.write_mode = 'lose_response'
        with self.assertRaises(CommandOutcomeUnknown):
            self.gate.release(self.owner, session)
        self.adapter.write_mode = 'apply'
        before = len(self.adapter.writes)
        self.gate.release(self.owner, session)
        self.assertEqual(len(self.adapter.writes), before)  # fresh R1 confirms finished
        self.coordinator.step()
        other, owner = self.make_gate(), uuid4()
        other_session = self.acquire(other, owner)
        new_request = self.store.read_r1().request
        new_grant = self.store.read_r2()
        before = len(self.adapter.writes)
        self.gate.release(self.owner, session)
        self.assertEqual(len(self.adapter.writes), before)
        self.assertEqual(self.store.read_r1().request, new_request)
        self.assertEqual(self.store.read_r2(), new_grant)
        other.validate(owner, other_session)

    def test_unknown_delayed_finished_does_not_clear_new_grant(self):
        session = self.acquire(seconds=10)
        self.adapter.write_mode = 'defer'
        with self.assertRaises(CommandOutcomeUnknown):
            self.gate.release(self.owner, session)
        delayed = self.server.requests[-1]
        self.adapter.write_mode = 'apply'
        self.coordinator.step()
        other, owner = self.make_gate(), uuid4()
        self.pending(other, owner)
        self.clock.advance(self.store.read_r2().lease_s)
        self.coordinator.step()
        granted = self.store.read_r2()
        self.assertEqual(granted.owner, str(owner))
        # R1 can replay the stale header (accepted risk), but finished is bound
        # to the old grant and cannot clear the newer owner's R2.
        self.server.apply(delayed)
        self.coordinator.step()
        self.assertEqual(self.store.read_r2(), granted)

    def test_pending_renewal_late_ack_and_changed_duration_do_not_extend_deadline(self):
        session = self.acquire()
        sent = self.clock.now()
        with self.assertRaises(DeviceControlBusy):
            self.gate.renew(self.owner, session, lease_seconds=10)
        self.clock.advance(5)
        self.coordinator.step()
        renewed = self.gate.renew(self.owner, session, lease_seconds=600)
        self.assertEqual(renewed.expires_at, sent + 10)
        self.clock.advance(10)
        with self.assertRaises(ControlOwnershipRequired):
            self.gate.validate(self.owner, renewed)
        # A late ack of a new heartbeat confers no temporary admission.
        self.setUp()
        session = self.acquire()
        with self.assertRaises(DeviceControlBusy):
            self.gate.renew(self.owner, session, lease_seconds=10)
        self.coordinator.step()
        self.clock.advance(10)
        with self.assertRaises(DeviceControlBusy):
            self.gate.renew(self.owner, session, lease_seconds=10)

    def test_failed_off_after_grant_change_has_no_bypass_and_release_is_local_only(self):
        executor = CommandExecutor(gate=self.gate, clock=self.clock.now)
        binding = executor.bind()
        with self.assertRaises(DeviceControlBusy):
            binding.try_acquire_control(42, lease_seconds=60)
        self.coordinator.step()
        session = binding.try_acquire_control(42, lease_seconds=60)
        changed = replace(self.store.read_r2(), grant_id=str(uuid4()), owner=str(uuid4()))
        self.store.write_r2(changed)
        command = type('Command', (), {'device_id': 42})()
        sent = []
        with self.assertRaises(ControlOwnershipRequired):
            binding.execute(command, session, lambda: 'off', sent.append)
        self.assertEqual(sent, [])
        before = len(self.adapter.writes)
        binding.release_control(session)
        self.assertEqual(len(self.adapter.writes), before)
        self.assertEqual(self.store.read_r2(), changed)

    def test_unknown_coordinator_renewal_never_reports_success_or_retries_write(self):
        session = self.acquire()
        with self.assertRaises(DeviceControlBusy):
            self.gate.renew(self.owner, session, lease_seconds=60)
        self.adapter.write_mode = 'lose_response'
        before = len(self.adapter.writes)
        with self.assertRaises(LeaseLost):
            self.coordinator.step()
        self.assertEqual(len(self.adapter.writes), before + 1)
        with self.assertRaises(LeaseLost):
            self.coordinator.step()
        self.assertEqual(len(self.adapter.writes), before + 1)
        # The exact nonce ack can still be observed even after coordinator loss;
        # role cancellation itself does not revoke executor permissions.
        self.adapter.write_mode = 'apply'
        renewed = self.gate.renew(self.owner, session, lease_seconds=60)
        self.gate.validate(self.owner, renewed)

    def test_short_window_overrun_loses_role_and_gate_fails_before_command(self):
        session = self.acquire()
        self.adapter.read_delay = .6
        with self.assertRaises(ControlOwnershipRequired):
            self.gate.validate(self.owner, session)
        # Full coordinator window includes every metadata read and write.
        self.adapter.read_delay = .3
        other, owner = self.make_gate(), uuid4()
        self.pending(other, owner)
        self.clock.advance(self.store.read_r2().lease_s)
        self.coordinator.step()  # first unchanged response begins observation
        self.clock.advance(self.store.read_r2().lease_s)
        with self.assertRaises(LeaseLost):
            self.coordinator.step()

    def test_executor_release_unknown_can_be_explicitly_retried_without_overtake(self):
        executor = CommandExecutor(gate=self.gate, clock=self.clock.now)
        binding, other = executor.bind(), executor.bind()
        with self.assertRaises(DeviceControlBusy):
            binding.try_acquire_control(42, lease_seconds=60)
        self.coordinator.step()
        session = binding.try_acquire_control(42, lease_seconds=60)
        self.adapter.write_mode = 'lose_response'
        with self.assertRaises(CommandOutcomeUnknown):
            binding.release_control(session)
        with self.assertRaises(DeviceControlBusy):
            other.try_acquire_control(42, lease_seconds=60)
        self.adapter.write_mode = 'apply'
        binding.release_control(session)
        self.coordinator.step()
        with self.assertRaises(DeviceControlBusy):  # first explicit remote request
            other.try_acquire_control(42, lease_seconds=60)
        self.coordinator.step()
        other.try_acquire_control(42, lease_seconds=60)

    def test_request_and_finished_loss_require_explicit_repeat(self):
        self.pending()
        request = self.store.read_r1().request
        self.store.write_r1(R1Record(self.leader.record))
        self.coordinator.step()
        self.assertIsNone(self.store.read_r2())
        self.pending()
        self.assertEqual(self.store.read_r1().request, request)
        self.coordinator.step()
        session = self.gate.try_acquire(self.owner, 42, lease_seconds=60)
        self.gate.release(self.owner, session)
        self.store.write_r1(R1Record(self.leader.record))
        self.coordinator.step()
        self.assertIsNotNone(self.store.read_r2())
        self.gate.release(self.owner, session)  # explicit idempotent completion retry
        self.coordinator.step()
        self.assertIsNone(self.store.read_r2())

    def test_admission_just_before_at_after_deadline_and_no_finally_bypass(self):
        for offset, accepted in ((-.001, True), (0, False), (.001, False)):
            self.setUp()
            executor = CommandExecutor(gate=self.gate, clock=self.clock.now)
            binding = executor.bind()
            with self.assertRaises(DeviceControlBusy):
                binding.try_acquire_control(42, lease_seconds=10)
            self.coordinator.step()
            session = binding.try_acquire_control(42, lease_seconds=10)
            command = type('Command', (), {'device_id': 42})()
            self.clock.advance(session.expires_at - self.clock.now() + offset)
            sent = []
            def send(prepared):
                sent.append(prepared)
                self.clock.advance(2)
                return 'accepted'
            if accepted:
                self.assertEqual(binding.execute(command, session, lambda: command, send), 'accepted')
                self.assertGreater(self.clock.now(), session.expires_at)
            else:
                with self.assertRaises(ControlOwnershipRequired):
                    binding.execute(command, session, lambda: command, send)
                self.assertEqual(sent, [])
            with self.assertRaises(ControlOwnershipRequired):
                binding.execute(command, session, lambda: 'off', send)


if __name__ == '__main__':
    unittest.main()
