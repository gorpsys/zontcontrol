"""Sensor reading and token issuance through the official ZONT Widget API v3."""

import base64
import json
import math
import socket
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .inventory import device_inventory
from .commands import PreparedCommand, validate_element
from .history import history_period, normalize_device_history


API_URL = "https://my.zont.online/api/widget/v3"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class ZontError(Exception):
    """An error safe to display without credentials or response bodies."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward credentials to a redirected host.
        return None


def _header_value(value: str, name: str) -> str:
    if not value.strip() or any(ord(char) < 32 or ord(char) > 126 for char in value):
        raise ValueError(f"{name} must contain nonempty printable ASCII text")
    return value


class ZontClient:
    def __init__(
        self,
        contact: str,
        *,
        token: str | None = None,
        login: str | None = None,
        password: str | None = None,
        timeout: float = 15,
    ):
        if not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError("timeout must be between 0 and 120 seconds")
        self._timeout = timeout
        self._headers = {
            "X-ZONT-Client": _header_value(contact, "contact"),
            "Accept": "application/json",
        }
        if token is not None:
            if login is not None or password is not None:
                raise ValueError("choose token or login authentication")
            self._headers["X-ZONT-Token"] = _header_value(token, "token")
        else:
            if not login or not password or ":" in login:
                raise ValueError("login and password are required; login cannot contain ':'")
            credentials = base64.b64encode(f"{login}:{password}".encode()).decode("ascii")
            self._headers["Authorization"] = f"Basic {credentials}"
        self._opener = build_opener(_NoRedirect())

    def create_token(self, client_name: str = "zontcontrol") -> str:
        """Issue a token using Basic Auth without persisting the secret."""
        if "Authorization" not in self._headers:
            raise ValueError("token creation requires login and password")
        if not isinstance(client_name, str) or not client_name.strip():
            raise ValueError("client_name must be nonempty text")
        request = Request(
            f"{API_URL}/authtokens",
            data=json.dumps({"client_name": client_name}).encode("utf-8"),
            headers={**self._headers, "Content-Type": "application/json"},
            method="POST",
        )
        data = self._request(request)
        token = data.get("token")
        if not isinstance(token, str):
            raise ZontError("API response does not contain a valid token")
        try:
            return _header_value(token, "token")
        except ValueError:
            raise ZontError("API response does not contain a valid token") from None

    def read_sensors(self, device_id: int | None = None) -> dict:
        """Return sensors and heating targets; fetched_at is NOT measurement time."""
        if device_id is not None and (type(device_id) is not int or device_id <= 0):
            raise ValueError("device_id must be a positive integer")
        # This server accepts a comma-separated field list, not repeated keys.
        query = {"fields": "sensors,circuits,modes"}
        if device_id is not None:
            query["device_ids"] = device_id
        request = Request(f"{API_URL}/devices?{urlencode(query)}", headers=self._headers)
        data = self._request(request)
        devices = data.get("devices")
        if not isinstance(devices, list):
            raise ZontError("API response does not contain a device list")
        if device_id is not None and not any(
            isinstance(device, dict) and device.get("id") == device_id for device in devices
        ):
            raise ZontError("requested device is not available in this account")
        snapshots = [_device_snapshot(device) for device in devices]
        if any(device["circuits"] for device in snapshots):
            self._load_circuit_setpoints(snapshots)
        return {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "devices": snapshots,
        }

    def list_controls(self, device_id: int | None = None) -> dict:
        """List device elements and their documented actions using one GET request."""
        if device_id is not None and (type(device_id) is not int or device_id <= 0):
            raise ValueError("device_id must be a positive integer")
        query = "" if device_id is None else "?" + urlencode({"device_ids": device_id})
        data = self._request(Request(f"{API_URL}/devices{query}", headers=self._headers))
        devices = data.get("devices")
        if not isinstance(devices, list):
            raise ZontError("API response does not contain a device list")
        if device_id is not None and not any(
            isinstance(device, dict) and device.get("id") == device_id for device in devices
        ):
            raise ZontError("requested device is not available in this account")
        return {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "devices": [
                device_inventory(device, _device_snapshot(device), API_URL) for device in devices
            ],
        }

    def read_history(self, start: datetime, end: datetime, *, device_id: int | None = None,
                     circuit_id: int | None = None, include_controls: bool = False) -> dict:
        """Read z3k history, decoding absolute timestamps and forward time deltas."""
        start, end = history_period(start, end)
        if type(include_controls) is not bool:
            raise ValueError("include_controls must be boolean")
        for value in (device_id, circuit_id):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError("device and circuit IDs must be positive integers")
        query = {"fields": "sensors,circuits,modes"}
        if device_id is not None:
            query["device_ids"] = device_id
        data = self._request(Request(f"{API_URL}/devices?{urlencode(query)}", headers=self._headers))
        raw_devices = data.get("devices")
        if not isinstance(raw_devices, list):
            raise ZontError("API response does not contain a device list")
        devices = [_device_snapshot(device) for device in raw_devices]
        if device_id is not None:
            devices = [device for device in devices if device["id"] == device_id]
            if not devices:
                raise ZontError("requested device is not available in this account")
        if circuit_id is not None:
            if len(devices) != 1:
                raise ValueError("choose a single device when filtering by circuit")
            if not any(circuit["id"] == circuit_id for circuit in devices[0]["circuits"]):
                raise ValueError("requested circuit is not available on this device")
        responses = {}
        if devices:
            data_types = ["z3k_temperature", "z3k_heating_circuit"]
            if include_controls:
                data_types.append("z3k_web_element")
            body = {"requests": [
                {"device_id": device["id"], "data_types": data_types,
                 "mintime": int(start.timestamp()), "maxtime": int(end.timestamp())}
                for device in devices
            ]}
            data = self._request(Request(
                "https://my.zont.online/api/load_data", data=json.dumps(body).encode("utf-8"),
                headers={**self._headers, "Content-Type": "application/json"}, method="POST",
            ))
            raw_responses = data.get("responses")
            if not isinstance(raw_responses, list):
                raise ZontError("API response does not contain a history response list")
            for response in raw_responses:
                if not isinstance(response, dict) or type(response.get("device_id")) is not int:
                    raise ZontError("invalid device identity in history response")
                if response.get("ok") is not True:
                    raise ZontError("ZONT rejected a device history request")
                if response["device_id"] in responses:
                    raise ZontError("duplicate device history response")
                responses[response["device_id"]] = response
        if any(device["id"] not in responses for device in devices):
            raise ZontError("missing device history response")
        return {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "requested_period": {"start": start.isoformat(), "end": end.isoformat()},
            "devices": [normalize_device_history(device, responses[device["id"]], start, end, circuit_id)
                        for device in devices],
        }

    def apply_command(self, command: PreparedCommand, *, execute: bool = False) -> dict:
        """Preview without I/O; explicit execution checks inventory then sends once."""
        if not isinstance(command, PreparedCommand) or type(execute) is not bool:
            raise ValueError("a prepared command and boolean execute flag are required")
        preview = command.preview(API_URL)
        if not execute:
            return preview
        inventory = self.list_controls(command.device_id)
        device = next((d for d in inventory["devices"] if d["id"] == command.device_id), None)
        if device is None:
            raise ZontError("requested device is not available in this account")
        if device["online"] is not True:
            raise ZontError("device is offline; command was not sent")
        element = next((e for e in device["elements"]
                        if e["id"] == command.element_id and e["kind"] == command.kind), None)
        if element is None:
            raise ValueError("requested control element is not available on this device")
        validate_element(command, element)
        if not any(a["method"] == "POST" and a["url"] == preview["url"] for a in element["actions"]):
            raise ValueError("command is not advertised by the control element")
        request = Request(
            preview["url"], data=json.dumps(command.body, allow_nan=False).encode("utf-8"),
            headers={**self._headers, "Content-Type": "application/json"}, method="POST",
        )
        try:
            data = self._request(request)
        except ZontError as error:
            # A timeout can occur after the command took effect. Never retry.
            raise ZontError(
                f"command failed or was not confirmed: {error}; read device state before any retry"
            ) from None
        result = {"ok": True, "executed": True, "command": {**preview, "dry_run": False}}
        if isinstance(data.get("device"), dict):
            if data["device"].get("id") != command.device_id:
                raise ZontError("command response refers to another device; read device state before retry")
            result["device"] = _device_snapshot(data["device"])
        return result

    def _load_circuit_setpoints(self, devices: list[dict]) -> None:
        # Widget v3 omits the consumer's calculated coolant setpoint.
        request = Request(
            "https://my.zont.online/api/devices",
            data=b'{"load_io": true}',
            headers={**self._headers, "Content-Type": "application/json"},
            method="POST",
        )
        data = self._request(request)
        raw_devices = data.get("devices")
        if not isinstance(raw_devices, list):
            raise ZontError("full API response does not contain a device list")
        selected_devices = {device["id"]: device for device in devices}
        for raw_device in raw_devices:
            if not isinstance(raw_device, dict) or type(raw_device.get("id")) is not int:
                raise ZontError("invalid device identity in full API response")
            device = selected_devices.get(raw_device["id"])
            if device is None or not device["circuits"]:
                continue
            io = raw_device.get("io", {})
            if not isinstance(io, dict):
                raise ZontError("invalid device state in full API response")
            states = io.get("z3k-state", {})
            if not isinstance(states, dict):
                raise ZontError("invalid heating state in full API response")
            for circuit in device["circuits"]:
                state = states.get(str(circuit["id"]), {})
                if not isinstance(state, dict):
                    raise ZontError("invalid circuit state in full API response")
                value = state.get("setpoint_temp")
                if value is not None and (
                    type(value) not in (int, float) or not math.isfinite(value)
                ):
                    raise ZontError("invalid coolant setpoint in full API response")
                circuit["setpoint_temp"] = value

    def _request(self, request: Request) -> dict:
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            status = error.code
            error.close()
            if status in (401, 403):
                raise ZontError("access denied: check credentials and API access") from None
            if status == 429:
                raise ZontError("API rate limit exceeded; try again later") from None
            raise ZontError(f"ZONT returned HTTP {status}") from None
        except (URLError, TimeoutError, socket.timeout, OSError):
            raise ZontError("could not reach ZONT within the request timeout") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ZontError("API response exceeds the size limit")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ZontError("ZONT returned invalid JSON") from None
        if not isinstance(data, dict) or data.get("ok") is not True:
            # Server error strings may contain secrets. Do not print the raw body.
            raise ZontError("ZONT API rejected the request (ok is not true)")
        return data


def _device_snapshot(device: dict) -> dict:
    if not isinstance(device, dict):
        raise ZontError("invalid device in API response")
    if type(device.get("id")) is not int or not isinstance(device.get("name"), str):
        raise ZontError("invalid device identity in API response")
    if type(device.get("online")) is not bool:
        raise ZontError("invalid device connection state in API response")
    info = device.get("device_info", {})
    sensors = device.get("sensors", [])
    circuits = device.get("circuits", [])
    modes = device.get("modes", [])
    if not isinstance(info, dict) or not isinstance(sensors, list):
        raise ZontError("invalid device metadata or sensor list in API response")
    if not isinstance(circuits, list) or not isinstance(modes, list):
        raise ZontError("invalid heating circuit or mode list in API response")
    mode_snapshots = [_mode_snapshot(mode) for mode in modes]
    mode_names = {mode["id"]: mode["name"] for mode in mode_snapshots}
    return {
        "id": device["id"],
        "name": device["name"],
        "model": info.get("model"),
        "online": device["online"],
        "sensors": [_sensor_snapshot(sensor) for sensor in sensors],
        "circuits": [_circuit_snapshot(circuit, mode_names) for circuit in circuits],
        "modes": mode_snapshots,
    }


def _sensor_snapshot(sensor: dict) -> dict:
    if not isinstance(sensor, dict) or type(sensor.get("id")) is not int:
        raise ZontError("invalid sensor identity in API response")
    if any(not isinstance(sensor.get(key), str) for key in ("name", "type", "status")):
        raise ZontError("invalid sensor metadata in API response")
    value = sensor.get("value")
    if value is not None and (
        type(value) not in (int, float) or not math.isfinite(value)
    ):
        raise ZontError("invalid sensor value in API response")
    triggered = sensor.get("triggered")
    if triggered is not None and type(triggered) is not bool:
        raise ZontError("invalid sensor trigger state in API response")
    unit = sensor.get("unit")
    if unit is not None and not isinstance(unit, str):
        raise ZontError("invalid sensor unit in API response")
    return {
        "id": sensor["id"],
        "name": sensor["name"],
        "type": sensor["type"],
        "status": sensor["status"],
        "value": value,
        "unit": unit,
        "triggered": triggered,
    }


def _mode_snapshot(mode: dict) -> dict:
    if not isinstance(mode, dict) or type(mode.get("id")) is not int:
        raise ZontError("invalid heating mode identity in API response")
    if not isinstance(mode.get("name"), str):
        raise ZontError("invalid heating mode name in API response")
    return {"id": mode["id"], "name": mode["name"]}


def _circuit_snapshot(circuit: dict, mode_names: dict[int, str]) -> dict:
    if not isinstance(circuit, dict) or type(circuit.get("id")) is not int:
        raise ZontError("invalid heating circuit identity in API response")
    if any(not isinstance(circuit.get(key), str) for key in ("name", "type")):
        raise ZontError("invalid heating circuit metadata in API response")
    for key in ("status", "work_mode"):
        if circuit.get(key) is not None and not isinstance(circuit[key], str):
            raise ZontError("invalid heating circuit status in API response")
    for key in ("active", "is_off", "in_summer_mode"):
        if type(circuit.get(key)) is not bool:
            raise ZontError("invalid heating circuit operating state in API response")
    current_mode = circuit.get("current_mode")
    if current_mode is not None and type(current_mode) is not int:
        raise ZontError("invalid heating mode ID in API response")
    temperatures = {}
    for key in ("actual_temp", "target_temp", "min", "max", "step"):
        value = circuit.get(key)
        if value is not None and (
            type(value) not in (int, float) or not math.isfinite(value)
        ):
            raise ZontError("invalid heating circuit temperature in API response")
        temperatures[key] = value
    return {
        "id": circuit["id"],
        "name": circuit["name"],
        "type": circuit["type"],
        "status": circuit.get("status"),
        **temperatures,
        "setpoint_temp": None,
        "unit": "°C",
        "current_mode": current_mode,
        "current_mode_name": mode_names.get(current_mode),
        "active": circuit["active"],
        "is_off": circuit["is_off"],
        "in_summer_mode": circuit["in_summer_mode"],
        "work_mode": circuit.get("work_mode"),
    }
