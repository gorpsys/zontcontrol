"""Catalog registration and lifecycle; modules receive only constructor DI.

Registration constructs and validates each enabled module without running it.
That construction-only probe is discarded: every accepted launch constructs a
fresh module in its runtime worker, with all launch DI/bootstrap inside the once
budget. Factories own config loading and validation; they must not perform
control operations or start background work in their constructors.
"""

from dataclasses import dataclass, replace
from threading import Event, Lock, Thread
from time import monotonic

from .contracts import UUIDFactory
from ..config import ConfigurationError, RegistryConfig
from .contracts import (
    CAPABILITIES, AlgorithmBusy, AlgorithmError, AlgorithmFailed,
    AlgorithmRegistration, AlgorithmSpec, AlgorithmState, Mode, validate_catalog,
)
from .runtime import AlgorithmRuntime, _seconds


@dataclass
class _Slot:
    spec: AlgorithmSpec
    runtime: AlgorithmRuntime | None = None
    error: Exception | None = None
    blocked: bool = False
    ready: bool = False


class AlgorithmRegistry:
    """One application catalog; the composition root owns the shared facade.

    controller_factory supplies the shared controller to each module.
    close_controller closes resources only after every worker and finally.
    close(wait_timeout) bounds the caller's wait, never the workers' lifetime.
    """

    def __init__(self, config, controller_factory, *, available_capabilities=CAPABILITIES,
                 clock=monotonic, close_controller=None,
                 uuid_factory: UUIDFactory | None = None):
        if (not isinstance(config, RegistryConfig) or not callable(controller_factory)
                or not callable(clock) or not isinstance(available_capabilities, frozenset)
                or not available_capabilities <= CAPABILITIES
                or close_controller is not None and not callable(close_controller)):
            raise ValueError('Invalid registry dependencies')
        self._config = config
        self._controller_factory = controller_factory
        self._capabilities = available_capabilities
        self._clock = clock
        self._uuid_factory = uuid_factory
        self._close_controller = close_controller
        self._lock = Lock()
        self._slots = {}
        self._registered = False
        self._registration_done = Event()
        self._closed = False
        self._close_started = False
        self._shutdown = None
        self._shutdown_done = Event()
        self._close_failed = False

    @staticmethod
    def _factory(spec):
        def construct(controller):
            module = spec.factory(controller)
            if any(not callable(getattr(module, f'run_{mode.value}', None)) for mode in spec.modes):
                raise AlgorithmFailed(spec.algorithm_id)
            return module
        return construct

    def register_all(self, catalog):
        catalog = tuple(catalog)
        enabled = validate_catalog(catalog, self._config)
        with self._lock:
            if self._registered:
                raise ValueError('Catalog is already registered')
            if self._closed:
                raise AlgorithmBusy('registry', 'closed')
            self._registered = True
            self._slots = {spec.algorithm_id: _Slot(spec) for spec in enabled}
        try:
            for slot in self._slots.values():
                try:
                    # Pure per-spec checks precede DI, module config and workers.
                    slot.spec.validate_requirements(self._capabilities)
                    self._config.timeout_for(slot.spec)
                except ValueError as error:
                    with self._lock:
                        slot.error, slot.blocked, slot.ready = error, True, True
                    continue
                construct = self._factory(slot.spec)
                slot.runtime = AlgorithmRuntime(replace(slot.spec, factory=construct),
                                                self._controller_factory, clock=self._clock,
                                                uuid_factory=self._uuid_factory)
                try:
                    construct(self._controller_factory())
                except Exception as error:
                    # ConfigurationError is the safe, module-owned diagnostic.
                    with self._lock:
                        slot.error = (error if isinstance(error, (ConfigurationError, AlgorithmError))
                                      else AlgorithmFailed(slot.spec.algorithm_id))
                        slot.ready = True
                    continue
                with self._lock:
                    slot.ready = True
                if self._config.should_autostart(slot.spec):
                    try:
                        self.start(slot.spec.algorithm_id)
                    except AlgorithmError as error:
                        slot.error = error
            rows = {row.spec.algorithm_id: row for row in self.list_algorithms()}
            return tuple(rows.get(spec.algorithm_id,
                                  AlgorithmRegistration(spec, None, disabled=True))
                         for spec in catalog)
        finally:
            self._registration_done.set()

    def list_algorithms(self, mode=None):
        if mode is not None and not isinstance(mode, Mode):
            raise ValueError('Expected Mode')
        with self._lock:
            return tuple(self._snapshot(slot)
                for slot in self._slots.values() if mode is None or mode in slot.spec.modes)

    @staticmethod
    def _snapshot(slot):
        state = slot.runtime.state if slot.runtime is not None else AlgorithmState.REGISTERED
        if slot.error:
            state = AlgorithmState.FAILED
        error = slot.error or (AlgorithmFailed(slot.spec.algorithm_id)
                               if state is AlgorithmState.FAILED else None)
        return AlgorithmRegistration(slot.spec, state, error)

    def list_continuous(self):
        return self.list_algorithms(Mode.CONTINUOUS)

    def _launch(self, algorithm_id, mode, timeout=None):
        with self._lock:
            if self._closed:
                raise AlgorithmBusy(algorithm_id, 'closed')
            slot = self._slots[algorithm_id]
            if mode not in slot.spec.modes:
                raise ValueError('Unsupported algorithm mode')
            if slot.blocked:
                raise slot.error
            if not slot.ready:
                raise AlgorithmBusy(algorithm_id, 'registering')
            if mode is Mode.ONCE:
                timeout = self._config.timeout_for(slot.spec).resolve(timeout)
            handle = slot.runtime.start(mode, timeout=timeout)
            slot.error = None
            return handle

    def run_once(self, algorithm_id, timeout=None):
        return self._launch(algorithm_id, Mode.ONCE, timeout).result()

    def run_all_once(self, timeouts=None):
        timeouts = {} if timeouts is None else dict(timeouts)
        rows = self.list_algorithms(Mode.ONCE)
        if timeouts.keys() - {row.spec.algorithm_id for row in rows}:
            raise KeyError('Unknown once algorithm ID')
        outcomes, handles = {}, {}
        for row in rows:
            name = row.spec.algorithm_id
            try:
                handles[name] = self._launch(name, Mode.ONCE, timeouts.get(name))
            except (AlgorithmError, ValueError) as error:
                outcomes[name] = error
        # Admit all independent workers before waiting for any result.
        for name, handle in handles.items():
            try:
                outcomes[name] = handle.result()
            except AlgorithmError as error:
                outcomes[name] = error
        return {row.spec.algorithm_id: outcomes[row.spec.algorithm_id] for row in rows}

    def start(self, algorithm_id):
        return self._launch(algorithm_id, Mode.CONTINUOUS)

    def stop(self, algorithm_id, *, wait_timeout=0):
        if wait_timeout is not None:
            _seconds(wait_timeout, allow_zero=True)
        with self._lock:
            slot = self._slots[algorithm_id]
            runtime = slot.runtime
        return True if runtime is None else runtime.stop(wait_timeout=wait_timeout)

    def _finish_close(self):
        self._registration_done.wait()
        try:
            runtimes = [slot.runtime for slot in self._slots.values() if slot.runtime is not None]
            # Signal everyone before joining anyone, so finally can cooperate.
            for runtime in runtimes:
                runtime.close(wait_timeout=0)
            for runtime in runtimes:
                runtime.close()
            if self._close_controller is not None:
                self._close_controller()
        except BaseException:
            self._close_failed = True
        finally:
            self._shutdown_done.set()

    def close(self, *, wait_timeout=None):
        if wait_timeout is not None:
            _seconds(wait_timeout, allow_zero=True)
        with self._lock:
            self._closed = True
            if not self._registered:
                self._registration_done.set()
            if not self._close_started:
                self._close_started = True
                self._shutdown = Thread(target=self._finish_close,
                                        name='registry-close', daemon=False)
                self._shutdown.start()
            runtimes = [slot.runtime for slot in self._slots.values() if slot.runtime is not None]
        # Publish cancellation before returning, even with a zero wait timeout.
        for runtime in runtimes:
            runtime.close(wait_timeout=0)
        if not self._shutdown_done.wait(wait_timeout):
            return False
        if self._close_failed:
            raise RuntimeError('Shared controller shutdown failed') from None
        return True
