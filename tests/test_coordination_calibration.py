"""Calibration counts every request and exercises the real v2 objects offline."""

from dataclasses import replace
from threading import Event
import unittest
from unittest.mock import Mock

from scripts.calibrate_coordination import (
    Pacing, Safety, candidates, normal_inputs, run_series, request_summary,
    select_profile, live_series, run_fault_matrix, cached_codecs,
)
from zontcontrol.controller.contracts import (
    CommandOutcomeUnknown, ControlDescriptor, ControlKey, ControlOwnershipRequired,
    ControlRequestResult, ControlRequestState, ControlSession, Snapshot,
    ValueKey, ValueReading, ValueType, Quality,
)
from datetime import datetime, timezone
from uuid import UUID


class CalibrationTests(unittest.TestCase):
    def test_full_grid_and_matrix_are_fixed(self):
        grid = list(candidates(Safety()))
        self.assertEqual(len(grid), 432)
        self.assertEqual({p.poll_seconds for p in grid}, {.25, .5, 1, 2, 3, 5})
        self.assertEqual({p.renewal_fraction for p in grid}, {1/5, 1/4, 1/3})
        inputs = list(normal_inputs())
        self.assertEqual(len(inputs), 90)
        self.assertEqual({i.seed for i in inputs}, {17, 42, 73})
        self.assertTrue(all(i.requests == 100 for i in inputs))
        self.assertEqual({i.clock_rates for i in inputs}, {(1, 1), (.995, 1.005)})

    def test_success_rate_precedes_successful_p99_and_timeout_is_censored(self):
        poor = request_summary([{'outcome': 'GRANTED', 'wait_s': .1, 'timeout_s': 30}]
            + [{'outcome': 'timeout', 'wait_s': None, 'timeout_s': 30}] * 99)
        good = request_summary([{'outcome': 'GRANTED', 'wait_s': 20, 'timeout_s': 30}] * 100)
        a, b = Pacing(.25, .25, 0, .2), Pacing(1, 1, 0, .2)
        winner = select_profile([{'pacing': a.as_dict(), 'summary': poor, 'http_count': 1},
                                {'pacing': b.as_dict(), 'summary': good, 'http_count': 100}])
        self.assertEqual(winner['pacing'], b.as_dict())
        self.assertEqual(poor['started_requests'], 100)
        self.assertEqual(poor['timeout_fraction'], .99)
        self.assertEqual(poor['all_wait_lower_bounds']['p99'], 30)
        self.assertEqual(poor['successful_wait']['count'], 1)
        self.assertEqual(poor['censoring'], 'right; timeout wait >= timeout_s')

    def test_unknown_missing_violation_and_zero_success_cannot_win(self):
        for rows, violations in (([{'outcome': 'unknown', 'timeout_s': 30}], []),
                                 ([{'outcome': 'timeout', 'timeout_s': 30}], []),
                                 ([{'outcome': 'GRANTED', 'wait_s': 1, 'timeout_s': 30}], ['overlap'])):
            summary = request_summary(rows, violations=violations, expected=100)
            self.assertEqual(summary['started_requests'], 100)
            self.assertIsNone(select_profile([{'pacing': Pacing(1, 1, 0, .2).as_dict(),
                                               'summary': summary, 'http_count': 0}]))
        for wait in (float('nan'), float('inf'), -1, 30, 31, None):
            summary = request_summary([{'outcome': 'GRANTED', 'wait_s': wait, 'timeout_s': 30}])
            self.assertEqual(summary['unknown'], 1)
            self.assertFalse(summary['eligible'])

    def test_ties_are_order_independent(self):
        summary = request_summary([{'outcome': 'GRANTED', 'wait_s': 1, 'timeout_s': 30}])
        rows = [{'pacing': p.as_dict(), 'summary': summary, 'http_count': 4}
                for p in (Pacing(1, 1, 0, .2), Pacing(.5, 1, 0, .2))]
        self.assertEqual(select_profile(rows), select_profile(list(reversed(rows))))
        self.assertEqual(select_profile(rows)['pacing']['poll_seconds'], .5)

    def test_real_protocol_series_is_reproducible_and_contains_all_requests(self):
        item = replace(next(normal_inputs()), requests=4)
        first = run_series(Pacing(1, 1, 0, .2), item, Safety())
        second = run_series(Pacing(1, 1, 0, .2), item, Safety())
        self.assertEqual(first, second)
        self.assertEqual(len(first['samples']), 4)
        self.assertGreater(first['http_count'], 0)
        self.assertGreater(first['leader_terms'], 0)
        self.assertEqual(first['summary']['unknown'], 0)
        self.assertEqual(first['violations'], [])
        self.assertEqual(first['status'], 'simulation')
        with cached_codecs():
            self.assertEqual(first, run_series(Pacing(1, 1, 0, .2), item, Safety()))

    def test_safety_rho_uses_original_period_and_invalid_pacing_is_excluded(self):
        for period, rho in ((60, .1), (60.001, .05), (300, .05),
                            (300.001, .01), (600, .01), (600.001, .005)):
            self.assertEqual(Safety(period=period).timing.clock_rho, rho)
        self.assertEqual(list(candidates(Safety(period=30))), [])

    def test_separate_fault_matrix_checks_all_declared_contracts(self):
        rows = run_fault_matrix(Pacing(1, 1, 0, .2), Safety(), seeds=(17,))
        self.assertEqual({r['scenario'] for r in rows},
                         {'timeout', 'lost-ack', '429', 'cancel', 'freeze', 'stop-finally'})
        self.assertTrue(all(r['passed'] for r in rows), rows)
        self.assertTrue(all(r['matrix'] == 'fault' for r in rows))

    def test_displacer_is_in_the_same_started_denominator(self):
        item = replace(next(i for i in normal_inputs() if i.scenario == 'r2-displaced'), requests=4)
        row = run_series(Pacing(1, 1, 0, .2), item, Safety())
        self.assertEqual(len(row['samples']), 4)
        self.assertEqual(row['summary']['started_requests'], 4)
        displaced = [s for s in row['samples'] if s.get('reason') == 'one-shot-contention']
        self.assertEqual(len(displaced), 1)
        self.assertEqual(displaced[0]['outcome'], 'DECLINED')

    def test_term_handover_renews_active_role_to_cap_and_acquires_fresh_term(self):
        item = replace(next(i for i in normal_inputs() if i.scenario == 'term-handover'),
                       requests=20)
        row = run_series(Pacing(1, 1, 0, .25), item, Safety())
        trace = row['term_trace']
        renewals = [r for r in trace if r['event'] == 'renewed']
        near = next(r for r in trace if r['event'] == 'near-cap')
        self.assertGreater(len(renewals), 10)
        self.assertTrue(near['active'])
        self.assertEqual(near['expires_at'], near['cap'])
        self.assertGreater(near['cap']-near['at'], 100)
        retired = next(r for r in trace if r['event'] == 'retired')
        self.assertEqual(retired['reason'], 'term-limit')
        self.assertLessEqual(retired['at'], near['cap']+5)
        acquired = next(r for r in trace if r['event'] == 'acquired')
        self.assertNotEqual(acquired['term'], near['term'])
        self.assertGreater(acquired['at'], near['cap'])
        self.assertEqual(row['summary']['started_requests'], 20)
        self.assertEqual(row['violations'], [])
        with cached_codecs():
            self.assertEqual(row, run_series(Pacing(1, 1, 0, .25), item, Safety()))


class LiveCalibrationTests(unittest.TestCase):
    def controller(self):
        controller = Mock()
        self.circuit = ControlKey(42, 'circuit', 11)
        self.mode = ControlKey(42, 'mode', 21)
        controls = (ControlDescriptor(self.circuit, 'Отопление', ('target-temp',)),
                    ControlDescriptor(ControlKey(42, 'circuit', 12), 'Котел', ('target-temp',)),
                    ControlDescriptor(self.mode, 'Расписание', ('activate',)))
        controller.list_controls.return_value = controls
        self.target, self.current_mode = 20., 21
        self.calls = []
        now = datetime(2026, 10, 19, tzinfo=timezone.utc)
        def read(device, keys=None):
            values = tuple(ValueReading(ValueKey(42, 'circuit', 11, field), value,
                ValueType.NUMBER if field == 'target_temp' else ValueType.INTEGER,
                '°C' if field == 'target_temp' else None, 'circuits', None, now, Quality.UNKNOWN)
                for field, value in (('target_temp', self.target), ('mode_id', self.current_mode)))
            return Snapshot(42, now, values)
        controller.read_current.side_effect = read
        session = ControlSession(42, UUID(int=1), 1000)
        controller.acquire_control.return_value = ControlRequestResult(
            UUID(int=2), 42, ControlRequestState.GRANTED, session)
        controller.prepare_command.side_effect = lambda device, key, action, args: (key, action, args)
        def send(command, session):
            self.calls.append(command)
            if command[0] == self.circuit:
                self.target = command[2]['target_temp']
                self.current_mode = 0
            else:
                self.current_mode = 21
        controller.execute_command.side_effect = send
        return controller

    def test_alternation_and_one_outer_finally_restore_schedule(self):
        controller = self.controller()
        report = live_series(controller, 42, count=4)
        temps = [c[2]['target_temp'] for c in self.calls if c[0] == self.circuit]
        self.assertEqual(temps, [20.1, 20., 20.1, 20.])
        self.assertEqual(self.calls[-1][0], self.mode)
        self.assertTrue(report['schedule_confirmed'])
        self.assertEqual(report['physical_tail']['status'], 'unknown')
        self.assertEqual(report['status'], 'unmeasured')

    def test_unknown_exception_stop_and_expiry_always_attempt_schedule(self):
        for error in (CommandOutcomeUnknown(), RuntimeError(), ControlOwnershipRequired(), None):
            with self.subTest(error=type(error).__name__):
                controller = self.controller()
                original = controller.execute_command.side_effect
                once = [True]
                def send(command, session):
                    if once[0]:
                        once[0] = False
                        if error:
                            raise error
                    return original(command, session)
                controller.execute_command.side_effect = send
                stop = Event()
                if error is None:
                    stop.set()
                report = live_series(controller, 42, count=4, cancelled=stop)
                self.assertTrue(report['schedule_confirmed'])
                self.assertEqual(self.calls[-1][0], self.mode)
                self.assertLessEqual(sum(c[0] == self.circuit for c in self.calls), 1)

    def test_lost_finally_lease_reacquires_once_without_blind_unknown_retry(self):
        for error, expected in ((ControlOwnershipRequired(), 2), (CommandOutcomeUnknown(), 1)):
            controller = self.controller()
            self.current_mode = 0
            send = controller.execute_command.side_effect
            failed = [False]
            def execute(command, session):
                if command[0] == self.mode and not failed[0]:
                    failed[0] = True
                    raise error
                return send(command, session)
            controller.execute_command.side_effect = execute
            report = live_series(controller, 42, count=0)
            self.assertEqual(controller.acquire_control.call_count, expected)
            self.assertEqual(controller.execute_command.call_count, expected)
            self.assertEqual(report['schedule_confirmed'], expected == 2)

    def test_invalid_step_does_not_substitute_command_and_failed_readback_fails(self):
        controller = self.controller()
        def prepare(device, key, action, args):
            if key == self.circuit:
                raise ValueError()
            return key, action, args
        controller.prepare_command.side_effect = prepare
        report = live_series(controller, 42, count=2)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], self.mode)
        self.assertTrue(report['schedule_confirmed'])
        controller = self.controller()
        controller.execute_command.side_effect = lambda *a: None
        self.current_mode = 0
        self.assertFalse(live_series(controller, 42, count=0)['schedule_confirmed'])

    def test_reported_device_step_blocks_smaller_temperature_change(self):
        for step in (1., .3):
            with self.subTest(step=step):
                controller = self.controller()
                controls = controller.list_controls.return_value
                controller.list_controls.return_value = (
                    replace(controls[0], step=step), *controls[1:])
                report = live_series(controller, 42, count=4)
                self.assertEqual(report['preflight_error'], 'unsupported_air_target_step')
                self.assertEqual(report['air_target_step'], step)
                self.assertEqual(self.calls, [(self.mode, 'activate', {})])
                self.assertTrue(report['schedule_confirmed'])

    def test_step_guard_accepts_exact_substeps_and_schedule_only(self):
        for step, count in ((.1, 2), (.05, 2), (1., 0)):
            with self.subTest(step=step, count=count):
                controller = self.controller()
                controls = controller.list_controls.return_value
                controller.list_controls.return_value = (
                    replace(controls[0], step=step), *controls[1:])
                report = live_series(controller, 42, count=count)
                self.assertIsNone(report['error'])
                self.assertTrue(report['schedule_confirmed'])
                self.assertEqual(sum(c[0] == self.circuit for c in self.calls), count)

    def test_grant_denial_is_bounded_and_sends_no_commands(self):
        from zontcontrol.controller.contracts import ControlRequestReason
        controller = self.controller()
        controller.acquire_control.return_value = ControlRequestResult(UUID(int=2), 42,
            ControlRequestState.DENIED, reason=ControlRequestReason.TIMEOUT)
        report = live_series(controller, 42, count=4)
        self.assertFalse(report['schedule_confirmed'])
        self.assertEqual(controller.acquire_control.call_count, 2)
        controller.execute_command.assert_not_called()
        self.assertEqual(report['summary']['started_requests'], 2)
        self.assertEqual(report['summary']['timeout'], 2)


if __name__ == '__main__':
    unittest.main()
