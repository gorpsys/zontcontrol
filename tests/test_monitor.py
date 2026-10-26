import gc
import unittest
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier
from unittest.mock import patch

from zontcontrol import monitor
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


class RowCacheTests(unittest.TestCase):
    def test_hit_uses_identity_and_retains_immutable_preparation(self):
        cache = monitor._RowCache()
        row = typed_series(typed_point(0, 23))
        prepared = cache.get(row, START)
        self.assertIs(cache.get(row, START), prepared)
        self.assertIsInstance(prepared[0], tuple)
        self.assertIsInstance(prepared[1], tuple)
        with self.assertRaises(TypeError):
            prepared[0][0] = typed_point(0, 99)
        equal_copy = HeatingSeries(row.points)
        self.assertEqual(row, equal_copy)
        self.assertIsNot(cache.get(equal_copy, START), prepared)
        self.assertEqual(len(cache._entries), 2)

    def test_revisions_and_equal_scalar_types_never_alias(self):
        cache = monitor._RowCache()
        preparations = []
        for value, quality in ((False, 'good'), (0, 'good'), (0.0, 'good'),
                               (1, 'good'), (1.0, 'good'), (None, 'unknown'), (0, 'stale')):
            with self.subTest(value=value, quality=quality):
                row = typed_series(typed_point(0, value, quality))
                prepared = cache.get(row, START)
                self.assertTrue(all(prepared is not old for old in preparations))
                self.assertIs(type(prepared[0][0].value), type(value))
                self.assertEqual(prepared[0][0].quality, quality)
                preparations.append(prepared)

    def test_exact_cutoff_forward_backward_and_return(self):
        row = typed_series(typed_point(-1, 10), typed_point(0, 20),
                           typed_point(.000001, 21), typed_point(1, 22))
        cache = monitor._RowCache()
        history = heating_history(air=row)
        original = cache.get(row, START)
        for offset in (1, .000001, -1, 0, 2, -2):
            with self.subTest(offset=offset):
                cutoff = START + timedelta(seconds=offset)
                points, index = cache.get(row, cutoff)
                expected = monitor._points(replace(history, received_at=cutoff), row)
                self.assertEqual(points, tuple(expected))
                self.assertEqual(index, tuple(point.measured_at for point in expected))
        self.assertIs(cache.get(row, START), original)

    def test_stable_filter_retains_anchors_duplicates_null_and_quality(self):
        values = ((None, 'good'), (22, 'unknown'), (23, 'missing'), (24, 'stale'))
        row = typed_series(typed_point(-60, 18), typed_point(0, 20),
                           *(typed_point(0, value, quality) for value, quality in values),
                           typed_point(1, 99))
        points, index = monitor._RowCache().get(row, START)
        self.assertEqual(points, row.points[:-1])
        self.assertTrue(all(actual is source for actual, source in zip(points, row.points)))
        self.assertEqual(index, (START - timedelta(minutes=1),) + (START,) * 5)
        self.assertEqual(points[-1].quality, 'stale')

    def test_none_empty_and_future_rows(self):
        cache = monitor._RowCache()
        self.assertEqual(cache.get(None, START), ((), ()))
        self.assertFalse(cache._entries)
        empty = HeatingSeries(())
        future = typed_series(typed_point(1, 20))
        for row in (empty, future):
            self.assertEqual(cache.get(row, START), ((), ()))
        self.assertEqual(len(cache._entries), 2)
        self.assertIs(cache.get(empty, START), cache.get(empty, START))

    def test_lru_hit_promotes_entry_and_eviction_recomputes(self):
        cache = monitor._RowCache(capacity=2)
        a, b, c = (typed_series(typed_point(0, value)) for value in (10, 20, 30))
        prepared_a = cache.get(a, START)
        prepared_b = cache.get(b, START)
        self.assertIs(cache.get(a, START), prepared_a)
        cache.get(c, START)
        self.assertEqual(len(cache._entries), 2)
        self.assertNotIn((id(b), START), cache._entries)
        self.assertIs(cache.get(a, START), prepared_a)
        self.assertIsNot(cache.get(b, START), prepared_b)
        self.assertEqual(len(cache._entries), 2)

    def test_row_reference_is_retained_until_eviction_or_cache_destruction(self):
        class TrackedSeries(HeatingSeries):
            pass

        cache = monitor._RowCache(capacity=1)
        row = TrackedSeries((typed_point(0, 20),))
        reference = weakref.ref(row)
        cache.get(row, START)
        del row
        gc.collect()
        self.assertIsNotNone(reference())
        cache.get(typed_series(typed_point(0, 21)), START)
        gc.collect()
        self.assertIsNone(reference())
        row = TrackedSeries((typed_point(0, 22),))
        reference = weakref.ref(row)
        cache.get(row, START)
        del row, cache
        gc.collect()
        self.assertIsNone(reference())

    def test_invalid_capacity(self):
        for capacity in (True, False, 0, -1, 1.0, None, '7'):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                monitor._RowCache(capacity)

    def test_all_capacities_preserve_results_and_expected_assessment_trace(self):
        history = heating_history()
        rows = [getattr(history, name) for name in (
            'air', 'coolant', 'air_target', 'coolant_target', 'worktime', 'target_sensor_id', 'reset')]
        trace = rows + [history.air, history.coolant, history.reset, history.reset]
        for capacity in range(6, 15):
            with self.subTest(capacity=capacity):
                cache = monitor._RowCache(capacity)
                hits = misses = evictions = 0
                for row in trace:
                    key = (id(row), history.end)
                    before = set(cache._entries)
                    hits += key in before
                    misses += key not in before
                    points, index = cache.get(row, history.end)
                    evictions += len(before - set(cache._entries))
                    expected = monitor._points(history, row)
                    self.assertEqual(points, tuple(expected))
                    self.assertEqual(index, tuple(point.measured_at for point in expected))
                    self.assertLessEqual(len(cache._entries), capacity)
                self.assertEqual((hits, misses, evictions), (2, 9, 3) if capacity == 6 else (4, 7, 0))


class RowCacheIntegrationTests(unittest.TestCase):
    class ReferenceRows:
        def get(self, row, cutoff):
            points = monitor._points(heating_history(received_at=cutoff), row)
            return points, [point.measured_at for point in points]

    def test_complete_results_and_samples_match_uncached_preparation(self):
        base = heating_history()
        cases = [base, replace(base, air=None), replace(base, reset=None),
                 replace(base, air=HeatingSeries(())), replace(base, time_truncated=True),
                 replace(base, air_sensor_id=999), replace(base, device_id=99, circuit_id=88),
                 replace(base, start=base.end - timedelta(minutes=2)),
                 replace(base, worktime=typed_series(typed_point(0, float('inf')))),
                 replace(base, coolant=typed_series(typed_point(0, float('nan')))),
                 replace(base, end=base.end - timedelta(seconds=17)),
                 replace(base, received_at=base.end - timedelta(seconds=70)),
                 replace(base, reset=typed_series(typed_point(0, 0), typed_point(2670, 1))),
                 replace(base, reset=typed_series(typed_point(0, 0), typed_point(2650, 1),
                                                  typed_point(2680, 0))),
                 replace(base, coolant=typed_series(*base.coolant.points, typed_point(2700, None, 'missing')))]
        for index, history in enumerate(cases):
            with self.subTest(case=index):
                with patch.object(monitor, '_RowCache', self.ReferenceRows):
                    expected = assess_history(history)
                    samples = list(history_samples(history))
                self.assertEqual(assess_history(history), expected)
                self.assertEqual(list(history_samples(history)), samples)

    def test_config_and_metadata_are_never_memoized(self):
        history = heating_history()
        smart = monitor.SMART_2_HEATING_CONFIG
        for config in (None, smart, replace(smart, warning_minutes=5, confirm_minutes=5), smart):
            with self.subTest(config=config):
                with patch.object(monitor, '_RowCache', self.ReferenceRows):
                    expected = assess_history(history, config=config)
                self.assertEqual(assess_history(history, config=config), expected)
        self.assertEqual(assess_history(history), assess_history(history, config=None))

    def test_cutoff_minimum_and_reset_helper_match_original(self):
        base = heating_history(reset=typed_series(typed_point(0, 0), typed_point(2690, 1)))
        for end_offset, received_offset in ((0, 0), (0, -60), (-60, 0), (-60, -60),
                                            (-.000001, 0), (0, -.000001)):
            with self.subTest(end=end_offset, received=received_offset):
                history = replace(base, end=base.end + timedelta(seconds=end_offset),
                                  received_at=base.end + timedelta(seconds=received_offset))
                self.assertEqual(monitor._reset_at_end(history, row_cache=monitor._RowCache()),
                                 monitor._reset_at_end(history))
                with patch.object(monitor, '_RowCache', self.ReferenceRows):
                    expected = assess_history(history)
                self.assertEqual(assess_history(history), expected)

    def test_one_cache_per_call_and_exact_lookup_trace(self):
        original = monitor._RowCache
        created = []

        class RecordingCache(original):
            def __init__(self):
                super().__init__()
                self.rows = []
                created.append(self)

            def get(self, row, cutoff):
                self.rows.append(row)
                return super().get(row, cutoff)

        history = heating_history()
        with patch.object(monitor, '_RowCache', RecordingCache):
            assess_history(history)
            assess_history(history)
        self.assertEqual(len(created), 2)
        expected = [getattr(history, name) for name in (
            'air', 'coolant', 'air_target', 'coolant_target', 'worktime', 'target_sensor_id', 'reset')]
        expected += [history.air, history.coolant, history.reset, history.reset]
        for cache in created:
            self.assertEqual(len(cache.rows), 11)
            self.assertTrue(all(actual is wanted for actual, wanted in zip(cache.rows, expected)))
            self.assertEqual(len(cache._entries), 7)
        self.assertIsNot(created[0], created[1])

    def test_generators_are_lazy_interleaved_and_release_their_caches(self):
        factory = monitor._RowCache
        references = []

        def create():
            cache = factory()
            references.append(weakref.ref(cache))
            return cache

        first_history = heating_history()
        second_history = heating_history(air_sensor_id=999)
        expected_first = list(history_samples(first_history))
        expected_second = list(history_samples(second_history))
        with patch.object(monitor, '_RowCache', create):
            first = history_samples(first_history)
            second = history_samples(second_history)
            self.assertFalse(references)
            self.assertEqual(next(first), expected_first[0])
            self.assertEqual(next(second), expected_second[0])
            self.assertEqual(len(references), 2)
            first.close()
            gc.collect()
            self.assertIsNone(references[0]())
            self.assertIsNotNone(references[1]())
            self.assertEqual(list(second), expected_second[1:])
            gc.collect()
            self.assertIsNone(references[1]())

    def test_parallel_assessments_are_isolated(self):
        histories = (heating_history(), heating_history(time_truncated=True, reset=None))
        expected = [assess_history(history) for history in histories]
        barrier = Barrier(2)

        def run(history):
            barrier.wait(timeout=5)
            return assess_history(history)

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(run, histories)), expected)

    def test_new_right_support_recomputes_the_previous_unknown_minute(self):
        t = START
        air = typed_series(typed_point(-90, 22),
                           *(typed_point(30 + 60 * i, 22) for i in range(4)))
        coolant = typed_series(typed_point(-90, 20),
                               *(typed_point(30 + 60 * i, 22 + i) for i in range(4)))
        history = heating_history(start=t - timedelta(minutes=46), end=t, received_at=t,
                                  air=air, coolant=coolant, worktime=typed_series(typed_point(-7200, 0)),
                                  air_target=typed_series(typed_point(-7200, 23)),
                                  coolant_target=typed_series(typed_point(-7200, 50)),
                                  target_sensor_id=typed_series(typed_point(-7200, 8207)),
                                  reset=typed_series(typed_point(-7200, False)))
        past_minute = t - timedelta(minutes=1)
        before = {sample.time: sample for sample in history_samples(history)}
        self.assertIsNone(before[past_minute].coolant)
        advanced = replace(history, end=t + timedelta(minutes=1), received_at=t + timedelta(minutes=1))
        after = {sample.time: sample for sample in history_samples(advanced)}
        self.assertAlmostEqual(after[past_minute].coolant, 20.5)
        current = replace(history, end=t + timedelta(minutes=4), received_at=t + timedelta(minutes=4))
        self.assertEqual(assess_history(current)['state_at_period_end'], 'normal')


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
