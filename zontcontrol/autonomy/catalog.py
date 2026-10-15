"""Explicit trusted specs supplied by the composition root; no package scanning.

Builtins join this catalog when their modules exist. Importing the catalog
neither loads module configuration nor constructs any controller or algorithm.
"""

from .contracts import AlgorithmSpec


class AlgorithmCatalog:
    def __init__(self, specs=()):
        self._specs = tuple(specs)
        if not all(isinstance(spec, AlgorithmSpec) for spec in self._specs):
            raise ValueError('Expected AlgorithmSpec catalog entries')
        if len({spec.algorithm_id for spec in self._specs}) != len(self._specs):
            raise ValueError('Duplicate algorithm ID in catalog')

    def __iter__(self):
        return iter(self._specs)
