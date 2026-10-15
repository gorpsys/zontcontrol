"""SMART 2.0 profile fitted to owner-labelled July–October 2026 history."""

from .anomaly import DetectionConfig


SMART_2_HEATING_CONFIG = DetectionConfig(
    boiler_limit=85,
    supply_gap=4,
    pulse_gain=3,
    pulse_window_minutes=5,
    warning_minutes=13,
    confirm_minutes=13,
    reset_grace_minutes=5,
    minimum_worktime=40,
    confirm_air_deficit=0,
    confirm_coolant_drop=3,
    hot_coolant_above=60,
    hot_confirm_minutes=14,
)
