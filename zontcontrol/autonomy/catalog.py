"""Explicit trusted specs supplied by the composition root; no package scanning.

Builtins join this catalog when their modules exist. Importing the catalog
neither loads module configuration nor constructs any controller or algorithm.
"""

from .contracts import AlgorithmSpec, Mode
from ..algorithms.boiler_reset import BoilerReset, MINIMUM_SECONDS
from ..algorithms.boiler_cli import BoilerCLI


BUILTIN_SPECS = (AlgorithmSpec(
    'boiler_reset', 'Calibrated heating assessment and owned 40-second reset pulse',
    frozenset({Mode.ONCE, Mode.CONTINUOUS}), BoilerReset,
    frozenset({'describe_values', 'read_history', 'list_devices', 'read_current',
               'prepare_command', 'acquire_control', 'execute_command', 'release_control'}),
    MINIMUM_SECONDS), AlgorithmSpec(
    'boiler_cli', 'Persistent interactive readings, explicit commands and SVG history',
    frozenset({Mode.CONTINUOUS}), BoilerCLI,
    frozenset({'list_devices', 'describe_values', 'read_current', 'read_history',
               'list_controls', 'prepare_command', 'acquire_control',
               'release_control', 'execute_command'})),)


class AlgorithmCatalog:
    def __init__(self, specs=()):
        self._specs = tuple(specs)
        if not all(isinstance(spec, AlgorithmSpec) for spec in self._specs):
            raise ValueError('Expected AlgorithmSpec catalog entries')
        if len({spec.algorithm_id for spec in self._specs}) != len(self._specs):
            raise ValueError('Duplicate algorithm ID in catalog')

    def __iter__(self):
        return iter(self._specs)
