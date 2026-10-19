"""Adversarial schedules and independent physical oracle; no reliability claim."""

from dataclasses import replace
import unittest

from tests.fakes.protocol import Scenario
from zontcontrol.control_types import (CommandOutcomeUnknown, ControlOwnershipRequired,
                                      DeviceControlBusy, RateLimited)
from zontcontrol.controller.ownership import CommandExecutor
from zontcontrol.coordination.grants import GrantCoordinator
from zontcontrol.coordination.leader import LeaseLost
from zontcontrol.coordination.models import encode_r1
from zontcontrol.coordination.statistics import Statistics, analyze
from zontcontrol.coordination.timing import OperationWindow


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.s = Scenario()
        self.rows = []
        self.stats = Statistics(self.rows.append, protocol_version='2',
                                profile='adversarial', source='counterexample')
        self.s.gate._statistics = self.stats
        self.s.coordinator._statistics = self.stats
        self.s.leader._statistics = self.stats

    def counts(self):
        return analyze(self.rows)['groups'][0]['events']

    def test_real_protocol_events_lost_request_repeat_heartbeat_cleanup(self):
        s = self.s
        s.pending()
        s.gate._stores[42].publish_request(None, window=OperationWindow(s.clock, 1))
        s.pending()
        s.coordinator.step()
        session = s.gate.try_acquire(s.owner, 42, lease_seconds=60)
        with self.assertRaises(DeviceControlBusy):
            s.gate.renew(s.owner, session, lease_seconds=60)
        s.coordinator.step()
        session = s.gate.renew(s.owner, session, lease_seconds=60)
        s.gate.release(s.owner, session)
        s.coordinator.step()
        self.assertTrue(s.store.read_r1().grants)
        s.advance(80)
        s.coordinator.step()
        self.assertEqual(s.store.read_r1().grants, ())
        counts = self.counts()
        self.assertEqual(counts['grant'], 1)
        self.assertEqual(counts['grant_ack'], 2)
        self.assertEqual(counts['request_lost'], 1)
        self.assertEqual(counts['request_repeated'], 1)
        self.assertEqual(counts['cleanup'], 1)
        self.assertEqual(counts['finished'], 1)
        self.assertIn('heartbeat', counts)
        self.assertEqual(analyze(self.rows)['groups'][0]['episodes']['success'], 0)

    def test_lost_finished_waits_for_expiry_without_recovery(self):
        s = self.s
        session = s.acquire(seconds=10)
        first = s.store.read_r1().grants[0]
        s.adapter.write_mode = 'defer'
        with self.assertRaises(CommandOutcomeUnknown):
            s.gate.release(s.owner, session)
        deferred = s.server.requests[-1]
        s.adapter.write_mode = 'apply'
        s.pending(owner=s.uid())
        second = s.coordinator.step()
        self.assertEqual(s.store.read_r1().grants, (first, second))
        self.assertGreaterEqual(second.valid_from, first.valid_until+s.leader.timing.command_guard)
        s.advance(20)
        s.coordinator.step()
        s.server.apply(deferred)
        s.coordinator.step()
        self.assertEqual(s.store.read_r1().grants, (second,))
        self.assertTrue(s.server.out_of_model_faults)
        self.assertGreater(analyze(self.rows)['groups'][0]['episodes']['unknown'], 0)
        self.assertEqual(self.counts()['timeout'], 1)

    def test_leader_change_during_active_grant_is_observed_without_replacement(self):
        s = self.s
        session = s.acquire()
        old = s.gate._held[s.owner, 42].grant
        record = s.store.read_r1()
        s.inject_r1(replace(record, grants=(), leader=replace(record.leader,
            term_id=str(s.uid()), clock_id=str(s.uid()))))
        s.gate.validate(s.owner, session)
        self.assertIs(s.gate._held[s.owner, 42].grant, old)
        self.assertFalse(s.leader.role_cancelled.is_set())
        with self.assertRaises(LeaseLost):
            s.coordinator.step()
        self.assertTrue(s.leader.role_cancelled.is_set())
        s.gate.validate(s.owner, session)

    def test_late_apply_after_unknown_ack_never_becomes_success_from_polling(self):
        s = self.s
        s.adapter.write_mode = 'defer'
        with self.assertRaises(CommandOutcomeUnknown):
            s.gate.try_acquire(s.owner, 42, lease_seconds=60)
        sent = s.server.requests[-1]
        s.adapter.write_mode = 'apply'
        s.advance(2)
        s.server.apply(sent)
        self.assertTrue(s.server.out_of_model_faults)
        s.coordinator.step()
        session = s.gate.try_acquire(s.owner, 42, lease_seconds=60)
        s.gate.validate(s.owner, session)
        writes = len(s.adapter.writes)
        for _ in range(10):
            s.coordinator.step()
        self.assertEqual(len(s.adapter.writes), writes)
        group = analyze(self.rows)['groups'][0]
        self.assertEqual(group['episodes']['unknown'], 1)
        self.assertEqual(group['episodes']['success'], 0)

    def test_429_and_delta_breaches_have_distinct_events_and_no_retry(self):
        s = self.s
        s.adapter.write_error = RateLimited()
        before = len(s.server.requests)
        with self.assertRaises(RateLimited):
            s.gate.try_acquire(s.owner, 42, lease_seconds=60)
        self.assertEqual(len(s.server.requests), before+1)
        self.assertEqual(self.counts()['rate_limited'], 1)
        s.adapter.write_error = None
        session = s.acquire()
        s.adapter.read_delay = .6
        # Cached admission is local; slow metadata cannot revoke its interval.
        s.gate.validate(s.owner, session)
        with self.assertRaises(LeaseLost):
            s.coordinator.step()
        s.gate.validate(s.owner, session)
        self.assertIn('rtt', self.counts())
        self.assertEqual(self.counts()['leader_lost'], 1)

    def test_broken_sink_does_not_change_accepted_command_finally_or_exception(self):
        s = self.s
        class Broken:
            def emit(self, *args, **kwargs):
                raise OSError('SECRET')
        s.gate._statistics = Broken()
        executor = CommandExecutor(gate=s.gate, clock=s.clock.now, uuid_factory=s.uid)
        binding = executor.bind()
        with self.assertLogs('zontcontrol.coordination.statistics', level='WARNING') as logs:
            with self.assertRaises(DeviceControlBusy):
                binding.try_acquire_control(42, lease_seconds=60)
            s.coordinator.step()
            session = binding.try_acquire_control(42, lease_seconds=60)
            command = type('Command', (), {'device_id': 42})()
            sent = []
            original = RuntimeError('command failed')
            def failing_send(value):
                sent.append(value)
                raise original
            with self.assertRaises(RuntimeError) as caught:
                try:
                    binding.execute(command, session, lambda: 'on', failing_send)
                finally:
                    binding.execute(command, session, lambda: 'off', sent.append)
                    binding.release_control(session)
        self.assertIs(caught.exception, original)
        self.assertEqual(sent, ['on', 'off'])
        self.assertNotIn('SECRET', ''.join(logs.output))

    def test_historical_stale_r1_forwarding_reproduces_overlap_outside_v2_capabilities(self):
        # Historical v1 counterexample: an executor could forward stale leader
        # headers through its RMW request channel. Model only those register
        # copies here, without a v1 production codec or compatibility writer.
        s = self.s
        old = s.store.read_r1()
        a = old.leader.term_id
        b = str(s.uid())
        header_a = encode_r1(s.prefix, old)
        header_b = encode_r1(s.prefix, replace(old, leader=replace(old.leader, term_id=b)))
        pending_a = s.server.send_update(42, {'name': header_a})
        s.server.apply(s.server.send_update(42, {'name': header_b}))
        effect_b = s.server.begin_control(42, b)
        s.clock.advance(.2)
        s.server.apply(pending_a)
        self.assertLessEqual(s.clock.now()-pending_a.sent_at, 1)
        effect_a = s.server.begin_control(42, a)
        s.clock.advance(.2)
        s.server.finish_control(effect_b)
        s.clock.advance(.2)
        s.server.finish_control(effect_a)
        self.assertEqual(len(s.server.control_conflicts), 1)
        self.assertFalse(hasattr(s.gate._stores[42], 'write_r1'))
        self.assertFalse(hasattr(s.store, 'publish_request'))
        self.stats.emit('episode_started', device_id=42, episode_id='historical-overlap')
        self.stats.emit('conflict', device_id=42, episode_id='historical-overlap')
        self.stats.emit('episode_finished', device_id=42, episode_id='historical-overlap',
                        outcome='conflict', coverage='complete')
        self.assertEqual(analyze(self.rows)['groups'][0]['episodes']['conflict'], 1)

    def test_physical_intervals_do_not_overlap_when_grant_and_tail_bounds_hold(self):
        s = self.s
        first = s.acquire(seconds=10)
        old = s.store.read_r1().grants[0]
        effect = s.server.begin_control(42, str(s.owner))
        other, owner = s.make_gate(), s.uid()
        s.pending(gate=other, owner=owner)
        next_grant = s.coordinator.step()
        with self.assertRaises(DeviceControlBusy):
            other.try_acquire(owner, 42, lease_seconds=60)
        s.advance(first.expires_at-s.clock.now()+.5)
        s.server.finish_control(effect)
        pending = other._pending[owner, 42]
        s.advance(pending.interval.usable_from-s.clock.now())
        second = other.try_acquire(owner, 42, lease_seconds=60)
        other.validate(owner, second)
        later = s.server.begin_control(42, str(owner))
        s.advance(.2)
        s.server.finish_control(later)
        self.assertGreaterEqual(next_grant.valid_from, old.valid_until+s.leader.timing.command_guard)
        self.assertEqual(s.server.control_conflicts, ())
