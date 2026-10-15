"""Describe device elements and documented commands without executing them."""


def _items(container: dict, key: str) -> list:
    items = container.get(key, [])
    if not isinstance(items, list):
        raise ValueError("invalid element list in API response")
    return items


def _element(item: dict, kind: str, source: str) -> dict:
    if not isinstance(item, dict) or type(item.get("id")) is not int:
        raise ValueError("invalid control element identity in API response")
    name = item.get("name")
    labels = {}
    if isinstance(name, dict):
        labels = {key: name.get(key) for key in ("active_label", "inactive_label")}
        if any(value is not None and not isinstance(value, str) for value in labels.values()):
            raise ValueError("invalid control state labels in API response")
        name = name.get("name")
    if not isinstance(name, str):
        raise ValueError("invalid control element name in API response")
    return {
        "id": item["id"], "name": name, "kind": kind, "source": source,
        **labels, "actions": [],
    }


def _action(base: str, resource: str, entity_id: int, action: str, parameters: dict) -> dict:
    return {
        "method": "POST", "url": f"{base}/{resource}/{entity_id}/actions/{action}",
        "parameters": parameters,
    }


def _metadata(item: dict, keys: tuple[str, ...]) -> dict:
    return {key: item[key] for key in keys if key in item}


def device_inventory(raw: dict, snapshot: dict, api_url: str) -> dict:
    """Expose only element identities, state and supported API action descriptions."""
    base = f"{api_url}/devices/{snapshot['id']}"
    elements = []
    for circuit in _items(raw, "circuits"):
        item = _element(circuit, "circuit", "circuits")
        item["state"] = _metadata(circuit, (
            "type", "status", "actual_temp", "target_temp", "current_mode",
            "active", "is_off", "is_dependent", "min", "max", "step",
        ))
        dependent = circuit.get("is_dependent", False)
        if type(dependent) is not bool:
            raise ValueError("invalid dependent circuit flag in API response")
        if not dependent:
            action = _action(base, "circuits", item["id"], "target-temp", {
                "target_temp": {"type": "number", "required": True},
            })
            action["effect"] = "Переводит контур в ручной режим и сбрасывает текущий режим."
            item["actions"].append(action)
        else:
            item["reason"] = "Зависимый контур: уставка берётся из главного контура."
        elements.append(item)
    for mode in _items(raw, "modes"):
        item = _element(mode, "mode", "modes")
        item["state"] = _metadata(mode, ("applied", "can_be_applied"))
        action = _action(base, "modes", item["id"], "activate", {
            "duration": {"type": "integer", "required": False, "unit": "minutes"},
            "schedule": {"type": "object", "required": False,
                         "properties": {"hour": "integer", "minute": "integer"}, "timezone": "UTC"},
        })
        action["scope"] = "device"
        action["note"] = "Опциональный circuit_id документирован только для линеек + и PRO."
        item["actions"].append(action)
        elements.append(item)
    controls = raw.get("controls", {})
    if not isinstance(controls, dict):
        raise ValueError("invalid controls in API response")
    for category in controls:
        for control in _items(controls, category):
            item = _element(control, category, f"controls.{category}")
            item["state"] = _metadata(control, ("active", "value", "min", "max", "step", "unit", "view"))
            if category == "buttons":
                item["actions"].append(_action(base, "controls", item["id"], "trigger", {}))
            if category == "toggle_buttons":
                item["actions"].append(_action(base, "controls", item["id"], "trigger", {
                    "target_state": {"type": "boolean", "required": True},
                }))
            if category == "regulators":
                item["actions"].append(_action(base, "controls", item["id"], "set-voltage", {
                    "target_value": {"type": "number", "required": True},
                }))
            if not item["actions"]:
                item["reason"] = "В API v3 нет документированной команды для этого типа элемента."
            elements.append(item)
    for category, resource, parameters in [
        ("scenarios", "scenarios", {}),
        ("guard_zones", "guard-zones", {"enable": {"type": "boolean", "required": True}}),
    ]:
        for control in _items(raw, category):
            item = _element(control, category, category)
            item["state"] = _metadata(control, ("state", "alarm"))
            item["actions"].append(_action(base, resource, item["id"], "activate", parameters))
            elements.append(item)
    actuators = raw.get("actuators", {})
    if not isinstance(actuators, dict):
        raise ValueError("invalid actuators in API response")
    for category in actuators:
        for actuator in _items(actuators, category):
            item = _element(actuator, category, f"actuators.{category}")
            item["state"] = _metadata(actuator, ("on", "failed", "testing", "unknown", "no_connection"))
            item["reason"] = "Прямой команды управления исполнительным устройством в API v3 нет."
            elements.append(item)
    return {
        **{key: snapshot[key] for key in ("id", "name", "model", "online")},
        "elements": elements,
    }
