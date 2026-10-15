"""Offline, validated preparation of documented ZONT control commands."""

import math
from dataclasses import dataclass


_ACTIONS = {
    "set-temp": ("circuit", "circuits", "target-temp"),
    "activate-mode": ("mode", "modes", "activate"),
    "press-button": ("buttons", "controls", "trigger"),
    "set-switch": ("toggle_buttons", "controls", "trigger"),
    "set-regulator": ("regulators", "controls", "set-voltage"),
    "run-scenario": ("scenarios", "scenarios", "activate"),
    "set-guard": ("guard_zones", "guard-zones", "activate"),
}


@dataclass(frozen=True)
class PreparedCommand:
    action: str
    device_id: int
    element_id: int
    parameters: tuple[tuple[str, float | int | bool], ...] = ()

    def __post_init__(self):
        for value in (self.device_id, self.element_id):
            if type(value) is not int or value <= 0:
                raise ValueError("device and element IDs must be positive integers")
        if not isinstance(self.action, str) or self.action not in _ACTIONS:
            raise ValueError("unsupported control command")
        if not isinstance(self.parameters, tuple) or any(
            not isinstance(pair, tuple) or len(pair) != 2 or not isinstance(pair[0], str)
            for pair in self.parameters
        ):
            raise ValueError("command parameters must be immutable key/value pairs")
        body = dict(self.parameters)
        if len(body) != len(self.parameters):
            raise ValueError("duplicate command parameters")
        expected = {
            "set-temp": {"target_temp"}, "set-switch": {"target_state"},
            "set-regulator": {"target_value"}, "set-guard": {"enable"},
            "press-button": set(), "run-scenario": set(),
        }
        if self.action == "activate-mode":
            if set(body) - {"duration"}:
                raise ValueError("unsupported heating mode parameter")
            duration = body.get("duration")
            if "duration" in body and (type(duration) is not int or duration <= 0):
                raise ValueError("duration must be a positive number of minutes")
            return
        if set(body) != expected[self.action]:
            raise ValueError("missing or unsupported command parameter")
        for key, value in body.items():
            if key in ("target_state", "enable"):
                if type(value) is not bool:
                    raise ValueError("switch and guard states must be boolean")
                continue
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError("temperature and regulator values must be finite numbers")

    @property
    def kind(self) -> str:
        return _ACTIONS[self.action][0]

    @property
    def body(self) -> dict:
        return dict(self.parameters)

    def preview(self, api_url: str) -> dict:
        _, resource, action = _ACTIONS[self.action]
        result = {
            "dry_run": True, "method": "POST",
            "url": f"{api_url}/devices/{self.device_id}/{resource}/{self.element_id}/actions/{action}",
            "body": self.body,
        }
        if self.action == "set-temp":
            result["effect"] = "Переводит контур в ручной режим и сбрасывает текущий режим."
        if self.action == "activate-mode":
            result["effect"] = "Применяет режим ко всем подходящим контурам устройства."
        return result


def prepare_command(action: str, device_id: int, element_id: int, **parameters) -> PreparedCommand:
    return PreparedCommand(action, device_id, element_id, tuple(sorted(parameters.items())))


def validate_element(command: PreparedCommand, element: dict) -> None:
    """Check capabilities and limits against a freshly obtained device inventory."""
    if command.kind != element["kind"] or command.element_id != element["id"]:
        raise ValueError("command does not match the control element")
    if not element["actions"]:
        raise ValueError("this element does not support a documented control command")
    if command.action == "activate-mode":
        if element.get("state", {}).get("can_be_applied") == []:
            raise ValueError("heating mode cannot be applied to any circuit")
    key = {"set-temp": "target_temp", "set-regulator": "target_value"}.get(command.action)
    if key is None:
        return
    state = element.get("state", {})
    value = command.body[key]
    for bound in ("min", "max"):
        limit = state.get(bound)
        if limit is None:
            continue
        if type(limit) not in (int, float) or not math.isfinite(limit):
            raise ValueError("invalid control limit in device inventory")
        if (bound == "min" and value < limit) or (bound == "max" and value > limit):
            raise ValueError("requested value is outside the device limits")
