"""Read-only anomaly assessment from normalized history, with causal time alignment."""

import bisect
import math
from datetime import datetime, timezone

from .anomaly import HeatingSample, NoHeatDetector
from .detection_profile import SMART_2_HEATING_CONFIG


def _epoch(value):
    return int(datetime.fromisoformat(value).timestamp())


def _reset_at_end(device, reset_id, end):
    element = next((item for item in device.get("web_elements", []) if item["id"] == reset_id), None)
    points = sorted((point for point in element["series"].get("state", [])
                     if datetime.fromisoformat(point["time"]) <= end),
                    key=lambda point: datetime.fromisoformat(point["time"])) if element else []
    active = None
    changed_at = None
    previous = None
    for point in points:
        value = point["value"]
        state = bool(value) if type(value) in (bool, int) and value in (0, 1) else None
        if previous is not None and state is not None and state != previous:
            changed_at = datetime.fromisoformat(point["time"])
        previous = state
        active = state
    return active, changed_at


def history_samples(device, start, end, *, circuit_id, air_id, coolant_id, reset_id=8832):
    circuit = next((item for item in device["circuits"] if item["id"] == circuit_id), None)
    if circuit is None:
        raise ValueError("requested circuit has no history")
    sensors = {item["id"]: item["points"] for item in device["temperature_sensors"]}
    data = {"air": sensors.get(air_id, []), "coolant": sensors.get(coolant_id, []),
            **circuit["series"]}
    reset = next((item for item in device.get("web_elements", []) if item["id"] == reset_id), None)
    data["reset"] = reset["series"]["state"] if reset else []
    series = {key: sorted(((_epoch(point["time"]), point["value"]) for point in points), key=lambda point: point[0])
              for key, points in data.items()}
    indexes = {key: [time for time, value in points] for key, points in series.items()}
    transitions = []
    previous = None
    for time, value in series["reset"]:
        if previous is not None and value != previous:
            transitions.append(time)
        previous = value
    for time in range((int(start.timestamp()) // 60 + 1) * 60, int(end.timestamp()) + 1, 60):
        values = {}
        for key, points in series.items():
            index = bisect.bisect_right(indexes[key], time) - 1
            value = points[index][1] if index >= 0 else None
            if key in ("air", "coolant") and index >= 0 and time - points[index][0] > 90:
                value = None
            values[key] = value
        reset_index = bisect.bisect_right(transitions, time) - 1
        yield HeatingSample(
            datetime.fromtimestamp(time, timezone.utc), values["air"], values.get("target_temp"),
            values["coolant"], values.get("setpoint_temp"), values.get("worktime"),
            bool(values["reset"]) if values["reset"] in (0, 1) else None,
            values.get("target_sensor_id") == air_id,
            datetime.fromtimestamp(transitions[reset_index], timezone.utc) if reset_index >= 0 else None,
        )


def assess_history(history, *, circuit_id, air_id=8207, coolant_id=8429, reset_id=8832, config=None):
    if len(history["devices"]) != 1:
        raise ValueError("choose exactly one device for anomaly assessment")
    device = history["devices"][0]
    start = datetime.fromisoformat(history["requested_period"]["start"])
    end = datetime.fromisoformat(history["requested_period"]["end"])
    config = config or SMART_2_HEATING_CONFIG
    detector = NoHeatDetector(config, require_known_reset=True)
    events = []
    valid = 0
    last_sample = None
    for sample in history_samples(device, start, end, circuit_id=circuit_id, air_id=air_id,
                                  coolant_id=coolant_id, reset_id=reset_id):
        event = detector.update(sample)
        last_sample = sample
        if event:
            events.append(event)
        if sample.air is not None and sample.coolant is not None:
            valid += 1
    last_measurements = []
    for sensor_id in (air_id, coolant_id):
        sensor = next((item for item in device["temperature_sensors"] if item["id"] == sensor_id), None)
        times = [datetime.fromisoformat(point["time"]) for point in sensor["points"]
                 if point["value"] is not None and datetime.fromisoformat(point["time"]) <= end] if sensor else []
        last_measurements.append(max(times) if times else None)
    latest = min(last_measurements) if all(last_measurements) else None
    reset_active, reset_changed_at = _reset_at_end(device, reset_id, end)
    in_grace = (reset_changed_at is not None
                and (end - reset_changed_at).total_seconds() < config.reset_grace_minutes * 60)
    values = ((last_sample.air, last_sample.air_target, last_sample.coolant,
               last_sample.coolant_target, last_sample.worktime) if last_sample else ())
    usable = (last_sample is not None and last_sample.selected_air_sensor
              and all(type(value) in (int, float) and math.isfinite(value) for value in values)
              and 0 <= last_sample.worktime <= 60)
    state = detector.level
    reason = "current_no_heating_response" if state != "normal" else "no_current_pattern"
    if device["time_truncated"]:
        state, reason = "unknown", "truncated_history"
    elif reset_active is None:
        state, reason = "unknown", "reset_state_unknown"
    elif reset_active:
        state, reason = "suppressed", "intentional_shutdown"
    elif in_grace:
        state, reason = "suppressed", "restart_grace"
    elif (latest is None or (end - latest).total_seconds() > 120 or not usable
          or len(detector.samples) < 5):
        state, reason = "unknown", "insufficient_or_invalid_telemetry"
    anomaly = None if state == "unknown" else state in ("warning", "confirmed_pattern")
    return {
        "fetched_at": history["fetched_at"], "requested_period": history["requested_period"],
        "device_id": device["id"], "circuit_id": circuit_id,
        "state_at_period_end": state, "anomaly": anomaly, "decision_reason": reason,
        "valid_temperature_samples": valid,
        "latest_valid_sample": latest.isoformat() if latest else None,
        "reset_history_available": any(item["id"] == reset_id for item in device.get("web_elements", [])),
        "reset_active_at_period_end": reset_active,
        "events": events, "automatic_control": False,
        "incident_pending": detector.incident_pending,
    }
