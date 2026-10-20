"""Convert saved archive packets into heating inputs without changing values.

Archive quality was not used by the research path. Its points retain that
projection as quality='good'; it is not a claim about the original measurement.
"""

from datetime import datetime

from zontcontrol.heating import HeatingHistory, HeatingPoint, HeatingSeries


def _mapping(value):
    if not isinstance(value, dict):
        raise ValueError()
    return value


def _element(rows, element_id):
    if not isinstance(rows, list):
        raise ValueError()
    selected = [row for row in rows if _mapping(row).get('id') == element_id
                and type(row.get('id')) is int]
    if len(selected) > 1:
        raise ValueError()
    return selected[0] if selected else None


def _reasons(value):
    if not isinstance(value, (list, tuple)):
        raise ValueError()
    return tuple(value)


def _series(points, metadata):
    if not isinstance(points, list):
        raise ValueError()
    parsed = tuple(HeatingPoint(datetime.fromisoformat(_mapping(point)['time']),
                                point['value'], 'good') for point in points)
    return HeatingSeries(
        points=tuple(sorted(parsed, key=lambda point: point.measured_at)),
        source=metadata.get('source'), unit=metadata.get('unit'),
        value_type=metadata.get('value_type'), kind=metadata.get('history_kind'),
        incomplete_reasons=_reasons(metadata.get('incomplete_reasons', ())))


def from_archive_packet(packet: dict, *, device_id: int, circuit_id: int,
                        air_id: int, coolant_id: int, reset_id: int) -> HeatingHistory:
    """Require one named device/circuit, keeping missing and empty rows distinct."""
    try:
        devices = _mapping(packet)['devices']
        if not isinstance(devices, list) or len(devices) != 1:
            raise ValueError()
        device = _element(devices, device_id)
        if device is None:
            raise ValueError()
        circuit = _element(device.get('circuits', []), circuit_id)
        if circuit is None:
            raise ValueError()
        circuit_rows = _mapping(circuit.get('series', {}))
        circuit_metadata = _mapping(circuit.get('series_metadata', {}))

        def sensor(element_id):
            row = _element(device.get('temperature_sensors', []), element_id)
            return _series(row['points'], row) if row is not None and 'points' in row else None

        def circuit_series(field):
            return (_series(circuit_rows[field], _mapping(circuit_metadata.get(field, {})))
                    if field in circuit_rows else None)

        reset = _element(device.get('web_elements', []), reset_id)
        reset_rows = _mapping(reset.get('series', {})) if reset is not None else {}
        period = _mapping(packet['requested_period'])
        return HeatingHistory(
            device_id=device_id, circuit_id=circuit_id, air_sensor_id=air_id,
            coolant_sensor_id=coolant_id, reset_control_id=reset_id,
            start=datetime.fromisoformat(period['start']), end=datetime.fromisoformat(period['end']),
            received_at=datetime.fromisoformat(packet['fetched_at']),
            air=sensor(air_id), coolant=sensor(coolant_id),
            air_target=circuit_series('target_temp'), coolant_target=circuit_series('setpoint_temp'),
            worktime=circuit_series('worktime'), target_sensor_id=circuit_series('target_sensor_id'),
            reset=_series(reset_rows['state'], reset) if 'state' in reset_rows else None,
            time_truncated=device['time_truncated'],
            incomplete_reasons=_reasons(packet.get('incomplete_reasons', ())))
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValueError('Invalid heating archive packet') from error
