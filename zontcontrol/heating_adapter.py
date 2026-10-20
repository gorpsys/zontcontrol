"""Select explicit Controller API v2 bindings without I/O or resampling."""

from .controller.contracts import HistoryResult
from .heating import HeatingHistory, HeatingPoint, HeatingSeries


def from_controller_history(history: HistoryResult, *, device_id: int,
                            circuit_id: int, air_id: int, coolant_id: int,
                            reset_id: int) -> HeatingHistory:
    if any(type(value) is not int or value <= 0 for value in (
            device_id, circuit_id, air_id, coolant_id, reset_id)) or air_id == coolant_id:
        raise ValueError('Invalid heating bindings')
    if not isinstance(history, HistoryResult) or history.device_id != device_id:
        raise ValueError('Heating history belongs to a different device')
    rows = {(row.key.device_id, row.key.kind, row.key.element_id, row.key.field): row
            for row in history.series}

    def series(kind: str, element_id: int, field: str) -> HeatingSeries | None:
        row = rows.get((device_id, kind, element_id, field))
        if row is None:
            return None
        return HeatingSeries(
            points=tuple(HeatingPoint(point.measured_at, point.value, point.quality.value)
                         for point in row.points),
            source=row.source, unit=row.unit, value_type=row.value_type.value,
            kind=row.kind.value, incomplete_reasons=row.incomplete_reasons)

    return HeatingHistory(
        device_id=device_id, circuit_id=circuit_id, air_sensor_id=air_id,
        coolant_sensor_id=coolant_id, reset_control_id=reset_id,
        start=history.start, end=history.end, received_at=history.received_at,
        air=series('sensor', air_id, 'value'), coolant=series('sensor', coolant_id, 'value'),
        air_target=series('circuit', circuit_id, 'target_temp'),
        coolant_target=series('circuit', circuit_id, 'setpoint_temp'),
        worktime=series('circuit', circuit_id, 'worktime'),
        target_sensor_id=series('circuit', circuit_id, 'target_sensor_id'),
        reset=series('toggle_buttons', reset_id, 'active'),
        time_truncated=history.time_truncated, incomplete_reasons=history.incomplete_reasons)
