"""Describe device elements and documented commands without executing them."""

import math

from .controller.contracts import (
    ControlDescriptor, ControlKey, DeviceInfo, InvalidArguments, SeriesKind,
    ValueDescriptor, ValueKey, ValueType,
)


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
        **labels, "hidden": item.get('hidden', False), "actions": [],
    }


def _metadata(item: dict, keys: tuple[str, ...]) -> dict:
    return {key: item[key] for key in keys if key in item}


def control_elements(raw: dict) -> tuple[dict, ...]:
    """Normalize control identities, availability and limits once per response."""
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
            item["actions"].append('target-temp')
        else:
            item["reason"] = "Зависимый контур: уставка берётся из главного контура."
        elements.append(item)
    for mode in _items(raw, "modes"):
        item = _element(mode, "mode", "modes")
        item["state"] = _metadata(mode, ("applied", "can_be_applied"))
        item["actions"].append('activate')
        elements.append(item)
    controls = raw.get("controls", {})
    if not isinstance(controls, dict):
        raise ValueError("invalid controls in API response")
    for category in controls:
        for control in _items(controls, category):
            item = _element(control, category, f"controls.{category}")
            item["state"] = _metadata(control, ("active", "value", "min", "max", "step", "unit", "view"))
            if category in ("buttons", "toggle_buttons"):
                item["actions"].append('trigger')
            if category == "regulators":
                item["actions"].append('set-voltage')
            if not item["actions"]:
                item["reason"] = "В API v3 нет документированной команды для этого типа элемента."
            elements.append(item)
    for category in ("scenarios", "guard_zones"):
        for control in _items(raw, category):
            item = _element(control, category, category)
            item["state"] = _metadata(control, ("state", "alarm"))
            item["actions"].append('activate')
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
    return tuple(elements)


def device_info(raw: dict) -> DeviceInfo:
    """Project public identity only; never retain a full API response in a DTO."""
    if not isinstance(raw, dict) or not isinstance(raw.get('device_info', {}), dict):
        raise InvalidArguments()
    return DeviceInfo(raw.get('id'), raw.get('name'), raw.get('device_info', {}).get('model'),
                      raw.get('online'))


def control_descriptors(raw: dict, *, elements=None) -> tuple[ControlDescriptor, ...]:
    info = device_info(raw)
    elements = control_elements(raw) if elements is None else elements
    result = []
    for item in elements:
        labels = tuple((state, item[field]) for state, field in (
            (True, 'active_label'), (False, 'inactive_label')) if item.get(field))
        state = item['state']
        result.append(ControlDescriptor(
            ControlKey(info.device_id, item['kind'], item['id']), item['name'],
            tuple(item['actions']), labels,
            state.get('min'), state.get('max'), state.get('step'),
            item['hidden'],
            (item['reason'],) if 'reason' in item else ()))
    if len({row.key for row in result}) != len(result):
        raise InvalidArguments()
    return tuple(result)


# Public field names are independent of io/z3k containers. Only the documented
# history channels below are advertised; presence of a current value is no proof.
CIRCUIT_VALUE_FIELDS = {
    'actual_temp': (ValueType.NUMBER, '°C', False),
    'target_temp': (ValueType.NUMBER, '°C', True),
    'setpoint_temp': (ValueType.NUMBER, '°C', True),
    'mode_id': (ValueType.INTEGER, None, True),
    'status': (ValueType.INTEGER, None, True),
    'worktime': (ValueType.INTEGER, None, True),
    'target_sensor_id': (ValueType.INTEGER, None, True),
}


def value_descriptors(raw: dict) -> tuple[ValueDescriptor, ...]:
    device_id = device_info(raw).device_id
    result = []

    def add(kind, item, field, value_type, unit, source, history=False, step=False):
        result.append(ValueDescriptor(
            ValueKey(device_id, kind, item['id'], field), item['name'], unit, source,
            field, value_type, True, history,
            (SeriesKind.STEP if step else SeriesKind.MEASUREMENT) if history else None,
            None if history else 'history_not_supported'))

    for item in _items(raw, 'sensors'):
        item = {**item, **_element(item, 'sensor', 'sensors')}
        # Widget adds a virtual supply display with no addressable sensor ID.
        if item['id'] == 0:
            continue
        temperature = item.get('type') == 'temperature'
        unit = '°C' if temperature and item.get('unit') == '°' else item.get('unit')
        add('sensor', item, 'value', ValueType.NUMBER, unit, 'sensors', temperature)
        if 'triggered' in item:
            add('sensor', item, 'triggered', ValueType.BOOLEAN, None, 'sensors')
    for item in _items(raw, 'circuits'):
        _element(item, 'circuit', 'circuits')
        for field, (value_type, unit, history) in CIRCUIT_VALUE_FIELDS.items():
            source = 'circuits' if field in ('actual_temp', 'target_temp', 'mode_id') else 'full_api'
            add('circuit', item, field, value_type, unit, source, history,
                field != 'actual_temp')
    controls = raw.get('controls', {})
    if not isinstance(controls, dict):
        raise InvalidArguments()
    for category in controls:
        for item in _items(controls, category):
            item = {**item, **_element(item, category, 'controls')}
            for field, value_type in [('active', ValueType.BOOLEAN), ('value', ValueType.NUMBER)]:
                if field in item or category == 'toggle_buttons' and field == 'active':
                    add(category, item, field, value_type, item.get('unit'), 'controls',
                        category == 'toggle_buttons' and field == 'active', True)
    if len({row.key for row in result}) != len(result):
        raise InvalidArguments()
    return tuple(result)


def current_value(raw: dict, full: dict, descriptor: ValueDescriptor):
    """Project a scalar. No response time is a measurement time."""
    key = descriptor.key
    if key.kind == 'sensor':
        item = next(item for item in raw['sensors'] if item['id'] == key.element_id)
        value = item.get(key.field)
    elif key.kind == 'circuit':
        item = next(item for item in raw['circuits'] if item['id'] == key.element_id)
        if descriptor.source == 'full_api':
            io = full.get('io', {})
            if not isinstance(io, dict) or not isinstance(io.get('z3k-state', {}), dict):
                raise InvalidArguments()
            state = io.get('z3k-state', {}).get(str(key.element_id), {})
            if not isinstance(state, dict):
                raise InvalidArguments()
            value = state.get(key.field)
        else:
            value = item.get('current_mode' if key.field == 'mode_id' else key.field)
    else:
        item = next(item for item in raw['controls'][key.kind] if item['id'] == key.element_id)
        value = item.get(key.field)
    # DTO construction validates declared type; do not coerce bool into a number.
    if type(value) in (int, float) and not math.isfinite(value):
        raise InvalidArguments()
    return value
