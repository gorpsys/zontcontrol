"""Process composition root and registrar entrypoint.

Only this boundary reads token environment. Algorithms receive the shared controller,
never adapters or credentials.
Start/stop act on this application's threads; there is no remote daemon.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping, Sequence
import os
import signal
import sys
from threading import Event
from typing import Protocol, Self

from . import __version__
from .autonomy.catalog import BUILTIN_SPECS
from .autonomy.contracts import (
    AlgorithmError, AlgorithmRegistration, AlgorithmResult, AlgorithmSpec, Mode, UUIDFactory, validate_catalog,
)
from .autonomy.registry import AlgorithmRegistry
from .autonomy.runtime import RunHandle
from .config import (
    ConfigurationError, ControllerConfig, JSONReader,
    load_controller_config, load_registry_config, read_json,
)
from .controller.auth import token_headers
from .controller.contracts import Controller, ControllerError
from .controller.service import ControllerService, ReadControllerService
from .controller.transport import HttpTransport
from .controller.zont import ZontAdapter


class TransportFactory(Protocol):
    """Construct a bounded HTTP transport without issuing a request."""

    def __call__(self, *, timeout: float) -> HttpTransport: ...


def _credentials(environ: Mapping[str, str]) -> str:
    token = environ.get('ZONT_TOKEN')
    if not isinstance(token, str) or not token.strip():
        raise ValueError('Set ZONT_TOKEN in the environment. Obtain a token separately with '
                         'python3 -m zontcontrol.get_token (zontcontrol-get-token).')
    return token


def _adapter(environ: Mapping[str, str] | None, reader: JSONReader,
             transport_factory: TransportFactory) -> tuple[ControllerConfig, HttpTransport, ZontAdapter]:
    token = _credentials(os.environ if environ is None else environ)
    config = load_controller_config(reader=reader)
    headers = token_headers(config.contact, token)
    transport = transport_factory(timeout=config.http_timeout_seconds)
    return config, transport, ZontAdapter(transport, headers)


def create_read_controller(*, environ: Mapping[str, str] | None = None,
                           reader: JSONReader = read_json,
                           transport_factory: TransportFactory | None = None) -> ReadControllerService:
    """Research boundary: data and previews, no command method or algorithms."""
    _, _, adapter = _adapter(environ, reader, transport_factory or HttpTransport)
    return ReadControllerService(adapter)


def _close_transport(transport: HttpTransport) -> None:
    close = getattr(transport, 'close', None)
    if close is not None:
        close()


class Application:
    """Own one registry and its resources in the caller's process.

    close(wait_timeout) may return False while a worker is stopping. Shared
    resources stay available until every registered module finishes its finally.
    """

    def __init__(self, controller: ControllerService, registry: AlgorithmRegistry) -> None:
        self._controller, self._registry = controller, registry

    def controller(self) -> Controller:
        return self._controller

    def list_algorithms(self, mode: Mode | None = None) -> tuple[AlgorithmRegistration, ...]:
        return self._registry.list_algorithms(mode)

    def list_continuous(self) -> tuple[AlgorithmRegistration, ...]:
        return self._registry.list_continuous()

    def run_once(self, algorithm_id: str, timeout: float | None = None) -> AlgorithmResult:
        return self._registry.run_once(algorithm_id, timeout)

    def run_all_once(self, timeouts: Mapping[str, float] | None = None
                     ) -> dict[str, AlgorithmResult | Exception]:
        return self._registry.run_all_once(timeouts)

    def start(self, algorithm_id: str) -> RunHandle:
        return self._registry.start(algorithm_id)

    def stop(self, algorithm_id: str, *, wait_timeout: float | None = 0) -> bool:
        return self._registry.stop(algorithm_id, wait_timeout=wait_timeout)

    def host(self, cancelled: Event | None = None) -> None:
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

    def close(self, *, wait_timeout: float | None = None) -> bool:
        return self._registry.close(wait_timeout=wait_timeout)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def create_application(*, environ: Mapping[str, str] | None = None, reader: JSONReader = read_json,
                       catalog: Iterable[AlgorithmSpec] = BUILTIN_SPECS,
                       transport_factory: TransportFactory | None = None,
                       uuid_factory: UUIDFactory | None = None) -> Application:
    """Compose one account controller and independent algorithm threads.

    Modules share the controller through ordinary DI. There is no device
    coordinator or command ownership. Shutdown joins module finally blocks
    before closing the transport. External callers finish their own calls
    before closing the application.
    """
    _credentials(os.environ if environ is None else environ)
    registry_config = load_registry_config(reader=reader)
    catalog = tuple(catalog)
    validate_catalog(catalog, registry_config)
    _, transport, adapter = _adapter(environ, reader, transport_factory or HttpTransport)
    controller = ControllerService(adapter)
    registry = AlgorithmRegistry(registry_config, lambda: controller,
                                 close_controller=lambda: _close_transport(transport),
                                 uuid_factory=uuid_factory)
    app = Application(controller, registry)
    try:
        registry.register_all(catalog)
    except BaseException:
        app.close()
        raise
    return app


def main(argv: Sequence[str] | None = None) -> int:
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
