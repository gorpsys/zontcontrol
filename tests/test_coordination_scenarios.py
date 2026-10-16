"""Reproducible adversarial schedules, not a representative reliability sample."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
import unittest
from unittest.mock import patch
from uuid import uuid4

from tests.fakes.clock import FakeClock
from tests.fakes.zont import FakeMetadataAdapter, FakeZontServer
from tests.test_coordination_store import PREFIX
from zontcontrol.controller.contracts import (
    CommandOutcomeUnknown, ControlOwnershipRequired, DeviceControlBusy, RateLimited,
)
from zontcontrol.controller.ownership import CommandExecutor
from zontcontrol.coordination.clock import LeaseTiming
from zontcontrol.coordination.grants import ExecutorGate, GrantCoordinator
from zontcontrol.coordination.leader import LeaderElection, LeaseLost
from zontcontrol.coordination.models import R1Record
from zontcontrol.coordination.statistics import Statistics, analyze
from zontcontrol.coordination.store import ExecutorStore, LeaderStore


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.server = FakeZontServer({42: {'name': PREFIX, 'notes': None}}, clock=self.clock)
        self.adapter = FakeMetadataAdapter(self.server)
        self.rows = []
        self.stats = Statistics(self.rows.append, protocol_version='r1r2-v1',
                                profile='adversarial', source='counterexample')
        self.store = self.make_store(LeaderStore)
        self.leader = self.make_leader()
        self.leader.try_acquire()
        self.coordinator = GrantCoordinator(self.store, self.leader, clock=self.clock,
                                             statistics=self.stats)
        self.owner = uuid4()
        self.gate = self.make_gate()

    def make_store(self, cls, adapter=None):
        return cls(adapter or self.adapter, 42, original_name=PREFIX,
                   notes_capacity=10000, name_capacity=10000)

    def make_leader(self):
        return LeaderElection(self.store, str(uuid4()), LeaseTiming(1000, 1),
                              clock=self.clock, statistics=self.stats)

    def make_gate(self, adapter=None):
        return ExecutorGate({42: self.make_store(ExecutorStore, adapter)}, clock=self.clock,
                            delta=1, statistics=self.stats)

    def pending(self, gate=None, owner=None, seconds=60):
        with self.assertRaises(DeviceControlBusy):
            (gate or self.gate).try_acquire(owner or self.owner, 42, lease_seconds=seconds)

    def acquire(self):
        self.pending()
        self.coordinator.step()
        return self.gate.try_acquire(self.owner, 42, lease_seconds=60)

    def counts(self):
        return analyze(self.rows)['groups'][0]['events']

    def test_real_protocol_events_lost_request_repeat_heartbeat_cleanup(self):
        self.pending()
        self.store.write_r1(R1Record(self.leader.record))
        self.pending()
        self.coordinator.step()
        session = self.gate.try_acquire(self.owner, 42, lease_seconds=60)
        with self.assertRaises(DeviceControlBusy):
            self.gate.renew(self.owner, session, lease_seconds=60)
        self.coordinator.step()
        session = self.gate.renew(self.owner, session, lease_seconds=60)
        self.gate.release(self.owner, session)
        self.coordinator.step()
        counts = self.counts()
        self.assertEqual(counts['grant'], 1)
        self.assertEqual(counts['grant_ack'], 1)
        self.assertEqual(counts['request_lost'], 1)
        self.assertEqual(counts['request_repeated'], 1)
        self.assertEqual(counts['cleanup'], 1)
        self.assertEqual(counts['finished'], 1)
        self.assertIn('heartbeat', counts)
        self.assertEqual(analyze(self.rows)['groups'][0]['episodes']['success'], 0)

    def test_lost_finished_waits_for_expiry_without_recovery(self):
        session = self.acquire()
        self.adapter.write_mode = 'defer'
        with self.assertRaises(CommandOutcomeUnknown):
            self.gate.release(self.owner, session)
        deferred = self.server.requests[-1]
        self.adapter.write_mode = 'apply'
        other, owner = self.make_gate(), uuid4()
        self.pending(other, owner)
        self.coordinator.step()
        self.assertEqual(self.store.read_r2().owner, str(self.owner))
        self.clock.advance(self.store.read_r2().lease_s)
        self.coordinator.step()
        granted = self.store.read_r2()
        self.assertEqual(granted.owner, str(owner))
        self.server.apply(deferred)
        self.coordinator.step()
        self.assertEqual(self.store.read_r2(), granted)
        writes = len(self.adapter.writes)
        result = analyze(self.rows)
        self.assertGreater(result['groups'][0]['episodes']['unknown'], 0)
        self.assertEqual(len(self.adapter.writes), writes)
        self.assertEqual(self.counts()['timeout'], 1)

    def test_leader_change_during_active_grant_is_observed_without_replacement(self):
        session = self.acquire()
        old = self.store.read_r2()
        self.leader.release()
        other = self.make_leader()
        other.try_acquire()
        coordinator = GrantCoordinator(self.store, other, clock=self.clock, statistics=self.stats)
        coordinator.step()
        coordinator.step()
        self.assertEqual(self.store.read_r2(), old)
        self.assertEqual(self.counts()['leader_changed_active_grant'], 1)
        with self.assertRaises(ControlOwnershipRequired):
            self.gate.validate(self.owner, session)

    def test_late_apply_after_unknown_ack_never_becomes_success_from_polling(self):
        self.adapter.write_mode = 'defer'
        with self.assertRaises(CommandOutcomeUnknown):
            self.gate.try_acquire(self.owner, 42, lease_seconds=60)
        sent = self.server.requests[-1]
        self.adapter.write_mode = 'apply'
        self.server.apply(sent)
        self.coordinator.step()
        self.gate.try_acquire(self.owner, 42, lease_seconds=60)
        writes = len(self.adapter.writes)
        for _ in range(10):
            self.coordinator.step()
        self.assertEqual(len(self.adapter.writes), writes)
        group = analyze(self.rows)['groups'][0]
        self.assertEqual(group['episodes']['unknown'], 1)
        self.assertEqual(group['episodes']['success'], 0)

    def test_429_and_delta_breaches_have_distinct_events_and_no_retry(self):
        self.adapter.write_error = RateLimited()
        before = len(self.server.requests)
        with self.assertRaises(RateLimited):
            self.gate.try_acquire(self.owner, 42, lease_seconds=60)
        self.assertEqual(len(self.server.requests), before + 1)
        self.assertEqual(self.counts()['rate_limited'], 1)
        self.adapter.write_error = None
        session = self.acquire()
        self.adapter.read_delay = .6
        with self.assertRaises(ControlOwnershipRequired):
            self.gate.validate(self.owner, session)
        with self.assertRaises(LeaseLost):
            self.coordinator.step()
        self.assertGreaterEqual(self.counts()['delta_breach'], 2)
        self.assertIn('rtt', self.counts())

    def test_broken_sink_does_not_change_accepted_command_finally_or_exception(self):
        class Broken:
            def emit(self, *args, **kwargs):
                raise OSError('SECRET')
        self.gate._statistics = Broken()
        executor = CommandExecutor(gate=self.gate, clock=self.clock.now)
        binding = executor.bind()
        with self.assertLogs('zontcontrol.coordination.statistics', level='WARNING') as logs:
            with self.assertRaises(DeviceControlBusy):
                binding.try_acquire_control(42, lease_seconds=60)
            self.coordinator.step()
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

    def test_stale_r1_replay_reproduces_real_overlap_detected_by_resource_oracle(self):
        # Copy chains carry FREE across A's barrier, then A across B's barrier.
        # Every individual stale copy is applied within Delta; alternating
        # ordinary RMWs can nevertheless preserve an obsolete header forever.
        # These are adversarial copies by virtual independent executors, not
        # CAS, a provider frequency model or permission to bypass R2 in runtime.
        self.leader.release()
        carry_source = [self.server.read_device(42)['notes']]
        carried = [None]

        def replaying_barrier(cancelled, seconds):
            current = self.server.read_device(42)['notes']
            pending = [self.server.send_update(42, {'notes': raw})
                       for raw in (carry_source[0], current)]
            steps = round(seconds / .25)
            self.assertEqual(steps * .25, seconds)
            self.assertEqual(steps % 2, 0)
            for index in range(steps):
                self.clock.advance(.25)
                lane = index % 2
                self.assertLessEqual(self.clock.now() - pending[lane].sent_at, .5)
                self.server.apply(pending[lane])
                # A subsequent executor reads this replay and forwards it in a
                # new bounded copy, just as request publication preserves R1.
                raw = self.server.read_device(42)['notes']
                pending[lane] = self.server.send_update(42, {'notes': raw})
            carried[0] = pending[0]
            return cancelled.is_set()

        self.leader = self.make_leader()
        self.coordinator = GrantCoordinator(self.store, self.leader, clock=self.clock,
                                             statistics=self.stats)
        with patch.object(self.clock, 'wait', side_effect=replaying_barrier):
            self.assertTrue(self.leader.try_acquire())
            carry_source[0] = self.server.read_device(42)['notes']
            self.server.apply(carried[0])  # old FREE, copied less than Delta ago
            second = self.make_leader()
            self.assertTrue(second.try_acquire())
        first_replay = carried[0]  # a fresh copy of A, ready to replay during B
        gate_y, owner_y = self.make_gate(), uuid4()
        self.pending(gate_y, owner_y)
        second_header = self.store.read_r1()
        y_coordinator = GrantCoordinator(self.store, second, clock=self.clock,
                                         statistics=self.stats)
        ready_y, resume_y = Event(), Event()
        ready_x, resume_x = Event(), Event()
        original_write = self.store.write_r2
        def paused_write(grant):
            ready, resume = ((ready_y, resume_y) if grant.owner == str(owner_y)
                             else (ready_x, resume_x))
            ready.set()
            if not resume.wait(2):
                raise AssertionError('scenario barrier timed out')
            return original_write(grant)
        with ThreadPoolExecutor(max_workers=2) as pool:
            # Y has read FREE and confirmed B; its write waits while A confirms
            # its replayed header and also reads FREE. Neither write has CAS.
            with patch.object(self.store, 'write_r2', side_effect=paused_write):
                future = pool.submit(y_coordinator.step)
                self.assertTrue(ready_y.wait(2))
                self.assertLessEqual(self.clock.now() - first_replay.sent_at, .5)
                self.server.apply(first_replay)
                self.pending()
                first_request = self.store.read_r1()
                first_future = pool.submit(self.coordinator.step)
                self.assertTrue(ready_x.wait(2))
                self.store.write_r1(second_header)
                resume_y.set()
                future.result(2)
                y_session = gate_y.try_acquire(owner_y, 42, lease_seconds=60)
                gate_y.validate(owner_y, y_session)
                y = self.server.begin_control(42, str(owner_y))
                self.clock.advance(.2)
                self.store.write_r1(first_request)
                resume_x.set()
                first_future.result(2)
            self.store.write_r1(first_request)
            x_session = self.gate.try_acquire(self.owner, 42, lease_seconds=60)
            self.gate.validate(self.owner, x_session)
            x = self.server.begin_control(42, str(self.owner))
            self.clock.advance(.2)
            self.server.finish_control(y)
            self.clock.advance(.2)
            self.server.finish_control(x)
        self.assertEqual(len(self.server.control_conflicts), 1)
        # Oracle knows applied intervals, independently of reported grant success.
        self.stats.emit('episode_started', device_id=42, episode_id='oracle-overlap')
        self.stats.emit('conflict', device_id=42, episode_id='oracle-overlap')
        self.stats.emit('episode_finished', device_id=42, episode_id='oracle-overlap',
                        outcome='conflict', coverage='complete')
        self.assertEqual(analyze(self.rows)['groups'][0]['episodes']['conflict'], 1)


if __name__ == '__main__':
    unittest.main()
