"""Fixed infrastructure configuration and a JSON reader usable by modules.

No environment lookup, token, transport, network or algorithm schema lives here.
Paths are relative to the working directory; root/reader are offline DI seams,
not CLI overrides. Each algorithm reads and validates its own fixed file.
"""

from dataclasses import dataclass
import json
import math
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .autonomy.contracts import Mode, valid_id
from .coordination.clock import LeaseTiming


class ConfigurationError(ValueError):
    """Never include raw JSON values or exception text that could contain secrets."""

    def __init__(self, path, reason):
        self.path = Path(path)
        super().__init__(f'{self.path}: {reason}. Create or correct this file; see config/README.md')


def _reject_constant(_value):
    raise ValueError()


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError()
    return number


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def read_json(path) -> dict:
    """Read one UTF-8 JSON object; no duplicate keys or nonfinite constants."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_object,
                          parse_constant=_reject_constant, parse_float=_finite_float)
    except (OSError, ValueError):
        raise ConfigurationError(path, 'Missing or invalid JSON object') from None
    if not isinstance(data, dict):
        raise ConfigurationError(path, 'Expected a JSON object')
    return data


def _keys(data, required, optional=()):
    if not isinstance(data, dict) or not set(required) <= data.keys() \
            or data.keys() - set(required) - set(optional):
        raise ValueError()


def _number(value, *, maximum=None):
    try:
        valid = (type(value) in (int, float) and math.isfinite(value) and value > 0
                 and (maximum is None or value <= maximum))
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError()
    return value


def _text(value):
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError()
    return value


@dataclass(frozen=True)
class OnceTimeout:
    minimum_seconds: float
    recommended_seconds: float

    def __post_init__(self):
        _number(self.minimum_seconds)
        _number(self.recommended_seconds)
        if self.recommended_seconds < self.minimum_seconds:
            raise ValueError()

    def resolve(self, timeout=None) -> float:
        value = self.recommended_seconds if timeout is None else timeout
        try:
            _number(value)
            if value < self.minimum_seconds:
                raise ValueError()
        except ValueError:
            raise ConfigurationError('config/registry.json', 'Invalid once timeout') from None
        return value


@dataclass(frozen=True)
class RegistryConfig:
    disabled_modules: frozenset[str]
    autostart_dual_mode: bool
    once_timeouts: Mapping[str, OnceTimeout]

    def timeout_for(self, spec) -> OnceTimeout | None:
        if Mode.ONCE not in spec.modes:
            return None
        try:
            pair = self.once_timeouts[spec.algorithm_id]
            return OnceTimeout(max(pair.minimum_seconds, spec.minimum_seconds),
                               pair.recommended_seconds)
        except (KeyError, ValueError):
            raise ConfigurationError('config/registry.json',
                                     f'Missing or insufficient once timeout for {spec.algorithm_id}') from None

    def should_autostart(self, spec) -> bool:
        return (spec.algorithm_id not in self.disabled_modules and Mode.CONTINUOUS in spec.modes
                and (Mode.ONCE not in spec.modes or self.autostart_dual_mode))


@dataclass(frozen=True)
class CoordinationConfig:
    delta_seconds: float
    leader_period_seconds: float

    @property
    def timing(self) -> LeaseTiming:
        # The agreed rho table is selected by LeaseTiming using original periods.
        return LeaseTiming(self.leader_period_seconds, self.delta_seconds)


@dataclass(frozen=True)
class StatisticsConfig:
    path: Path
    profile: str


@dataclass(frozen=True)
class ControllerConfig:
    contact: str
    http_timeout_seconds: float
    coordination: CoordinationConfig
    statistics: StatisticsConfig


def load_registry_config(*, root=Path('.'), reader=read_json) -> RegistryConfig:
    path = Path(root) / 'config/registry.json'
    data = reader(path)
    try:
        _keys(data, (), ('disabled_modules', 'autostart_dual_mode', 'once_timeouts'))
        disabled = data.get('disabled_modules', [])
        autostart = data.get('autostart_dual_mode', False)
        timeouts = data.get('once_timeouts', {})
        if (not isinstance(disabled, list) or not all(valid_id(item) for item in disabled)
                or len(disabled) != len(set(disabled)) or type(autostart) is not bool
                or not isinstance(timeouts, dict)):
            raise ValueError()
        pairs = {}
        for algorithm_id, pair in timeouts.items():
            if not valid_id(algorithm_id):
                raise ValueError()
            _keys(pair, ('minimum_seconds', 'recommended_seconds'))
            pairs[algorithm_id] = OnceTimeout(**pair)
        return RegistryConfig(frozenset(disabled), autostart, MappingProxyType(pairs))
    except ValueError:
        raise ConfigurationError(path, 'Invalid registrar settings') from None


def load_controller_config(*, root=Path('.'), reader=read_json) -> ControllerConfig:
    path = Path(root) / 'config/controller.json'
    data = reader(path)
    try:
        _keys(data, ('contact', 'http_timeout_seconds', 'coordination', 'statistics'))
        contact = _text(data['contact'])
        if not contact.isascii() or any(ord(c) == 127 for c in contact):
            raise ValueError()
        timeout = _number(data['http_timeout_seconds'], maximum=120)
        _keys(data['coordination'], ('delta_seconds', 'leader_period_seconds'))
        coordination = CoordinationConfig(**data['coordination'])
        coordination.timing  # Validate lease feasibility before constructing any transport.
        _keys(data['statistics'], ('path', 'profile'))
        statistics = StatisticsConfig(Path(_text(data['statistics']['path'])),
                                      _text(data['statistics']['profile']))
        return ControllerConfig(contact, timeout, coordination, statistics)
    except (TypeError, ValueError, OverflowError):
        raise ConfigurationError(path, 'Invalid controller settings') from None
