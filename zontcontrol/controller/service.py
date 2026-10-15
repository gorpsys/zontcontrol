"""Account read facade. Its adapter is injected; reads never acquire control.

This module does not depend on the prototype client. Command ownership and
execution are separate responsibilities, added by the command executor.
"""

from datetime import datetime, timedelta, timezone
from functools import wraps

from ..history import history_period, merge_history_points, project_history
from ..inventory import control_descriptors, current_value, device_info, value_descriptors
from .contracts import (
    DeviceUnavailable, HistoryResult, HistorySeries, InvalidArguments, Quality,
    Snapshot, UnsupportedAction, ValueKey, ValueReading,
)


def _safe_data(method):
    @wraps(method)
    def call(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except (ValueError, TypeError, KeyError, AttributeError, StopIteration, OverflowError):
            raise InvalidArguments() from None
    return call


class ControllerService:
    """Read API v1; fresh configuration for each call, with bounded archive work.

    At most 31 daily parts by default. Empty/missing parts and inconsistent
    boundary points are reported, never repaired from current device state.
    clock supplies aware UTC wall time for response provenance, not lease time.
    """

    def __init__(self, adapter, *, clock=None, history_chunk=timedelta(days=1),
                 max_history_parts=31):
        if (not isinstance(history_chunk, timedelta) or history_chunk < timedelta(seconds=1)
                or history_chunk.microseconds or type(max_history_parts) is not int
                or max_history_parts <= 0):
            raise InvalidArguments()
        self._adapter = adapter
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._history_chunk = history_chunk
        self._max_history_parts = max_history_parts

    @staticmethod
    def _keys(device_id, keys, *, required=False):
        if type(device_id) is not int or device_id <= 0:
            raise InvalidArguments()
        if keys is None and not required:
            return
        if (not isinstance(keys, tuple) or required and not keys
                or any(not isinstance(key, ValueKey) or key.device_id != device_id for key in keys)
                or len(set(keys)) != len(keys)):
            raise InvalidArguments()

    def _device(self, device_id):
        devices = self._adapter.devices(device_id)
        matches = [raw for raw in devices if device_info(raw).device_id == device_id]
        if not matches:
            raise DeviceUnavailable()
        if len(matches) != 1:
            raise InvalidArguments()
        return matches[0]

    @staticmethod
    def _select(raw, keys):
        descriptors = value_descriptors(raw)
        if keys is None:
            return descriptors
        catalog = {row.key: row for row in descriptors}
        if any(key not in catalog for key in keys):
            raise InvalidArguments()
        return tuple(catalog[key] for key in keys)

    @_safe_data
    def list_devices(self):
        result = tuple(device_info(raw) for raw in self._adapter.devices())
        if len({row.device_id for row in result}) != len(result):
            raise InvalidArguments()
        return result

    @_safe_data
    def describe_values(self, device_id):
        self._keys(device_id, None)
        return value_descriptors(self._device(device_id))

    @_safe_data
    def list_controls(self, device_id):
        self._keys(device_id, None)
        return control_descriptors(self._device(device_id))

    @_safe_data
    def read_current(self, device_id, value_keys=None):
        self._keys(device_id, value_keys)
        raw = self._device(device_id)
        raw_received_at = self._clock()
        descriptors = self._select(raw, value_keys)
        full = {}
        full_received_at = raw_received_at
        if any(row.source == 'full_api' for row in descriptors):
            full = self._adapter.full_device(device_id)
            if type(full.get('id')) is not int or full['id'] != device_id:
                raise InvalidArguments()
            full_received_at = self._clock()
        values = []
        for row in descriptors:
            value = current_value(raw, full, row)
            # Widget API exposes no measurement timestamp. Even online/ok does
            # not establish freshness; do not manufacture a GOOD measurement.
            quality = Quality.MISSING if value is None else Quality.UNKNOWN
            if value is not None and raw.get('online') is False:
                quality = Quality.STALE
            received_at = full_received_at if row.source == 'full_api' else raw_received_at
            values.append(ValueReading(row.key, value, row.value_type, row.unit, row.source,
                                       None, received_at, quality))
        return Snapshot(device_id, full_received_at, tuple(values))

    @_safe_data
    def read_history(self, device_id, value_keys, start, end):
        self._keys(device_id, value_keys, required=True)
        start, end = history_period(start, end, preserve_precision=True)
        if end > self._clock() or end - start > self._history_chunk * self._max_history_parts:
            raise InvalidArguments()
        raw = self._device(device_id)
        descriptors = self._select(raw, value_keys)
        if any(not row.history_available for row in descriptors):
            raise UnsupportedAction()
        chunks = {row.key: [] for row in descriptors}
        truncated = False
        include_controls = any(row.key.kind == 'toggle_buttons' for row in descriptors)
        part_start = start
        received_at = self._clock()
        while part_start < end:
            part_end = min(part_start + self._history_chunk, end)
            response = self._adapter.load_history(device_id, part_start, part_end,
                                                  include_controls=include_controls)
            received_at = self._clock()
            flag = response.get('time_truncated', False)
            if type(flag) is not bool:
                raise InvalidArguments()
            truncated |= flag
            for row in descriptors:
                chunks[row.key].append(project_history(response, row, part_start, part_end,
                                                       received_at=received_at))
            part_start = part_end
        series = []
        for row in descriptors:
            points, reasons = merge_history_points(chunks[row.key])
            series.append(HistorySeries(row.key, row.value_type, row.unit, row.source,
                                        row.history_kind, points, reasons))
        return HistoryResult(device_id, start, end, received_at, tuple(series), truncated,
                             ('time_truncated',) if truncated else ())
