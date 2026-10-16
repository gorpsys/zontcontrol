"""Decode ZONT temperature and circuit history into timestamped UTC series."""

import math
from datetime import datetime, timedelta, timezone

from .controller.contracts import HistoryPoint, InvalidArguments, Quality, ValueType


def history_period(start: datetime, end: datetime, *, preserve_precision: bool = False
                   ) -> tuple[datetime, datetime]:
    for value in (start, end):
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("history timestamps must include a timezone")
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    if not preserve_precision:
        start = start.replace(microsecond=0)
        end = end.replace(microsecond=0)
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


def project_history(response, descriptor, start, end, *, received_at):
    """Project one declared value, preserving native timestamps and missing data.

    A missing archive channel/field is an empty part, never today's value. A
    sparse state series is not interpolated, including reset pulses between
    minute measurements. Boundary omissions are reported conservatively.
    """
    key = descriptor.key
    channel, field = {
        'sensor': ('z3k_temperature', None),
        'circuit': ('z3k_heating_circuit', key.field),
        'toggle_buttons': ('z3k_web_element', 'state'),
    }[key.kind]
    data = response.get(channel, {})
    if not isinstance(data, dict):
        raise InvalidArguments()
    # Validate IDs even when no selected object has a row. Do not accept aliases
    # such as "007" or two keys that would collapse onto the same numeric ID.
    ids = [_object_id(item) for item in data]
    if len(set(ids)) != len(ids) or any(str(value) != name for value, name in zip(ids, data)):
        raise InvalidArguments()
    rows = data.get(str(key.element_id), [] if field is None else {})
    if field is not None:
        if not isinstance(rows, dict):
            raise InvalidArguments()
        rows = rows.get(field, [])
    decoded = decode_delta_array(rows, start, end, integer=descriptor.value_type in (
        ValueType.INTEGER, ValueType.BOOLEAN))
    reasons = []
    points = []
    for point in decoded:
        value = point['value']
        if descriptor.value_type is ValueType.BOOLEAN and value is not None:
            if value not in (0, 1):
                raise InvalidArguments()
            value = bool(value)
        moment = datetime.fromisoformat(point['time'])
        if moment > received_at:
            reasons.append('future_points')
            continue
        points.append(HistoryPoint(moment, value, Quality.MISSING if value is None else Quality.GOOD))
    if not points:
        reasons.append('missing_part')
    else:
        if points[0].measured_at != start:
            reasons.append('missing_start')
        if points[-1].measured_at != end:
            reasons.append('missing_end')
        if any(point.value is None for point in points):
            reasons.append('null_values')
    return tuple(points), tuple(dict.fromkeys(reasons))


def merge_history_points(parts):
    """Deduplicate equal boundary points and expose conflicting observations."""
    points = {}
    reasons = []
    for rows, incomplete in parts:
        reasons.extend(incomplete)
        for point in rows:
            old = points.get(point.measured_at)
            if old is not None and old != point:
                points[point.measured_at] = HistoryPoint(point.measured_at, None, Quality.UNKNOWN)
                reasons.append('conflicting_timestamps')
            else:
                points[point.measured_at] = point
    return tuple(points[moment] for moment in sorted(points)), tuple(dict.fromkeys(reasons))
