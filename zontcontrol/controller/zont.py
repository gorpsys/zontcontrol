"""Internal ZONT operations; no consumer facade or ownership policy here.

Methods return raw API data for the normalization layer. POST reads are
explicitly distinguished from effects when translating transport failures.
"""

import json
from collections.abc import Callable
from datetime import datetime, timezone
import logging
import re
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request

from ..commands import PreparedCommand
from ..history import history_period
from .contracts import (
    AuthenticationError, CommandOutcomeUnknown, DeviceUnavailable, InvalidArguments, RateLimited,
)
from .transport import API_URL, FULL_API_URL, FailureKind, HttpTransport, TransportError


logger = logging.getLogger(__name__)
_CONNECTION_SUFFIX = re.compile(
    r' \[zontcontrol connected \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00\]$')


def raise_controller_error(error: TransportError, *, write: bool):
    """Translate the structured outcome, never an exception's private text."""
    kind = error.kind
    if kind is FailureKind.AUTHENTICATION:
        raise AuthenticationError() from None
    if kind is FailureKind.RATE_LIMITED:
        raise RateLimited() from None
    if kind is FailureKind.NOT_SENT:
        raise DeviceUnavailable() from None
    if write and kind in (FailureKind.UNCONFIRMED, FailureKind.INVALID_RESPONSE):
        raise CommandOutcomeUnknown() from None
    if kind is FailureKind.UNCONFIRMED:
        raise DeviceUnavailable() from None
    raise InvalidArguments() from None


def _device_id(value):
    if type(value) is not int or value <= 0:
        raise InvalidArguments()


def _devices(data, device_id=None):
    devices = data.get("devices")
    if not isinstance(devices, list) or any(
        not isinstance(device, dict) or type(device.get("id")) is not int or device["id"] <= 0
        for device in devices
    ):
        raise InvalidArguments()
    if len({device["id"] for device in devices}) != len(devices):
        raise InvalidArguments()
    if device_id is not None:
        devices = [device for device in devices if device["id"] == device_id]
        if not devices:
            raise DeviceUnavailable()
    return devices


class ZontAdapter:
    def __init__(self, transport: HttpTransport, headers: dict[str, str], *,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._transport = transport
        self._headers = dict(headers)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._connection_attempted: set[int] = set()

    def _mark_connection(self, device: dict[str, Any]) -> None:
        """One best-effort metadata update per selected device and client lifetime."""
        device_id = device['id']
        if device_id in self._connection_attempted:
            return
        # Record before I/O: an ambiguous write must never be repeated.
        self._connection_attempted.add(device_id)
        try:
            name = device['name']
            if not isinstance(name, str):
                raise InvalidArguments()
            connected_at = self._clock()
            if connected_at.tzinfo is None or connected_at.utcoffset() is None:
                raise InvalidArguments()
            timestamp = connected_at.astimezone(timezone.utc).isoformat(timespec='seconds')
            name = _CONNECTION_SUFFIX.sub('', name) + f' [zontcontrol connected {timestamp}]'
            self._request(f'{FULL_API_URL}/update_device',
                          body={'device_id': device_id, 'name': name}, write=True)
            event, message, level = ('client_connection_marked', 'Client connection marked', logging.INFO)
        except Exception:
            event, message, level = ('client_connection_marker_failed',
                                      'Client connection marker unavailable', logging.WARNING)
        try:
            logger.log(level, message, extra={'event': event, 'device_id': device_id})
        except Exception:
            pass

    def _request(self, url, *, body=None, write=False):
        try:
            payload = None if body is None else json.dumps(body, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, UnicodeError):
            raise InvalidArguments() from None
        headers = self._headers if body is None else {**self._headers, "Content-Type": "application/json"}
        request = Request(url, data=payload, headers=headers, method="GET" if body is None else "POST")
        try:
            return self._transport.request(request)
        except TransportError as error:
            raise_controller_error(error, write=write)

    def devices(self, device_id: int | None = None) -> list[dict[str, Any]]:
        query = {}
        if device_id is not None:
            _device_id(device_id)
            query["device_ids"] = device_id
        suffix = "?" + urlencode(query) if query else ""
        devices = _devices(self._request(f"{API_URL}/devices{suffix}"), device_id)
        if device_id is not None:
            self._mark_connection(devices[0])
        return devices

    def full_device(self, device_id):
        _device_id(device_id)
        data = self._request(f"{FULL_API_URL}/devices", body={"load_io": True})
        return _devices(data, device_id)[0]

    def send_command(self, command: PreparedCommand):
        """Send one command already validated by the controller."""
        if not isinstance(command, PreparedCommand):
            raise InvalidArguments()
        data = self._request(command.preview(API_URL)["url"], body=command.body, write=True)
        if "device" in data:
            device = data["device"]
            if (not isinstance(device, dict) or type(device.get("id")) is not int
                    or device["id"] != command.device_id):
                raise CommandOutcomeUnknown()
        return data

    def load_history(self, device_id, start, end, *, include_controls=False):
        _device_id(device_id)
        if type(include_controls) is not bool:
            raise InvalidArguments()
        try:
            start, end = history_period(start, end, preserve_precision=True)
        except (ValueError, TypeError, AttributeError):
            raise InvalidArguments() from None
        data_types = ["z3k_temperature", "z3k_heating_circuit"]
        if include_controls:
            data_types.append("z3k_web_element")
        data = self._request(f"{FULL_API_URL}/load_data", body={"requests": [{
            "device_id": device_id, "data_types": data_types,
            # Queries use whole seconds and enclose the requested interval;
            # the facade filters actual points to its exact original bounds.
            "mintime": int(start.timestamp()),
            "maxtime": int(end.timestamp()) + bool(end.microsecond),
        }]})
        responses = data.get("responses")
        if (not isinstance(responses, list) or len(responses) != 1
                or not isinstance(responses[0], dict)
                or type(responses[0].get("device_id")) is not int
                or responses[0]["device_id"] != device_id or responses[0].get("ok") is not True):
            raise InvalidArguments()
        return responses[0]
