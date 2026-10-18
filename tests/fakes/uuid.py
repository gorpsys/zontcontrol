"""Fresh deterministic UUID mocks for isolated, optionally threaded scenarios."""

from itertools import count
from threading import Lock
from typing import Any
from unittest.mock import Mock
from uuid import UUID, uuid4


class _LockedMock(Mock):
    """Keep mock call accounting and UUID allocation in the same critical section."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._call_lock = Lock()

    def __call__(self, *args: Any, **kwargs: Any) -> UUID:
        with self._call_lock:
            return super().__call__(*args, **kwargs)


def uuid_generator(*, start: int = 1) -> Mock:
    """Return unique, predictable IDs without sharing state between tests.

    Share this mock between participants in one scenario. The lock protects
    allocation, not thread scheduling; concurrent callers may arrive in either
    order. Never use this sequential source against an actual device.
    """
    values = count(start)

    def next_uuid() -> UUID:
        return UUID(int=next(values))

    return _LockedMock(spec=uuid4, side_effect=next_uuid, name='uuid_generator')
