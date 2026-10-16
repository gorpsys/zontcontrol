"""Stable, transport-independent controller API v1.

Collections are tuples and values are scalars: DTOs never retain mutable API
responses, credentials, URLs or HTTP request bodies. Times are UTC datetimes,
except ControlSession.expires_at, which belongs to the executor's local clock.
Only an identity-bound controller can issue/validate a session; constructing a
DTO does not confer ownership. Command preparation is not an authorization.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Mapping, Protocol, TypeAlias, runtime_checkable
from uuid import UUID


API_VERSION = 1
Scalar: TypeAlias = str | float | int | bool | None
Arguments: TypeAlias = tuple[tuple[str, Scalar], ...]


class ControllerError(Exception):
    """Safe public failure. Arbitrary upstream messages are never accepted."""

    message = "Controller operation failed."

    def __init__(self):
        super().__init__(self.message)


class AuthenticationError(ControllerError):
    message = "Authentication failed."


class DeviceUnavailable(ControllerError):
    message = "Device or connection unavailable."


class UnsupportedAction(ControllerError):
    message = "Action is not supported."


class InvalidArguments(ControllerError):
    message = "Invalid controller arguments or data."


class IncompleteHistory(ControllerError):
    message = "Required history is incomplete."


class RateLimited(ControllerError):
    message = "API rate limit reached."


class CommandOutcomeUnknown(ControllerError):
    """A write was sent but its outcome could not be established; do not retry."""

    message = "Command outcome is unknown after sending."


class DeviceControlBusy(ControllerError):
    message = "Device control is busy."


class ControlOwnershipRequired(ControllerError):
    message = "A current control session for this device is required."


class ValueType(Enum):
    NUMBER = "number"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    TEXT = "text"


class Quality(Enum):
    GOOD = "good"
    UNKNOWN = "unknown"
    MISSING = "missing"
    STALE = "stale"


class SeriesKind(Enum):
    MEASUREMENT = "measurement"
    STEP = "step"


def _require(condition):
    if not condition:
        raise InvalidArguments()


def _id(value):
    _require(type(value) is int and value > 0)


def _text(value):
    _require(isinstance(value, str) and bool(value.strip()))


def _bool(value):
    _require(type(value) is bool)


def _number(value):
    _require(type(value) in (int, float))
    try:
        _require(math.isfinite(value))
    except OverflowError:
        raise InvalidArguments() from None


def _scalar(value, value_type=None):
    _require(value is None or type(value) in (str, int, float, bool))
    if value is None:
        return
    if type(value) in (int, float):
        _number(value)
    if value_type is not None:
        _require(isinstance(value_type, ValueType))
        allowed = {ValueType.NUMBER: (int, float), ValueType.INTEGER: (int,),
                   ValueType.BOOLEAN: (bool,), ValueType.TEXT: (str,)}
        _require(type(value) in allowed[value_type])


def _time(value):
    _require(isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None)
    return value.astimezone(timezone.utc)


def _utc(dto, *names):
    for name in names:
        object.__setattr__(dto, name, _time(getattr(dto, name)))


def _tuple(values, item_type):
    _require(isinstance(values, tuple) and all(isinstance(item, item_type) for item in values))


def _strings(values):
    _tuple(values, str)
    for value in values:
        _text(value)
    _require(len(set(values)) == len(values))


def _unique_keys(values):
    _require(len({value.key for value in values}) == len(values))


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    device_id: int
    name: str
    model: str | None
    online: bool | None

    def __post_init__(self):
        _id(self.device_id)
        _text(self.name)
        if self.model is not None:
            _text(self.model)
        if self.online is not None:
            _bool(self.online)


@dataclass(frozen=True, slots=True)
class ValueKey:
    device_id: int
    kind: str
    element_id: int
    field: str

    def __post_init__(self):
        _id(self.device_id)
        _id(self.element_id)
        _text(self.kind)
        _text(self.field)


@dataclass(frozen=True, slots=True)
class ControlKey:
    device_id: int
    kind: str
    element_id: int

    def __post_init__(self):
        _id(self.device_id)
        _id(self.element_id)
        _text(self.kind)


@dataclass(frozen=True, slots=True)
class ValueDescriptor:
    key: ValueKey
    name: str
    unit: str | None
    source: str
    meaning: str
    value_type: ValueType
    current_available: bool
    history_available: bool
    history_kind: SeriesKind | None = None
    unavailable_reason: str | None = None

    def __post_init__(self):
        _require(isinstance(self.key, ValueKey) and isinstance(self.value_type, ValueType))
        for value in (self.name, self.source, self.meaning):
            _text(value)
        if self.unit is not None:
            _text(self.unit)
        if self.unavailable_reason is not None:
            _text(self.unavailable_reason)
        _bool(self.current_available)
        _bool(self.history_available)
        _require(isinstance(self.history_kind, SeriesKind) if self.history_available
                 else self.history_kind is None)


@dataclass(frozen=True, slots=True)
class ValueReading:
    key: ValueKey
    value: Scalar
    value_type: ValueType
    unit: str | None
    source: str
    measured_at: datetime | None
    received_at: datetime
    quality: Quality

    def __post_init__(self):
        _require(isinstance(self.key, ValueKey) and isinstance(self.value_type, ValueType))
        _require(isinstance(self.quality, Quality))
        _scalar(self.value, self.value_type)
        _text(self.source)
        if self.unit is not None:
            _text(self.unit)
        _utc(self, "received_at")
        if self.measured_at is not None:
            _utc(self, "measured_at")
            _require(self.measured_at <= self.received_at)


@dataclass(frozen=True, slots=True)
class Snapshot:
    device_id: int
    received_at: datetime
    values: tuple[ValueReading, ...]
    is_atomic: bool = field(default=False, init=False)

    def __post_init__(self):
        _id(self.device_id)
        _utc(self, "received_at")
        _tuple(self.values, ValueReading)
        _unique_keys(self.values)
        _require(all(value.key.device_id == self.device_id and value.received_at <= self.received_at
                     for value in self.values))


@dataclass(frozen=True, slots=True)
class HistoryPoint:
    measured_at: datetime
    value: Scalar
    quality: Quality = Quality.GOOD

    def __post_init__(self):
        _utc(self, "measured_at")
        _scalar(self.value)
        _require(isinstance(self.quality, Quality))


@dataclass(frozen=True, slots=True)
class HistorySeries:
    key: ValueKey
    value_type: ValueType
    unit: str | None
    source: str
    kind: SeriesKind
    points: tuple[HistoryPoint, ...]
    incomplete_reasons: tuple[str, ...] = ()

    def __post_init__(self):
        _require(isinstance(self.key, ValueKey) and isinstance(self.value_type, ValueType))
        _require(isinstance(self.kind, SeriesKind))
        _text(self.source)
        if self.unit is not None:
            _text(self.unit)
        _tuple(self.points, HistoryPoint)
        _strings(self.incomplete_reasons)
        for point in self.points:
            _scalar(point.value, self.value_type)
        _require(all(left.measured_at < right.measured_at
                     for left, right in zip(self.points, self.points[1:])))

    @property
    def coverage_start(self) -> datetime | None:
        return self.points[0].measured_at if self.points else None

    @property
    def coverage_end(self) -> datetime | None:
        return self.points[-1].measured_at if self.points else None


@dataclass(frozen=True, slots=True)
class HistoryResult:
    device_id: int
    start: datetime
    end: datetime
    received_at: datetime
    series: tuple[HistorySeries, ...]
    time_truncated: bool = False
    incomplete_reasons: tuple[str, ...] = ()

    def __post_init__(self):
        _id(self.device_id)
        _utc(self, "start", "end", "received_at")
        _require(self.start < self.end)
        _tuple(self.series, HistorySeries)
        _unique_keys(self.series)
        _bool(self.time_truncated)
        _strings(self.incomplete_reasons)
        for row in self.series:
            _require(row.key.device_id == self.device_id)
            _require(all(self.start <= point.measured_at <= self.end
                         and point.measured_at <= self.received_at for point in row.points))

    @property
    def complete(self) -> bool:
        """Coverage is necessary; adapters must also report gaps and truncation."""
        return bool(self.series) and not self.time_truncated and not self.incomplete_reasons and all(
            not row.incomplete_reasons and row.coverage_start == self.start
            and row.coverage_end == self.end
            and all(point.value is not None and point.quality is Quality.GOOD for point in row.points)
            for row in self.series)


@dataclass(frozen=True, slots=True)
class ControlDescriptor:
    key: ControlKey
    name: str
    actions: tuple[str, ...]
    labels: tuple[tuple[Scalar, str], ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    hidden: bool = False
    unavailable_reasons: tuple[str, ...] = ()

    def __post_init__(self):
        _require(isinstance(self.key, ControlKey))
        _text(self.name)
        _strings(self.actions)
        _strings(self.unavailable_reasons)
        _bool(self.hidden)
        _tuple(self.labels, tuple)
        for pair in self.labels:
            _require(len(pair) == 2)
            _scalar(pair[0])
            _text(pair[1])
        _require(len({pair[0] for pair in self.labels}) == len(self.labels))
        for value in (self.minimum, self.maximum, self.step):
            if value is not None:
                _number(value)
        if self.minimum is not None and self.maximum is not None:
            _require(self.minimum <= self.maximum)
        if self.step is not None:
            _require(self.step > 0)
        _require(bool(self.actions) or bool(self.unavailable_reasons))


@dataclass(frozen=True, slots=True)
class ValidatedCommand:
    """Pure preview; execution must recheck current capabilities and ownership."""

    control_key: ControlKey
    action: str
    arguments: Arguments = ()

    def __post_init__(self):
        _require(isinstance(self.control_key, ControlKey))
        _text(self.action)
        _tuple(self.arguments, tuple)
        for pair in self.arguments:
            _require(len(pair) == 2)
            _text(pair[0])
            _scalar(pair[1])
        _require(len({pair[0] for pair in self.arguments}) == len(self.arguments))

    @property
    def device_id(self) -> int:
        return self.control_key.device_id


@dataclass(frozen=True, slots=True)
class ControlSession:
    device_id: int
    session_id: UUID
    expires_at: float

    def __post_init__(self):
        _id(self.device_id)
        _require(isinstance(self.session_id, UUID) and self.session_id.int != 0)
        _number(self.expires_at)
        _require(self.expires_at > 0)


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: ValidatedCommand
    received_at: datetime
    observed_state: Snapshot | None = None
    api_accepted: bool = field(default=True, init=False)

    def __post_init__(self):
        _require(isinstance(self.command, ValidatedCommand))
        _utc(self, "received_at")
        if self.observed_state is not None:
            _require(isinstance(self.observed_state, Snapshot))
            _require(self.observed_state.device_id == self.command.device_id)


@runtime_checkable
class Controller(Protocol):
    """One account, bound by DI to one consumer; no credentials in this API.

    Reads and preparation never acquire control. Acquisition is immediate:
    return a session or raise DeviceControlBusy, without a hidden wait queue.
    Reacquiring an unexpired owned device returns the same session and deadline.
    The caller chooses lease_seconds for grant acquisition, its whole critical
    section, finally and its own margin. Expiry rejects new commands including
    finally; an accepted command is synchronous and is never cancelled by a
    later timeout/stop. Release retains ownership until accepted work finishes.
    The same lease bounds idle local ownership: expiry permits local eviction
    on acquisition or shutdown, without a remote release or automatic renewal.
    Acquisition, accepted work and a pending/running release finish first.
    Application shutdown rejects new bindings and acquisitions, but does not
    revoke an issued, unexpired session that is not being released. Its owner
    must finish cleanup and explicitly release it, including between commands.
    External consumers arrange their own cancellation; the registry does not
    stop their threads. Do not wait for full app.close() in the same thread
    while holding a session: it delays cleanup until lease expiry. Release
    first, or use bounded close, cleanup and
    then full close. A bounded close may return False with resources still open.
    Shutdown waits for session work, not arbitrary external reads without one;
    it neither extends the lease nor guarantees cleanup after process death.
    Session identity and device must be checked by the bound implementation.
    """

    def list_devices(self) -> tuple[DeviceInfo, ...]: ...

    def describe_values(self, device_id: int) -> tuple[ValueDescriptor, ...]: ...

    def read_current(self, device_id: int,
                     value_keys: tuple[ValueKey, ...] | None = None) -> Snapshot: ...

    def read_history(self, device_id: int, value_keys: tuple[ValueKey, ...],
                     start: datetime, end: datetime) -> HistoryResult: ...

    def list_controls(self, device_id: int) -> tuple[ControlDescriptor, ...]: ...

    def prepare_command(self, device_id: int, control_key: ControlKey, action: str,
                        arguments: Mapping[str, Scalar]) -> ValidatedCommand: ...

    def try_acquire_control(self, device_id: int, *, lease_seconds: float) -> ControlSession: ...

    def release_control(self, control_session: ControlSession) -> None: ...

    def execute_command(self, command: ValidatedCommand,
                        control_session: ControlSession) -> CommandResult: ...
