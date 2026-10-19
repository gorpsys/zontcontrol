"""Shared virtual devices with separate send, application and response phases.

No implicit retries or compare-and-swap. Multiple adapters can share a server;
read/modify/write races remain possible, just as in the assumed ZONT resource.
"""

from copy import deepcopy
from dataclasses import dataclass, replace
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


@dataclass(frozen=True)
class ControlInterval:
    command_id: int
    device_id: int
    owner: str
    started_at: float
    finished_at: float | None = None


class FakeZontServer:
    def __init__(self, devices, *, clock=None):
        self.clock = clock or FakeClock()
        self._devices = deepcopy(devices)
        self._pending = {}
        self._requests = []
        self._lock = RLock()
        self.fail_before_send = False
        self._control_intervals = []
        self._out_of_model_faults = []

    @property
    def out_of_model_faults(self):
        """Fault injection oracle; a late apply violates v2's server assumption."""
        with self._lock:
            return tuple(self._out_of_model_faults)

    def begin_control(self, device_id, owner):
        """Independent resource oracle: record actual effects, not permissions."""
        with self._lock:
            if device_id not in self._devices:
                raise DeviceUnavailable()
            command = ControlInterval(len(self._control_intervals) + 1, device_id,
                                      owner, self.clock.now())
            self._control_intervals.append(command)
            return command

    def finish_control(self, command):
        with self._lock:
            current = self._control_intervals[command.command_id - 1]
            if current is not command or command.finished_at is not None:
                raise AssertionError('oracle command already finished or not owned')
            self._control_intervals[command.command_id - 1] = replace(
                command, finished_at=self.clock.now())

    @property
    def control_conflicts(self):
        """Actual overlapping intervals on the same device, independent of R1/R2."""
        with self._lock:
            now = self.clock.now()
            result = []
            for index, first in enumerate(self._control_intervals):
                for second in self._control_intervals[index + 1:]:
                    end_first = now if first.finished_at is None else first.finished_at
                    end_second = now if second.finished_at is None else second.finished_at
                    if (first.device_id == second.device_id and first.owner != second.owner
                            and max(first.started_at, second.started_at) < min(end_first, end_second)):
                        result.append((first, second))
            return tuple(result)

    @property
    def requests(self):
        with self._lock:
            return tuple(self._requests)

    def read_device(self, device_id):
        with self._lock:
            if device_id not in self._devices:
                raise DeviceUnavailable()
            return deepcopy(self._devices[device_id])

    def send_update(self, device_id, changes, *, timeout_seconds=None):
        with self._lock:
            if self.fail_before_send or device_id not in self._devices:
                raise DeviceUnavailable()
            request = SentRequest(len(self._requests) + 1, device_id, self.clock.now())
            self._requests.append(request)
            self._pending[request.request_id] = {
                "request": request, "changes": deepcopy(changes), "state": "sent", "error": None,
                "deadline": (None if timeout_seconds is None
                             else self.clock.now() + timeout_seconds),
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
            if entry["deadline"] is not None and self.clock.now() > entry["deadline"]:
                self._out_of_model_faults.append((request, "late_apply"))
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
        self.write_error = None
        self._confirmation_pending = False

    def read_metadata_field(self, device_id, field, *, timeout_seconds=None):
        if timeout_seconds == 0:
            raise DeviceUnavailable()
        if timeout_seconds is not None and self.read_delay > timeout_seconds:
            self.server.clock.advance(timeout_seconds)
            raise DeviceUnavailable()
        self.server.clock.advance(self.read_delay)
        if self._confirmation_pending:
            self._confirmation_pending = False
            if self.fail_confirmation:
                raise DeviceUnavailable()
        return self.server.read_device(device_id)[field]

    def update_metadata(self, device_id, field, value, *, timeout_seconds=None):
        if timeout_seconds == 0:
            raise DeviceUnavailable()
        self.writes.append((device_id, field, value))
        normalized = self.normalize.get(field, lambda raw: raw)(value)
        request = self.server.send_update(device_id, {field: normalized},
                                          timeout_seconds=timeout_seconds)
        if timeout_seconds is not None and self.write_delay > timeout_seconds:
            self.server.clock.advance(timeout_seconds)
            self.server.respond(request, lose_response=True)
        self.server.clock.advance(self.write_delay)
        if self.write_error is not None:
            self.server.reject(request, self.write_error)
            self.server.respond(request)
        if self.write_mode == "defer":
            self.server.respond(request, lose_response=True)
        self.server.apply(request)
        self.server.respond(request, lose_response=self.write_mode == "lose_response")
        self._confirmation_pending = True
