"""Shared control failures, session identity and scalar validation."""

from __future__ import annotations

from collections.abc import Callable
import math
from dataclasses import dataclass
from typing import ClassVar, TypeAlias, cast
from uuid import UUID


UUIDFactory: TypeAlias = Callable[[], UUID]


class ControllerError(Exception):
    """Safe public failure. Arbitrary upstream messages are never accepted."""

    message: ClassVar[str] = "Controller operation failed."

    def __init__(self) -> None:
        super().__init__(self.message)


class InvalidArguments(ControllerError):
    message = "Invalid controller arguments or data."


class RateLimited(ControllerError):
    message = "API rate limit reached."


class CommandOutcomeUnknown(ControllerError):
    """A write was sent but its outcome could not be established; do not retry."""

    message = "Command outcome is unknown after sending."


class DeviceControlBusy(ControllerError):
    message = "Device control is busy."


class ControlOwnershipRequired(ControllerError):
    message = "A current control session for this device is required."


def _require(condition: bool) -> None:
    if not condition:
        raise InvalidArguments()


def _id(value: object) -> None:
    _require(type(value) is int and value > 0)


def _number(value: object) -> None:
    _require(type(value) in (int, float))
    try:
        _require(math.isfinite(cast(float, value)))
    except OverflowError:
        raise InvalidArguments() from None


@dataclass(frozen=True, slots=True)
class ControlSession:
    device_id: int
    session_id: UUID
    expires_at: float

    def __post_init__(self) -> None:
        _id(self.device_id)
        _require(isinstance(self.session_id, UUID) and self.session_id.int != 0)
        _number(self.expires_at)
        _require(self.expires_at > 0)
