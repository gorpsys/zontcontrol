"""Local asynchronous acquisition; all state shares the executor Condition.

The callbacks own reservations and coordination I/O. This module owns only
request outcomes, per-device FIFO scheduling and the lifetime of its workers.
"""

import math
from collections import deque
from dataclasses import dataclass
from threading import Event, Thread, current_thread
from uuid import uuid4
from weakref import WeakKeyDictionary, WeakSet

from .contracts import (
    CommandOutcomeUnknown, ControlRequest, ControlRequestErrorCode,
    ControlRequestReason as Reason, ControlRequestResult, ControlRequestState as State,
    DeviceControlBusy, DeviceUnavailable, InvalidArguments, RateLimited,
)


@dataclass
class RequestState:
    binding: object
    handle: ControlRequest
    lease_seconds: float
    timeout: float
    poll_seconds: float
    cancelled: Event | None
    created_at: float
    wait_deadline: float
    lease_deadline: float
    next_poll: float
    result: ControlRequestResult
    in_flight: bool = False


class ControlRequestManager:
    def __init__(self, *, idle, clock, admission, attempt, decision, cleanup, abandon,
                 uuid_factory=uuid4):
        self._idle = idle
        self._clock = clock
        self._admission = admission
        self._attempt = attempt
        self._decision = decision
        self._cleanup = cleanup
        self._abandon = abandon
        self._uuid = uuid_factory
        self._closed = False
        self._active = {}
        self._terminal = WeakKeyDictionary()
        self._queues = {}
        self._workers = {}
        self._threads = WeakSet()

    @staticmethod
    def _arguments(device_id, lease_seconds, timeout, poll_seconds, cancelled):
        if type(device_id) is not int or device_id <= 0:
            raise InvalidArguments()
        for value in (lease_seconds, timeout, poll_seconds):
            try:
                valid = type(value) in (int, float) and math.isfinite(value) and value > 0
            except OverflowError:
                valid = False
            if not valid:
                raise InvalidArguments()
        if timeout > lease_seconds or cancelled is not None and not isinstance(cancelled, Event):
            raise InvalidArguments()

    def request(self, binding, device_id, *, lease_seconds, timeout, poll_seconds=1,
                cancelled=None):
        self._arguments(device_id, lease_seconds, timeout, poll_seconds, cancelled)
        with self._idle:
            now = self._clock()
            self._admission(binding, device_id, now + lease_seconds)
            for state in self._queues.get(device_id, ()):
                if state.binding is binding and state.result.state is State.REQUESTED:
                    if ((state.lease_seconds, state.timeout, state.poll_seconds)
                            != (lease_seconds, timeout, poll_seconds)
                            or state.cancelled is not cancelled):
                        raise InvalidArguments()
                    return state.handle
            handle = ControlRequest(self._uuid(), device_id)
            state = RequestState(binding, handle, lease_seconds, timeout, poll_seconds, cancelled,
                                 now, now + timeout, now + lease_seconds, now,
                                 ControlRequestResult(handle.request_id, device_id, State.REQUESTED))
            self._active[handle] = state
            self._queues.setdefault(device_id, deque()).append(state)
            if device_id not in self._workers:
                worker = Thread(target=self._run, args=(device_id,),
                                name=f'control-request-{device_id}')
                self._workers[device_id] = worker
                self._threads.add(worker)
                try:
                    worker.start()
                except BaseException as error:
                    self._threads.discard(worker)
                    del self._workers[device_id]
                    self._deny_locked(state, Reason.CONTROLLER_ERROR,
                                      ControlRequestErrorCode.from_error(error))
                    self._forget_locked(state)
            self._idle.notify_all()
            return handle

    def _lookup_locked(self, binding, handle):
        if type(handle) is not ControlRequest:
            raise InvalidArguments()
        state = self._active.get(handle)
        if state is not None:
            if state.binding is not binding:
                raise InvalidArguments()
            return state.result
        terminal = self._terminal.get(handle)
        if terminal is None or terminal[0] is not binding:
            raise InvalidArguments()
        return terminal[1]

    def get(self, binding, handle):
        with self._idle:
            self._lookup_locked(binding, handle)
            state = self._active.get(handle)
            if state is not None:
                self._check_locked(state)
            return self._lookup_locked(binding, handle)

    def cancel(self, binding, handle):
        with self._idle:
            self._lookup_locked(binding, handle)
            state = self._active.get(handle)
            if state is not None and state.result.state is State.REQUESTED:
                self._check_locked(state)
                if state.result.state is State.REQUESTED:
                    self._deny_locked(state, Reason.CANCELLED)
            return self._lookup_locked(binding, handle)

    def acquire(self, binding, device_id, *, lease_seconds, timeout, poll_seconds=1,
                cancelled=None):
        handle = self.request(binding, device_id, lease_seconds=lease_seconds, timeout=timeout,
                              poll_seconds=poll_seconds, cancelled=cancelled)
        with self._idle:
            while True:
                state = self._active.get(handle)
                if state is not None:
                    # The device worker may be blocked in gate I/O. The caller
                    # can still settle this request, under the same state lock.
                    self._check_locked(state)
                result = self._lookup_locked(binding, handle)
                if result.state is not State.REQUESTED:
                    return result
                now = self._clock()
                self._idle.wait(min(.1, state.wait_deadline - now, state.lease_deadline - now))

    def busy_locked(self, binding, device_id, request_id=None):
        return any(state.binding is binding and state.handle.request_id != request_id
                   and state.result.state is not State.GRANTED
                   for state in self._queues.get(device_id, ()))

    def live_locked(self, binding, device_id, request_id):
        return any(state.binding is binding and state.handle.request_id == request_id
                   and state.result.state is State.REQUESTED
                   for state in self._queues.get(device_id, ()))

    def _finish_locked(self, state, result):
        state.result = result
        # Values retain only binding and UUID-bearing DTO, never their weak key.
        self._terminal[state.handle] = (state.binding, result)
        self._idle.notify_all()

    def _deny_locked(self, state, reason, code=None):
        if state.result.state is not State.REQUESTED:
            return
        # Protect the first cleanup attempt from expiry/drain before unlocking.
        self._decision(state.binding, state.handle.request_id, None,
                       state.lease_deadline, grant=False)
        self._finish_locked(state, ControlRequestResult(
            state.handle.request_id, state.handle.device_id, State.DENIED,
            reason=reason, error_code=code))

    def _check_locked(self, state):
        if state.result.state is not State.REQUESTED:
            return
        now = self._clock()
        reason = (Reason.SHUTDOWN if self._closed else
                  Reason.CANCELLED if state.cancelled is not None and state.cancelled.is_set() else
                  Reason.LEASE_EXPIRED if now >= state.lease_deadline else
                  Reason.TIMEOUT if now >= state.wait_deadline else None)
        if reason is not None:
            self._deny_locked(state, reason)

    def begin_close_locked(self):
        self._closed = True
        for state in self._active.values():
            self._check_locked(state)
        self._idle.notify_all()

    def _forget_locked(self, state):
        del self._active[state.handle]
        queue = self._queues[state.handle.device_id]
        queue.remove(state)
        if not queue:
            del self._queues[state.handle.device_id]
        self._idle.notify_all()

    def _retire_locked(self, device_id, worker):
        """Remove only this registration, atomically with the empty-queue check."""
        if self._queues.get(device_id):
            return False
        if self._workers.get(device_id) is worker:
            del self._workers[device_id]
        self._idle.notify_all()
        return True

    def _cleanup_state(self, state):
        if state.result.state is State.DENIED:
            try:
                # The attempt may have saved a session before raising, without
                # ever returning its handle. Find it by binding/request identity.
                self._cleanup(state.binding, state.handle.request_id)
            except BaseException:
                # A failed/unknown release is not retried. Ownership retains
                # that reservation through its original expiry.
                pass
            finally:
                try:
                    self._abandon(state.binding, state.handle.device_id, state.handle.request_id)
                except BaseException:
                    pass

    def _attempt_state(self, state):
        session = None
        code = None
        try:
            session = self._attempt(
                state.binding, state.handle.device_id, state.lease_seconds,
                request_id=state.handle.request_id, local_deadline=state.lease_deadline,
                attempt_deadline=min(state.wait_deadline, state.lease_deadline))
        except (DeviceControlBusy, CommandOutcomeUnknown, DeviceUnavailable, RateLimited):
            pass
        except BaseException as error:
            code = ControlRequestErrorCode.from_error(error)
        finally:
            with self._idle:
                state.in_flight = False
                self._check_locked(state)
                if state.result.state is State.REQUESTED:
                    if code is not None:
                        self._deny_locked(state, Reason.CONTROLLER_ERROR, code)
                    elif session is not None:
                        if self._clock() >= session.expires_at:
                            self._deny_locked(state, Reason.LEASE_EXPIRED)
                        else:
                            try:
                                granted = self._decision(
                                    state.binding, state.handle.request_id, session,
                                    state.lease_deadline, grant=True)
                            except BaseException as error:
                                self._deny_locked(state, Reason.CONTROLLER_ERROR,
                                                  ControlRequestErrorCode.from_error(error))
                            else:
                                if granted:
                                    self._finish_locked(state, ControlRequestResult(
                                        state.handle.request_id, state.handle.device_id,
                                        State.GRANTED, session=session))
                    if state.result.state is State.REQUESTED:
                        # Busy/stale reuse is only retried at the requested poll.
                        state.next_poll = self._clock() + state.poll_seconds
                self._idle.notify_all()

    def _run(self, device_id):
        worker = current_thread()
        while True:
            with self._idle:
                queue = self._queues.get(device_id, ())
                for queued in queue:
                    self._check_locked(queued)
                if self._retire_locked(device_id, worker):
                    return
                state = self._queues[device_id][0]
                terminal = state.result.state is not State.REQUESTED
                if not terminal:
                    now = self._clock()
                    if now < state.next_poll:
                        self._idle.wait(min(.1, state.next_poll - now,
                                            state.wait_deadline - now,
                                            state.lease_deadline - now))
                        state = None
                        continue
                    state.in_flight = True
            if not terminal:
                self._attempt_state(state)
            if state.result.state is not State.REQUESTED:
                self._cleanup_state(state)
                with self._idle:
                    self._forget_locked(state)
            # Do not retain the previous handle in a live/idle worker frame.
            state = None
            queued = None
            queue = None

    def thread_snapshot_locked(self):
        return tuple(self._threads)
