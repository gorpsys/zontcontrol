"""Device coordination runtime and R1/R2 primitives."""

from .runtime import CoordinationRuntime, CoordinatorWorker
from .ports import Clock, CoordinationGate, CoordinationSettings, EpochSampler, MetadataPort
from .clock import BootClock
from .timing import LeaseTiming
from .models import ProtocolProfile
from .statistics import (
    JsonlSink, ObservationError, ObservationSink, ObservationValue, Observer,
    Statistics, analyze, error_event, observe, read_jsonl,
)

__all__ = [
    'CoordinationRuntime', 'CoordinatorWorker',
    'CoordinationGate', 'MetadataPort', 'CoordinationSettings', 'Clock', 'EpochSampler',
    'BootClock', 'LeaseTiming', 'ProtocolProfile',
    'Statistics', 'JsonlSink', 'ObservationError', 'observe', 'error_event',
    'analyze', 'read_jsonl', 'Observer', 'ObservationSink', 'ObservationValue',
]
