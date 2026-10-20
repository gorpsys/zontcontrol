"""Fixed infrastructure configuration and a JSON reader usable by modules.

No environment lookup, token, transport, network or algorithm schema lives here.
Paths are relative to the working directory; root/reader are offline DI seams,
not CLI overrides. Each algorithm reads and validates its own fixed file.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import json
import math
from pathlib import Path
from types import MappingProxyType
from typing import Any, NoReturn, TypeAlias, cast

from .autonomy.contracts import AlgorithmSpec, Mode, valid_id


JSONReader: TypeAlias = Callable[[Path], dict[str, Any]]
PathInput: TypeAlias = str | Path


class ConfigurationError(ValueError):
    """Never include raw JSON values or exception text that could contain secrets."""

    def __init__(self, path: PathInput, reason: str) -> None:
        self.path = Path(path)
        super().__init__(f'{self.path}: {reason}. Create or correct this file; see config/README.md')


def _reject_constant(_value: str) -> NoReturn:
    raise ValueError()


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError()
    return number


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def read_json(path: PathInput) -> dict[str, Any]:
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


def _keys(data: object, required: Iterable[str], optional: Iterable[str] = ()) -> None:
    if not isinstance(data, dict) or not set(required) <= data.keys() \
            or data.keys() - set(required) - set(optional):
        raise ValueError()


def _number(value: Any, *, maximum: float | None = None) -> float:
    try:
        valid = (type(value) in (int, float) and math.isfinite(value) and value > 0
                 and (maximum is None or value <= maximum))
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError()
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError()
    return cast(str, value)


@dataclass(frozen=True)
class OnceTimeout:
    minimum_seconds: float
    recommended_seconds: float

    def __post_init__(self) -> None:
        _number(self.minimum_seconds)
        _number(self.recommended_seconds)
        if self.recommended_seconds < self.minimum_seconds:
            raise ValueError()

    def resolve(self, timeout: float | None = None) -> float:
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

    def __post_init__(self) -> None:
        if (not isinstance(self.disabled_modules, frozenset)
                or not all(valid_id(item) for item in self.disabled_modules)
                or type(self.autostart_dual_mode) is not bool
                or not isinstance(self.once_timeouts, Mapping)
                or not all(valid_id(key) and isinstance(pair, OnceTimeout)
                           for key, pair in self.once_timeouts.items())):
            raise ValueError('Invalid registrar settings')
        object.__setattr__(self, 'once_timeouts', MappingProxyType(dict(self.once_timeouts)))

    def timeout_for(self, spec: AlgorithmSpec) -> OnceTimeout | None:
        if Mode.ONCE not in spec.modes:
            return None
        try:
            pair = self.once_timeouts[spec.algorithm_id]
            return OnceTimeout(max(pair.minimum_seconds, spec.minimum_seconds),
                               pair.recommended_seconds)
        except (KeyError, ValueError):
            raise ConfigurationError('config/registry.json',
                                     f'Missing or insufficient once timeout for {spec.algorithm_id}') from None

    def should_autostart(self, spec: AlgorithmSpec) -> bool:
        return (spec.algorithm_id not in self.disabled_modules and Mode.CONTINUOUS in spec.modes
                and (Mode.ONCE not in spec.modes or self.autostart_dual_mode))


@dataclass(frozen=True)
class ControllerConfig:
    contact: str
    http_timeout_seconds: float


def load_registry_config(*, root: PathInput = Path('.'), reader: JSONReader = read_json
                         ) -> RegistryConfig:
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


def load_controller_config(*, root: PathInput = Path('.'), reader: JSONReader = read_json
                           ) -> ControllerConfig:
    path = Path(root) / 'config/controller.json'
    data = reader(path)
    try:
        _keys(data, ('contact', 'http_timeout_seconds'))
        contact = _text(data['contact'])
        if not contact.isascii() or any(ord(c) == 127 for c in contact):
            raise ValueError()
        return ControllerConfig(contact, _number(data['http_timeout_seconds'], maximum=120))
    except (TypeError, ValueError, OverflowError):
        raise ConfigurationError(path, 'Invalid contact or HTTP timeout; see controller.example.json') from None
