"""Decode ZONT temperature and circuit history into timestamped UTC series."""

import math
from datetime import datetime, timedelta, timezone


CIRCUIT_FIELDS = ("target_temp", "setpoint_temp", "mode_id", "status", "worktime", "target_sensor_id")


def history_period(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    for value in (start, end):
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("history timestamps must include a timezone")
    start = start.astimezone(timezone.utc).replace(microsecond=0)
    end = end.astimezone(timezone.utc).replace(microsecond=0)
    if start.timestamp() <= 0 or start >= end:
        raise ValueError("history start must be after 1970 and earlier than end")
    return start, end


def cli_period(start: str | None, end: str | None, hours: float | None) -> tuple[datetime, datetime]:
    if start is not None and hours is not None:
        raise ValueError("choose --from or --hours")
    try:
        end_time = datetime.fromisoformat(end) if end else datetime.now(timezone.utc)
        if end_time.tzinfo is None or end_time.utcoffset() is None:
            raise ValueError("history timestamps must include a timezone")
        if start:
            start_time = datetime.fromisoformat(start)
        else:
            duration = 24 if hours is None else hours
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("hours must be a positive finite number")
            start_time = end_time.astimezone(timezone.utc) - timedelta(hours=duration)
    except (OverflowError, ValueError):
        raise ValueError("invalid history period; use timezone-aware RFC 3339 dates or positive --hours") from None
    return history_period(start_time, end_time)


def decode_delta_array(rows: list, start: datetime, end: datetime, *, integer: bool = False) -> list[dict]:
    if not isinstance(rows, list):
        raise ValueError("invalid history series in API response")
    points = []
    timestamp = None
    for row in rows:
        if not isinstance(row, list) or len(row) != 2 or type(row[0]) is not int or row[0] == 0:
            raise ValueError("invalid delta-time history point in API response")
        delta, value = row
        if delta < 0:
            if timestamp is None:
                raise ValueError("history series must start with an absolute timestamp")
            # ZONT encodes FORWARD elapsed seconds as a negative number.
            timestamp -= delta
        else:
            timestamp = delta
        if value is not None and (
            type(value) not in ((int,) if integer else (int, float)) or not math.isfinite(value)
        ):
            raise ValueError("invalid numeric history value in API response")
        try:
            moment = datetime.fromtimestamp(timestamp, timezone.utc)
        except (OverflowError, OSError, ValueError):
            raise ValueError("invalid history timestamp in API response") from None
        if start <= moment <= end:
            points.append({"time": moment.isoformat(), "value": value})
    points.sort(key=lambda point: point["time"])
    return points


def _object_id(key: str) -> int:
    if not isinstance(key, str) or not key.isascii() or not key.isdecimal() or int(key) <= 0:
        raise ValueError("invalid history object ID in API response")
    return int(key)


def normalize_device_history(device: dict, response: dict, start: datetime, end: datetime,
                             circuit_id: int | None) -> dict:
    truncated = response.get("time_truncated", False)
    if type(truncated) is not bool:
        raise ValueError("invalid history truncation flag in API response")
    sensor_data = response.get("z3k_temperature", {})
    circuit_data = response.get("z3k_heating_circuit", {})
    if not isinstance(sensor_data, dict) or not isinstance(circuit_data, dict):
        raise ValueError("invalid temperature or circuit history in API response")
    sensor_names = {sensor["id"]: sensor["name"] for sensor in device["sensors"]}
    circuit_names = {circuit["id"]: circuit["name"] for circuit in device["circuits"]}
    sensors = []
    for key, rows in sensor_data.items():
        object_id = _object_id(key)
        sensors.append({"id": object_id, "name": sensor_names.get(object_id), "unit": "°C",
                        "points": decode_delta_array(rows, start, end)})
    circuits = []
    for key, fields in circuit_data.items():
        object_id = _object_id(key)
        if circuit_id is not None and object_id != circuit_id:
            continue
        if not isinstance(fields, dict):
            raise ValueError("invalid circuit history fields in API response")
        series = {
            field: decode_delta_array(fields.get(field, []), start, end,
                                      integer=field not in ("target_temp", "setpoint_temp"))
            for field in CIRCUIT_FIELDS
        }
        circuits.append({"id": object_id, "name": circuit_names.get(object_id), "series": series})
    web_data = response.get("z3k_web_element", {})
    if not isinstance(web_data, dict):
        raise ValueError("invalid web element history in API response")
    web_elements = []
    for key, fields in web_data.items():
        if not isinstance(fields, dict):
            raise ValueError("invalid web element history fields in API response")
        web_elements.append({"id": _object_id(key), "series": {
            "state": decode_delta_array(fields.get("state", []), start, end),
        }})
    return {
        **{key: device[key] for key in ("id", "name", "model")},
        "time_truncated": truncated, "temperature_sensors": sensors,
        "circuits": circuits, "modes": device["modes"], "web_elements": web_elements,
    }
