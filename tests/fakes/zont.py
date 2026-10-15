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
