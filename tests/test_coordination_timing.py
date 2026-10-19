"""Offline v2 clock projections and bounded operations, independent of v1 runtime."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from threading import Event
import unittest

from tests.fakes.clock import FakeClock
from tests.fakes.uuid import uuid_generator
from zontcontrol.coordination import clock as clocks
from zontcontrol.coordination import timing


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.ids = uuid_generator()
        self.clock = FakeClock(start=700, wall_start=datetime(2030, 1, 1, tzinfo=timezone.utc))

    def snapshot(self, **kwargs):
        return clocks.LeaderTimeline.capture(self.clock, delta=5,
                                            epoch_sampler=self.clock.epoch_now,
                                            uuid_factory=self.ids, **kwargs)

    def test_fixed_snapshot_ignores_ntp_and_advances_through_suspend(self):
        timeline = self.snapshot()
        initial = timeline.now()
        self.clock.jump_wall(-100000)
        self.clock.advance(40)
        self.assertEqual(timeline.now(), initial + 40)
        self.clock.jump_wall(200000)
        self.assertEqual(timeline.now(), initial + 40)
        self.ids.assert_called_once_with()

    def test_each_incarnation_has_new_clock_id_and_independent_epoch(self):
        first = self.snapshot()
        self.clock.jump_wall(10000)
        second = self.snapshot()
        self.assertNotEqual(first.clock_id, second.clock_id)
        self.assertEqual(second.now() - first.now(), 10000)

    def test_ambiguous_snapshot_is_rejected_instead_of_using_last_read_as_start(self):
        def delayed_epoch():
            self.clock.advance(5.001)
            return self.clock.epoch_now()

        with self.assertRaises(timing.TimingViolation):
            clocks.LeaderTimeline.capture(self.clock, delta=5, epoch_sampler=delayed_epoch,
                                         uuid_factory=self.ids)

    def test_snapshot_at_delta_is_accepted_and_sampler_is_called_only_once(self):
        calls = []

        def sample():
            calls.append(1)
            self.clock.advance(5)
            return self.clock.epoch_now()

        timeline = clocks.LeaderTimeline.capture(self.clock, delta=5, epoch_sampler=sample,
                                                uuid_factory=self.ids)
        initial = timeline.now()
        self.clock.advance(1)
        self.assertEqual(timeline.now(), initial + 1)
        self.assertEqual(calls, [1])

    def test_invalid_epoch_and_uuid_are_rejected(self):
        for epoch in (True, float('nan'), float('inf')):
            with self.subTest(epoch=epoch), self.assertRaises(ValueError):
                clocks.LeaderTimeline.capture(self.clock, delta=5,
                                             epoch_sampler=lambda: epoch, uuid_factory=self.ids)
        with self.assertRaises(ValueError):
            clocks.LeaderTimeline.capture(self.clock, delta=5, epoch_sampler=self.clock.epoch_now,
                                         uuid_factory=lambda: 'not-a-uuid')


class ClockSampleTests(unittest.TestCase):
    def setUp(self):
        self.ids = uuid_generator()
        self.clock_id, self.nonce = self.ids(), self.ids()

    def sample(self, **overrides):
        fields = dict(clock_id=self.clock_id, nonce=self.nonce, sampled_at=1000000,
                      sent_at=100, received_at=102, lease_seconds=60, clock_rho=.1)
        return clocks.ClockSample(**(fields | overrides))

    def test_immutable_sample_and_exact_projection_with_future_start(self):
        sample = self.sample()
        factor = clocks.clock_factor(60)
        interval = sample.project(valid_from=1000003, valid_until=1000060 * 1.0,
                                  local_lease_deadline=160)
        self.assertAlmostEqual(interval.usable_from, 102 + factor * 3)
        self.assertAlmostEqual(interval.expires_at, 100 + 60 / factor)
        self.assertFalse(interval.contains(interval.usable_from - .001))
        self.assertTrue(interval.contains(interval.usable_from))
        self.assertFalse(interval.contains(interval.expires_at))
        with self.assertRaises(FrozenInstanceError):
            sample.received_at = 105
        with self.assertRaises(FrozenInstanceError):
            interval.expires_at = 200

    def test_send_not_ack_is_origin_and_fixed_local_deadline_wins(self):
        sample = self.sample(received_at=110)
        interval = sample.project(valid_from=1000000, valid_until=1000000 + 60 * clocks.clock_factor(60),
                                  local_lease_deadline=159)
        self.assertEqual(interval.usable_from, 110)
        self.assertAlmostEqual(interval.expires_at, 159)

    def test_repeated_read_neither_reanchors_sample_nor_extends_lease(self):
        sample = self.sample()
        args = dict(valid_from=1000000, valid_until=1000060, local_lease_deadline=160)
        original = sample.project(**args)
        observer = FakeClock(start=102)
        for elapsed in (1, 10, 100):
            observer.advance(elapsed)
            self.assertEqual(sample.project(**args), original)
        self.assertFalse(original.contains(observer.now()))

    def test_slow_reply_or_future_start_can_leave_no_usable_interval(self):
        for sample, start, end in ((self.sample(received_at=159), 1000000, 1000060),
                                   (self.sample(), 1000100, 1000110)):
            with self.subTest(start=start), self.assertRaises(timing.TimingViolation):
                sample.project(valid_from=start, valid_until=end, local_lease_deadline=160)

    def test_bounds_cover_independent_offsets_epochs_and_extreme_clock_rates(self):
        for period in (60, 300, 600, 1000):
            rho = clocks.clock_rho(period)
            for leader_rate, observer_rate in ((1-rho, 1+rho), (1+rho, 1-rho), (1, 1)):
                with self.subTest(period=period, rates=(leader_rate, observer_rate)):
                    leader = FakeClock(start=7000, rate=leader_rate,
                                       wall_start=datetime(2040, 1, 1, tzinfo=timezone.utc))
                    observer = FakeClock(start=30, rate=observer_rate,
                                         wall_start=datetime(2020, 1, 1, tzinfo=timezone.utc))
                    timeline = clocks.LeaderTimeline.capture(leader, delta=5,
                                                            epoch_sampler=leader.epoch_now,
                                                            uuid_factory=self.ids)
                    sent = observer.now()
                    leader.advance(1); observer.advance(1)
                    tau = timeline.now()
                    leader.advance(1); observer.advance(1)
                    sample = self.sample(clock_id=timeline.clock_id, sent_at=sent,
                                         received_at=observer.now(), sampled_at=tau,
                                         lease_seconds=period, clock_rho=rho)
                    leader.jump_wall(-100000000)
                    observer.jump_wall(100000000)
                    for duration in (0, 1, period / 2):
                        leader.advance(duration); observer.advance(duration)
                        lower, upper = sample.leader_bounds(observer.now())
                        self.assertLessEqual(lower, timeline.now() + 1e-5)
                        self.assertGreaterEqual(upper + 1e-5, timeline.now())

    def test_original_period_keeps_rho_when_adjusted_interval_crosses_band(self):
        for period in (60, 300, 600):
            sample = self.sample(lease_seconds=period, clock_rho=clocks.clock_rho(period))
            interval = sample.project(valid_from=sample.sampled_at,
                                      valid_until=sample.sampled_at + period * clocks.clock_factor(period),
                                      local_lease_deadline=100 + period)
            self.assertAlmostEqual(interval.expires_at, 100 + period)

    def test_invalid_samples_and_noncausal_times_are_rejected(self):
        for fields in ({'sent_at': 103}, {'clock_rho': .05}, {'lease_seconds': True},
                       {'sampled_at': float('inf')}, {'received_at': float('nan')},
                       {'nonce': 'invalid'}, {'clock_id': 'invalid'}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.sample(**fields)
        with self.assertRaises(ValueError):
            self.sample().leader_bounds(101)
        with self.assertRaises(ValueError):
            self.sample().project(valid_from=2, valid_until=1, local_lease_deadline=160)


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(start=100)

    def test_original_start_is_not_reset_by_io_or_last_write_guard(self):
        window = timing.OperationWindow(self.clock, 5)
        self.clock.advance(3)
        self.assertEqual(window.remaining, 2)
        self.assertEqual(window.before_io(), 2)
        self.assertEqual(window.guard_write(role_deadline=120, stop_margin=5), 2)
        self.clock.advance(2)
        self.assertEqual(window.started_at, 100)
        self.assertEqual(window.deadline, 105)
        with self.assertRaises(timing.TimingViolation):
            window.guard_write(role_deadline=120, stop_margin=5)

    def test_response_at_deadline_fits_but_late_ack_is_terminal(self):
        window = timing.OperationWindow(self.clock, 5)
        self.clock.advance(5)
        self.assertEqual(window.complete(), 5)
        self.clock.advance(.001)
        with self.assertRaises(timing.TimingViolation):
            window.complete()
        with self.assertRaises(timing.TimingViolation):
            window.before_io()

    def test_role_must_cover_remaining_full_window_and_stop_margin(self):
        window = timing.OperationWindow(self.clock, 5)
        self.clock.advance(1)
        self.assertEqual(window.guard_write(role_deadline=110, stop_margin=5), 4)
        with self.assertRaises(timing.TimingViolation):
            window.guard_write(role_deadline=109.999, stop_margin=5)
        with self.assertRaises(timing.TimingViolation):
            window.complete(role_deadline=101)

    def test_freeze_guard_accounts_for_expected_wakeup_not_planned_wait(self):
        guard = timing.FreezeGuard(self.clock, 5)
        guard.expect_resume(110)
        self.assertFalse(self.clock.wait(Event(), 10))
        self.assertEqual(guard.check(), 0)
        self.clock.advance(5)
        self.assertEqual(guard.check(), 5)
        self.clock.advance(.001)
        with self.assertRaises(timing.TimingViolation):
            guard.check()

    def test_guard_keeps_original_expected_resume_and_rejects_suspend(self):
        window = timing.OperationWindow(self.clock, 5, expected_resume_at=90)
        with self.assertRaises(timing.TimingViolation):
            window.guard_write(role_deadline=120)
        fresh = timing.OperationWindow(self.clock, 5)
        self.clock.advance(6)
        with self.assertRaises(timing.TimingViolation):
            fresh.before_io()

    def test_invalid_windows_and_guards_are_rejected(self):
        for seconds in (0, -1, True, float('inf'), float('nan')):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                timing.OperationWindow(self.clock, seconds)
        with self.assertRaises(ValueError):
            timing.OperationWindow(self.clock, 5, started_at=101)
        with self.assertRaises(ValueError):
            timing.FreezeGuard(self.clock, 5).expect_resume(float('nan'))


class V2LeaseTimingTests(unittest.TestCase):
    def test_bands_barrier_stop_margin_quarantine_and_continuous_budget(self):
        for period, rho in ((60, .1), (60.01, .05), (300, .05),
                            (300.01, .01), (600, .01), (600.01, .005)):
            with self.subTest(period=period):
                policy = timing.V2LeaseTiming(period, 5, 5)
                self.assertEqual(policy.clock_rho, rho)
                self.assertEqual(policy.barrier, 10)
                self.assertEqual(policy.stop_margin, 5)
                self.assertEqual(policy.quarantine, 5 * clocks.clock_factor(5))
                self.assertEqual(policy.command_guard, policy.quarantine)
                self.assertAlmostEqual(policy.lease_s, period * clocks.clock_factor(period))
                self.assertAlmostEqual(policy.term_budget, clocks.LeaseTiming(period, 1).term_budget)

    def test_large_command_guard_uses_its_own_rho_and_can_make_period_infeasible(self):
        policy = timing.V2LeaseTiming(600, 5, 100)
        self.assertEqual(policy.quarantine, 100 * clocks.clock_factor(100))
        self.assertGreater(policy.quarantine, policy.barrier)
        self.assertNotEqual(policy.quarantine, 100 * clocks.clock_factor(600))
        with self.assertRaises(ValueError):
            timing.V2LeaseTiming(60, 5, 100)

    def test_deadlines_start_at_send_and_renewal_never_moves_term_limit(self):
        policy = timing.V2LeaseTiming(300, 5, 5)
        first_send = 100
        self.assertEqual(policy.lease_deadline(first_send, term_started_at=first_send), 400)
        renewed = policy.lease_deadline(1200, term_started_at=first_send)
        self.assertAlmostEqual(renewed, first_send + policy.term_budget)
        self.assertEqual(policy.lease_deadline(1250, term_started_at=first_send), renewed)

    def test_minimum_eight_delta_and_twenty_minute_adjusted_limit(self):
        timing.V2LeaseTiming(40, 5, 1)
        for args in ((39.999, 5, 1), (1200, 5, 5), (300, 0, 5),
                     (300, 5, 0), (True, 5, 5), (300, 5, float('inf'))):
            with self.subTest(args=args), self.assertRaises(ValueError):
                timing.V2LeaseTiming(*args)
        policy = timing.V2LeaseTiming(300, 5, 5)
        with self.assertRaises(FrozenInstanceError):
            policy.period = 400
