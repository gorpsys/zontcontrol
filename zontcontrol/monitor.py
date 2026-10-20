"""Pure assessment of typed heating history on a causal minute grid."""

import bisect
import math
from datetime import timedelta

from .anomaly import HeatingSample, NoHeatDetector
from .detection_profile import SMART_2_HEATING_CONFIG
from .heating import HeatingHistory


def _value(point):
    return point.value if point.quality == 'good' else None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _points(history, row):
    cutoff = min(history.end, history.received_at)
    return sorted((point for point in row.points if point.measured_at <= cutoff),
                  key=lambda point: point.measured_at) if row is not None else []


def _reset_at_end(history: HeatingHistory):
    active = None
    changed_at = None
    previous = None
    for point in _points(history, history.reset):
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
    series = {name: _points(history, getattr(history, name)) for name in (
        'air', 'coolant', 'air_target', 'coolant_target', 'worktime', 'target_sensor_id', 'reset')}
    indexes = {name: [point.measured_at for point in points] for name, points in series.items()}
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
    detector = NoHeatDetector(config, require_known_reset=True)
    events = []
    valid = 0
    last_sample = None
    for sample in history_samples(history):
        event = detector.update(sample)
        last_sample = sample
        if event:
            events.append(event)
        if sample.air is not None and sample.coolant is not None:
            valid += 1
    last_measurements = []
    for row in (history.air, history.coolant):
        times = [point.measured_at for point in _points(history, row) if _value(point) is not None]
        last_measurements.append(max(times) if times else None)
    latest = min(last_measurements) if all(last_measurements) else None
    reset_active, reset_changed_at = _reset_at_end(history)
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
        'latest_valid_sample': latest.isoformat() if latest else None,
        'reset_history_available': history.reset is not None,
        'reset_active_at_period_end': reset_active,
        'events': events, 'automatic_control': False,
        'incident_pending': detector.incident_pending,
    }
