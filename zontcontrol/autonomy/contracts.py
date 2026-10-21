"""Trusted algorithms use ordinary DI, cooperative cancellation and no transport.

Factories own their fixed config/<algorithm_id>.json and its validation. Extra
dependencies (clock, input, renderer) can be bound in the factory; a registry is
never passed to an algorithm. Continuous has no registrar deadline. The
runtime owns the once result timeout, independently of run_once returning.
"""

from dataclasses import dataclass
from enum import Enum
import math
import re
from threading import Event
from typing import Protocol, TypeAlias

from ..controller.contracts import Controller


CAPABILITIES = frozenset({
    'list_devices', 'describe_values', 'read_current', 'read_history',
    'list_controls', 'prepare_command', 'execute_command',
    'read_notes', 'write_notes',
})


def valid_id(value):
    return isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_]*', value) is not None


class Mode(Enum):
    ONCE = 'once'
    CONTINUOUS = 'continuous'


class AlgorithmState(Enum):
    REGISTERED = 'registered'
    RUNNING_ONCE = 'running_once'
    RUNNING_CONTINUOUS = 'running_continuous'
    STOPPING = 'stopping'
    STOPPED = 'stopped'
    FAILED = 'failed'


class ResultStatus(Enum):
    SUCCESS = 'success'
    UNKNOWN = 'unknown'
    SUPPRESSED = 'suppressed'


@dataclass(frozen=True)
class AlgorithmResult:
    """A module owns its payload; unknown never implies normal or failure."""

    status: ResultStatus
    value: object = None

    def __post_init__(self):
        if not isinstance(self.status, ResultStatus):
            raise ValueError('Expected ResultStatus')


class AlgorithmError(Exception):
    """Safe lifecycle error; arbitrary module exception text is never exposed."""

    def __init__(self, algorithm_id, reason, message):
        self.algorithm_id = algorithm_id
        self.reason = reason
        super().__init__(message)


class AlgorithmBusy(AlgorithmError):
    def __init__(self, algorithm_id, reason):
        message = ('Разовый запуск недоступен: алгоритм уже работает в постоянном режиме'
                   if reason == 'already_running_continuous' else f'Algorithm busy: {reason}')
        super().__init__(algorithm_id, reason, message)


class AlgorithmTimeout(AlgorithmError, TimeoutError):
    def __init__(self, algorithm_id):
        super().__init__(algorithm_id, 'timeout', 'Timed out waiting for the one-time algorithm result')


class AlgorithmCancelled(AlgorithmError):
    def __init__(self, algorithm_id):
        super().__init__(algorithm_id, 'cancelled', 'Waiting for the one-time algorithm result was cancelled')


class AlgorithmFailed(AlgorithmError):
    def __init__(self, algorithm_id):
        super().__init__(algorithm_id, 'failed', 'Algorithm bootstrap, execution or cleanup failed')


@dataclass(frozen=True)
class RunContext:
    """Lifecycle information only, never authorization for controller commands.

    deadline is one immutable monotonic deadline for once, None for continuous.
    Modules use wait instead of sleep so stop/timeout can interrupt their waits.
    """

    algorithm_id: str
    cancelled: Event
    deadline: float | None

    def wait(self, seconds: float | None = None) -> bool:
        return self.cancelled.wait(seconds)


class OnceAlgorithm(Protocol):
    """Cancellation asks for finally; accepted commands finish normally."""

    def run_once(self, context: RunContext) -> AlgorithmResult: ...


class ContinuousAlgorithm(Protocol):
    """The module owns its loop and intervals; there is no registrar deadline."""

    def run_continuous(self, context: RunContext) -> None: ...


Algorithm: TypeAlias = OnceAlgorithm | ContinuousAlgorithm


class AlgorithmFactory(Protocol):
    """Config validation/construction only; execution belongs to run methods.

    Registration probes and discards an instance. Every launch constructs a
    fresh instance with the shared controller in the runtime worker.
    Constructors must not start background work or send control commands.
    """

    def __call__(self, controller: Controller) -> Algorithm: ...


@dataclass(frozen=True)
class AlgorithmSpec:
    algorithm_id: str
    description: str
    modes: frozenset[Mode]
    factory: AlgorithmFactory
    required_capabilities: frozenset[str] = frozenset()
    minimum_seconds: float = 0

    def __post_init__(self):
        valid_minimum = type(self.minimum_seconds) in (int, float)
        try:
            valid_minimum = (valid_minimum and math.isfinite(self.minimum_seconds)
                             and self.minimum_seconds >= 0)
        except OverflowError:
            valid_minimum = False
        if (not valid_id(self.algorithm_id) or not isinstance(self.description, str)
                or not self.description.strip() or not isinstance(self.modes, frozenset)
                or not self.modes or not all(isinstance(mode, Mode) for mode in self.modes)
                or not callable(self.factory) or not valid_minimum
                or not isinstance(self.required_capabilities, frozenset)
                or not self.required_capabilities <= CAPABILITIES):
            raise ValueError('Invalid algorithm specification')

    def validate_requirements(self, available_capabilities: frozenset[str]) -> None:
        """Check per module before factory/network; registrar records its failure."""
        missing = self.required_capabilities - available_capabilities
        if missing:
            raise ValueError('Missing controller capabilities: ' + ', '.join(sorted(missing)))


@dataclass(frozen=True)
class AlgorithmRegistration:
    """Snapshot of one catalog entry; disabled entries exist only in the report."""

    spec: AlgorithmSpec
    state: AlgorithmState | None
    error: Exception | None = None
    disabled: bool = False


def validate_catalog(catalog, config) -> tuple[AlgorithmSpec, ...]:
    """Pure catalog boundary, not a registrar: never call a module factory.

    Validate ID spelling against the full trusted catalog, then remove disabled
    specs before any module config/DI is constructed. Per-module timeouts and
    capabilities are checked separately so one failure does not hide the rest.
    """
    # Local import avoids config/spec initialization coupling.
    from ..config import ConfigurationError

    catalog = tuple(catalog)
    if not all(isinstance(spec, AlgorithmSpec) for spec in catalog):
        raise ValueError('Expected AlgorithmSpec catalog entries')
    ids = {spec.algorithm_id for spec in catalog}
    if len(ids) != len(catalog):
        raise ConfigurationError('config/registry.json', 'Duplicate algorithm ID in catalog')
    if config.disabled_modules - ids or set(config.once_timeouts) - ids:
        raise ConfigurationError('config/registry.json', 'Unknown algorithm ID')
    return tuple(spec for spec in catalog if spec.algorithm_id not in config.disabled_modules)
