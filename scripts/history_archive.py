"""Research archive projection of controller DTOs, not an application contract.

The collector and offline calibration tools own this format. Nothing here can
register algorithms, acquire ownership or execute a command.
"""


def read_archive(controller, device_id, circuit_id, start, end):
    devices = controller.list_devices()
    device = next(row for row in devices if row.device_id == device_id)
    descriptors = controller.describe_values(device_id)
    selected = tuple(row for row in descriptors if row.history_available and (
        row.key.kind in ('sensor', 'toggle_buttons') or
        row.key.kind == 'circuit' and row.key.element_id == circuit_id))
    result = controller.read_history(device_id, tuple(row.key for row in selected), start, end)
    catalog = {row.key: row for row in selected}
    sensors, circuits, controls = [], {}, {}
    for series in result.series:
        key = series.key
        points = [{'time': point.measured_at.isoformat(), 'value': point.value}
                  for point in series.points]
        if key.kind == 'sensor':
            sensors.append({'id': key.element_id, 'name': catalog[key].name, 'points': points})
        elif key.kind == 'circuit':
            circuit = circuits.setdefault(key.element_id,
                {'id': key.element_id, 'name': catalog[key].name, 'series': {}})
            circuit['series'][key.field] = points
        elif key.kind == 'toggle_buttons':
            controls[key.element_id] = {'id': key.element_id, 'series': {'state': [
                {**point, 'value': None if point['value'] is None else int(point['value'])}
                for point in points]}}
    return {'fetched_at': result.received_at.isoformat(),
        'requested_period': {'start': start.isoformat(), 'end': end.isoformat()},
        'devices': [{'id': device_id, 'name': device.name,
            'time_truncated': result.time_truncated,
            'temperature_sensors': sensors, 'circuits': list(circuits.values()),
            'web_elements': list(controls.values()),
            'modes': [{'id': row.key.element_id, 'name': row.name}
                      for row in controller.list_controls(device_id) if row.key.kind == 'mode']}]}
