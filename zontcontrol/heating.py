"""Immutable heating inputs shared by the controller and archive adapters."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, TypeAlias


Scalar: TypeAlias = int | float | bool | str | None


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError('Invalid heating history structure')


def _utc(value: datetime) -> datetime:
    _require(isinstance(value, datetime) and value.tzinfo is not None
             and value.utcoffset() is not None)
    return value.astimezone(timezone.utc)


def _reasons(values: tuple[str, ...]) -> None:
    _require(isinstance(values, tuple) and all(isinstance(value, str) for value in values))


@dataclass(frozen=True, slots=True)
class HeatingPoint:
    measured_at: datetime
    value: Scalar
    quality: Literal['good', 'unknown', 'missing', 'stale']

    def __post_init__(self) -> None:
        object.__setattr__(self, 'measured_at', _utc(self.measured_at))
        _require(self.value is None or type(self.value) in (int, float, bool, str))
        _require(self.quality in ('good', 'unknown', 'missing', 'stale'))


@dataclass(frozen=True, slots=True)
class HeatingSeries:
    points: tuple[HeatingPoint, ...]
    source: str | None = None
    unit: str | None = None
    value_type: str | None = None
    kind: str | None = None
    incomplete_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require(isinstance(self.points, tuple)
                 and all(isinstance(point, HeatingPoint) for point in self.points))
        _require(all(left.measured_at <= right.measured_at
                     for left, right in zip(self.points, self.points[1:])))
        for value in (self.source, self.unit, self.value_type, self.kind):
            _require(value is None or isinstance(value, str))
        _reasons(self.incomplete_reasons)


@dataclass(frozen=True, slots=True)
class HeatingHistory:
    device_id: int
    circuit_id: int
    air_sensor_id: int
    coolant_sensor_id: int
    reset_control_id: int
    start: datetime
    end: datetime
    received_at: datetime
    air: HeatingSeries | None = None
    coolant: HeatingSeries | None = None
    air_target: HeatingSeries | None = None
    coolant_target: HeatingSeries | None = None
    worktime: HeatingSeries | None = None
    target_sensor_id: HeatingSeries | None = None
    reset: HeatingSeries | None = None
    time_truncated: bool = False
    incomplete_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require(all(type(value) is int and value > 0 for value in (
            self.device_id, self.circuit_id, self.air_sensor_id,
            self.coolant_sensor_id, self.reset_control_id)))
        _require(self.air_sensor_id != self.coolant_sensor_id)
        for name in ('start', 'end', 'received_at'):
            object.__setattr__(self, name, _utc(getattr(self, name)))
        _require(self.start < self.end)
        for row in (self.air, self.coolant, self.air_target, self.coolant_target,
                    self.worktime, self.target_sensor_id, self.reset):
            _require(row is None or isinstance(row, HeatingSeries))
        _require(type(self.time_truncated) is bool)
        _reasons(self.incomplete_reasons)
