import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from zontcontrol.monitor import assess_history, history_samples
from zontcontrol.heating import HeatingHistory, HeatingPoint, HeatingSeries


START = datetime(2026, 10, 1, tzinfo=timezone.utc)


def typed_point(seconds, value, quality='good'):
    return HeatingPoint(START + timedelta(seconds=seconds), value, quality)


def typed_series(*points):
    return HeatingSeries(tuple(sorted(points, key=lambda p: p.measured_at)))


def heating_history(**changes):
    return replace(HeatingHistory(
        42, 20496, 8207, 8429, 8832, START, START + timedelta(minutes=45),
        START + timedelta(minutes=45),
        air=typed_series(*(typed_point(i * 60, 23.5) for i in range(46))),
        coolant=typed_series(*(typed_point(i * 60, 40 - .2 * i) for i in range(46))),
        air_target=typed_series(typed_point(0, 23.5)), coolant_target=typed_series(typed_point(0, 60)),
        worktime=typed_series(typed_point(0, 60)), target_sensor_id=typed_series(typed_point(0, 8207)),
        reset=typed_series(typed_point(0, 0))), **changes)


class NormalizationTests(unittest.TestCase):
    def sample(self, points, **changes):
        return list(history_samples(heating_history(air=typed_series(*points), **changes)))[-1]

    def test_linear_temperature_and_independent_step_states(self):
        end = START + timedelta(seconds=2730)
        sample = self.sample([typed_point(2690, 20), typed_point(2730, 20.4)], end=end, received_at=end,
                             coolant=typed_series(typed_point(2680, 30), typed_point(2720, 32)),
                             air_target=typed_series(typed_point(0, 23), typed_point(2710, 25)))
        self.assertAlmostEqual(sample.air, 20.1)
        self.assertEqual(sample.coolant, 31)
        self.assertEqual(sample.air_target, 23)

    def test_subsecond_order_selects_nearest_right_support(self):
        end = START + timedelta(seconds=2730)
        sample = self.sample([typed_point(2640, 20), typed_point(2700.9, 24), typed_point(2700.1, 22)],
                             end=end, received_at=end)
        self.assertAlmostEqual(sample.air, 20 + 2 * 60 / 60.1)

    def test_both_supports_and_exact_points_use_last_original_duplicate(self):
        end = START + timedelta(seconds=2730)
        cases = (([typed_point(2640, 20), typed_point(2730, 24), typed_point(2730, 26)], 24),
                 ([typed_point(2640, 20), typed_point(2640, 22), typed_point(2730, 25)], 24),
                 ([typed_point(2700, 20), typed_point(2700, 22)], 22))
        for points, expected in cases:
            with self.subTest(points=points):
                self.assertEqual(self.sample(points, end=end, received_at=end).air, expected)

    def test_last_null_or_bad_duplicate_blocks_exact_and_both_supports(self):
        end = START + timedelta(seconds=2730)
        for quality, value in (('good', None), ('unknown', 22), ('missing', 22), ('stale', 22)):
            for seconds in (2640, 2700, 2730):
                with self.subTest(quality=quality, seconds=seconds):
                    points = [typed_point(2640, 20), typed_point(2730, 22),
                              typed_point(seconds, 21), typed_point(seconds, value, quality)]
                    self.assertIsNone(self.sample(points, end=end, received_at=end).air)

    def test_bad_nonfinite_or_null_support_is_not_bridged(self):
        end = START + timedelta(seconds=2730)
        for invalid in (typed_point(2670, None), typed_point(2670, 21, 'stale'),
                        typed_point(2670, float('nan')), typed_point(2670, True), typed_point(2670, '21')):
            with self.subTest(invalid=invalid):
                self.assertIsNone(self.sample([typed_point(2640, 20), invalid, typed_point(2730, 22)],
                                             end=end, received_at=end).air)

    def test_120_seconds_is_inclusive_and_longer_pair_is_unknown(self):
        end = START + timedelta(seconds=2730)
        for left, expected in ((2610, 21), (2609, None)):
            with self.subTest(left=left):
                self.assertEqual(self.sample([typed_point(left, 18), typed_point(2730, 22)],
                                             end=end, received_at=end).air, expected)

    def test_end_and_received_at_cutoffs_exclude_future_support(self):
        for changes in ({'end': START + timedelta(seconds=2700)},
                        {'received_at': START + timedelta(seconds=2700)}):
            settings = dict(end=START + timedelta(seconds=2730),
                            received_at=START + timedelta(seconds=2730))
            sample = self.sample([typed_point(2640, 20), typed_point(2730, 22)], **{**settings, **changes})
            self.assertEqual(sample.time, START + timedelta(seconds=2640))
            self.assertEqual(sample.air, 20)

    def test_future_step_states_and_reset_do_not_cross_received_cutoff(self):
        end = START + timedelta(seconds=2730)
        data = heating_history(end=end, received_at=START + timedelta(seconds=2700),
            reset=typed_series(typed_point(0, 0), typed_point(2720, 1)),
            air_target=typed_series(typed_point(0, 23.5), typed_point(2720, 99)))
        self.assertEqual(list(history_samples(data))[-1].air_target, 23.5)
        result = assess_history(data)
        self.assertIs(result['reset_active_at_period_end'], False)
        self.assertEqual(result['state_at_period_end'], 'confirmed_pattern')

    def test_shift_is_once_with_45_minutes_and_no_extrapolation(self):
        data = heating_history(start=START - timedelta(minutes=1),
                               air=typed_series(*(typed_point(i * 60, 23) for i in range(-1, 45))))
        samples = list(history_samples(data))
        self.assertEqual(len(samples), 45)
        self.assertEqual(samples[0].time, START)
        self.assertEqual(samples[-1].time, START + timedelta(minutes=44))
        sample = list(history_samples(replace(data, air=typed_series(typed_point(2400, 23)))))[-1]
        self.assertEqual(sample.time, START + timedelta(minutes=44))
        self.assertIsNone(sample.air)

    def test_bad_value_does_not_shift_grid_to_hide_it(self):
        for quality, value in (('good', None), ('stale', 23)):
            sample = self.sample([typed_point(2640, 23), typed_point(2700, value, quality)])
            self.assertEqual(sample.time, START + timedelta(minutes=45))
            self.assertIsNone(sample.air)

    def test_internal_unknown_stays_on_grid(self):
        data = heating_history()
        points = tuple(replace(p, value=None) if p.measured_at == START + timedelta(minutes=20)
                       else p for p in data.air.points)
        samples = list(history_samples(replace(data, air=replace(data.air, points=points))))
        self.assertEqual(len(samples), 45)
        self.assertIsNone(samples[19].air)
        self.assertEqual(samples[20].air, 23.5)

    def test_original_age_and_reset_are_checked_at_poll_time_after_shift(self):
        end = START + timedelta(minutes=45, seconds=59)
        data = heating_history(end=end, received_at=end,
                               air=typed_series(typed_point(2610, 23.5), typed_point(2640, 23.5)))
        result = assess_history(data)
        self.assertEqual(result['latest_valid_sample'], (START + timedelta(minutes=44)).isoformat())
        stale = replace(data, air=typed_series(typed_point(2610, 23.5), typed_point(2630, 23.5)))
        self.assertEqual(assess_history(stale)['state_at_period_end'], 'unknown')
        reset = typed_series(typed_point(0, 0), typed_point(2730, 1))
        self.assertEqual(assess_history(replace(data, reset=reset))['decision_reason'], 'intentional_shutdown')

    def test_quality_applies_to_states_and_latest_raw_measurement(self):
        data = heating_history()
        air = replace(data.air, points=(*data.air.points[:-1], typed_point(2700, 99, 'stale')))
        result = assess_history(replace(data, air=air))
        self.assertEqual(result['latest_valid_sample'], (START + timedelta(minutes=44)).isoformat())
        self.assertEqual(result['state_at_period_end'], 'unknown')
        result = assess_history(replace(data, reset=typed_series(typed_point(0, 0, 'stale'))))
        self.assertEqual(result['decision_reason'], 'reset_state_unknown')


class MonitorTests(unittest.TestCase):
    def test_unusable_telemetry_never_accumulates_usable_samples(self):
        data = heating_history()
        cases = {
            'missing_temperature': replace(data, air=None),
            'bad_temperature_quality': replace(data, air=replace(data.air, points=tuple(
                replace(point, quality='stale') for point in data.air.points))),
            'wrong_sensor': replace(data, target_sensor_id=typed_series(typed_point(0, 999))),
            'invalid_worktime': replace(data, worktime=typed_series(typed_point(0, 61))),
        }
        for name, history in cases.items():
            with self.subTest(name=name):
                result = assess_history(history)
                self.assertEqual((result['usable_samples'], result['detector_buffer_size']), (0, 0))
                self.assertEqual(result['state_at_period_end'], 'unknown')
                self.assertEqual(result['valid_temperature_samples'],
                                 0 if name in ('missing_temperature', 'bad_temperature_quality') else 45)

    def test_future_temperatures_do_not_change_latest_measurement_or_age(self):
        end = START + timedelta(seconds=2739.5)
        for received, future_seconds in ((end, 2740), (START + timedelta(seconds=2700), 2730)):
            with self.subTest(received=received):
                data = heating_history(end=end, received_at=received)
                future = typed_point(future_seconds, 99)
                with_future = replace(data,
                    air=replace(data.air, points=(*data.air.points, future)),
                    coolant=replace(data.coolant, points=(*data.coolant.points, future)))
                decision = assess_history(with_future)
                self.assertEqual(decision, assess_history(data))
                self.assertEqual(decision['latest_valid_sample'], typed_point(2700, 0).measured_at.isoformat())
                self.assertEqual(decision['latest_valid_sample_age_seconds'], 39.5)
                only_future = replace(with_future, air=typed_series(future))
                missing = assess_history(only_future)
                self.assertIsNone(missing['latest_valid_sample'])
                self.assertIsNone(missing['latest_valid_sample_age_seconds'])
                self.assertEqual((missing['state_at_period_end'], missing['decision_reason']),
                                 ('unknown', 'insufficient_or_invalid_telemetry'))
                self.assertEqual(only_future.air.points, (future,))
                self.assertEqual(with_future.air.points, (*data.air.points, future))

    def test_last_bad_reset_duplicate_keeps_its_timestamp_and_quality(self):
        data = heating_history(reset=typed_series(typed_point(0, False),
            typed_point(2700, False), typed_point(2700, False, 'stale')))
        decision = assess_history(data)
        self.assertTrue(decision['reset_history_available'])
        self.assertEqual(decision['reset_latest_measured_at'], data.end.isoformat())
        self.assertEqual(decision['reset_latest_quality'], 'stale')
        self.assertIsNone(decision['reset_active_at_period_end'])
        self.assertEqual((decision['state_at_period_end'], decision['decision_reason']),
                         ('unknown', 'reset_state_unknown'))
        self.assertEqual(len(data.reset.points), 3)

    def test_diagnostics_distinguish_valid_temperatures_usable_samples_and_buffer(self):
        data = heating_history()
        decision = assess_history(data)
        self.assertEqual((decision['valid_temperature_samples'], decision['usable_samples'],
                          decision['detector_buffer_size']), (45, 45, 40))
        self.assertEqual(decision['latest_valid_sample_age_seconds'], 0)
        self.assertEqual(decision['reset_latest_measured_at'], START.isoformat())
        self.assertEqual(decision['reset_latest_quality'], 'good')
        # A missing target clears the buffer without invalidating temperatures.
        target = typed_series(typed_point(0, 23.5), typed_point(2640, None))
        decision = assess_history(replace(data, air_target=target))
        self.assertEqual((decision['valid_temperature_samples'], decision['usable_samples'],
                          decision['detector_buffer_size']), (45, 43, 0))

    def test_diagnostic_age_and_reset_keep_causal_timestamps_quality_and_nulls(self):
        end = START + timedelta(seconds=2739.5)
        decision = assess_history(heating_history(end=end, received_at=end,
            air=typed_series(typed_point(2400, 23.5)),
            reset=typed_series(typed_point(0, 0), typed_point(2700, 0, 'stale'),
                               typed_point(2740, 1))))
        self.assertEqual(decision['latest_valid_sample_age_seconds'], 339.5)
        self.assertEqual(decision['reset_latest_measured_at'], typed_point(2700, 0).measured_at.isoformat())
        self.assertEqual(decision['reset_latest_quality'], 'stale')
        self.assertIsNone(decision['reset_active_at_period_end'])
        decision = assess_history(heating_history(air=None, reset=None, time_truncated=True))
        for name in ('latest_valid_sample', 'latest_valid_sample_age_seconds',
                     'reset_latest_measured_at', 'reset_latest_quality'):
            self.assertIsNone(decision[name])
        self.assertTrue(decision['time_truncated'])
        self.assertEqual(decision['usable_samples'], 0)

    def test_read_only_assessment_returns_current_pattern(self):
        result = assess_history(heating_history())
        self.assertEqual(result['state_at_period_end'], 'confirmed_pattern')
        self.assertIs(result['anomaly'], True)
        self.assertFalse(result['automatic_control'])
        self.assertEqual([event['type'] for event in result['events']], ['confirmed_pattern'])

    def test_missing_telemetry_is_unknown_and_is_not_relabelled_fresh(self):
        data = heating_history()
        result = assess_history(replace(data, air=replace(data.air, points=data.air.points[:40]),
                                        coolant=replace(data.coolant, points=data.coolant.points[:40])))
        self.assertEqual(result['state_at_period_end'], 'unknown')
        self.assertIsNone(result['anomaly'])
        self.assertEqual(result['latest_valid_sample'], (START + timedelta(minutes=39)).isoformat())

    def test_truncation_and_wrong_control_sensor_are_unknown(self):
        for changes in ({'time_truncated': True}, {'target_sensor_id': typed_series(typed_point(0, 999))}):
            with self.subTest(changes=changes):
                self.assertEqual(assess_history(heating_history(**changes))['state_at_period_end'], 'unknown')

    def test_reset_between_minute_samples_is_visible(self):
        result = assess_history(heating_history(reset=typed_series(
            typed_point(0, 0), typed_point(2540, 1), typed_point(2550, 0))))
        self.assertEqual(result['state_at_period_end'], 'suppressed')
        self.assertIs(result['anomaly'], False)

    def test_old_incident_does_not_override_current_decision(self):
        result = assess_history(heating_history(worktime=typed_series(typed_point(0, 60), typed_point(2400, 0))))
        self.assertTrue(any(event['type'] == 'confirmed_pattern' for event in result['events']))
        self.assertTrue(result['incident_pending'])
        self.assertEqual(result['state_at_period_end'], 'normal')
        self.assertIs(result['anomaly'], False)

    def test_missing_or_invalid_reset_state_prevents_a_definite_decision(self):
        for reset in (None, typed_series(), typed_series(typed_point(0, None)),
                      typed_series(typed_point(0, '0')), typed_series(typed_point(2760, 0))):
            with self.subTest(reset=reset):
                result = assess_history(heating_history(reset=reset))
                self.assertEqual(result['decision_reason'], 'reset_state_unknown')
                self.assertIsNone(result['anomaly'])

    def test_intentional_shutdown_overrides_temperature_pattern(self):
        result = assess_history(heating_history(air=None, reset=typed_series(typed_point(0, 0), typed_point(2640, 1))))
        self.assertEqual(result['decision_reason'], 'intentional_shutdown')
        self.assertIs(result['anomaly'], False)
        self.assertIs(result['reset_active_at_period_end'], True)

    def test_time_with_unknown_reset_does_not_accumulate_evidence(self):
        result = assess_history(heating_history(reset=typed_series(typed_point(2400, 0))))
        self.assertIs(result['anomaly'], False)
        self.assertEqual(result['events'], [])

    def test_reset_after_last_grid_minute_is_used_and_future_reset_is_ignored(self):
        end = START + timedelta(minutes=45, seconds=30)
        for offset, expected in ((20, False), (50, True)):
            with self.subTest(offset=offset):
                reset = typed_series(typed_point(0, 0), typed_point(2700 + offset, 1))
                result = assess_history(heating_history(end=end, received_at=end, reset=reset))
                self.assertIs(result['anomaly'], expected)

    def test_float_reset_is_known_in_samples_but_unknown_at_period_end(self):
        data = heating_history(reset=typed_series(typed_point(0, 0.0)))
        self.assertIs(list(history_samples(data))[-1].reset_active, False)
        self.assertIsNone(assess_history(data)['reset_active_at_period_end'])
        self.assertEqual(assess_history(data)['decision_reason'], 'reset_state_unknown')

    def test_short_window_or_invalid_current_worktime_is_unknown(self):
        for changes in ({'start': START + timedelta(minutes=42)}, {'worktime': typed_series(typed_point(2700, 80))}):
            with self.subTest(changes=changes):
                self.assertIsNone(assess_history(heating_history(**changes))['anomaly'])

    def test_identical_independent_calls_return_identical_decisions(self):
        self.assertEqual(assess_history(heating_history()), assess_history(heating_history()))

    def test_no_future_state_is_applied_to_past_measurements(self):
        data = heating_history(air_target=typed_series(typed_point(0, 23.5), typed_point(1800, 25)))
        samples = list(history_samples(data))
        self.assertEqual(samples[28].air_target, 23.5)
        self.assertEqual(samples[29].air_target, 25)
