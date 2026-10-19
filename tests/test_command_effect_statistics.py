"""Offline timing samples never grant control or prove physical completion by RTT."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.fakes.clock import FakeClock
from tests.fakes.uuid import uuid_generator
from tests.test_control_ownership import LocalTestGate
from zontcontrol.controller.contracts import (
    CommandOutcomeUnknown, ControlKey, ControlOwnershipRequired, ValidatedCommand,
)
from zontcontrol.controller.ownership import CommandExecutor
from zontcontrol.coordination.statistics import (
    Statistics, analyze_command_effects,
)


class CommandEffectStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.rows = []
        self.stats = Statistics(self.rows.append, protocol_version='r1r2-v2', profile='test')
        self.uid = uuid_generator()
        self.clock = FakeClock(start=100)
        self.gate = LocalTestGate(self.clock, uuid_factory=self.uid)
        self.executor = CommandExecutor(gate=self.gate, clock=self.clock.now,
                                        statistics=self.stats, uuid_factory=self.uid)
        self.owner = self.executor.bind()
        self.session = self.owner.try_acquire_control(42, lease_seconds=50)
        self.command = ValidatedCommand(ControlKey(42, 'toggle_buttons', 31),
                                        'trigger', (('target_state', False),))

    def send(self, prepared):
        self.clock.advance(2)
        return 'api-success'

    def execute(self, send=None, validate=None):
        return self.owner.execute(self.command, self.session, validate or (lambda: 'prepared'),
                                   send or self.send)

    def effect(self, kind, **fields):
        self.stats.emit('command_effect', device_id=42, sample_kind=kind, **fields)

    def test_command_uses_uuid_and_exact_last_admission_boottime(self):
        readings = []
        def now():
            value = self.clock.now()
            readings.append(value)
            self.clock.advance(.1)
            return value
        self.executor._clock = now
        self.execute(validate=lambda: self.clock.advance(1))
        started = next(row for row in self.rows if row['event'] == 'command_started')
        finished = next(row for row in self.rows if row['event'] == 'command_finished')
        self.assertEqual(started['admitted_at'], readings[1])
        self.assertEqual(finished['api_response_at'], readings[2])
        self.assertEqual(started['command_observation_id'], finished['command_observation_id'])
        self.assertNotEqual(started['command_observation_id'], str(self.session.session_id))
        self.assertEqual(finished['rtt_s'], readings[2] - readings[1])
        report = analyze_command_effects(self.rows)['groups'][0]
        self.assertEqual(report['command']['count'], 1)
        self.assertEqual(report['residual_effect']['status'], 'unknown')
        self.assertIsNone(report['delta_cmd_recommendation_s'])
        self.assertEqual(report['delta_cmd_recommendation_status'], 'unknown')

    def test_algorithm_section_never_becomes_command_tail(self):
        self.execute()
        self.effect('critical_section', started_at=100, finished_at=140,
                    coverage='complete', outcome='success')
        report = analyze_command_effects(self.rows)['groups'][0]
        self.assertEqual(report['critical_section']['max'], 40)
        self.assertIsNone(report['critical_section']['exceedances'])
        self.assertEqual(report['command']['max'], 2)
        self.assertEqual(report['residual_effect']['count'], 0)
        self.assertIsNone(report['delta_cmd_recommendation_s'])

    def test_physical_upper_bound_includes_pause_after_admission(self):
        def send(prepared):
            self.clock.advance(3)  # Pause before the first byte is sent.
            sent_at = self.clock.now()
            self.clock.advance(1)
            return sent_at
        sent_at = self.execute(send)
        start = next(row for row in self.rows if row['event'] == 'command_started')
        self.effect('residual_effect', command_observation_id=start['command_observation_id'],
                    clock_id=start['clock_id'], admitted_at=start['admitted_at'],
                    sent_at=sent_at, effect_completed_at=106,
                    completion_basis='physical_upper_bound', coverage='complete', outcome='success')
        report = analyze_command_effects(self.rows)['groups'][0]
        self.assertEqual(report['residual_effect']['max'], 6)
        self.assertEqual(report['network_to_effect']['max'], 3)
        self.assertEqual(report['delta_cmd_recommendation_s'], 60)
        self.assertEqual(report['delta_cmd_recommendation_status'], 'observed')
        self.effect('residual_effect', command_observation_id=start['command_observation_id'],
                    clock_id='incomparable', admitted_at=start['admitted_at'],
                    effect_completed_at=106, completion_basis='physical_upper_bound',
                    coverage='complete', outcome='success')
        mixed = analyze_command_effects(self.rows)['groups'][0]
        self.assertEqual(mixed['residual_effect']['count'], 1)
        self.assertEqual(mixed['residual_effect']['unknown'], 1)
        self.assertEqual(mixed['delta_cmd_recommendation_s'], 60)
        self.assertEqual(mixed['delta_cmd_recommendation_status'], 'incomplete')

    def test_incomparable_missing_and_api_only_completion_remain_unknown(self):
        for changes in ({'clock_id': 'other-clock'}, {'admitted_at': 101},
                        {'completion_basis': 'api_response'}, {'coverage': 'partial'},
                        {'effect_completed_at': 99}, {'command_observation_id': 'missing'},
                        {'sent_at': 999}):
            with self.subTest(changes=changes):
                self.rows.clear()
                self.execute()
                start = next(row for row in self.rows if row['event'] == 'command_started')
                fields = dict(command_observation_id=start['command_observation_id'],
                              clock_id=start['clock_id'], admitted_at=start['admitted_at'],
                              effect_completed_at=self.clock.now()+1,
                              completion_basis='physical_upper_bound',
                              coverage='complete', outcome='success')
                self.effect('residual_effect', **(fields | changes))
                report = analyze_command_effects(self.rows)['groups'][0]
                self.assertEqual(report['residual_effect']['unknown'], 1)
                self.assertIsNone(report['delta_cmd_recommendation_s'])
                self.assertEqual(report['delta_cmd_recommendation_status'], 'unknown')

    def test_residual_exceedances_use_its_own_profile_bound(self):
        self.execute()
        start = next(row for row in self.rows if row['event'] == 'command_started')
        self.effect('residual_effect', command_observation_id=start['command_observation_id'],
                    clock_id=start['clock_id'], admitted_at=100, effect_completed_at=106,
                    delta_cmd_s=10, completion_basis='physical_upper_bound',
                    coverage='complete', outcome='success')
        report = analyze_command_effects(self.rows)['groups'][0]
        self.assertEqual(report['residual_effect']['exceedances'], 0)
        lost = analyze_command_effects([r for r in self.rows if r['event'] != 'command_started'])
        self.assertEqual(lost['groups'][0]['residual_effect']['unknown'], 1)
        self.assertIsNone(lost['groups'][0]['delta_cmd_recommendation_s'])

    def test_timeout_and_dropped_finish_are_censored_without_retry(self):
        sent = []
        def send(prepared):
            sent.append(prepared)
            self.clock.advance(3)
            raise CommandOutcomeUnknown()
        with self.assertRaises(CommandOutcomeUnknown):
            self.execute(send)
        report = analyze_command_effects(self.rows)['groups'][0]
        self.assertEqual(report['command']['unknown'], 1)
        self.assertEqual(report['command']['count'], 0)
        self.assertEqual(report['api_elapsed']['max'], 3)
        self.assertEqual(sent, ['prepared'])
        dropped = analyze_command_effects([r for r in self.rows if r['event'] != 'command_finished'])
        self.assertEqual(dropped['groups'][0]['command']['unknown'], 1)

    def test_failed_sink_and_observation_uuid_leave_send_finally_and_exception_intact(self):
        def broken(row):
            raise OSError('private')
        self.executor._statistics = Statistics(broken, protocol_version='v2', profile='test')
        with self.assertLogs('zontcontrol.coordination.statistics', level='WARNING'):
            self.assertEqual(self.execute(), 'api-success')
            with self.assertRaises(CommandOutcomeUnknown):
                try:
                    self.execute(lambda prepared: (_ for _ in ()).throw(CommandOutcomeUnknown()))
                finally:
                    self.assertEqual(self.execute(), 'api-success')
            self.owner.release_control(self.session)
        self.assertEqual(self.gate.releases, [self.session])
        # An observer UUID failure is instrumentation failure, not command failure.
        session = self.owner.try_acquire_control(42, lease_seconds=50)
        self.session = session
        self.executor._uuid_factory = lambda: (_ for _ in ()).throw(RuntimeError('private'))
        with self.assertLogs('zontcontrol.coordination.statistics', level='WARNING'):
            self.assertEqual(self.execute(), 'api-success')

    def test_rejected_command_has_no_admission_sample_and_does_not_send(self):
        self.clock.advance(50)
        with self.assertRaises(ControlOwnershipRequired):
            self.execute()
        self.assertFalse(any(row['event'] == 'command_started' for row in self.rows))

    def test_offline_cli_requires_explicit_sample_kind_and_never_writes_input(self):
        self.effect('critical_section', started_at=100, finished_at=140,
                    coverage='complete', outcome='success')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'samples.jsonl'
            path.write_text(''.join(json.dumps(row)+'\n' for row in self.rows))
            before = path.read_bytes()
            result = subprocess.run([sys.executable, 'scripts/analyze_command_effects.py', str(path)],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            group = json.loads(result.stdout)['groups'][0]
            self.assertEqual(group['critical_section']['max'], 40)
            self.assertIsNone(group['delta_cmd_recommendation_s'])
            self.assertEqual(path.read_bytes(), before)
            bad = self.rows[0].copy()
            del bad['sample_kind']
            path.write_text(json.dumps(bad)+'\n')
            failed = subprocess.run([sys.executable, 'scripts/analyze_command_effects.py', str(path)],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(failed.returncode, 2)

    def test_residual_nearest_rank_and_profile_separation(self):
        # Distinct commands, each with a matching local admission anchor.
        for number in range(1, 1001):
            identifier = str(self.uid())
            self.stats.emit('command_started', device_id=42, command_observation_id=identifier,
                            clock_id='test-clock', admitted_at=100)
            self.stats.emit('command_finished', device_id=42, command_observation_id=identifier,
                            clock_id='test-clock', admitted_at=100, api_response_at=101,
                            outcome='success')
            self.effect('residual_effect', command_observation_id=identifier,
                        clock_id='test-clock', admitted_at=100,
                        effect_completed_at=100 + number,
                        completion_basis='physical_upper_bound', coverage='complete', outcome='success')
        report = analyze_command_effects(self.rows)['groups'][0]
        self.assertEqual(report['residual_effect']['p99'], 990)
        self.assertEqual(report['residual_effect']['p99_9'], 999)
        self.assertEqual(report['residual_effect']['exceedances'], 995)
        self.assertEqual(report['delta_cmd_recommendation_s'], 9990)
        foreign = dict(self.rows[-1], profile='foreign')
        groups = analyze_command_effects(self.rows + [foreign])['groups']
        self.assertEqual(len(groups), 2)
        self.assertIsNone(next(g for g in groups if g['profile'] == 'foreign')['delta_cmd_recommendation_s'])
