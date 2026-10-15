"""Shared virtual devices with separate send, application and response phases.

No implicit retries or compare-and-swap. Multiple adapters can share a server;
read/modify/write races remain possible, just as in the assumed ZONT resource.
"""

from copy import deepcopy
from dataclasses import dataclass
from threading import RLock

from zontcontrol.controller.contracts import (
    CommandOutcomeUnknown, ControllerError, DeviceUnavailable,
)

from .clock import FakeClock


@dataclass(frozen=True)
class SentRequest:
    request_id: int
    device_id: int
    sent_at: float


class FakeZontServer:
    def __init__(self, devices, *, clock=None):
        self.clock = clock or FakeClock()
        self._devices = deepcopy(devices)
        self._pending = {}
        self._requests = []
        self._lock = RLock()
        self.fail_before_send = False

    @property
    def requests(self):
        with self._lock:
            return tuple(self._requests)

    def read_device(self, device_id):
        with self._lock:
            if device_id not in self._devices:
                raise DeviceUnavailable()
            return deepcopy(self._devices[device_id])

    def send_update(self, device_id, changes):
        with self._lock:
            if self.fail_before_send or device_id not in self._devices:
                raise DeviceUnavailable()
            request = SentRequest(len(self._requests) + 1, device_id, self.clock.now())
            self._requests.append(request)
            self._pending[request.request_id] = {
                "request": request, "changes": deepcopy(changes), "state": "sent", "error": None,
            }
            return request

    def _entry(self, request):
        entry = self._pending.get(request.request_id)
        if entry is None or entry["request"] is not request:
            raise AssertionError("request does not belong to this virtual server")
        return entry

    def apply(self, request):
        with self._lock:
            entry = self._entry(request)
            if entry["state"] != "sent":
                raise AssertionError("request already resolved")
            self._devices[request.device_id].update(deepcopy(entry["changes"]))
            entry["state"] = "applied"

    def reject(self, request, error):
        with self._lock:
            entry = self._entry(request)
            if entry["state"] != "sent" or not isinstance(error, ControllerError):
                raise AssertionError("rejection needs an unresolved request and a safe error")
            entry.update(state="rejected", error=error)

    def respond(self, request, *, lose_response=False):
        with self._lock:
            entry = self._entry(request)
            if lose_response:
                raise CommandOutcomeUnknown()
            if entry["state"] == "sent":
                raise AssertionError("explicitly apply or reject the request before replying")
            if entry["error"] is not None:
                raise entry["error"]
            return True


class FakeMetadataAdapter:
    """Metadata client with explicit delayed application and failed readback."""

    def __init__(self, server):
        self.server = server
        self.writes = []
        self.normalize = {}
        self.write_mode = "apply"
        self.fail_confirmation = False
        self.read_delay = 0
        self.write_delay = 0
        self._confirmation_pending = False

    def read_metadata_field(self, device_id, field):
        self.server.clock.advance(self.read_delay)
        if self._confirmation_pending:
            self._confirmation_pending = False
            if self.fail_confirmation:
                raise DeviceUnavailable()
        return self.server.read_device(device_id)[field]

    def update_metadata(self, device_id, field, value):
        self.writes.append((device_id, field, value))
        normalized = self.normalize.get(field, lambda raw: raw)(value)
        request = self.server.send_update(device_id, {field: normalized})
        self.server.clock.advance(self.write_delay)
        if self.write_mode == "defer":
            self.server.respond(request, lose_response=True)
        self.server.apply(request)
        self.server.respond(request, lose_response=self.write_mode == "lose_response")
        self._confirmation_pending = True
