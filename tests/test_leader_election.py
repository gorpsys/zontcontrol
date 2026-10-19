"""Conditional leader leases on virtual clocks and non-CAS metadata only."""

from dataclasses import replace
from threading import Event
import unittest
from unittest.mock import patch

from zontcontrol.coordination.clock import BootClock, LeaseTiming, clock_factor, clock_rho
from zontcontrol.coordination.leader import LeaderElection, LeaseLost
from zontcontrol.coordination.models import LeaderRecord, R1Record, RecordError
from zontcontrol.coordination.store import LeaderStore
from tests.fakes.clock import FakeClock
from tests.fakes.zont import FakeMetadataAdapter, FakeZontServer
from tests.test_coordination_store import OWNER, PREFIX, request


OTHER = "82f6c16d-2aca-45ce-8891-1072eb0a7c04"


class TimingTests(unittest.TestCase):
    def test_policy_boundaries_and_independent_short_window_and_term_factors(self):
        for period, rho in ((60, .1), (60.01, .05), (300, .05),
                            (300.01, .01), (600, .01), (600.01, .005)):
            with self.subTest(period=period):
                timing = LeaseTiming(period, 1)
                self.assertEqual(clock_rho(period), rho)
                self.assertEqual(timing.clock_rho, rho)
                self.assertAlmostEqual(timing.lease_s, period * clock_factor(period))
                self.assertAlmostEqual(timing.valid_for, period)
                self.assertAlmostEqual(timing.renew_after, period / 3)
                self.assertAlmostEqual(timing.poll_after, timing.lease_s / 10)
                self.assertEqual(timing.barrier, 2)
                self.assertEqual(timing.delta_factor, clock_factor(1))
                self.assertEqual(timing.term_budget, 1200 / clock_factor(1200))

    def test_expected_work_and_jitter_validation(self):
        self.assertAlmostEqual(LeaseTiming(90, 5, expected_work=60).valid_for, 90)
        self.assertGreater(LeaseTiming(90, 5, barrier_jitter=1, poll_jitter=2).barrier, 10)
        for args in ((7.99, 1), (1200, 1), (90, 5, 70), (60, 0),
                     (True, 1), (float("nan"), 1), (60, float("inf"))):
            with self.subTest(args=args), self.assertRaises(ValueError):
                LeaseTiming(*args)
        for kwargs in ({"barrier_jitter": -1}, {"poll_jitter": -1}):
            with self.assertRaises(ValueError):
                LeaseTiming(60, 1, **kwargs)

    def test_boottime_is_required_and_wait_is_interruptible(self):
        with patch("zontcontrol.coordination.clock.time") as time:
            time.CLOCK_BOOTTIME = 7
            time.clock_gettime.return_value = 123
            self.assertEqual(BootClock().now(), 123)
            time.clock_gettime.assert_called_with(7)
            del time.CLOCK_BOOTTIME
            with self.assertRaises(RuntimeError):
                BootClock()
        for error in (OSError, ValueError):
            with self.subTest(error=error), patch(
                    "zontcontrol.coordination.clock.time.clock_gettime", side_effect=error):
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
            holder = FakeClock(rate=1 - rho)
            observer = FakeClock(rate=1 + rho)
            timing = LeaseTiming(period, 1)
            elapsed_real = timing.lease_s / (1 + rho)
            holder.advance(elapsed_real)
            observer.advance(elapsed_real)
            self.assertAlmostEqual(observer.now(), timing.lease_s)
            self.assertGreaterEqual(holder.now() + 1e-10, timing.valid_for)

    def test_short_delta_and_term_never_reuse_long_lease_rho(self):
        timing = LeaseTiming(1000, 60)
        self.assertEqual(timing.clock_rho, .005)
        self.assertEqual(timing.delta_factor, clock_factor(60))
        self.assertGreater(timing.delta_factor, clock_factor(1000))
        self.assertGreater(timing.barrier, timing.delta_factor * 60)
        short = LeaseTiming(60, 1)
        self.assertAlmostEqual(short.term_budget, 1188.0597014925374)
        self.assertNotEqual(short.term_budget, 1200 / clock_factor(60))


class ElectionTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(start=100)
        self.server = FakeZontServer({42: {"name": PREFIX, "notes": None}}, clock=self.clock)
        self.adapter = FakeMetadataAdapter(self.server)
        self.store = LeaderStore(self.adapter, 42, original_name=PREFIX,
                                 notes_capacity=10000, name_capacity=10000)
        self.timing = LeaseTiming(60, 1)
        self.leader = self.candidate()

    def candidate(self, owner=OWNER, **kwargs):
        return LeaderElection(self.store, owner, kwargs.pop("timing", self.timing),
                              clock=self.clock, **kwargs)

    def test_free_acquisition_barrier_and_deadline_from_send(self):
        self.adapter.write_delay = .25
        self.assertTrue(self.leader.try_acquire())
        self.assertAlmostEqual(self.clock.now(), 102.25)
        self.assertEqual(self.leader.expires_at, 160)
        self.assertEqual(self.leader.term_expires_at, 100 + self.timing.term_budget)
        self.assertEqual(self.leader.record, self.store.read_r1().leader)
        self.assertTrue(self.leader.can_coordinate(1))
        self.assertFalse(self.leader.role_cancelled.is_set())
        self.assertEqual(self.server.read_device(42)["name"], PREFIX)

    def test_no_actions_during_barrier_and_overwritten_candidate_loses(self):
        other = self.candidate(OTHER)
        original_wait = self.clock.wait

        def race(event, duration):
            self.assertFalse(self.leader.can_coordinate(.1))
            with patch.object(self.clock, "wait", wraps=original_wait):
                self.assertFalse(other.try_acquire())  # still unexpired
            changed = replace(self.store.read_r1().leader, owner=OTHER, token="b" * 32)
            self.store.write_r1(R1Record(changed))
            return original_wait(event, duration)

        with patch.object(self.clock, "wait", side_effect=race), self.assertRaises(LeaseLost):
            self.leader.try_acquire()
        self.assertTrue(self.leader.role_cancelled.is_set())
        self.assertEqual(len(self.adapter.writes), 2)

    def test_expiry_observation_starts_at_response_and_ends_at_last_read_send(self):
        self.assertTrue(self.leader.try_acquire())
        other = self.candidate(OTHER)
        self.adapter.read_delay = .1
        self.assertFalse(other.try_acquire())
        first_response = self.clock.now()
        self.clock.advance(self.leader.record.lease_s - .05)
        self.assertFalse(other.try_acquire())  # response past L, send still before L
        self.assertGreater(self.clock.now() - first_response, self.leader.record.lease_s)
        self.assertTrue(other.try_acquire())
        self.assertEqual(self.store.read_r1().leader.owner, OTHER)

    def test_observation_resets_for_every_leader_field_but_not_request(self):
        for field, change in (("owner", OTHER), ("token", "c" * 32), ("seq", 1),
                              ("lease_s", 120 * clock_factor(120)), ("clock_rho", .05)):
            self.setUp()
            self.leader.try_acquire()
            other = self.candidate(OTHER)
            self.assertFalse(other.try_acquire())
            old = self.store.read_r1().leader
            changes = {field: change}
            if field == "lease_s":
                changes["clock_rho"] = .05
            if field == "clock_rho":
                changes["lease_s"] = 120 * clock_factor(120)
            self.store.write_r1(R1Record(replace(old, **changes)))
            self.clock.advance(old.lease_s + 1)
            self.assertFalse(other.try_acquire())
        self.setUp()
        self.leader.try_acquire()
        other = self.candidate(OTHER)
        self.assertFalse(other.try_acquire())
        old = self.store.read_r1().leader
        self.store.write_r1(R1Record(old, request()))
        self.clock.advance(old.lease_s)
        self.assertTrue(other.try_acquire())
        self.assertEqual(self.store.read_r1().request, request())

    def test_renew_grows_seq_preserves_request_and_cap(self):
        self.leader.try_acquire()
        initial = self.leader.record
        cap = self.leader.term_expires_at
        self.store.write_r1(R1Record(initial, request()))
        self.clock.advance(20)
        self.leader.renew()
        self.assertEqual(self.leader.record.seq, 1)
        self.assertEqual(self.leader.record.token, initial.token)
        self.assertEqual(self.leader.expires_at, self.clock.now() + 60)
        self.assertEqual(self.leader.term_expires_at, cap)
        self.assertEqual(self.store.read_r1().request, request())

    def test_late_renewal_cannot_move_twenty_minute_cap(self):
        self.leader = self.candidate(timing=LeaseTiming(1100, 1))
        self.leader.try_acquire()
        cap = self.leader.term_expires_at
        self.clock.advance(1000)
        self.leader.renew()
        self.assertEqual(self.leader.expires_at, cap)
        self.clock.advance(cap - self.clock.now())
        self.assertFalse(self.leader.can_coordinate(0))
        self.assertTrue(self.leader.role_cancelled.is_set())
        with self.assertRaises(LeaseLost):
            self.leader.renew()

    def test_can_coordinate_requires_entire_window_and_expiry_is_strict(self):
        self.leader.try_acquire()
        remaining = self.leader.expires_at - self.clock.now()
        self.assertFalse(self.leader.can_coordinate(remaining))
        self.assertTrue(self.leader.can_coordinate(remaining - .1))
        for duration in (-1, True, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                self.leader.can_coordinate(duration)
        self.clock.advance(remaining)
        self.assertFalse(self.leader.can_coordinate(0))

    def test_confirm_detects_replaced_term_before_operation(self):
        self.leader.try_acquire()
        self.store.write_r1(R1Record(replace(self.leader.record, token="d" * 32)))
        effects = []
        with self.assertRaises(LeaseLost):
            self.leader.coordinate(lambda record: effects.append(record), 1)
        self.assertEqual(effects, [])
        self.assertTrue(self.leader.role_cancelled.is_set())

    def test_full_coordination_window_includes_confirmation_and_callback(self):
        self.leader.try_acquire()
        self.adapter.read_delay = .4
        result = self.leader.coordinate(lambda record: "done", .5)
        self.assertEqual(result, "done")
        with self.assertRaises(LeaseLost):
            self.leader.coordinate(lambda record: self.clock.advance(.2), .5)

    def test_slow_acquisition_and_late_barrier_response_never_enter(self):
        for delay, phase in ((1.01, "write"), (.4, "read")):
            self.setUp()
            setattr(self.adapter, phase + "_delay", delay)
            with self.assertRaises(LeaseLost):
                self.leader.try_acquire()
            self.assertFalse(self.leader.can_coordinate(0))
        self.setUp()
        original = self.clock.wait
        with patch.object(self.clock, "wait", side_effect=lambda e, s: original(e, 100)):
            with self.assertRaises(LeaseLost):
                self.leader.try_acquire()

    def test_pause_after_permitting_read_prevents_even_capture_send(self):
        def delayed_token():
            self.clock.advance(1.01)
            return "e" * 32

        candidate = self.candidate(token_factory=delayed_token)
        with self.assertRaises(LeaseLost):
            candidate.try_acquire()
        self.assertEqual(self.adapter.writes, [])

    def test_competing_candidates_read_free_before_one_completes_capture(self):
        other = self.candidate(OTHER)
        read = self.store.read_r1
        first = True

        def racing_read():
            nonlocal first
            result = read()
            if first:
                first = False
                self.assertTrue(other.try_acquire())
            return result

        with patch.object(self.store, "read_r1", side_effect=racing_read):
            with self.assertRaises(LeaseLost):
                self.leader.try_acquire()
        self.assertEqual(self.store.read_r1().leader.owner, OTHER)
        self.assertEqual(len(self.adapter.writes), 1)
        self.assertFalse(self.leader.can_coordinate(0))
        self.assertTrue(other.can_coordinate(0))

    def test_renewal_response_after_old_deadline_cannot_revive_role(self):
        self.leader.try_acquire()
        self.clock.advance(56.9)  # old remaining 1.1, enough to start Delta=1
        self.adapter.write_delay = 1.2
        with self.assertRaises(LeaseLost):
            self.leader.renew()
        self.assertTrue(self.leader.role_cancelled.is_set())
        self.assertFalse(self.leader.can_coordinate(0))

    def test_stop_interrupts_capture_barrier_without_free(self):
        def stopped_wait(event, seconds):
            self.leader.stop()
            return event.is_set()

        with patch.object(self.clock, "wait", side_effect=stopped_wait):
            with self.assertRaises(LeaseLost):
                self.leader.try_acquire()
        self.assertEqual(len(self.adapter.writes), 1)
        self.assertIsNotNone(self.store.read_r1().leader)

    def test_bounded_confirmation_after_barrier_and_monotonic_clock_only(self):
        wait = self.clock.wait

        def delayed_confirmation(event, seconds):
            self.adapter.read_delay = 1.01
            return wait(event, seconds)

        with patch.object(self.clock, "wait", side_effect=delayed_confirmation):
            with self.assertRaises(LeaseLost):
                self.leader.try_acquire()
        self.assertFalse(self.leader.can_coordinate(0))
        self.setUp()
        with patch.object(self.clock, "wall_now", side_effect=AssertionError("wall clock used")):
            self.leader.try_acquire()
            self.leader.renew()
            self.leader.release()

    def test_busy_role_and_failed_read_never_emit_free(self):
        self.leader.try_acquire()
        self.assertTrue(self.leader.try_acquire())
        self.assertEqual(len(self.adapter.writes), 1)
        with patch.object(self.store, "read_r1", side_effect=RecordError):
            with self.assertRaises(LeaseLost):
                self.leader.renew()
        with self.assertRaises(LeaseLost):
            self.leader.release()
        self.assertEqual(len(self.adapter.writes), 1)

    def test_confirmation_uses_full_declared_window(self):
        self.leader.try_acquire()
        self.adapter.read_delay = .6
        with self.assertRaises(LeaseLost):
            self.leader.confirm(.5)
        self.assertFalse(self.leader.can_coordinate(0))

    def test_delayed_confirmation_and_late_renewal_lose_without_write(self):
        self.leader.try_acquire()
        self.clock.advance(57.5)
        before = len(self.adapter.writes)
        with self.assertRaises(LeaseLost):
            self.leader.renew()
        self.assertEqual(len(self.adapter.writes), before)
        self.setUp()
        self.leader.try_acquire()
        self.adapter.read_delay = 1.01
        with self.assertRaises(LeaseLost):
            self.leader.confirm(2)

    def test_unknown_capture_renew_release_do_not_retry_or_write_free(self):
        for operation in ("try_acquire", "renew", "release"):
            for mode in ("defer", "lose_response"):
                self.setUp()
                if operation != "try_acquire":
                    self.leader.try_acquire()
                before = len(self.adapter.writes)
                self.adapter.write_mode = mode
                with self.subTest(operation=operation, mode=mode), self.assertRaises(LeaseLost):
                    getattr(self.leader, operation)()
                self.assertEqual(len(self.adapter.writes), before + 1)
                self.assertTrue(self.leader.role_cancelled.is_set())
                with self.assertRaises(LeaseLost):
                    self.leader.release()
                self.assertEqual(len(self.adapter.writes), before + 1)

    def test_release_preserves_request_and_fresh_capture_has_new_token(self):
        self.leader.try_acquire()
        token = self.leader.record.token
        self.store.write_r1(R1Record(self.leader.record, request()))
        cancelled = self.leader.role_cancelled
        self.leader.release()
        self.assertEqual(self.store.read_r1(), R1Record(request=request()))
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.leader.can_coordinate(0))
        self.assertTrue(self.leader.try_acquire())
        self.assertNotEqual(self.leader.record.token, token)
        self.assertTrue(cancelled.is_set())  # old role remains cancelled

    def test_unknown_record_is_not_free_and_cooperative_stop_does_not_release(self):
        pending = self.server.send_update(42, {"notes": "unknown metadata"})
        self.server.apply(pending)
        with self.assertRaises(LeaseLost):
            self.leader.try_acquire()
        self.assertEqual(self.adapter.writes, [])
        self.setUp()
        self.leader.try_acquire()
        self.leader.stop()
        self.assertTrue(self.leader.role_cancelled.is_set())
        with self.assertRaises(LeaseLost):
            self.leader.renew()
        self.assertEqual(len(self.adapter.writes), 1)

    def test_incompatible_policy_fails_before_capture(self):
        raw = '{"v":1,"leader":{"owner":"' + OWNER + '","token":"' + "a" * 32
        raw += '","seq":0,"lease_s":60,"clock_rho":0.005},"request":null}'
        change = self.server.send_update(42, {"notes": raw})
        self.server.apply(change)
        with self.assertRaises(LeaseLost):
            self.leader.try_acquire()
        self.assertEqual(self.adapter.writes, [])

    def test_stale_projection_replay_is_an_accepted_model_limitation(self):
        self.leader.try_acquire()
        stale = self.store.read_r1()
        self.leader.renew()
        self.store.write_r1(stale)  # non-CAS executor can replay an older projection
        self.assertNotEqual(self.store.read_r1().leader, self.leader.record)
        with self.assertRaises(LeaseLost):
            self.leader.confirm(1)

    def test_invalid_owner_or_token_is_refused_before_io(self):
        with self.assertRaises(RecordError):
            self.candidate("not-uuid")
        candidate = self.candidate(token_factory=lambda: "invalid")
        with self.assertRaises(RecordError):
            candidate.try_acquire()
        self.assertEqual(self.adapter.writes, [])
