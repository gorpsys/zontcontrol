"""Pure assessment of typed heating history on a causal minute grid."""

import bisect
import math
from collections import OrderedDict
from datetime import timedelta

from .anomaly import HeatingSample, NoHeatDetector
from .detection_profile import SMART_2_HEATING_CONFIG
from .heating import HeatingHistory


class _RowCache:
    """Retain exact immutable row preparations for one assessment."""

    def __init__(self, capacity=7):
        if type(capacity) is not int or capacity <= 0:
            raise ValueError('row cache capacity must be a positive integer')
        self._capacity = capacity
        self._entries = OrderedDict()

    def get(self, row, cutoff):
        if row is None:
            return (), ()
        key = (id(row), cutoff)
        entry = self._entries.get(key)
        if entry is not None:
            if entry[0] is row:
                self._entries.move_to_end(key)
                return entry[1]
            del self._entries[key]
        points = tuple(point for point in row.points if point.measured_at <= cutoff)
        prepared = (points, tuple(point.measured_at for point in points))
        self._entries[key] = (row, prepared)
        self._entries.move_to_end(key)
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)
        return prepared


def _value(point):
    return point.value if point.quality == 'good' else None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _points(history, row):
    cutoff = min(history.end, history.received_at)
    return sorted((point for point in row.points if point.measured_at <= cutoff),
                  key=lambda point: point.measured_at) if row is not None else []


def _reset_at_end(history: HeatingHistory, *, row_cache=None):
    active = None
    changed_at = None
    previous = None
    points = (_points(history, history.reset) if row_cache is None else
              row_cache.get(history.reset, min(history.end, history.received_at))[0])
    for point in points:
        value = _value(point)
        state = bool(value) if type(value) in (bool, int) and value in (0, 1) else None
        if previous is not None and state is not None and state != previous:
            changed_at = point.measured_at
        previous = state
        active = state
    return active, changed_at


def _temperature(points, times, time):
    left = bisect.bisect_right(times, time) - 1
    if left < 0:
        return None
    value = _value(points[left])
    if times[left] == time:
        return value if _number(value) else None
    if left + 1 == len(points):
        return None
    # Pick the last original duplicate at the right timestamp BEFORE checking
    # quality. A later null must never expose an earlier good duplicate.
    right = bisect.bisect_right(times, times[left + 1]) - 1
    next_value = _value(points[right])
    span = (times[right] - times[left]).total_seconds()
    if not (_number(value) and _number(next_value) and 0 < span <= 120):
        return None
    return value + (next_value - value) * (time - times[left]).total_seconds() / span


def history_samples(history: HeatingHistory):
    yield from _history_samples(history, _RowCache())


def _history_samples(history: HeatingHistory, row_cache):
    cutoff = min(history.end, history.received_at)
    series = {}
    indexes = {}
    for name in ('air', 'coolant', 'air_target', 'coolant_target', 'worktime', 'target_sensor_id', 'reset'):
        series[name], indexes[name] = row_cache.get(getattr(history, name), cutoff)
    transitions = []
    previous = None
    for point in series['reset']:
        value = _value(point)
        if previous is not None and value != previous:
            transitions.append(point.measured_at)
        previous = value
    end = history.end.replace(second=0, microsecond=0)
    if any(not indexes[name] or indexes[name][-1] < end for name in ('air', 'coolant')):
        end -= timedelta(minutes=1)
    start = max(history.start, end - timedelta(minutes=45))
    time = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
    while time <= end:
        values = {}
        for name, points in series.items():
            if name in ('air', 'coolant'):
                values[name] = _temperature(points, indexes[name], time)
            else:
                index = bisect.bisect_right(indexes[name], time) - 1
                values[name] = _value(points[index]) if index >= 0 else None
        reset_index = bisect.bisect_right(transitions, time) - 1
        yield HeatingSample(
            time, values['air'], values['air_target'], values['coolant'],
            values['coolant_target'], values['worktime'],
            bool(values['reset']) if values['reset'] in (0, 1) else None,
            values['target_sensor_id'] == history.air_sensor_id,
            transitions[reset_index] if reset_index >= 0 else None)
        time += timedelta(minutes=1)


def assess_history(history: HeatingHistory, *, config=None):
    config = config or SMART_2_HEATING_CONFIG
    row_cache = _RowCache()
    cutoff = min(history.end, history.received_at)
    detector = NoHeatDetector(config, require_known_reset=True)
    events = []
    valid = 0
    usable_samples = 0
    last_sample = None
    for sample in _history_samples(history, row_cache):
        event = detector.update(sample)
        if detector.samples and detector.samples[-1] is sample:
            usable_samples += 1
        last_sample = sample
        if event:
            events.append(event)
        if sample.air is not None and sample.coolant is not None:
            valid += 1
    last_measurements = []
    for row in (history.air, history.coolant):
        times = [point.measured_at for point in row_cache.get(row, cutoff)[0] if _value(point) is not None]
        last_measurements.append(max(times) if times else None)
    latest = min(last_measurements) if all(last_measurements) else None
    reset_active, reset_changed_at = _reset_at_end(history, row_cache=row_cache)
    reset_points = row_cache.get(history.reset, cutoff)[0]
    latest_reset = reset_points[-1] if reset_points else None
    in_grace = (reset_changed_at is not None
                and (history.end - reset_changed_at).total_seconds() < config.reset_grace_minutes * 60)
    values = ((last_sample.air, last_sample.air_target, last_sample.coolant,
               last_sample.coolant_target, last_sample.worktime) if last_sample else ())
    usable = (last_sample is not None and last_sample.selected_air_sensor
              and all(_number(value) for value in values)
              and 0 <= last_sample.worktime <= 60)
    state = detector.level
    reason = 'current_no_heating_response' if state != 'normal' else 'no_current_pattern'
    if history.time_truncated:
        state, reason = 'unknown', 'truncated_history'
    elif reset_active is None:
        state, reason = 'unknown', 'reset_state_unknown'
    elif reset_active:
        state, reason = 'suppressed', 'intentional_shutdown'
    elif in_grace:
        state, reason = 'suppressed', 'restart_grace'
    elif (latest is None or (history.end - latest).total_seconds() > 120 or not usable
          or len(detector.samples) < 5):
        state, reason = 'unknown', 'insufficient_or_invalid_telemetry'
    anomaly = None if state == 'unknown' else state in ('warning', 'confirmed_pattern')
    return {
        'fetched_at': history.received_at.isoformat(),
        'requested_period': {'start': history.start.isoformat(), 'end': history.end.isoformat()},
        'device_id': history.device_id, 'circuit_id': history.circuit_id,
        'state_at_period_end': state, 'anomaly': anomaly, 'decision_reason': reason,
        'valid_temperature_samples': valid,
        'usable_samples': usable_samples,
        'detector_buffer_size': len(detector.samples),
        'latest_valid_sample': latest.isoformat() if latest else None,
        'latest_valid_sample_age_seconds': (history.end - latest).total_seconds() if latest else None,
        'time_truncated': history.time_truncated,
        'reset_history_available': history.reset is not None,
        'reset_active_at_period_end': reset_active,
        'reset_latest_measured_at': latest_reset.measured_at.isoformat() if latest_reset else None,
        'reset_latest_quality': latest_reset.quality if latest_reset else None,
        'events': events, 'automatic_control': False,
        'incident_pending': detector.incident_pending,
    }
