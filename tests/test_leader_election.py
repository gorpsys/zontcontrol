"""Fischer v2 schedules, send-based deadlines, freeze and natural shutdown."""

from dataclasses import replace
from threading import Event
import unittest
from unittest.mock import patch

from tests.fakes.clock import FakeClock
from tests.fakes.protocol import Scenario
from zontcontrol.coordination.clock import BootClock, clock_factor, clock_rho
from zontcontrol.coordination.leader import LeaseLost
from zontcontrol.coordination.models import HandledRequest, RecordError
from zontcontrol.coordination.timing import LeaseTiming, OperationWindow


class TimingTests(unittest.TestCase):
    def test_policy_boundaries_and_independent_short_window_and_term_factors(self):
        for period, rho in ((60, .1), (60.01, .05), (300, .05),
                            (300.01, .01), (600, .01), (600.01, .005)):
            timing = LeaseTiming(period, 1, 1)
            self.assertEqual(clock_rho(period), rho)
            self.assertEqual(timing.clock_rho, rho)
            self.assertAlmostEqual(timing.lease_s, period * clock_factor(period))
            self.assertEqual(timing.barrier, 2)
            self.assertEqual(timing.stop_margin, 1)
            self.assertAlmostEqual(timing.command_guard, clock_factor(1))
            self.assertEqual(timing.term_budget, 1200 / clock_factor(1200))

    def test_expected_work_and_jitter_validation(self):
        self.assertEqual(LeaseTiming(90, 5, 1, expected_work=50).valid_for, 90)
        for args in ((7.99, 1, 1), (1200, 1, 1), (60, 0, 1),
                     (True, 1, 1), (float('nan'), 1, 1), (60, 1, float('inf'))):
            with self.subTest(args=args), self.assertRaises(ValueError):
                LeaseTiming(*args)
        with self.assertRaises(ValueError):
            LeaseTiming(90, 5, 5, expected_work=70)

    def test_boottime_is_required_and_wait_is_interruptible(self):
        with patch('zontcontrol.coordination.clock.time') as time:
            time.CLOCK_BOOTTIME = 7
            time.clock_gettime.return_value = 123
            self.assertEqual(BootClock().now(), 123)
            time.clock_gettime.assert_called_with(7)
            del time.CLOCK_BOOTTIME
            with self.assertRaises(RuntimeError):
                BootClock()
        for error in (OSError, ValueError):
            with patch('zontcontrol.coordination.clock.time.clock_gettime', side_effect=error):
                with self.assertRaises(RuntimeError):
                    BootClock()
        event = Event()
        event.set()
        self.assertTrue(BootClock().wait(event, 100))

    def test_fake_wait_uses_local_time_and_never_changes_wall_policy(self):
        for rho in (.1, .05, .01, .005):
            for rate in (1 - rho, 1 + rho):
                clock = FakeClock(rate=rate)
                self.assertFalse(clock.wait(Event(), 5))
                self.assertAlmostEqual(clock.now(), 5)

    def test_drift_bands_cover_a_fast_observer_and_slow_holder(self):
        for period, rho in ((60, .1), (300, .05), (600, .01), (1000, .005)):
            holder, observer = FakeClock(rate=1-rho), FakeClock(rate=1+rho)
            real = period * clock_factor(period) / (1 + rho)
            holder.advance(real)
            observer.advance(real)
            self.assertGreaterEqual(holder.now() + 1e-10, period)
            self.assertAlmostEqual(observer.now(), period * clock_factor(period))

    def test_short_delta_and_term_never_reuse_long_lease_rho(self):
        timing = LeaseTiming(1000, 60, 1)
        self.assertEqual(timing.clock_rho, .005)
        self.assertGreater(clock_factor(timing.delta), clock_factor(timing.period))
        self.assertGreater(timing.barrier, clock_factor(timing.delta) * 60)
        self.assertNotEqual(timing.term_budget, 1200 / clock_factor(60))


class ElectionTests(unittest.TestCase):
    def setUp(self):
        self.s = Scenario(ready=False)
        self.leader = self.s.leader

    def test_free_acquisition_barrier_and_deadline_from_send(self):
        self.s.adapter.write_delay = .25
        self.assertTrue(self.leader.try_acquire())
        self.assertAlmostEqual(self.s.clock.now(), 102.25)
        self.assertEqual(self.leader.expires_at, 1100)
        self.assertEqual(self.leader.term_expires_at, 100 + self.leader.timing.term_budget)
        self.assertEqual(self.leader.record, self.s.store.read_r1().leader)
        self.assertTrue(self.leader.can_coordinate(1))
        self.assertFalse(self.leader.role_cancelled.is_set())
        self.assertEqual(self.s.server.read_device(42)['notes'], None)

    def test_no_actions_during_barrier_and_overwritten_candidate_loses(self):
        wait = self.s.clock.wait
        def race(event, seconds):
            self.assertFalse(self.leader.can_coordinate(.1))
            record = self.s.store.read_r1()
            self.s.inject_r1(replace(record, record_id=str(self.s.uid()),
                leader=replace(record.leader, owner_id=str(self.s.uid()), term_id=str(self.s.uid()))))
            return wait(event, seconds)
        with patch.object(self.s.clock, 'wait', side_effect=race), self.assertRaises(LeaseLost):
            self.leader.try_acquire()
        self.assertTrue(self.leader.role_cancelled.is_set())
        self.assertEqual(len(self.s.adapter.writes), 1)

    def test_expiry_observation_starts_at_response_and_ends_at_last_read_send(self):
        self.leader.try_acquire()
        other = self.s.candidate()
        self.s.adapter.read_delay = .1
        self.assertFalse(other.try_acquire())
        response = self.s.clock.now()
        old = self.s.store.read_r1().leader
        wait = (old.leader_until-old.sampled_at)*clock_factor(old.period_s)
        self.s.advance(wait - .15)
        self.assertFalse(other.try_acquire())
        self.assertGreater(self.s.clock.now() - response, wait)
        self.assertTrue(other.try_acquire())
        self.assertNotEqual(other.record.term_id, self.leader.record.term_id)

    def test_observation_resets_for_every_record_id_including_replies(self):
        self.leader.try_acquire()
        other = self.s.candidate()
        self.assertFalse(other.try_acquire())
        old = self.s.store.read_r1()
        self.s.advance(1100)
        self.s.inject_r1(replace(old, record_id=str(self.s.uid())))
        self.assertFalse(other.try_acquire())
        self.assertEqual(len(self.s.adapter.writes), 1)

    def test_renew_grows_revision_preserves_marker_and_cap(self):
        self.leader.try_acquire()
        record = self.s.store.read_r1()
        marker = HandledRequest(str(self.s.uid()), str(self.s.uid()), str(self.s.uid()),
                                self.leader.record.term_id, 'acquire', None, 'expired')
        self.s.inject_r1(replace(record, record_id=str(self.s.uid()), handled_request=marker))
        initial, cap = self.leader.record, self.leader.term_expires_at
        self.s.advance(20)
        self.leader.renew()
        self.assertEqual(self.leader.record.revision, 1)
        self.assertEqual(self.leader.record.term_id, initial.term_id)
        self.assertEqual(self.leader.expires_at, self.s.clock.now() + 1000)
        self.assertEqual(self.leader.term_expires_at, cap)
        self.assertEqual(self.s.store.read_r1().handled_request, marker)

    def test_late_renewal_cannot_move_twenty_minute_cap(self):
        self.leader.try_acquire()
        cap = self.leader.term_expires_at
        self.s.advance(900)
        self.leader.renew()
        self.assertEqual(self.leader.expires_at, cap)
        self.s.advance(cap-self.s.clock.now())
        self.assertFalse(self.leader.can_coordinate(0))
        with self.assertRaises(LeaseLost):
            self.leader.renew()

    def test_can_coordinate_requires_entire_window_and_expiry_is_strict(self):
        self.leader.try_acquire()
        remaining = self.leader.expires_at-self.s.clock.now()-1
        self.assertFalse(self.leader.can_coordinate(remaining+.01))
        self.assertTrue(self.leader.can_coordinate(remaining-.01))
        for value in (-1, True, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                self.leader.can_coordinate(value)
        self.s.advance(remaining)
        self.assertFalse(self.leader.can_coordinate(0))

    def test_confirm_detects_replaced_term_before_operation(self):
        self.leader.try_acquire()
        record = self.s.store.read_r1()
        self.s.inject_r1(replace(record, leader=replace(record.leader, term_id=str(self.s.uid()))))
        effects = []
        with self.assertRaises(LeaseLost):
            self.leader.coordinate(lambda record, window: effects.append(record), 1)
        self.assertEqual(effects, [])

    def test_full_coordination_window_includes_confirmation_and_callback(self):
        self.leader.try_acquire()
        self.s.adapter.read_delay = .4
        self.assertEqual(self.leader.coordinate(lambda r, w: 'done', .5), 'done')
        with self.assertRaises(LeaseLost):
            self.leader.coordinate(lambda r, w: self.s.clock.advance(.2), .5)

    def test_slow_acquisition_and_late_barrier_response_never_enter(self):
        for field, delay in (('write_delay', 1.01), ('read_delay', .4)):
            self.setUp()
            setattr(self.s.adapter, field, delay)
            with self.assertRaises(LeaseLost):
                self.leader.try_acquire()
            self.assertFalse(self.leader.can_coordinate(0))

    def test_pause_after_permitting_read_prevents_even_capture_send(self):
        uid = self.s.uid
        def delayed_uuid():
            self.s.clock.advance(1.01)
            return uid()
        self.leader._uuid_factory = delayed_uuid
        with self.assertRaises(LeaseLost):
            self.leader.try_acquire()
        self.assertEqual(self.s.adapter.writes, [])

    def test_competing_candidates_read_free_before_one_completes_capture(self):
        other = self.s.candidate()
        read = self.s.store.read_r1
        first = True
        def race(**kwargs):
            nonlocal first
            result = read(**kwargs)
            if first:
                first = False
                self.assertTrue(other.try_acquire())
            return result
        with patch.object(self.s.store, 'read_r1', side_effect=race), self.assertRaises(LeaseLost):
            self.leader.try_acquire()
        self.assertEqual(len(self.s.adapter.writes), 1)
        self.assertTrue(other.can_coordinate(0))

    def test_renewal_response_after_old_deadline_cannot_revive_role(self):
        self.leader.try_acquire()
        self.s.advance(self.leader.expires_at-self.s.clock.now()-2.01)
        self.s.adapter.write_delay = 1.2
        with self.assertRaises(LeaseLost):
            self.leader.renew()
        self.assertFalse(self.leader.can_coordinate(0))

    def test_stop_interrupts_capture_barrier_without_free(self):
        def stop(event, seconds):
            self.leader.stop()
            return event.is_set()
        with patch.object(self.s.clock, 'wait', side_effect=stop), self.assertRaises(LeaseLost):
            self.leader.try_acquire()
        self.assertEqual(len(self.s.adapter.writes), 1)
        self.assertIsNotNone(self.s.store.read_r1().leader)

    def test_bounded_confirmation_after_barrier_and_monotonic_clock_only(self):
        wait = self.s.clock.wait
        def delayed(event, seconds):
            self.s.adapter.read_delay = 1.01
            return wait(event, seconds)
        with patch.object(self.s.clock, 'wait', side_effect=delayed), self.assertRaises(LeaseLost):
            self.leader.try_acquire()
        self.setUp()
        with patch.object(self.s.clock, 'wall_now', side_effect=AssertionError('wall read')):
            self.leader.try_acquire()
            self.leader.renew()
            self.leader.release()

    def test_busy_role_and_failed_read_never_emit_free(self):
        self.leader.try_acquire()
        self.assertTrue(self.leader.try_acquire())
        with patch.object(self.s.store, 'read_r1', side_effect=RecordError), self.assertRaises(LeaseLost):
            self.leader.renew()
        self.leader.release()
        self.assertEqual(len(self.s.adapter.writes), 1)

    def test_confirmation_uses_full_declared_window(self):
        self.leader.try_acquire()
        self.s.adapter.read_delay = .6
        with self.assertRaises(LeaseLost):
            self.leader.confirm(.5)

    def test_delayed_confirmation_and_late_renewal_lose_without_write(self):
        self.leader.try_acquire()
        self.s.advance(self.leader.expires_at-self.s.clock.now()-1.5)
        before = len(self.s.adapter.writes)
        with self.assertRaises(LeaseLost):
            self.leader.renew()
        self.assertEqual(len(self.s.adapter.writes), before)

    def test_unknown_capture_renew_do_not_retry_or_write_free(self):
        for operation in ('try_acquire', 'renew'):
            for mode in ('defer', 'lose_response'):
                self.setUp()
                if operation == 'renew':
                    self.leader.try_acquire()
                before = len(self.s.adapter.writes)
                self.s.adapter.write_mode = mode
                with self.assertRaises(LeaseLost):
                    getattr(self.leader, operation)()
                self.leader.release()
                self.assertEqual(len(self.s.adapter.writes), before+1)
                self.assertTrue(self.leader.role_cancelled.is_set())

    def test_release_preserves_horizon_and_fresh_capture_has_new_term(self):
        self.leader.try_acquire()
        record = self.s.store.read_r1()
        self.leader.release()
        self.assertEqual(self.s.store.read_r1(), record)
        self.assertFalse(self.leader.try_acquire())
        self.s.advance(1000 * clock_factor(1000))
        self.assertTrue(self.leader.try_acquire())
        self.assertNotEqual(self.leader.record.term_id, record.leader.term_id)

    def test_unknown_record_is_not_free_and_stop_does_not_release(self):
        self.s.server.apply(self.s.server.send_update(42, {'name': 'unknown metadata'}))
        with self.assertRaises(LeaseLost):
            self.leader.try_acquire()
        self.assertEqual(self.s.adapter.writes, [])
        self.setUp()
        self.leader.try_acquire()
        self.leader.stop()
        with self.assertRaises(LeaseLost):
            self.leader.renew()
        self.assertEqual(len(self.s.adapter.writes), 1)

    def test_incompatible_policy_fails_before_capture(self):
        from zontcontrol.coordination.models import ProtocolProfile, R1Record, encode_r1
        record = R1Record(ProtocolProfile(str(self.s.uid()), 1, 1), str(self.s.uid()))
        self.s.inject_r1(record)
        with self.assertRaises(LeaseLost):
            self.leader.try_acquire()
        self.assertEqual(self.s.adapter.writes, [])

    def test_stale_projection_replay_has_no_executor_writer_capability(self):
        self.leader.try_acquire()
        stale = self.s.store.read_r1()
        self.leader.renew()
        self.assertFalse(hasattr(self.s.gate._stores[42], 'write_r1'))
        self.assertNotEqual(stale.record_id, self.s.store.read_r1().record_id)

    def test_invalid_owner_or_uuid_is_refused_before_io(self):
        from zontcontrol.coordination.leader import LeaderElection
        with self.assertRaises(RecordError):
            LeaderElection(self.s.store, 'bad', LeaseTiming(300, 1, 1))
        self.leader._uuid_factory = lambda: 'invalid'
        with self.assertRaises(LeaseLost):
            self.leader.try_acquire()
        self.assertEqual(self.s.adapter.writes, [])

    def test_freeze_during_planned_wait_and_before_renewal(self):
        wait = self.s.clock.wait
        with patch.object(self.s.clock, 'wait', side_effect=lambda e, s: wait(e, s+1.01)):
            with self.assertRaises(LeaseLost):
                self.leader.try_acquire()
        self.setUp()
        self.leader.try_acquire()
        self.s.clock.advance(1.01)
        before = len(self.s.adapter.writes)
        with self.assertRaises(LeaseLost):
            self.leader.renew()
        self.assertEqual(len(self.s.adapter.writes), before)

    def test_delta_cmd_greater_than_delta_quarantines_new_epoch(self):
        s = Scenario(delta_cmd=100, ready=False)
        self.assertTrue(s.leader.try_acquire())
        self.assertAlmostEqual(s.leader.quarantine_until-s.leader.timeline.now(),
                               100*clock_factor(100), places=6)
        self.assertGreater(s.leader.quarantine_until-s.leader.timeline.now(), s.leader.timing.barrier)

    def test_candidate_refuses_unknown_r2_profile_before_writing_r1(self):
        from zontcontrol.coordination.models import R2Record, encode_r2
        unknown = encode_r2(R2Record(str(self.s.uid())))
        self.s.server.apply(self.s.server.send_update(42, {'notes': unknown}))
        with self.assertRaises(LeaseLost):
            self.leader.try_acquire()
        self.assertEqual(self.s.adapter.writes, [])

    def test_three_candidates_with_free_reads_and_ordered_writes_have_one_survivor(self):
        from zontcontrol.coordination.store import LeaderStore
        s = self.s
        bstore, cstore = s.make_store(LeaderStore), s.make_store(LeaderStore)
        b, c = s.candidate(store=bstore), s.candidate(store=cstore)
        wait = s.clock.wait
        state = {'depth': 0}
        winners = []
        def buffered_first_read(store):
            read = store.read_r1
            first = True
            def callback(**kwargs):
                nonlocal first
                if first:
                    first = False
                    return None  # All initial READs precede all three WRITEs.
                return read(**kwargs)
            return callback
        def interleaving(event, seconds):
            depth = state['depth']
            state['depth'] += 1
            if depth == 2:
                return wait(event, seconds)
            candidate = b if depth == 0 else c
            try:
                if candidate.try_acquire():
                    winners.append(candidate)
            except LeaseLost:
                pass
            return False
        with patch.object(bstore, 'read_r1', side_effect=buffered_first_read(bstore)), \
                patch.object(cstore, 'read_r1', side_effect=buffered_first_read(cstore)), \
                patch.object(s.clock, 'wait', side_effect=interleaving):
            with self.assertRaises(LeaseLost):
                self.leader.try_acquire()
        self.assertEqual(winners, [c])
        self.assertEqual(len(s.adapter.writes), 3)
        self.assertEqual(s.store.read_r1().leader, c.record)
        self.assertFalse(self.leader.can_coordinate(0))
        self.assertFalse(b.can_coordinate(0))
        self.assertTrue(c.can_coordinate(0))

    def test_pause_after_last_guard_is_observed_as_residual_and_never_revives_role(self):
        # The documented residual pause happens after the local check. The
        # client cannot retract the server write, but must reject its late ack.
        update = self.s.adapter.update_metadata
        def paused(*args, **kwargs):
            self.s.clock.advance(1.01)
            return update(*args, **kwargs)
        with patch.object(self.s.adapter, 'update_metadata', side_effect=paused):
            with self.assertRaises(LeaseLost):
                self.leader.try_acquire()
        self.assertEqual(len(self.s.adapter.writes), 1)
        self.assertIsNotNone(self.s.store.read_r1().leader)
        self.assertFalse(self.leader.can_coordinate(0))
