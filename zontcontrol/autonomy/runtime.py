"""One trusted algorithm at a time, in threads of the application's process.

This is a lifecycle primitive, not a catalog/registrar. Bootstrap and module
work run only in the worker. A separate once supervisor publishes the result
timeout without waiting for HTTP or finally. Cancellation never reaches the
controller; the module executes its cleanup through the same direct API. No thread can be forcibly stopped here.
"""

import math
from threading import Event, Lock, Thread, TIMEOUT_MAX
from time import monotonic
from uuid import uuid4

from .contracts import UUIDFactory
from .contracts import (
    AlgorithmBusy, AlgorithmCancelled, AlgorithmFailed, AlgorithmResult,
    AlgorithmSpec, AlgorithmState, AlgorithmTimeout, Mode, RunContext,
)


def _seconds(value, *, allow_zero=False):
    try:
        valid = (type(value) in (int, float) and math.isfinite(value)
                 and (value >= 0 if allow_zero else value > 0))
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError('Expected finite seconds')


class RunHandle:
    """A result can be final while its noncooperative worker is still stopping.

    result(wait_timeout) only bounds the caller's wait; it does not cancel work
    or replace the supervisor's once deadline. failure records a later cleanup
    failure even if the once result already contains Timeout/Cancelled.
    """

    def __init__(self, runtime, mode, context):
        self._runtime = runtime
        self.mode = mode
        self.context = context
        self.worker = None
        self.supervisor = None
        self._ready = Event()
        self._value = None
        self._error = None
        self._failure = None
        self._finished = False

    @property
    def failure(self):
        with self._runtime._lock:
            return self._failure

    def result(self, wait_timeout=None):
        if wait_timeout is not None:
            _seconds(wait_timeout, allow_zero=True)
        if not self._ready.wait(wait_timeout):
            raise TimeoutError('Result wait expired; algorithm is still running')
        with self._runtime._lock:
            value, error = self._value, self._error
        if error is not None:
            raise error
        return value

    def _publish(self, *, value=None, error=None):
        # Caller holds runtime lock. First result wins, including Timeout.
        if not self._ready.is_set():
            self._value, self._error = value, error
            self._ready.set()


class AlgorithmRuntime:
    """One spec/configuration slot, shared with the future registrar via DI.

    controller_factory supplies the controller in the worker, inside the
    once budget. close_controller is an optional owner-supplied shutdown hook;
    a shared facade should instead be closed by its composition root after all
    runtimes have closed. close returns False while cleanup is still running.
    Workers are non-daemon: a stuck module may prevent process termination.
    """

    def __init__(self, spec: AlgorithmSpec, controller_factory, *, clock=monotonic,
                 close_controller=None, uuid_factory: UUIDFactory | None = None):
        if (not isinstance(spec, AlgorithmSpec) or not callable(controller_factory)
                or not callable(clock)
                or close_controller is not None and not callable(close_controller)):
            raise ValueError('Invalid runtime dependencies')
        self.spec = spec
        self._controller_factory = controller_factory
        self._clock = clock
        self._uuid_factory = uuid4 if uuid_factory is None else uuid_factory
        self._close_controller = close_controller
        self._lock = Lock()
        self._state = AlgorithmState.REGISTERED
        self._active = None
        self._closed = False
        self._close_claimed = False
        self._resources_closed = Event()
        self._close_failure = None

    @property
    def state(self):
        with self._lock:
            return self._state

    def start(self, mode: Mode, *, timeout=None) -> RunHandle:
        if not isinstance(mode, Mode) or mode not in self.spec.modes:
            raise ValueError('Unsupported algorithm mode')
        if mode is Mode.ONCE:
            _seconds(timeout)
            if timeout < self.spec.minimum_seconds:
                raise ValueError('Once timeout is below algorithm minimum')
        elif timeout is not None:
            raise ValueError('Continuous has no registrar timeout')
        with self._lock:
            if self._closed:
                raise AlgorithmBusy(self.spec.algorithm_id, 'closed')
            previous = self._active
            if previous is not None and previous.worker.is_alive():
                if self._state is AlgorithmState.RUNNING_CONTINUOUS:
                    if mode is Mode.CONTINUOUS:
                        return previous
                    raise AlgorithmBusy(self.spec.algorithm_id, 'already_running_continuous')
                reason = ('stopping' if self._state is not AlgorithmState.RUNNING_ONCE
                          else 'already_running_once')
                raise AlgorithmBusy(self.spec.algorithm_id, reason)
            # Admission starts the budget before thread creation or any DI.
            deadline = self._clock() + timeout if mode is Mode.ONCE else None
            context = RunContext(self.spec.algorithm_id, self._uuid_factory(), self._uuid_factory(),
                                 Event(), deadline)
            handle = RunHandle(self, mode, context)
            handle.worker = Thread(target=self._work, args=(handle,),
                                   name=f'{self.spec.algorithm_id}-worker', daemon=False)
            if mode is Mode.ONCE:
                handle.supervisor = Thread(target=self._supervise, args=(handle,),
                                           name=f'{self.spec.algorithm_id}-supervisor', daemon=True)
            self._active = handle
            self._state = (AlgorithmState.RUNNING_ONCE if mode is Mode.ONCE
                           else AlgorithmState.RUNNING_CONTINUOUS)
            try:
                if handle.supervisor is not None:
                    handle.supervisor.start()
                handle.worker.start()
            except RuntimeError:
                self._state = AlgorithmState.FAILED
                handle.context.cancelled.set()
                failure = AlgorithmFailed(self.spec.algorithm_id)
                handle._failure = failure
                handle._publish(error=failure)
                raise failure from None
            return handle

    def run_once(self, timeout) -> AlgorithmResult:
        return self.start(Mode.ONCE, timeout=timeout).result()

    def _expire(self, handle):
        # Caller holds lock. No module/transport call ever runs on the timer.
        if not handle._ready.is_set():
            self._state = AlgorithmState.STOPPING
            handle.context.cancelled.set()
            handle._publish(error=AlgorithmTimeout(self.spec.algorithm_id))

    def _supervise(self, handle):
        while not handle._ready.is_set():
            remaining = handle.context.deadline - self._clock()
            if remaining <= 0:
                with self._lock:
                    self._expire(handle)
                return
            # Finite configured budgets may exceed the platform lock limit.
            handle._ready.wait(min(remaining, TIMEOUT_MAX))

    def _work(self, handle):
        value, failure = None, None
        try:
            if not handle.context.cancelled.is_set():
                controller = self._controller_factory()
                if not handle.context.cancelled.is_set():
                    module = self.spec.factory(controller)
                    if not handle.context.cancelled.is_set():
                        if handle.mode is Mode.ONCE:
                            value = module.run_once(handle.context)
                            if not isinstance(value, AlgorithmResult):
                                raise ValueError('Expected AlgorithmResult')
                        else:
                            module.run_continuous(handle.context)
        except BaseException:
            # Keep safe failure independent of the result already returned.
            failure = AlgorithmFailed(self.spec.algorithm_id)
        finally:
            with self._lock:
                if (handle.context.deadline is not None
                        and self._clock() >= handle.context.deadline):
                    self._expire(handle)
                handle._failure = failure
                handle._publish(value=value, error=failure)
                handle._finished = True
                self._state = AlgorithmState.FAILED if failure else AlgorithmState.STOPPED
                closed = self._closed
            if closed:
                self._close_resources()

    def stop(self, *, wait_timeout=0) -> bool:
        with self._lock:
            handle = self._active
            if handle is not None and handle.worker.is_alive() and not handle._finished:
                self._state = AlgorithmState.STOPPING
                handle.context.cancelled.set()
                if handle.mode is Mode.ONCE:
                    if self._clock() >= handle.context.deadline:
                        self._expire(handle)
                    else:
                        handle._publish(error=AlgorithmCancelled(self.spec.algorithm_id))
        return self.wait(wait_timeout)

    def wait(self, wait_timeout=None) -> bool:
        if wait_timeout is not None:
            _seconds(wait_timeout, allow_zero=True)
        with self._lock:
            handle = self._active
        if handle is None or handle.worker.ident is None:
            return True
        handle.worker.join(wait_timeout)
        return not handle.worker.is_alive()

    def _close_resources(self):
        with self._lock:
            if self._close_claimed:
                return
            self._close_claimed = True
        try:
            if self._close_controller is not None:
                self._close_controller()
        except BaseException:
            with self._lock:
                self._close_failure = AlgorithmFailed(self.spec.algorithm_id)
                self._state = AlgorithmState.FAILED
        finally:
            self._resources_closed.set()

    def close(self, *, wait_timeout=None) -> bool:
        if wait_timeout is not None:
            _seconds(wait_timeout, allow_zero=True)
        deadline = None if wait_timeout is None else monotonic() + wait_timeout
        with self._lock:
            self._closed = True
        if not self.stop(wait_timeout=wait_timeout):
            return False
        self._close_resources()
        remaining = None if deadline is None else max(0, deadline - monotonic())
        if not self._resources_closed.wait(remaining):
            return False
        if self._close_failure is not None:
            raise self._close_failure
        return True
