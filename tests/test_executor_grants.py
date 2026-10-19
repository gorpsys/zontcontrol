"""V2 causality, two-slot reservations and unchanged local API invariants."""

from dataclasses import replace
import unittest
from unittest.mock import patch
from uuid import UUID

from tests.fakes.clock import FakeClock
from tests.fakes.protocol import Scenario
from zontcontrol.control_types import (
    CommandOutcomeUnknown, ControlOwnershipRequired, DeviceControlBusy,
)
from zontcontrol.controller.ownership import CommandExecutor
from zontcontrol.coordination.clock import clock_factor, clock_rho
from zontcontrol.coordination.grants import GrantCoordinator
from zontcontrol.coordination.leader import LeaseLost
from zontcontrol.coordination.models import RequestRecord
from zontcontrol.coordination.statistics import Statistics
from zontcontrol.coordination.timing import OperationWindow


class GrantTests(unittest.TestCase):
    def setUp(self):
        self.s = Scenario()

    def publish(self, req):
        self.s.gate._stores[42].publish_request(req, window=OperationWindow(self.s.clock, 1))

    def start_request(self, **changes):
        self.start = self.s.clock.now()
        self.rid = self.s.uid()
        options = dict(request_id=self.rid, local_deadline=self.start+30,
                       attempt_deadline=self.start+20)
        options.update(changes)
        self.options = options
        with self.assertRaises(DeviceControlBusy):
            self.attempt()
        return self.s.gate._pending[self.s.owner, 42]

    def attempt(self, **changes):
        return self.s.gate.try_acquire(self.s.owner, 42, lease_seconds=60,
                                      **{**self.options, **changes})

    def test_injected_uuid_sequence_covers_request_grant_session_and_renewal(self):
        s = self.s
        before = s.uid.call_count
        s.pending()
        req = s.store.read_r2().request
        self.assertEqual((req.request_id, req.nonce),
                         (str(UUID(int=before+1)), str(UUID(int=before+2))))
        s.pending()
        self.assertEqual(s.uid.call_count, before+2)
        grant = s.coordinator.step()
        self.assertEqual(grant.grant_id, str(UUID(int=before+4)))
        session = s.gate.try_acquire(s.owner, 42, lease_seconds=60)
        self.assertEqual(session.session_id, UUID(int=before+6))
        self.assertIs(s.gate.try_acquire(s.owner, 42, lease_seconds=600), session)
        with self.assertRaises(DeviceControlBusy):
            s.gate.renew(s.owner, session, lease_seconds=60)
        self.assertEqual(s.store.read_r2().request.nonce, str(UUID(int=before+7)))
        s.coordinator.step()
        renewed = s.gate.renew(s.owner, session, lease_seconds=60)
        self.assertEqual(renewed.session_id, UUID(int=before+10))
        self.assertEqual(s.store.read_r1().grants[0].grant_id, grant.grant_id)

    def test_request_identity_deadlines_survive_lost_request_and_retries(self):
        pending = self.start_request()
        first, sent = pending.request, pending.sent
        self.publish(None)
        self.s.advance(2)
        with self.assertRaises(DeviceControlBusy):
            self.attempt(local_deadline=self.start+100, attempt_deadline=self.start+100)
        self.assertIs(self.s.gate._pending[self.s.owner, 42], pending)
        self.assertEqual((pending.request, pending.sent), (first, sent))
        self.assertEqual((pending.local_deadline, pending.attempt_deadline),
                         (self.start+30, self.start+20))
        self.s.coordinator.step()
        self.assertEqual(self.attempt().expires_at, self.start+30)

    def test_uncertain_request_only_polls_original_identity(self):
        self.s.adapter.write_mode = 'lose_response'
        with self.assertRaises(CommandOutcomeUnknown):
            self.start_request()
        pending = self.s.gate._pending[self.s.owner, 42]
        self.s.adapter.write_mode = 'apply'
        self.publish(None)
        before = len(self.s.adapter.writes)
        with self.assertRaises(DeviceControlBusy):
            self.attempt()
        self.assertEqual(len(self.s.adapter.writes), before)
        self.assertIs(self.s.gate._pending[self.s.owner, 42], pending)

    def test_ack_observer_precedes_commit_and_rechecks_deadlines(self):
        for mode in ('interrupt', 'attempt', 'lease'):
            self.setUp()
            pending = self.start_request(attempt_deadline=self.s.clock.now()+100) if mode == 'lease' else self.start_request()
            self.s.coordinator.step()
            def sink(row):
                if row['event'] == 'grant_ack':
                    if mode == 'interrupt':
                        raise SystemExit()
                    self.s.clock.advance(20 if mode == 'attempt' else 30)
            self.s.gate._statistics = Statistics(sink, protocol_version='2', profile='test')
            with self.assertRaises(SystemExit if mode == 'interrupt' else ControlOwnershipRequired):
                self.attempt()
            self.assertNotIn((self.s.owner, 42), self.s.gate._held)
            self.assertIn((self.s.owner, 42), self.s.gate._pending)
            before = len(self.s.adapter.writes)
            self.s.gate.abandon_request(self.s.owner, 42, self.rid)
            self.assertNotIn((self.s.owner, 42), self.s.gate._pending)
            self.assertEqual(len(self.s.adapter.writes), before)

    def test_deadline_crossing_in_ack_or_lost_read_starts_no_publication(self):
        for stage in ('ack', 'lost_read'):
            self.setUp()
            self.start_request(attempt_deadline=self.s.clock.now()+.5)
            if stage == 'ack':
                self.s.coordinator.step()
            else:
                self.publish(None)
            self.s.adapter.read_delay = .5
            before = len(self.s.adapter.writes)
            with self.assertRaises(ControlOwnershipRequired):
                self.attempt()
            self.assertEqual(len(self.s.adapter.writes), before)

    def test_started_publication_finishes_late_without_session(self):
        self.start_request(attempt_deadline=self.s.clock.now()+.5)
        self.publish(None)
        write = self.s.gate._stores[42].publish_request
        def delayed(req, **kwargs):
            result = write(req, **kwargs)
            self.s.clock.advance(.5)
            return result
        with patch.object(self.s.gate._stores[42], 'publish_request', side_effect=delayed):
            with self.assertRaises(ControlOwnershipRequired):
                self.attempt()
        self.assertNotIn((self.s.owner, 42), self.s.gate._held)

    def test_cache_does_not_lend_lost_session_to_new_request(self):
        self.start_request()
        self.s.coordinator.step()
        session = self.attempt()
        with self.assertRaises(DeviceControlBusy):
            self.attempt(request_id=self.s.uid())
        self.assertIs(self.s.gate._held[self.s.owner, 42].session, session)

    def test_cache_checks_exact_deadline_before_reuse(self):
        self.start_request()
        self.s.coordinator.step()
        self.attempt()
        self.s.advance(20)
        with self.assertRaises(ControlOwnershipRequired):
            self.attempt()

    def test_queue_delay_and_late_ack_preserve_original_local_lease(self):
        start = self.s.clock.now()
        self.s.pending(seconds=10)
        self.s.advance(4)
        self.s.coordinator.step()
        session = self.s.gate.try_acquire(self.s.owner, 42, lease_seconds=600)
        self.assertLessEqual(session.expires_at, start+10)

    def test_interrupted_ack_leaves_no_lower_or_executor_session(self):
        self.start_request()
        self.s.coordinator.step()
        def interrupt(row):
            if row['event'] == 'grant_ack':
                raise SystemExit()
        self.s.gate._statistics = Statistics(interrupt, protocol_version='2', profile='test')
        with self.assertRaises(SystemExit):
            self.attempt()
        self.assertEqual(self.s.gate._held, {})
        self.assertEqual(self.s.gate._pending[self.s.owner, 42].request.request_id, str(self.rid))

    def test_gate_and_executor_issue_exact_same_session(self):
        s = self.s
        executor = CommandExecutor(gate=s.gate, clock=s.clock.now, uuid_factory=s.uid)
        binding = executor.bind()
        with self.assertRaises(DeviceControlBusy):
            binding.try_acquire_control(42, lease_seconds=60)
        s.coordinator.step()
        session = binding.try_acquire_control(42, lease_seconds=60)
        self.assertIs(next(iter(s.gate._held.values())).session, session)
        self.assertIs(binding.try_acquire_control(42, lease_seconds=600), session)

    def test_abandon_is_local_identity_scoped_and_new_request_rejects_old_ack(self):
        pending = self.start_request()
        self.s.gate.abandon_request(self.s.uid(), 42, self.rid)
        self.assertIs(self.s.gate._pending[self.s.owner, 42], pending)
        before = len(self.s.adapter.writes)
        self.s.gate.abandon_request(self.s.owner, 42, self.rid)
        self.assertEqual(len(self.s.adapter.writes), before)
        with self.assertRaises(DeviceControlBusy):
            self.attempt(request_id=self.s.uid())
        self.assertNotEqual(self.s.store.read_r2().request.nonce, pending.request.nonce)

    def test_request_grant_heartbeat_finished_and_release(self):
        s = self.s
        session = s.acquire()
        original = s.store.read_r1().grants[0]
        with self.assertRaises(DeviceControlBusy):
            s.gate.renew(s.owner, session, lease_seconds=60)
        s.coordinator.step()
        renewed = s.gate.renew(s.owner, session, lease_seconds=60)
        current = s.store.read_r1().grants[0]
        self.assertEqual(current.grant_id, original.grant_id)
        self.assertEqual(current.revision, original.revision+1)
        s.gate.release(s.owner, renewed)
        s.coordinator.step()
        self.assertEqual(s.store.read_r1().grants, (current,))
        self.assertEqual(s.store.read_r1().handled_request.outcome, 'finished')
        with self.assertRaises(ControlOwnershipRequired):
            s.gate.validate(s.owner, renewed)

    def test_shutdown_settles_completion_without_new_grant_or_renewal(self):
        self.s.pending()
        self.s.coordinator.step(accepting=False)
        self.assertEqual(self.s.store.read_r1().grants, ())
        session = self.s.acquire()
        self.s.gate.release(self.s.owner, session)
        self.s.coordinator.step(accepting=False)
        self.assertEqual(self.s.store.read_r1().handled_request.outcome, 'finished')

    def test_expired_request_is_consumed_and_never_regranted(self):
        s = self.s
        s.acquire(seconds=10)
        req = s.store.read_r2().request
        s.advance(20)
        s.coordinator.step()
        self.assertEqual(s.store.read_r1().grants, ())
        before = len(s.adapter.writes)
        for _ in range(10):
            s.coordinator.step()
        self.assertEqual(len(s.adapter.writes), before)
        self.assertEqual(s.store.read_r2().request, req)
        self.assertEqual(s.store.read_r1().handled_request.nonce, req.nonce)

    def test_old_owner_records_are_pruned_without_losing_active_or_uncertain_release(self):
        s = self.s
        session = s.acquire(seconds=10)
        s.adapter.write_mode = 'lose_response'
        with self.assertRaises(CommandOutcomeUnknown):
            s.gate.release(s.owner, session)
        self.assertIn((s.owner, 42), s.gate._held)
        s.adapter.write_mode = 'apply'
        s.advance(20)
        s.pending(owner=s.uid())
        self.assertNotIn((s.owner, 42), s.gate._held)

    def test_independent_applications_future_grant_no_early_session(self):
        s = self.s
        first = s.acquire(seconds=10)
        other, owner = s.make_gate(), s.uid()
        s.pending(gate=other, owner=owner, seconds=60)
        new = s.coordinator.step()
        self.assertEqual(len(s.store.read_r1().grants), 2)
        old = s.store.read_r1().grants[0]
        self.assertGreaterEqual(new.valid_from, old.valid_until+s.leader.timing.command_guard)
        with self.assertRaises(DeviceControlBusy):
            other.try_acquire(owner, 42, lease_seconds=60)
        pending = other._pending[owner, 42]
        self.assertIsNotNone(pending.grant)
        s.gate.release(s.owner, first)
        s.coordinator.step()
        self.assertEqual(s.store.read_r1().grants[0], old)
        s.advance(pending.interval.usable_from-s.clock.now())
        session = other.try_acquire(owner, 42, lease_seconds=60)
        other.validate(owner, session)

    def test_rereading_and_reacquiring_do_not_extend_nonce_send_deadline(self):
        start = self.s.clock.now()
        session = self.s.acquire()
        self.assertAlmostEqual(session.expires_at, start+60, places=6)
        self.s.advance(20)
        self.assertIs(self.s.gate.try_acquire(self.s.owner, 42, lease_seconds=600), session)
        self.assertAlmostEqual(session.expires_at, start+60, places=6)

    def test_same_owner_can_request_again_after_unreleased_session_expires(self):
        old = self.s.acquire(seconds=10)
        self.s.advance(20)
        session = self.s.acquire()
        self.assertIsNot(old, session)
        with self.assertRaises(ControlOwnershipRequired):
            self.s.gate.validate(self.s.owner, old)

    def test_unknown_nonce_or_wrong_owner_request_grant_revision_never_authorizes(self):
        for field in ('owner_id', 'request_id', 'nonce', 'revision'):
            self.setUp()
            self.s.pending()
            self.s.coordinator.step()
            record = self.s.store.read_r1()
            g = record.grants[0]
            value = 2 if field == 'revision' else str(self.s.uid())
            self.s.inject_r1(replace(record, grants=(replace(g, **{field: value}),)))
            with self.subTest(field=field), self.assertRaises(DeviceControlBusy):
                self.s.gate.try_acquire(self.s.owner, 42, lease_seconds=60)
        self.setUp()
        session = self.s.acquire()
        with self.assertRaises(ControlOwnershipRequired):
            self.s.gate.validate(self.s.uid(), session)

    def test_missing_heartbeat_does_not_revoke_or_replace_grant(self):
        session = self.s.acquire()
        old = self.s.store.read_r1().grants
        self.publish(None)
        self.s.coordinator.step()
        self.assertEqual(self.s.store.read_r1().grants, old)
        self.s.gate.validate(self.s.owner, session)

    def test_late_ack_and_process_restart_never_create_right(self):
        self.start_request()
        self.s.coordinator.step()
        self.s.advance(30)
        with self.assertRaises(ControlOwnershipRequired):
            self.attempt()
        restarted = self.s.make_gate()
        self.assertIsNone(self.s.pending(gate=restarted))
        self.assertEqual(restarted._held, {})

    def test_all_drift_boundaries_use_original_period_not_scaled_lease(self):
        for period in (59.99, 60, 60.01, 299.99, 300, 300.01, 599.99, 600, 600.01):
            self.setUp()
            start = self.s.clock.now()
            session = self.s.acquire(seconds=period)
            g = self.s.store.read_r1().grants[0]
            self.assertEqual(g.clock_rho, clock_rho(period))
            self.assertAlmostEqual(g.valid_until-g.sampled_at, period*clock_factor(period), places=6)
            self.assertAlmostEqual(session.expires_at, start+period, places=6)

    def test_opposite_drift_bands_hold_old_owner_and_physical_tail(self):
        for period, rho in ((60, .1), (300, .05), (600, .01), (700, .005)):
            for sign in (-1, 1):
                self.setUp()
                holder = FakeClock(start=5000, rate=1+sign*rho)
                gate = self.s.make_gate(clock=holder)
                session = self.s.acquire(gate=gate, seconds=period)
                grant = self.s.store.read_r1().grants[0]
                real = period/(1+sign*rho)
                holder.advance(real)
                self.s.advance(real)
                self.assertGreaterEqual(holder.now()+1e-6, session.expires_at)
                reserved = grant.valid_until+self.s.leader.timing.command_guard
                self.assertGreater(reserved, grant.valid_until)

    def test_natural_expiry_keeps_reserve_until_exact_tail_boundary_and_promotes(self):
        s = self.s
        s.acquire(seconds=10)
        first = s.store.read_r1().grants[0]
        s.advance(first.valid_until-s.leader.timeline.now()+.5)
        s.pending(owner=s.uid(), seconds=60)
        second = s.coordinator.step()
        self.assertEqual(s.store.read_r1().grants[0], first)
        self.assertGreaterEqual(second.valid_from, first.valid_until+s.leader.timing.command_guard)
        s.advance(first.valid_until+s.leader.timing.command_guard-s.leader.timeline.now()+1e-6)
        s.coordinator.step()
        self.assertEqual(s.store.read_r1().grants, (second,))

    def test_finished_is_bound_to_exact_request_grant_revision(self):
        session = self.s.acquire()
        self.s.gate.release(self.s.owner, session)
        req = self.s.store.read_r2().request
        record = self.s.store.read_r1()
        for change in ({'grant_id': str(self.s.uid())}, {'request_id': str(self.s.uid())},
                       {'grant_revision': 123}):
            self.publish(replace(req, **change))
            self.s.coordinator.step()
            self.assertEqual(self.s.store.read_r1(), record)
        self.publish(req)
        self.s.coordinator.step()
        self.assertEqual(self.s.store.read_r1().grants, record.grants)
        self.assertEqual(self.s.store.read_r1().handled_request.outcome, 'finished')

    def test_new_leader_does_not_revoke_cached_valid_grant(self):
        session = self.s.acquire()
        cached = self.s.gate._held[self.s.owner, 42].grant
        record = self.s.store.read_r1()
        self.s.inject_r1(replace(record, grants=(), leader=replace(record.leader,
                              term_id=str(self.s.uid()), clock_id=str(self.s.uid()))))
        self.s.gate.validate(self.s.owner, session)
        self.assertIs(self.s.gate._held[self.s.owner, 42].grant, cached)
        self.s.advance(session.expires_at-self.s.clock.now())
        with self.assertRaises(ControlOwnershipRequired):
            self.s.gate.validate(self.s.owner, session)

    def test_unknown_renewal_is_not_blindly_retried_and_old_ack_is_not_renewal(self):
        session = self.s.acquire()
        self.s.adapter.write_mode = 'lose_response'
        before = len(self.s.adapter.writes)
        with self.assertRaises(CommandOutcomeUnknown):
            self.s.gate.renew(self.s.owner, session, lease_seconds=60)
        self.s.adapter.write_mode = 'apply'
        with self.assertRaises(DeviceControlBusy):
            self.s.gate.renew(self.s.owner, session, lease_seconds=60)
        self.assertEqual(len(self.s.adapter.writes), before+1)
        self.s.coordinator.step()
        renewed = self.s.gate.renew(self.s.owner, session, lease_seconds=60)
        self.assertIsNot(renewed, session)

    def test_replayed_heartbeat_cannot_renew_newer_revision(self):
        session = self.s.acquire()
        with self.assertRaises(DeviceControlBusy):
            self.s.gate.renew(self.s.owner, session, lease_seconds=60)
        old = self.s.store.read_r2().request
        self.s.coordinator.step()
        renewed = self.s.gate.renew(self.s.owner, session, lease_seconds=60)
        record = self.s.store.read_r1()
        self.publish(old)
        self.s.coordinator.step()
        self.assertEqual(self.s.store.read_r1(), record)
        self.s.gate.validate(self.s.owner, renewed)

    def test_leader_checks_r1_and_full_window_before_grant_writes(self):
        self.s.pending()
        record = self.s.store.read_r1()
        self.s.inject_r1(replace(record, leader=replace(record.leader, term_id=str(self.s.uid()))))
        before = len(self.s.adapter.writes)
        with self.assertRaises(LeaseLost):
            self.s.coordinator.step()
        self.assertEqual(len(self.s.adapter.writes), before)
        self.assertEqual(self.s.store.read_r1().grants, ())

    def test_unknown_finished_is_never_retried_and_never_erases_new_owner(self):
        session = self.s.acquire()
        self.s.adapter.write_mode = 'lose_response'
        with self.assertRaises(CommandOutcomeUnknown):
            self.s.gate.release(self.s.owner, session)
        self.s.adapter.write_mode = 'apply'
        before = len(self.s.adapter.writes)
        self.s.gate.release(self.s.owner, session)
        self.assertEqual(len(self.s.adapter.writes), before)
        self.s.coordinator.step()
        self.assertEqual(self.s.store.read_r1().grants[0].request_id,
                         self.s.store.read_r2().request.request_id)

    def test_unknown_delayed_finished_does_not_clear_new_grant(self):
        session = self.s.acquire(seconds=10)
        self.s.adapter.write_mode = 'defer'
        with self.assertRaises(CommandOutcomeUnknown):
            self.s.gate.release(self.s.owner, session)
        delayed = self.s.server.requests[-1]
        self.s.adapter.write_mode = 'apply'
        self.s.advance(20)
        self.s.pending(owner=self.s.uid())
        new = self.s.coordinator.step()
        self.s.server.apply(delayed)
        self.s.coordinator.step()
        self.assertEqual(self.s.store.read_r1().grants, (new,))
        self.assertTrue(self.s.server.out_of_model_faults)

    def test_pending_renewal_changed_duration_does_not_extend_deadline(self):
        session = self.s.acquire()
        sent = self.s.clock.now()
        with self.assertRaises(DeviceControlBusy):
            self.s.gate.renew(self.s.owner, session, lease_seconds=10)
        self.s.advance(5)
        self.s.coordinator.step()
        pending = self.s.gate._held[self.s.owner, 42].renewal
        with self.assertRaises(DeviceControlBusy):
            self.s.gate.renew(self.s.owner, session, lease_seconds=600)
        self.assertAlmostEqual(pending.local_deadline, sent+10, places=6)
        self.s.advance(10)
        with self.assertRaises(ControlOwnershipRequired):
            self.s.gate.renew(self.s.owner, session, lease_seconds=600)
        self.s.gate.validate(self.s.owner, session)

    def test_unacknowledged_short_heartbeat_preserves_old_cached_reservation(self):
        s = self.s
        session = s.acquire()
        original = s.store.read_r1().grants[0]
        s.advance(5)
        with self.assertRaises(DeviceControlBusy):
            s.gate.renew(s.owner, session, lease_seconds=10)
        s.coordinator.step()  # Deliberately never deliver this heartbeat ack.
        current = s.store.read_r1().grants[0]
        self.assertEqual(current, original)
        other, owner = s.make_gate(), s.uid()
        s.pending(gate=other, owner=owner, seconds=120)
        future = s.coordinator.step()
        self.assertGreaterEqual(future.valid_from,
                               original.valid_until+s.leader.timing.command_guard)
        s.advance(20)
        s.gate.validate(s.owner, session)
        with self.assertRaises(DeviceControlBusy):
            other.try_acquire(owner, 42, lease_seconds=120)

    def test_acquire_displaced_between_write_ack_and_readback_can_retry(self):
        s = self.s
        update = s.adapter.update_metadata
        # A separate adapter models a competing successful writer.
        from tests.fakes.zont import FakeMetadataAdapter
        competitor = FakeMetadataAdapter(s.server)
        def overwrite(*args, **kwargs):
            result = update(*args, **kwargs)
            from zontcontrol.coordination.models import R2Record, encode_r2
            req = s.store.read_r2().request
            raw = encode_r2(R2Record(s.profile.profile_id, replace(req,
                owner_id=str(s.uid()), request_id=str(s.uid()), nonce=str(s.uid()))))
            competitor.update_metadata(42, 'notes', raw)
            return result
        with patch.object(s.adapter, 'update_metadata', side_effect=overwrite):
            pending = self.start_request()
        self.assertFalse(pending.uncertain)
        original = pending.request, pending.sent, pending.local_deadline, pending.attempt_deadline
        with self.assertRaises(DeviceControlBusy):
            self.attempt()
        self.assertEqual(s.store.read_r2().request, pending.request)
        self.assertEqual(original, (pending.request, pending.sent, pending.local_deadline,
                                    pending.attempt_deadline))
        s.coordinator.step()
        self.attempt()

    def test_heartbeat_known_displacement_retries_same_sample_with_backoff(self):
        s = self.s
        session = s.acquire()
        s.gate._retry_seconds = 2
        with self.assertRaises(DeviceControlBusy):
            s.gate.renew(s.owner, session, lease_seconds=60)
        pending = s.gate._held[s.owner, 42].renewal
        original = pending.request, pending.sent, pending.local_deadline
        self.publish(replace(pending.request, operation='acquire', grant_id=None,
                             grant_revision=None, owner_id=str(s.uid()), nonce=str(s.uid())))
        before = len(s.adapter.writes)
        with self.assertRaises(DeviceControlBusy):
            s.gate.renew(s.owner, session, lease_seconds=600)
        self.assertEqual(len(s.adapter.writes), before)
        s.advance(2)
        with self.assertRaises(DeviceControlBusy):
            s.gate.renew(s.owner, session, lease_seconds=600)
        self.assertEqual(s.store.read_r2().request, pending.request)
        self.assertEqual(original, (pending.request, pending.sent, pending.local_deadline))
        s.coordinator.step()
        renewed = s.gate.renew(s.owner, session, lease_seconds=600)
        self.assertLessEqual(renewed.expires_at, pending.local_deadline)

    def test_failed_off_after_expiry_has_no_bypass_and_release_is_local_only(self):
        executor = CommandExecutor(gate=self.s.gate, clock=self.s.clock.now, uuid_factory=self.s.uid)
        binding = executor.bind()
        with self.assertRaises(DeviceControlBusy):
            binding.try_acquire_control(42, lease_seconds=10)
        self.s.coordinator.step()
        session = binding.try_acquire_control(42, lease_seconds=10)
        self.s.advance(11)
        command = type('Command', (), {'device_id': 42})()
        sent = []
        with self.assertRaises(ControlOwnershipRequired):
            binding.execute(command, session, lambda: 'off', sent.append)
        before = len(self.s.adapter.writes)
        binding.release_control(session)
        self.assertEqual(len(self.s.adapter.writes), before)
        self.assertEqual(sent, [])

    def test_unknown_coordinator_renewal_never_reports_success_or_retries_write(self):
        self.s.pending()
        self.s.adapter.write_mode = 'lose_response'
        before = len(self.s.adapter.writes)
        with self.assertRaises(LeaseLost):
            self.s.coordinator.step()
        self.assertEqual(len(self.s.adapter.writes), before+1)
        with self.assertRaises(LeaseLost):
            self.s.coordinator.step()
        self.assertEqual(self.s.store.read_r1().grants, ())

    def test_short_window_overrun_loses_role_but_cached_gate_remains_usable(self):
        session = self.s.acquire()
        self.s.adapter.read_delay = .6
        self.s.gate.validate(self.s.owner, session)
        with self.assertRaises(LeaseLost):
            self.s.coordinator.step()
        self.s.gate.validate(self.s.owner, session)

    def test_executor_release_unknown_preserves_reserve_without_overtake(self):
        session = self.s.acquire(seconds=10)
        first = self.s.store.read_r1().grants[0]
        self.s.adapter.write_mode = 'lose_response'
        with self.assertRaises(CommandOutcomeUnknown):
            self.s.gate.release(self.s.owner, session)
        self.s.adapter.write_mode = 'apply'
        self.s.pending(owner=self.s.uid())
        second = self.s.coordinator.step()
        self.assertGreaterEqual(second.valid_from, first.valid_until+self.s.leader.timing.command_guard)

    def test_request_and_finished_loss_cannot_shorten_reservation(self):
        self.s.pending()
        req = self.s.store.read_r2().request
        self.publish(None)
        self.s.coordinator.step()
        self.assertEqual(self.s.store.read_r1().grants, ())
        self.s.pending()
        self.assertEqual(self.s.store.read_r2().request, req)
        self.s.coordinator.step()
        session = self.s.gate.try_acquire(self.s.owner, 42, lease_seconds=60)
        first = self.s.store.read_r1().grants[0]
        self.s.gate.release(self.s.owner, session)
        self.publish(None)
        self.s.coordinator.step()
        self.assertEqual(self.s.store.read_r1().grants, (first,))

    def test_admission_just_before_at_after_deadline_and_no_finally_bypass(self):
        for offset, accepted in ((-.001, True), (0, False), (.001, False)):
            self.setUp()
            s = self.s
            executor = CommandExecutor(gate=s.gate, clock=s.clock.now, uuid_factory=s.uid)
            binding = executor.bind()
            with self.assertRaises(DeviceControlBusy):
                binding.try_acquire_control(42, lease_seconds=10)
            s.coordinator.step()
            session = binding.try_acquire_control(42, lease_seconds=10)
            command = type('Command', (), {'device_id': 42})()
            s.advance(session.expires_at-s.clock.now()+offset)
            sent = []
            def send(value):
                sent.append(value)
                s.clock.advance(2)
                return 'accepted'
            if accepted:
                self.assertEqual(binding.execute(command, session, lambda: command, send), 'accepted')
                self.assertGreater(s.clock.now(), session.expires_at)
            else:
                with self.assertRaises(ControlOwnershipRequired):
                    binding.execute(command, session, lambda: command, send)
                self.assertEqual(sent, [])
            with self.assertRaises(ControlOwnershipRequired):
                binding.execute(command, session, lambda: 'off', send)

    def test_grant_follows_separate_confirmed_renewal(self):
        self.s.pending()
        before = len(self.s.adapter.writes)
        revision = self.s.leader.record.revision
        grant = self.s.coordinator.step()
        writes = self.s.adapter.writes[before:]
        self.assertEqual([field for _, field, _ in writes], ['name', 'name'])
        from zontcontrol.coordination.models import decode_r1
        renewal, response = (decode_r1(raw, self.s.prefix) for _, _, raw in writes)
        self.assertEqual(renewal.grants, ())
        self.assertEqual(renewal.leader.revision, revision+1)
        self.assertEqual(response.grants, (grant,))
        self.assertGreaterEqual(response.leader.leader_until, grant.valid_until+1)

    def test_oversized_grant_declined_without_hidden_period_extension(self):
        self.s.pending(seconds=1100)
        self.assertIsNone(self.s.coordinator.step())
        self.assertEqual(self.s.store.read_r1().grants, ())
        self.assertEqual(self.s.leader.record.period_s, 1000)
        self.assertEqual(self.s.store.read_r1().reply.reason, 'horizon')

    def test_old_nonce_after_another_request_and_expiry_never_reissues(self):
        self.s.acquire(seconds=10)
        old = self.s.store.read_r2().request
        self.s.pending(owner=self.s.uid(), seconds=40)
        second = self.s.coordinator.step()
        self.s.advance(20)
        self.publish(old)
        self.s.coordinator.step()
        self.assertEqual(self.s.store.read_r1().grants, (second,))
        self.assertEqual(len(self.s.coordinator._results), 2)

    def test_pending_rebinds_new_term_nonce_without_new_request_or_budget(self):
        pending = self.start_request()
        req, deadline = pending.request, pending.local_deadline
        record = self.s.store.read_r1()
        newterm = str(self.s.uid())
        self.s.inject_r1(replace(record, leader=replace(record.leader, term_id=newterm)))
        with self.assertRaises(DeviceControlBusy):
            self.attempt(local_deadline=self.start+100)
        self.assertEqual(pending.request.request_id, req.request_id)
        self.assertNotEqual(pending.request.nonce, req.nonce)
        self.assertEqual(pending.request.leader_term_id, newterm)
        self.assertEqual(pending.local_deadline, deadline)

    def test_restart_does_not_serve_unchanged_old_term_request(self):
        self.s.acquire(seconds=10)
        old = self.s.store.read_r2()
        new = self.s.candidate(epoch=900000000)
        self.assertFalse(new.try_acquire())
        self.s.advance(1000*clock_factor(1000))
        self.assertTrue(new.try_acquire())
        self.s.advance(new.timing.quarantine, new)
        coordinator = GrantCoordinator(self.s.store, new, clock=self.s.clock, uuid_factory=self.s.uid)
        coordinator.step()
        self.assertEqual(self.s.store.read_r2(), old)
        self.assertEqual(self.s.store.read_r1().grants, ())
        self.assertEqual(self.s.store.read_r1().handled_request.nonce, old.request.nonce)

    def test_heartbeat_cannot_cross_preassigned_second_slot(self):
        first = self.s.acquire(seconds=10)
        self.s.pending(owner=self.s.uid(), seconds=60)
        second = self.s.coordinator.step()
        with self.assertRaises(DeviceControlBusy):
            self.s.gate.renew(self.s.owner, first, lease_seconds=60)
        self.s.coordinator.step()
        grants = self.s.store.read_r1().grants
        self.assertLessEqual(grants[0].valid_until+self.s.leader.timing.command_guard, second.valid_from)
        self.assertEqual(grants[1], second)
