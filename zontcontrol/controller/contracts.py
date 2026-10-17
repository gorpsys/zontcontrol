"""Stable, transport-independent controller API v1.

Collections are tuples and values are scalars: DTOs never retain mutable API
responses, credentials, URLs or HTTP request bodies. Times are UTC datetimes,
except ControlSession.expires_at, which belongs to the executor's local clock.
Only an identity-bound controller can issue/validate a session; constructing a
DTO does not confer ownership. Command preparation is not an authorization.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from threading import Event
from typing import Mapping, Protocol, TypeAlias, runtime_checkable
from uuid import UUID

from ..control_types import (
    CommandOutcomeUnknown, ControllerError, ControlOwnershipRequired, ControlSession,
    DeviceControlBusy, InvalidArguments, RateLimited, _id, _number, _require,
)


API_VERSION = 1
Scalar: TypeAlias = str | float | int | bool | None
Arguments: TypeAlias = tuple[tuple[str, Scalar], ...]


class AuthenticationError(ControllerError):
    message = "Authentication failed."


class DeviceUnavailable(ControllerError):
    message = "Device or connection unavailable."


class UnsupportedAction(ControllerError):
    message = "Action is not supported."


class IncompleteHistory(ControllerError):
    message = "Required history is incomplete."


class ControlRequestState(str, Enum):
    REQUESTED = "requested"
    GRANTED = "granted"
    DENIED = "denied"


class ControlRequestReason(str, Enum):
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    SHUTDOWN = "shutdown"
    LEASE_EXPIRED = "lease_expired"
    CONTROLLER_ERROR = "controller_error"


class ControlRequestErrorCode(str, Enum):
    AUTHENTICATION_ERROR = "authentication_error"
    DEVICE_UNAVAILABLE = "device_unavailable"
    UNSUPPORTED_ACTION = "unsupported_action"
    INVALID_ARGUMENTS = "invalid_arguments"
    INCOMPLETE_HISTORY = "incomplete_history"
    RATE_LIMITED = "rate_limited"
    COMMAND_OUTCOME_UNKNOWN = "command_outcome_unknown"
    DEVICE_CONTROL_BUSY = "device_control_busy"
    CONTROL_OWNERSHIP_REQUIRED = "control_ownership_required"
    CONTROLLER_ERROR = "controller_error"

    @classmethod
    def from_error(cls, error: BaseException) -> "ControlRequestErrorCode":
        """Classify only the fixed public types; never retain upstream details."""
        return _CONTROL_REQUEST_ERROR_CODES.get(type(error), cls.CONTROLLER_ERROR)


_CONTROL_REQUEST_ERROR_TYPES = {
    ControlRequestErrorCode.AUTHENTICATION_ERROR: AuthenticationError,
    ControlRequestErrorCode.DEVICE_UNAVAILABLE: DeviceUnavailable,
    ControlRequestErrorCode.UNSUPPORTED_ACTION: UnsupportedAction,
    ControlRequestErrorCode.INVALID_ARGUMENTS: InvalidArguments,
    ControlRequestErrorCode.INCOMPLETE_HISTORY: IncompleteHistory,
    ControlRequestErrorCode.RATE_LIMITED: RateLimited,
    ControlRequestErrorCode.COMMAND_OUTCOME_UNKNOWN: CommandOutcomeUnknown,
    ControlRequestErrorCode.DEVICE_CONTROL_BUSY: DeviceControlBusy,
    ControlRequestErrorCode.CONTROL_OWNERSHIP_REQUIRED: ControlOwnershipRequired,
    ControlRequestErrorCode.CONTROLLER_ERROR: ControllerError,
}
_CONTROL_REQUEST_ERROR_CODES = {error_type: code for code, error_type
                                in _CONTROL_REQUEST_ERROR_TYPES.items()}


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


def _text(value):
    _require(isinstance(value, str) and bool(value.strip()))


def _bool(value):
    _require(type(value) is bool)


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


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class ControlRequest:
    """Local identity handle; its UUID alone does not confer control rights."""

    request_id: UUID
    device_id: int
    initial_state: ControlRequestState = field(default=ControlRequestState.REQUESTED, init=False)

    def __post_init__(self):
        _require(isinstance(self.request_id, UUID) and self.request_id.int != 0)
        _id(self.device_id)


@dataclass(frozen=True, slots=True)
class ControlRequestResult:
    """Immutable request outcome with safe codes, never exception instances."""

    request_id: UUID
    device_id: int
    state: ControlRequestState
    session: ControlSession | None = None
    reason: ControlRequestReason | None = None
    error_code: ControlRequestErrorCode | None = None

    def __post_init__(self):
        _require(isinstance(self.request_id, UUID) and self.request_id.int != 0)
        _id(self.device_id)
        _require(isinstance(self.state, ControlRequestState))
        _require(self.reason is None or isinstance(self.reason, ControlRequestReason))
        _require(self.error_code is None or isinstance(self.error_code, ControlRequestErrorCode))
        if self.state is ControlRequestState.GRANTED:
            _require(isinstance(self.session, ControlSession))
            _require(self.session.device_id == self.device_id)
            _require(self.reason is None and self.error_code is None)
        elif self.state is ControlRequestState.DENIED:
            _require(self.session is None and self.reason is not None)
            _require((self.error_code is not None)
                     == (self.reason is ControlRequestReason.CONTROLLER_ERROR))
        else:
            _require(self.session is None and self.reason is None and self.error_code is None)

    def make_error(self) -> ControllerError | None:
        """Make an independent safe exception; raising it cannot mutate this DTO."""
        if self.error_code is None:
            return None
        return _CONTROL_REQUEST_ERROR_TYPES[self.error_code]()


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

    Reads and preparation never acquire control. try_acquire_control is immediate:
    return a session or raise DeviceControlBusy. request_control registers a local
    request without waiting for gate I/O. get/cancel require the exact issued
    handle and its binding, and return the same immutable terminal result.
    acquire_control waits for that request and returns only GRANTED or DENIED;
    timeout/cancellation can deny it before in-flight gate I/O finishes. A late
    session belongs to worker cleanup, never to the denied caller. Request
    deadlines use the executor's local clock, including time spent in its queue.
    Terminal status remains readable after shutdown, release and lease expiry;
    it records issuance, not current command authority.
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

    def request_control(self, device_id: int, *, lease_seconds: float, timeout: float,
                        poll_seconds: float = 1, cancelled: Event | None = None
                        ) -> ControlRequest: ...

    def get_control_request(self, request: ControlRequest) -> ControlRequestResult: ...

    def cancel_control_request(self, request: ControlRequest) -> ControlRequestResult: ...

    def acquire_control(self, device_id: int, *, lease_seconds: float, timeout: float,
                        poll_seconds: float = 1, cancelled: Event | None = None
                        ) -> ControlRequestResult: ...

    def release_control(self, control_session: ControlSession) -> None: ...

    def execute_command(self, command: ValidatedCommand,
                        control_session: ControlSession) -> CommandResult: ...
