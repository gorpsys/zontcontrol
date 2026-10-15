"""Causal detection of absent heating response; this module never sends commands."""

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class HeatingSample:
    time: datetime
    air: float | None
    air_target: float | None
    coolant: float | None
    coolant_target: float | None
    worktime: float | None
    reset_active: bool | None = None
    selected_air_sensor: bool = True
    reset_changed_at: datetime | None = None


@dataclass(frozen=True)
class DetectionConfig:
    boiler_limit: float = 85
    supply_gap: float = 8
    pulse_gain: float = 3
    pulse_window_minutes: int = 5
    warning_minutes: int = 6
    confirm_minutes: int = 20
    reset_grace_minutes: int = 5
    minimum_worktime: float = 40
    confirm_air_deficit: float = 0.5
    confirm_coolant_drop: float = 3
    hot_coolant_above: float | None = None
    hot_confirm_minutes: int | None = None

    def __post_init__(self):
        for value in (self.boiler_limit, self.supply_gap, self.pulse_gain, self.minimum_worktime):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError("detector thresholds must be positive finite numbers")
        for value in (self.confirm_air_deficit, self.confirm_coolant_drop):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("confirmation thresholds must be nonnegative finite numbers")
        for value in (self.pulse_window_minutes, self.warning_minutes, self.confirm_minutes, self.reset_grace_minutes):
            if type(value) is not int or value <= 0:
                raise ValueError("detector time windows must be positive integer minutes")
        if self.minimum_worktime > 60 or self.warning_minutes > self.confirm_minutes:
            raise ValueError("invalid demand or confirmation threshold")
        if (self.hot_coolant_above is None) != (self.hot_confirm_minutes is None):
            raise ValueError("hot coolant threshold and duration must be configured together")
        if self.hot_coolant_above is not None:
            if (type(self.hot_coolant_above) not in (int, float) or not math.isfinite(self.hot_coolant_above)
                    or self.hot_coolant_above <= 0 or type(self.hot_confirm_minutes) is not int
                    or self.hot_confirm_minutes < self.confirm_minutes):
                raise ValueError("invalid hot coolant confirmation thresholds")


class NoHeatDetector:
    """Consume timestamped measurements in order, using only past measurements."""

    def __init__(self, config: DetectionConfig | None = None, *, require_known_reset=False):
        self.config = config or DetectionConfig()
        self.require_known_reset = require_known_reset
        if not 0 < self.config.warning_minutes <= self.config.confirm_minutes:
            raise ValueError("warning must precede confirmation")
        self.samples = deque(maxlen=40)
        self.last_time = None
        self.last_pulse = None
        self.evidence_since = None
        self.baseline = None
        self.reset_until = None
        self.previous_reset = None
        self.level = "normal"
        self.incident_pending = False

    def _clear(self, sample, reason, *, heat_returned=False):
        event = None
        if self.level != "normal" or (self.incident_pending and heat_returned):
            event = {"type": "ended", "time": sample.time.isoformat(), "reason": reason,
                     "heat_returned": heat_returned}
        if heat_returned:
            self.incident_pending = False
        self.level = "normal"
        self.evidence_since = None
        self.baseline = None
        self.last_pulse = sample.time
        return event

    def update(self, sample: HeatingSample) -> dict | None:
        if not isinstance(sample.time, datetime) or sample.time.tzinfo is None or sample.time.utcoffset() is None:
            raise ValueError("measurement time must include a timezone")
        if sample.reset_active is not None and type(sample.reset_active) is not bool:
            raise ValueError("reset state must be boolean or unknown")
        if self.last_time is not None and sample.time <= self.last_time:
            raise ValueError("measurements must be strictly chronological")
        gap = self.last_time is not None and (sample.time - self.last_time).total_seconds() > 120
        self.last_time = sample.time
        if self.require_known_reset and sample.reset_active is None:
            self.samples.clear()
            return self._clear(sample, "reset_state_unknown")
        values = (sample.air, sample.air_target, sample.coolant, sample.coolant_target, sample.worktime)
        valid = all(type(value) in (int, float) and math.isfinite(value) for value in values)
        if gap or not valid or not sample.selected_air_sensor:
            self.samples.clear()
            return self._clear(sample, "telemetry_gap_or_invalid")
        if not 0 <= sample.worktime <= 60:
            self.samples.clear()
            return self._clear(sample, "invalid_worktime")
        if self.previous_reset is True and sample.reset_active is False:
            self.reset_until = sample.time + timedelta(minutes=self.config.reset_grace_minutes)
        if sample.reset_changed_at is not None:
            if sample.reset_changed_at.tzinfo is None or sample.reset_changed_at > sample.time:
                raise ValueError("reset event must be timezone-aware and not in the future")
            grace_end = sample.reset_changed_at + timedelta(minutes=self.config.reset_grace_minutes)
            self.reset_until = max(self.reset_until, grace_end) if self.reset_until else grace_end
        self.previous_reset = sample.reset_active
        if sample.reset_active is True or (self.reset_until and sample.time < self.reset_until):
            self.samples.clear()
            return self._clear(sample, "reset_or_restart_grace")
        self.samples.append(sample)
        pulse_window = [point.coolant for point in self.samples
                        if sample.time - point.time <= timedelta(minutes=self.config.pulse_window_minutes)]
        rising = len(self.samples) > 1 and sample.coolant >= self.samples[-2].coolant
        if self.incident_pending and rising and sample.coolant - min(pulse_window) >= 5:
            return self._clear(sample, "heating_response_returned", heat_returned=True)
        recent = [point for point in self.samples if sample.time - point.time < timedelta(minutes=5)]
        demand = len(recent) >= 5 and sum(point.worktime for point in recent) / len(recent) >= self.config.minimum_worktime
        gap_value = min(sample.coolant_target, self.config.boiler_limit) - sample.coolant
        if not demand or gap_value < self.config.supply_gap:
            return self._clear(sample, "request_ended_or_target_reached")
        if rising and (sample.coolant - min(pulse_window) >= self.config.pulse_gain
                       or sample.coolant - pulse_window[0] >= 1):
            self.last_pulse = sample.time
            return self._clear(sample, "heating_response_returned")
        if self.evidence_since is None:
            self.evidence_since = sample.time
            self.baseline = sample.coolant
        if self.last_pulse is None:
            self.last_pulse = sample.time
        absence = (sample.time - max(self.last_pulse, self.evidence_since)).total_seconds() / 60
        if absence < self.config.warning_minutes:
            return None
        # A warm, steady supply can be normal continuous heating, not a missing response.
        if self.baseline - sample.coolant < 0.5 and sample.coolant > sample.air + 10:
            return None
        severity = "warning"
        confirm_minutes = self.config.confirm_minutes
        if self.config.hot_coolant_above is not None and sample.coolant > self.config.hot_coolant_above:
            confirm_minutes = self.config.hot_confirm_minutes
        if absence >= confirm_minutes and (
            sample.air_target - sample.air >= self.config.confirm_air_deficit
            or self.baseline - sample.coolant >= self.config.confirm_coolant_drop
        ):
            severity = "confirmed_pattern"
        if self.level == "confirmed_pattern":
            severity = "confirmed_pattern"
        if severity == self.level:
            return None
        self.level = severity
        self.incident_pending = True
        return {
            "type": severity, "time": sample.time.isoformat(),
            "evidence_since": self.evidence_since.isoformat(),
            "air": sample.air, "air_target": sample.air_target,
            "coolant": sample.coolant, "coolant_target": sample.coolant_target,
            "effective_target": min(sample.coolant_target, self.config.boiler_limit),
            "minutes_without_response": round(absence, 1),
            "reset_state_known": sample.reset_active is not None,
        }
