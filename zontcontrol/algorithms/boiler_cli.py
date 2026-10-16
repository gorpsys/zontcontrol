"""Continuous terminal consumer of the controller API; no transport or registry.

The module owns its fixed JSON, minute schedule and interruptible terminal poll.
Only an explicit command --execute acquires ownership, for that command alone.
The reader is joined before releasing the terminal, including on stop or EOF.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import select
import shlex
import sys
from threading import Event, Lock, Thread
from tempfile import mkdtemp
from time import monotonic
import webbrowser
from zoneinfo import ZoneInfo

from .charts import GraphSpec, SeriesSpec, chart_path, render_charts
from ..config import ConfigurationError, read_json
from ..controller.contracts import (
    ControlKey, ControllerError, InvalidArguments, ValueKey, ValueType,
)


CONFIG_PATH = Path('config/boiler_cli.json')
UPDATE_SECONDS = 60
_TERMINALS = set()
_TERMINAL_LOCK = Lock()
HELP = ("command DEVICE KIND ELEMENT ACTION 'JSON arguments' [--execute]\n"
        'graph DEVICE GRAPH_ID START_ISO_WITH_OFFSET END_ISO_WITH_OFFSET [--view]\n'
        'help | quit (this UI only); current values refresh every 60 seconds')


def _require(condition):
    if not condition:
        raise ValueError()


def _keys(data, required, optional=()):
    _require(isinstance(data, dict) and set(required) <= data.keys()
             and not data.keys() - set(required) - set(optional))


@dataclass(frozen=True)
class AllowedControl:
    key: ControlKey
    actions: tuple[str, ...]


@dataclass(frozen=True)
class BoilerCLIConfig:
    devices: tuple[int, ...]
    controls: tuple[AllowedControl, ...]
    graphs: tuple[GraphSpec, ...]
    lease_seconds: float
    output_directory: Path
    display_timezone: str


def load_config(*, root=Path('.'), reader=None):
    path = Path(root) / CONFIG_PATH
    data = (reader or read_json)(path)
    try:
        _keys(data, ('devices', 'controls', 'graphs'),
              ('lease_seconds', 'output_directory', 'display_timezone'))
        devices = data['devices']
        _require(isinstance(devices, list) and bool(devices)
                 and all(type(item) is int and item > 0 for item in devices)
                 and len(devices) == len(set(devices)))
        _require(isinstance(data['controls'], list) and isinstance(data['graphs'], list))
        controls = []
        for row in data['controls']:
            _keys(row, ('device_id', 'kind', 'element_id', 'actions'))
            key = ControlKey(row['device_id'], row['kind'], row['element_id'])
            actions = row['actions']
            _require(key.device_id in devices and isinstance(actions, list) and bool(actions)
                     and all(isinstance(item, str) and item.strip() for item in actions)
                     and len(actions) == len(set(actions)))
            controls.append(AllowedControl(key, tuple(actions)))
        _require(len({item.key for item in controls}) == len(controls))
        graphs = []
        for row in data['graphs']:
            _keys(row, ('graph_id', 'title', 'series'))
            _require(isinstance(row['series'], list))
            series = []
            for item in row['series']:
                _keys(item, ('key',), ('label',))
                _keys(item['key'], ('device_id', 'kind', 'element_id', 'field'))
                series.append(SeriesSpec(ValueKey(**item['key']), item.get('label')))
            graph = GraphSpec(row['graph_id'], row['title'], tuple(series))
            _require(graph.device_id in devices)
            graphs.append(graph)
        _require(len({(item.device_id, item.graph_id) for item in graphs}) == len(graphs))
        lease = data.get('lease_seconds', 60)
        _require(type(lease) in (int, float) and math.isfinite(lease) and 0 < lease <= 1200)
        directory = data.get('output_directory', 'charts')
        _require(isinstance(directory, str) and bool(directory.strip()) and '\0' not in directory)
        zone = data.get('display_timezone', 'UTC')
        _require(isinstance(zone, str))
        ZoneInfo(zone)
        return BoilerCLIConfig(tuple(devices), tuple(controls), tuple(graphs), lease,
                               Path(root) / directory, zone)
    except (ValueError, TypeError, KeyError, ControllerError, OverflowError):
        raise ConfigurationError(path, 'Invalid boiler_cli devices, controls, graphs or parameters') from None


class Terminal:
    """Poll the canonical terminal fd without a permanently blocked input thread.

    os.read avoids TextIO buffering hiding a second pasted line from select.
    No terminal mode changes are needed: the human still edits complete lines.
    """

    def __init__(self, input_stream=None, output_stream=None):
        self.input = input_stream if input_stream is not None else sys.stdin
        self.output = output_stream if output_stream is not None else sys.stdout
        self._pending = b''

    def available(self):
        try:
            return self.input.isatty() and self.output.isatty()
        except (OSError, ValueError, AttributeError):
            return False

    @property
    def identity(self):
        return os.ttyname(self.input.fileno())

    def poll(self, timeout):
        if b'\n' not in self._pending:
            if not select.select([self.input], [], [], timeout)[0]:
                return None
            chunk = os.read(self.input.fileno(), 4096)
            if not chunk:
                raise EOFError()
            self._pending += chunk
        if b'\n' not in self._pending:
            return None
        line, self._pending = self._pending.split(b'\n', 1)
        return line.decode(self.input.encoding or 'utf-8', errors='replace').rstrip('\r')

    def write(self, text):
        self.output.write(text + '\n')
        self.output.flush()


def _view(path):
    webbrowser.open(path.as_uri())


class BoilerCLI:
    def __init__(self, controller, *, root=Path('.'), reader=None, terminal=None,
                 clock=monotonic, wall_clock=None, viewer=_view):
        self.config = load_config(root=root, reader=reader)
        self._terminal = terminal if terminal is not None else Terminal()
        if not self._terminal.available():
            raise ValueError('boiler_cli requires an interactive terminal')
        self._controller = controller
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._viewer = viewer
        self._output_lock = Lock()
        self._last = {}

    def _write(self, text):
        # Names and labels from upstream must not emit terminal control codes.
        text = ''.join(c if c in '\n\t' or c.isprintable() else '�' for c in str(text))
        with self._output_lock:
            self._terminal.write(text)

    def refresh(self):
        for device_id in self.config.devices:
            failed = False
            try:
                descriptors = self._controller.describe_values(device_id)
                snapshot = self._controller.read_current(device_id)
                self._last[device_id] = (descriptors, snapshot)
            except ControllerError as error:
                self._write(f'device {device_id}: {error}; previous data stale, refresh missed')
                failed = True
                if device_id not in self._last:
                    continue
                descriptors, snapshot = self._last[device_id]
            readings = {row.key: row for row in snapshot.values}
            lines = [f'device {device_id} | value | unit | quality | age_s (measurement)']
            now = self._wall_clock()
            for descriptor in descriptors:
                row = readings.get(descriptor.key)
                if not descriptor.current_available:
                    value, quality, age = 'unavailable', 'missing', 'unknown'
                elif row is None:
                    value, quality, age = 'missing', 'missing', 'unknown'
                else:
                    value = str(row.value)
                    quality = 'stale' if failed else row.quality.value
                    age = ('unknown' if row.measured_at is None else
                           f'{max(0, (now - row.measured_at).total_seconds()):g}')
                lines.append(f'{descriptor.name} [{descriptor.key.kind}/{descriptor.key.element_id}/'
                             f'{descriptor.key.field}] | {value} | {descriptor.unit or "—"} | '
                             f'{quality} | {age}')
            self._write('\n'.join(lines))

    def update_loop(self, stop, *, wait=None):
        wait = wait or stop.wait
        due = self._clock()
        while not stop.is_set():
            self.refresh()
            due += UPDATE_SECONDS
            now = self._clock()
            if due <= now:
                missed = int((now - due) // UPDATE_SECONDS) + 1
                due += missed * UPDATE_SECONDS
                self._write(f'refresh missed: {missed} scheduled slot(s); no catch-up')
            if wait(max(0, due - now)):
                return

    def run_continuous(self, context):
        identity = self._terminal.identity
        with _TERMINAL_LOCK:
            if identity in _TERMINALS:
                raise ValueError('boiler_cli terminal is already owned')
            _TERMINALS.add(identity)
        stop, reader_failed = Event(), Event()
        worker = None
        try:
            if context.cancelled.is_set():
                return
            self._write(HELP)
            def update():
                try:
                    self.update_loop(stop)
                except Exception:
                    reader_failed.set()
            worker = Thread(target=update, name='boiler_cli-current')
            worker.start()
            while not context.cancelled.is_set():
                if not self._terminal.available() or reader_failed.is_set():
                    raise ValueError('boiler_cli terminal or current reader lost')
                line = self._terminal.poll(.1)
                if context.cancelled.is_set():
                    return
                if line is not None and not self.handle_line(line, context):
                    return
        finally:
            stop.set()
            if worker is not None:
                worker.join()
            with _TERMINAL_LOCK:
                _TERMINALS.remove(identity)

    def handle_line(self, line, context):
        try:
            parts = shlex.split(line)
            if parts == ['quit']:
                return False
            if parts == ['help']:
                self._write(HELP)
            elif parts and parts[0] == 'command':
                self._command(parts[1:], context)
            elif parts and parts[0] == 'graph':
                self._graph(parts[1:])
            elif parts:
                raise InvalidArguments()
        except ControllerError as error:
            self._write(str(error))
        except (ValueError, TypeError, KeyError, OSError, OverflowError):
            self._write('Invalid or unavailable CLI command; see help')
        return True

    def _device(self, device_id):
        _require(device_id in self.config.devices)
        device = next((row for row in self._controller.list_devices()
                       if row.device_id == device_id), None)
        _require(device is not None)
        return device

    def _command(self, parts, context):
        execute = bool(parts and parts[-1] == '--execute')
        if execute:
            parts = parts[:-1]
        _require(len(parts) == 5)
        device_id, kind, element_id, action, arguments = parts
        key = ControlKey(int(device_id), kind, int(element_id))
        allowed = next((item for item in self.config.controls if item.key == key), None)
        _require(allowed is not None and action in allowed.actions)
        device = self._device(key.device_id)
        descriptor = next((item for item in self._controller.list_controls(key.device_id)
                           if item.key == key), None)
        _require(descriptor is not None and action in descriptor.actions
                 and not descriptor.unavailable_reasons)
        arguments = json.loads(arguments)
        _require(isinstance(arguments, dict))
        command = self._controller.prepare_command(key.device_id, key, action, arguments)
        self._write(f'preview: {device.name} [{key.device_id}] {kind}/{key.element_id} '
                    f'{command.action} {dict(command.arguments)}')
        if not execute or context.cancelled.is_set():
            return
        session = self._controller.try_acquire_control(key.device_id,
                                                      lease_seconds=self.config.lease_seconds)
        try:
            if context.cancelled.is_set():
                return
            try:
                self._controller.execute_command(command, session)
            except ControllerError as error:
                self._write(str(error))
            else:
                self._write('API accepted command; physical outcome not established')
        finally:
            self._controller.release_control(session)

    def _graph(self, parts):
        view = bool(parts and parts[-1] == '--view')
        if view:
            parts = parts[:-1]
        _require(len(parts) == 4)
        device_id, graph_id, start, end = parts
        device_id = int(device_id)
        start, end = datetime.fromisoformat(start), datetime.fromisoformat(end)
        _require(start.tzinfo is not None and end.tzinfo is not None and start < end)
        graph = next((item for item in self.config.graphs
                      if item.device_id == device_id and item.graph_id == graph_id), None)
        _require(graph is not None)
        device = self._device(device_id)
        descriptors = self._controller.describe_values(device_id)
        available = {row.key: row for row in descriptors}
        units = set()
        for item in graph.series:
            descriptor = available.get(item.key)
            _require(descriptor is not None and descriptor.history_available
                     and descriptor.value_type in (ValueType.NUMBER, ValueType.INTEGER))
            units.add(descriptor.unit)
        _require(len(units) == 1)
        history = self._controller.read_history(device_id, tuple(item.key for item in graph.series),
                                                start, end)
        charts = render_charts((graph,), devices=(device,), descriptors=descriptors,
                               histories=(history,), start=start, end=end,
                               display_timezone=self.config.display_timezone)
        for chart in charts:
            directory = self.config.output_directory
            directory.mkdir(parents=True, exist_ok=True)
            path = chart_path(directory, chart)
            # Exclusive creation avoids following symlinks or silently replacing
            # earlier user artifacts. Repeated requests receive a fresh directory.
            if path.exists():
                path = chart_path(mkdtemp(prefix='graph-', dir=directory), chart)
            with path.open('x', encoding='utf-8') as stream:
                stream.write(chart.svg)
            self._write(str(path))
            if view:
                self._viewer(path)
