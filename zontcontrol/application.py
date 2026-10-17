"""Process composition root and registrar entrypoint.

Only this boundary reads token environment. Algorithms receive private facade
bindings, never adapters, metadata stores, credentials or a permissive gate.
Start/stop act on this application's threads; there is no remote daemon.
"""

import argparse
import os
import signal
import sys
from threading import Event, Lock, Thread
from uuid import uuid4

from . import __version__
from .autonomy.catalog import BUILTIN_SPECS
from .autonomy.contracts import AlgorithmError, validate_catalog
from .autonomy.registry import AlgorithmRegistry
from .config import ConfigurationError, load_controller_config, load_registry_config, read_json
from .controller.auth import token_headers
from .controller.contracts import ControllerError, ControlOwnershipRequired, InvalidArguments
from .controller.ownership import CommandExecutor
from .controller.service import ControllerService
from .controller.transport import HttpTransport
from .controller.zont import ZontAdapter
from .coordination.clock import BootClock
from .coordination.grants import ExecutorGate, GrantCoordinator
from .coordination.leader import LeaderElection, LeaseLost
from .coordination.models import RecordError, original_name
from .coordination.statistics import JsonlSink, ObservationError, Statistics, observe
from .coordination.store import ExecutorStore, LeaderStore


def _credentials(environ):
    token = environ.get('ZONT_TOKEN')
    if not isinstance(token, str) or not token.strip():
        raise ValueError('Set ZONT_TOKEN in the environment. Obtain a token separately with '
                         'python3 -m zontcontrol.get_token (zontcontrol-get-token).')
    return token


def _adapter(environ, reader, transport_factory):
    token = _credentials(os.environ if environ is None else environ)
    config = load_controller_config(reader=reader)
    headers = token_headers(config.contact, token)
    try:
        Statistics(lambda row: None, protocol_version='1', profile=config.statistics.profile)
    except ObservationError:
        raise ConfigurationError('config/controller.json', 'Invalid statistics profile') from None
    transport = transport_factory(timeout=config.http_timeout_seconds)
    return config, transport, ZontAdapter(transport, headers)


def create_read_controller(*, environ=None, reader=read_json, transport_factory=None):
    """Research/read boundary: no registry, coordinator, worker or write binding."""
    _, _, adapter = _adapter(environ, reader, transport_factory or HttpTransport)
    return ControllerService(adapter)


def _close_transport(transport):
    close = getattr(transport, 'close', None)
    if close is not None:
        close()


class _DeviceCoordinator:
    def __init__(self, store, leader, coordinator, clock, statistics):
        self.store, self.leader, self.coordinator = store, leader, coordinator
        self.clock, self.statistics = clock, statistics
        self.cancelled = Event()
        self.thread = Thread(target=self._work, name=f'coordinator-{store.device_id}', daemon=False)

    def _work(self):
        renewed_at = self.clock.now()
        try:
            while not self.cancelled.is_set():
                try:
                    if not self.leader.can_coordinate(self.coordinator.window):
                        if not self.leader.try_acquire(cancelled=self.cancelled):
                            self.clock.wait(self.cancelled, self.leader.timing.poll_after)
                            continue
                        renewed_at = self.clock.now()
                    if self.cancelled.is_set():
                        break
                    if self.clock.now() - renewed_at >= self.leader.timing.renew_after:
                        self.leader.renew()
                        renewed_at = self.clock.now()
                    self.coordinator.step()
                except (LeaseLost, ControllerError, RecordError):
                    # Role loss never reaches algorithm cancellation or finally.
                    observe(self.statistics, 'error', device_id=self.store.device_id,
                            outcome='unknown')
                self.clock.wait(self.cancelled, self.leader.timing.poll_after)
        finally:
            # Foreign/unsettled grants must expire, never be blindly cleared.
            try:
                if self.leader.can_coordinate(self.leader.timing.delta):
                    self.coordinator.step(accepting=False)
                    if self.store.read_r2() is None:
                        self.leader.release()
            except (LeaseLost, ControllerError, RecordError):
                pass
            self.leader.stop()

    def close(self):
        self.cancelled.set()
        self.thread.join()


class _CoordinationRuntime:
    """Lazily attach independent device workers; R2 writers stay internal.

    Byte capacities are local refusal budgets, not inferred API limits.
    Names are recovered only from a strictly validated v1 suffix.
    """

    def __init__(self, adapter, config, clock, statistics):
        self.adapter, self.config, self.clock = adapter, config, clock
        self.statistics = statistics
        self._lock = Lock()
        self._devices = {}
        self._device_locks = {}
        self._closed = False

    def _check_attempt(self, local_deadline=None, attempt_deadline=None):
        if (self._closed or any(deadline is not None and self.clock.now() >= deadline
                              for deadline in (local_deadline, attempt_deadline))):
            raise ControlOwnershipRequired()

    def _device(self, device_id, *, local_deadline=None, attempt_deadline=None):
        if type(device_id) is not int or device_id <= 0:
            raise InvalidArguments()
        with self._lock:
            self._check_attempt(local_deadline, attempt_deadline)
            lock = self._device_locks.setdefault(device_id, Lock())
        # Account state lock never spans metadata I/O for a different device.
        with lock:
            with self._lock:
                self._check_attempt(local_deadline, attempt_deadline)
            if device_id not in self._devices:
                metadata = self.adapter.read_metadata(device_id)
                name = original_name(metadata['name'])
                options = dict(original_name=name, notes_capacity=65536, name_capacity=16384)
                executor_store = ExecutorStore(self.adapter, device_id, **options)
                leader_store = LeaderStore(self.adapter, device_id, **options)
                with self._lock:
                    self._check_attempt(local_deadline, attempt_deadline)
                executor_store.read_r1()
                with self._lock:
                    self._check_attempt(local_deadline, attempt_deadline)
                executor_store.read_r2()
                leader = LeaderElection(leader_store, str(uuid4()), self.config.timing,
                                        clock=self.clock, statistics=self.statistics)
                coordinator = GrantCoordinator(leader_store, leader, clock=self.clock,
                                               statistics=self.statistics)
                worker = _DeviceCoordinator(leader_store, leader, coordinator,
                                             self.clock, self.statistics)
                gate = ExecutorGate({device_id: executor_store}, clock=self.clock,
                                    delta=self.config.delta_seconds, statistics=self.statistics)
                with self._lock:
                    self._check_attempt(local_deadline, attempt_deadline)
                    self._devices[device_id] = (gate, worker)
                    worker.thread.start()
            with self._lock:
                self._check_attempt(local_deadline, attempt_deadline)
                return self._devices[device_id][0]

    def try_acquire(self, owner, device_id, *, lease_seconds, request_id=None,
                    local_deadline=None, attempt_deadline=None):
        gate = self._device(device_id, local_deadline=local_deadline,
                            attempt_deadline=attempt_deadline)
        return gate.try_acquire(owner, device_id, lease_seconds=lease_seconds,
                                request_id=request_id, local_deadline=local_deadline,
                                attempt_deadline=attempt_deadline)

    def abandon_request(self, owner, device_id, request_id):
        # Cancellation cannot initialize a device or start an independent role.
        with self._lock:
            device = self._devices.get(device_id)
        if device is not None:
            device[0].abandon_request(owner, device_id, request_id)

    def validate(self, owner, session):
        return self._device(session.device_id).validate(owner, session)

    def release(self, owner, session):
        return self._device(session.device_id).release(owner, session)

    def observation_context(self, owner, session):
        return self._device(session.device_id).observation_context(owner, session)

    def close(self):
        with self._lock:
            self._closed = True
            workers = tuple(worker for _, worker in self._devices.values())
        for worker in workers:
            worker.cancelled.set()
        for worker in workers:
            worker.close()


class Application:
    """Own one registry and its resources in the caller's process.

    close(wait_timeout) may return False while a worker is stopping. Shared
    resources stay available until every finally and accepted command ends.
    """

    def __init__(self, controller, coordination, registry):
        self._controller, self._coordination, self._registry = controller, coordination, registry

    def controller(self):
        return self._controller.for_consumer()

    def list_algorithms(self, mode=None):
        return self._registry.list_algorithms(mode)

    def list_continuous(self):
        return self._registry.list_continuous()

    def run_once(self, algorithm_id, timeout=None):
        return self._registry.run_once(algorithm_id, timeout)

    def run_all_once(self, timeouts=None):
        return self._registry.run_all_once(timeouts)

    def start(self, algorithm_id):
        return self._registry.start(algorithm_id)

    def stop(self, algorithm_id, *, wait_timeout=0):
        return self._registry.stop(algorithm_id, wait_timeout=wait_timeout)

    def host(self, cancelled=None):
        """Hold this process; start/stop use this same object, never IPC.

        A supplied Event supports embedding. Signal handlers only request
        shutdown; the context manager subsequently joins worker finally.
        """
        if cancelled is not None:
            cancelled.wait()
            return
        cancelled = Event()
        previous = {}
        try:
            for number in (signal.SIGINT, signal.SIGTERM):
                previous[number] = signal.signal(number, lambda *_: cancelled.set())
            cancelled.wait()
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)

    def close(self, *, wait_timeout=None):
        return self._registry.close(wait_timeout=wait_timeout)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def create_application(*, environ=None, reader=read_json, catalog=BUILTIN_SPECS,
                       transport_factory=None, statistics_sink=None, clock=None):
    """Load fixed cwd/config infrastructure; factories load their module files.

    Catalog/transport/sink/clock are offline DI seams. No option replaces the
    coordination gate with local permission.
    """
    _credentials(os.environ if environ is None else environ)
    registry_config = load_registry_config(reader=reader)
    catalog = tuple(catalog)
    validate_catalog(catalog, registry_config)
    config, transport, adapter = _adapter(environ, reader, transport_factory or HttpTransport)
    sink = statistics_sink
    try:
        clock = clock if clock is not None else BootClock()
        if sink is None:
            path = config.statistics.path
            path.parent.mkdir(parents=True, exist_ok=True)
            sink = JsonlSink(path.open('a', encoding='utf-8'))
        statistics = Statistics(sink, protocol_version='1', profile=config.statistics.profile)
    except BaseException:
        try:
            if statistics_sink is None and sink is not None:
                sink.close()
        finally:
            _close_transport(transport)
        raise
    coordination = _CoordinationRuntime(adapter, config.coordination, clock, statistics)
    executor = CommandExecutor(gate=coordination, clock=clock.now, statistics=statistics)
    controller = ControllerService(adapter, executor=executor)

    def close_resources():
        executor.wait_idle()
        try:
            coordination.close()
        finally:
            try:
                _close_transport(transport)
            finally:
                if statistics_sink is None:
                    sink.close()

    registry = AlgorithmRegistry(registry_config, controller.for_consumer,
                                 begin_close_controller=executor.begin_close,
                                 close_controller=close_resources)
    app = Application(controller, coordination, registry)
    try:
        registry.register_all(catalog)
    except BaseException:
        app.close()
        raise
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description='ZONT algorithm host (one process)')
    parser.add_argument('--version', action='version', version=f'zontcontrol {__version__}')
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('list', help='List registered modules and their state')
    once = commands.add_parser('once', help='Run one once module')
    once.add_argument('algorithm_id')
    once.add_argument('--timeout', type=float)
    commands.add_parser('all', help='Run all once modules')
    start = commands.add_parser('start', help='Start continuous module and hold this process')
    start.add_argument('algorithm_id')
    host = commands.add_parser('host', help='Hold autostart modules until SIGINT/SIGTERM')
    host.add_argument('--start', action='append', default=[], dest='starts', metavar='ALGORITHM')
    args = parser.parse_args(argv)
    try:
        with create_application() as app:
            if args.action == 'list':
                for row in app.list_algorithms():
                    print(f'{row.spec.algorithm_id}: {row.state.value}' +
                          (f' ({row.error})' if row.error is not None else ''))
            elif args.action == 'once':
                result = (app.run_once(args.algorithm_id) if args.timeout is None else
                          app.run_once(args.algorithm_id, args.timeout))
                print(result)
            elif args.action == 'all':
                result = app.run_all_once()
                for algorithm_id, outcome in result.items():
                    print(f'{algorithm_id}: {outcome}')
                return int(any(isinstance(outcome, Exception) for outcome in result.values()))
            else:
                for algorithm_id in ([args.algorithm_id] if args.action == 'start' else args.starts):
                    app.start(algorithm_id)
                app.host()
        return 0
    except (ControllerError, AlgorithmError, ValueError, KeyError, OSError, RuntimeError) as error:
        if isinstance(error, ConfigurationError) or (isinstance(error, ValueError)
                and str(error).startswith('Set ZONT_TOKEN')):
            message = str(error)
        else:
            message = 'Application failed; check configuration, module state and ZONT availability.'
        print(message, file=sys.stderr)
        return 1
