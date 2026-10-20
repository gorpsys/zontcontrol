"""Current calibrated anomaly -> one reset pulse, with cooperative cleanup.

No network or work starts during construction. Once callers may schedule this
module about every 15 minutes; continuous owns its polling and five-minute pause.
API acceptance of off is not evidence that heating recovered.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
from time import monotonic

from ..autonomy.contracts import AlgorithmResult, ResultStatus
from ..config import ConfigurationError, read_json
from ..controller.contracts import (
    ControlKey,
    DeviceUnavailable, InvalidArguments, Quality, ValueKey,
    ValueType,
)
from ..detection_profile import SMART_2_HEATING_CONFIG
from ..monitor import assess_history


CONFIG_PATH = Path('config/boiler_reset.json')
PULSE_SECONDS = 40
POST_RESET_SECONDS = 300
HISTORY_MINUTES = 45
# Forty seconds of reset plus an engineering budget for history and HTTP.
# This is a registrar minimum, not a bound on ZONT response times.
MINIMUM_SECONDS = 120


def _seconds(value):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError()


@dataclass(frozen=True)
class BoilerResetConfig:
    device_id: int
    circuit_id: int
    air_sensor_id: int
    coolant_sensor_id: int
    reset_control_id: int
    check_interval_seconds: float
    detection_profile: str = 'smart_2_heating'

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in (
                self.device_id, self.circuit_id, self.air_sensor_id,
                self.coolant_sensor_id, self.reset_control_id)):
            raise ValueError()
        if self.air_sensor_id == self.coolant_sensor_id:
            raise ValueError()
        _seconds(self.check_interval_seconds)
        if self.detection_profile != 'smart_2_heating':
            raise ValueError()

    @property
    def detector_config(self):
        return SMART_2_HEATING_CONFIG

    @property
    def reset_key(self):
        return ValueKey(self.device_id, 'toggle_buttons', self.reset_control_id, 'active')

    @property
    def history_keys(self):
        return (ValueKey(self.device_id, 'sensor', self.air_sensor_id, 'value'),
                ValueKey(self.device_id, 'sensor', self.coolant_sensor_id, 'value'),
                *(ValueKey(self.device_id, 'circuit', self.circuit_id, field) for field in
                  ('target_temp', 'setpoint_temp', 'worktime', 'target_sensor_id')),
                self.reset_key)


def load_config(*, root=Path('.'), reader=None):
    path = Path(root) / CONFIG_PATH
    data = (reader or read_json)(path)
    try:
        if not isinstance(data, dict):
            raise ValueError()
        return BoilerResetConfig(**data)
    except (ValueError, TypeError, OverflowError):
        raise ConfigurationError(path, 'Invalid boiler_reset bindings, profile or intervals') from None


def history_packet(history, config):
    """Adapt facade series without resampling, interpolation or current fallback.

    Preserve provenance, quality, incomplete reasons and original values. The
    existing detector consumes only value/time, so nongood points are explicitly
    unknown in that projection. Do not gate the whole 45 minutes on complete:
    the calibrated detector can recover after an old gap.
    """
    if history.device_id != config.device_id:
        raise InvalidArguments()
    sensors, circuits, controls = {}, {}, {}
    for row in history.series:
        points = [dict(time=p.measured_at.isoformat(),
                       value=p.value if p.quality is Quality.GOOD else None,
                       original_value=p.value, quality=p.quality.value) for p in row.points]
        metadata = dict(source=row.source, unit=row.unit, value_type=row.value_type.value,
                        history_kind=row.kind.value,
                        incomplete_reasons=list(row.incomplete_reasons))
        key = row.key
        if key.kind == 'sensor' and key.field == 'value':
            sensors[key.element_id] = dict(id=key.element_id, points=points, **metadata)
        elif key.kind == 'circuit':
            circuit = circuits.setdefault(key.element_id, dict(id=key.element_id, series={},
                                                               series_metadata={}))
            circuit['series'][key.field] = points
            circuit['series_metadata'][key.field] = metadata
        elif key.kind == 'toggle_buttons' and key.field == 'active':
            controls[key.element_id] = dict(id=key.element_id, series={'state': points}, **metadata)
    # A missing circuit remains explicit unknown telemetry rather than a hidden
    # boiler circuit supplying its target.
    circuits.setdefault(config.circuit_id, dict(id=config.circuit_id, series={}))
    return dict(fetched_at=history.received_at.isoformat(),
                requested_period=dict(start=history.start.isoformat(), end=history.end.isoformat()),
                incomplete_reasons=list(history.incomplete_reasons), devices=[dict(
                    id=history.device_id, time_truncated=history.time_truncated,
                    temperature_sensors=list(sensors.values()), circuits=list(circuits.values()),
                    web_elements=list(controls.values()))])


class BoilerReset:
    def __init__(self, controller, *, root=Path('.'), reader=None, clock=monotonic,
                 wall_clock=None, wait=None):
        self.config = load_config(root=root, reader=reader)
        self._config_path = Path(root) / CONFIG_PATH
        if not callable(clock) or wall_clock is not None and not callable(wall_clock) \
                or wait is not None and not callable(wait):
            raise ValueError('Invalid boiler_reset dependencies')
        self._controller = controller
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._wait = wait or (lambda context, seconds: context.wait(seconds))

    def _validate_bindings(self):
        values = {row.key: row for row in self._controller.describe_values(self.config.device_id)}
        for key in self.config.history_keys:
            row = values.get(key)
            expected = (ValueType.BOOLEAN if key == self.config.reset_key else
                        ValueType.INTEGER if key.field in ('worktime', 'target_sensor_id')
                        else ValueType.NUMBER)
            if row is None or not row.history_available or row.value_type is not expected:
                raise ConfigurationError(self._config_path,
                                         'Configured binding has no compatible history value')

    @staticmethod
    def _result(decision=None, *, reset_performed=False, reason=None):
        decision = dict(decision or {'state_at_period_end': 'unknown'})
        state = decision['state_at_period_end']
        status = (ResultStatus.UNKNOWN if state == 'unknown' else
                  ResultStatus.SUPPRESSED if state == 'suppressed' else ResultStatus.SUCCESS)
        return AlgorithmResult(status, dict(assessment=decision, reset_performed=reset_performed,
                                            reason=reason))

    def run_once(self, context):
        if context.cancelled.is_set():
            return self._result(reason='cancelled')
        self._validate_bindings()
        if context.cancelled.is_set():
            return self._result(reason='cancelled')
        end = self._wall_clock()
        data = self._controller.read_history(self.config.device_id, self.config.history_keys,
                                            end - timedelta(minutes=HISTORY_MINUTES), end)
        decision = assess_history(history_packet(data, self.config),
                                  circuit_id=self.config.circuit_id,
                                  air_id=self.config.air_sensor_id,
                                  coolant_id=self.config.coolant_sensor_id,
                                  reset_id=self.config.reset_control_id,
                                  config=self.config.detector_config)
        if decision['state_at_period_end'] != 'confirmed_pattern':
            return self._result(decision)
        if context.cancelled.is_set():
            return self._result(decision, reason='cancelled')
        online = next((row.online for row in self._controller.list_devices()
                       if row.device_id == self.config.device_id), None)
        if online is not True:
            raise DeviceUnavailable()
        snapshot = self._controller.read_current(self.config.device_id, (self.config.reset_key,))
        value = next((row for row in snapshot.values if row.key == self.config.reset_key), None)
        # Widget state has no measurement timestamp and quality UNKNOWN is
        # normal. Missing/stale state still cannot authorize a new pulse.
        active = (value.value if value is not None
                  and value.quality in (Quality.GOOD, Quality.UNKNOWN) else None)
        if active is True:
            return self._result({**decision, 'state_at_period_end': 'suppressed'},
                                reason='already_on')
        if active is not False:
            return self._result({**decision, 'state_at_period_end': 'unknown'},
                                reason='current_reset_unknown')
        key = ControlKey(self.config.device_id, 'toggle_buttons', self.config.reset_control_id)
        on = self._controller.prepare_command(self.config.device_id, key, 'trigger',
                                              {'target_state': True})
        off = self._controller.prepare_command(self.config.device_id, key, 'trigger',
                                               {'target_state': False})
        if context.cancelled.is_set():
            return self._result(decision, reason='cancelled')
        try:
            # Even unknown on may have applied. No retry; always attempt off
            # through the same direct controller API.
            self._controller.execute_command(on)
            self._wait(context, PULSE_SECONDS)
        finally:
            self._controller.execute_command(off)
        return self._result(decision, reset_performed=True)

    def run_continuous(self, context):
        next_check = self._clock()
        interval = self.config.check_interval_seconds
        while not context.cancelled.is_set():
            result = self.run_once(context)
            if context.cancelled.is_set():
                return
            if result.value['reset_performed']:
                if self._wait(context, POST_RESET_SECONDS):
                    return
                next_check = self._clock()
            else:
                next_check += interval
                now = self._clock()
                if next_check < now:
                    next_check += (math.floor((now - next_check) / interval) + 1) * interval
                if self._wait(context, max(0, next_check - now)):
                    return
