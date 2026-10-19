"""Adversarial schedules and independent physical oracle; no reliability claim."""

from dataclasses import replace
from threading import Event, Thread
import unittest
from unittest.mock import patch
from uuid import UUID

from tests.fakes.clock import FakeClock
from tests.fakes.protocol import Scenario
from zontcontrol.control_types import (CommandOutcomeUnknown, ControlOwnershipRequired,
                                      DeviceControlBusy, RateLimited)
from zontcontrol.controller.ownership import CommandExecutor
from zontcontrol.coordination.clock import clock_factor
from zontcontrol.coordination.grants import GrantCoordinator
from zontcontrol.coordination.leader import LeaderElection, LeaseLost
from zontcontrol.coordination.models import encode_r1, original_name
from zontcontrol.coordination.store import LeaderStore
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

    def test_out_of_model_late_apply_after_unknown_ack_never_becomes_success_from_polling(self):
        s = self.s
        s.adapter.write_mode = 'defer'
        with self.assertRaises(CommandOutcomeUnknown):
            s.gate.try_acquire(s.owner, 42, lease_seconds=60)
        sent = s.server.requests[-1]
        s.adapter.write_mode = 'apply'
        s.advance(2)
        s.server.apply(sent)
        self.assertEqual(s.server.out_of_model_faults, ((sent, 'late_apply'),))
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

    def test_request_before_first_leader_cannot_admit_a_command(self):
        s = Scenario(ready=False)
        executor = CommandExecutor(gate=s.gate, clock=s.clock.now, uuid_factory=s.uid)
        binding = executor.bind()
        before = s.uid.call_count
        with self.assertRaises(DeviceControlBusy):
            binding.try_acquire_control(42, lease_seconds=60)
        request = s.store.read_r2().request
        self.assertEqual((request.request_id, request.nonce),
                         (str(UUID(int=before+1)), str(UUID(int=before+2))))
        self.assertIsNone(request.leader_term_id)
        self.assertIsNone(s.store.read_r1())
        prepared, sent = [], []
        command = type('Command', (), {'device_id': 42})()
        with self.assertRaises(ControlOwnershipRequired):
            binding.execute(command, None, lambda: prepared.append('on'), sent.append)
        self.assertEqual((prepared, sent), ([], []))
        self.assertEqual([field for _, field, _ in s.adapter.writes], ['notes'])

        self.assertTrue(s.leader.try_acquire())
        s.advance(s.leader.timing.quarantine)
        s.coordinator.step()
        self.assertEqual(s.store.read_r1().grants, ())
        with self.assertRaises(DeviceControlBusy):
            binding.try_acquire_control(42, lease_seconds=60)
        rebound = s.store.read_r2().request
        self.assertEqual(rebound.request_id, request.request_id)
        self.assertNotEqual(rebound.nonce, request.nonce)
        self.assertEqual(rebound.leader_term_id, s.leader.record.term_id)
        s.coordinator.step()
        session = binding.try_acquire_control(42, lease_seconds=60)
        binding.execute(command, session, lambda: 'on', sent.append)
        self.assertEqual(sent, ['on'])
        binding.release_control(session)
        self.assertEqual(original_name(s.server.read_device(42)['name']), s.prefix)

    def test_simultaneous_bootstrap_candidates_and_early_executors_with_event_schedule(self):
        for count in (2, 5):
            with self.subTest(participants=count):
                s = Scenario(ready=False)
                stores = [s.store] + [s.make_store(LeaderStore) for _ in range(count-1)]
                candidates = [s.leader] + [s.candidate(store=store) for store in stores[1:]]
                executors = [(s.make_gate(), s.uid()) for _ in range(count)]
                requests = []
                for gate, owner in executors:
                    self.assertIsNone(s.pending(gate=gate, owner=owner))
                    requests.append(s.store.read_r2().request)
                    self.assertEqual(gate._held, {})
                self.assertEqual(s.server.read_device(42)['name'], s.prefix)
                self.assertTrue(all(request.leader_term_id is None for request in requests))

                # Every first READ sees the pristine R1. Gates then serialize
                # candidate WRITEs, keeping all confirmations behind all WRITEs.
                started = Event()
                read_ready = [Event() for _ in candidates]
                allow_read = [Event() for _ in candidates]
                waiting = [Event() for _ in candidates]
                allow_confirm = [Event() for _ in candidates]
                outcomes = [None] * count
                patches, threads = [], []
                before = s.uid.call_count

                def buffered_read(index, store):
                    read, first = store.read_r1, True
                    def callback(**kwargs):
                        nonlocal first
                        value = read(**kwargs)
                        if first:
                            first = False
                            read_ready[index].set()
                            if not allow_read[index].wait(2):
                                raise AssertionError('candidate READ gate was not released')
                        return value
                    return callback

                class CandidateClock:
                    def __init__(self, index):
                        self.index = index
                    def now(self):
                        return s.clock.now()
                    def wait(self, cancelled, seconds):
                        waiting[self.index].set()
                        if not allow_confirm[self.index].wait(2):
                            raise AssertionError('candidate confirmation gate was not released')
                        return cancelled.is_set()

                def run(index):
                    started.wait(2)
                    try:
                        outcomes[index] = candidates[index].try_acquire()
                    except LeaseLost:
                        outcomes[index] = False
                    except BaseException as error:
                        outcomes[index] = error

                try:
                    for index, (store, candidate) in enumerate(zip(stores, candidates)):
                        candidate._clock = CandidateClock(index)
                        item = patch.object(store, 'read_r1', side_effect=buffered_read(index, store))
                        item.start()
                        patches.append(item)
                        thread = Thread(target=run, args=(index,), name=f'bootstrap-{index}')
                        threads.append(thread)
                        thread.start()
                    started.set()
                    for ready in read_ready:
                        self.assertTrue(ready.wait(2))
                    for index in range(count):
                        allow_read[index].set()
                        self.assertTrue(waiting[index].wait(2))
                    s.clock.advance(s.leader.timing.barrier + 1e-7)
                    for gate in allow_confirm:
                        gate.set()
                    for thread in threads:
                        thread.join(2)
                        self.assertFalse(thread.is_alive())
                finally:
                    started.set()
                    for gate in (*allow_read, *allow_confirm):
                        gate.set()
                    for thread in threads:
                        thread.join(2)
                    for item in reversed(patches):
                        item.stop()
                self.assertEqual(outcomes, [False] * (count-1) + [True])
                elected = candidates[-1]
                self.assertEqual(s.store.read_r1().leader, elected.record)
                self.assertEqual(elected.record.term_id, str(UUID(int=before+2*count-1)))
                self.assertEqual(s.store.read_r1().record_id, str(UUID(int=before+2*count)))
                coordinator = GrantCoordinator(s.store, elected, clock=s.clock, uuid_factory=s.uid)
                coordinator.step()
                self.assertEqual(s.store.read_r1().grants, ())
                s.advance(elected.timing.quarantine, elected)
                gate, owner = executors[0]
                self.assertIsNone(s.pending(gate=gate, owner=owner))
                rebound = s.store.read_r2().request
                self.assertEqual(rebound.request_id, requests[0].request_id)
                self.assertEqual(rebound.leader_term_id, elected.record.term_id)
                grant = coordinator.step()
                self.assertEqual(grant.request_id, rebound.request_id)
                session = gate.try_acquire(owner, 42, lease_seconds=60)
                gate.validate(owner, session)
                self.assertEqual(original_name(s.server.read_device(42)['name']), s.prefix)
                self.assertEqual(s.server.out_of_model_faults, ())

    def test_dynamic_executors_lost_requests_future_grants_and_new_epoch_takeover(self):
        for count in (2, 5):
            for rates in ((.9, 1.1), (1.1, .9)):
                with self.subTest(participants=count, rates=rates):
                    s = Scenario()
                    clocks, active_leaders = [], [(s.leader, 1)]

                    def advance(seconds):
                        for leader, leader_rate in active_leaders:
                            leader.expect_resume(leader._clock.now() + seconds*leader_rate)
                        s.clock.advance(seconds)
                        for clock, rate in clocks:
                            clock.advance(seconds)

                    previous = None
                    issued = []
                    for index in range(count):
                        # Participants join while older grants still reserve
                        # the device; local BOOTTIMEs and wall clocks differ.
                        rate = rates[index % len(rates)]
                        clock = FakeClock(start=10000*(index+1), rate=rate)
                        clocks.append((clock, rate))
                        gate, owner = s.make_gate(clock=clock), s.uid()
                        effect = None
                        if previous is not None:
                            old_gate, old_owner, old_session, old_clock, old_rate = previous
                            advance((old_session.expires_at-old_clock.now()) / old_rate - .05)
                            old_gate.validate(old_owner, old_session)
                            effect = s.server.begin_control(42, str(old_owner))
                        before = s.uid.call_count
                        s.pending(gate=gate, owner=owner)
                        pending = gate._pending[owner, 42]
                        request, deadline = pending.request, pending.local_deadline
                        self.assertEqual((request.request_id, request.nonce),
                            (str(UUID(int=before+1)), str(UUID(int=before+2))))
                        # Another participant displaces R2. Retransmission keeps
                        # the original nonce, send sample and deadline.
                        s.pending(gate=s.make_gate(), owner=s.uid())
                        self.assertNotEqual(s.store.read_r2().request, request)
                        sent_at = pending.sent
                        s.pending(gate=gate, owner=owner)
                        self.assertEqual(s.store.read_r2().request, request)
                        self.assertEqual((pending.local_deadline, pending.sent), (deadline, sent_at))
                        before_grant = s.uid.call_count
                        grant = s.coordinator.step()
                        self.assertEqual(grant.grant_id, str(UUID(int=before_grant+2)))
                        self.assertEqual((grant.owner_id, grant.request_id, grant.nonce),
                                         (str(owner), request.request_id, request.nonce))
                        issued.append(grant)
                        # Wall corrections never change the cached causal map.
                        clock.jump_wall((-1 if index % 2 else 1) * 100000000)
                        if previous is not None:
                            self.assertGreater(grant.valid_from, grant.sampled_at)
                            with self.assertRaises(DeviceControlBusy):
                                gate.try_acquire(owner, 42, lease_seconds=60)
                            self.assertIsNotNone(pending.interval)
                            advance(.5)
                            s.server.finish_control(effect)
                            with self.assertRaises(ControlOwnershipRequired):
                                old_gate.validate(old_owner, old_session)
                            # Keep the first matched sample, then advance only
                            # to its local inclusive lower admission boundary.
                            local_wait = pending.interval.usable_from-clock.now()
                            if local_wait > 0:
                                advance(local_wait/rate + 1e-7)
                        session = gate.try_acquire(owner, 42, lease_seconds=60)
                        gate.validate(owner, session)
                        self.assertLess(clock.now(), session.expires_at)
                        previous = gate, owner, session, clock, rate

                    # Stop the old role and take over with both a different
                    # BOOTTIME origin and an absolute offset within Delta. Stale R2
                    # cannot be served in the new term or grant new authority.
                    gate, owner, session, clock, rate = previous
                    advance((session.expires_at-clock.now())/rate + 1)
                    old_request = s.store.read_r2()
                    old_term = s.leader.record.term_id
                    s.leader.stop()
                    takeover_rate = 1.0001
                    takeover_clock = FakeClock(start=700000, rate=takeover_rate)
                    clocks.append((takeover_clock, takeover_rate))
                    successor = LeaderElection(s.store, str(s.uid()), s.leader.timing,
                        clock=takeover_clock, epoch_sampler=lambda: s.leader.timeline.now()+.5,
                        uuid_factory=s.uid)
                    active_leaders[:] = [(successor, takeover_rate)]
                    self.assertFalse(successor.try_acquire())
                    record = s.store.read_r1()
                    old_horizon = record.leader.leader_until-record.leader.sampled_at
                    advance(old_horizon*clock_factor(record.leader.period_s)/takeover_rate + 1e-7)
                    before_takeover = s.uid.call_count

                    def wait(cancelled, seconds):
                        advance(seconds/takeover_rate)
                        return cancelled.is_set()

                    with patch.object(takeover_clock, 'wait', side_effect=wait):
                        self.assertTrue(successor.try_acquire())
                    self.assertEqual(successor.record.term_id, str(UUID(int=before_takeover+1)))
                    self.assertNotEqual(successor.record.term_id, old_term)
                    self.assertNotEqual(successor.timeline.clock_id, s.leader.timeline.clock_id)
                    self.assertLessEqual(abs(successor.timeline.now()-s.leader.timeline.now()), 1)
                    advance(successor.timing.quarantine/takeover_rate)
                    coordinator = GrantCoordinator(s.store, successor, clock=takeover_clock,
                                                   uuid_factory=s.uid)
                    coordinator.step()
                    self.assertEqual(s.store.read_r2(), old_request)
                    self.assertEqual(s.store.read_r1().grants, ())
                    with self.assertRaises(ControlOwnershipRequired):
                        gate.validate(owner, session)
                    for first, second in zip(issued, issued[1:]):
                        self.assertGreaterEqual(second.valid_from,
                            first.valid_until+s.leader.timing.command_guard)
                    self.assertEqual(s.server.control_conflicts, ())
                    self.assertEqual(s.server.out_of_model_faults, ())
