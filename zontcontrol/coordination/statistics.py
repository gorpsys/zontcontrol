"""Bounded observations, independent of protocol state and command execution.

JSONL is an output stream, never a recovery or ownership journal. No analysis
here imports a controller, contacts ZONT, retries a write or asserts reliability.
Whole episodes need explicit observation coverage; polls are only event counts.
"""

from collections import Counter
import json
import logging
import math
from pathlib import Path
import re
from threading import Lock


_LOG = logging.getLogger(__name__)
_LABEL = re.compile(r'[A-Za-z0-9_.-]{1,128}\Z')
_EVENTS = frozenset({
    'episode_started', 'episode_finished', 'attempt', 'poll', 'grant',
    'request', 'request_repeated', 'request_lost', 'heartbeat', 'finished',
    'leader_acquired', 'leader_renewed', 'leader_lost', 'leader_released',
    'leader_changed_active_grant', 'delta_breach', 'rtt', 'rate_limited',
    'timeout', 'error', 'cleanup', 'conflict', 'command', 'grant_ack', 'leader_attempt',
})
_IDS = frozenset({'episode_id', 'run_id', 'request_id', 'grant_id', 'leader_term'})
_NUMBERS = frozenset({'wait_s', 'rtt_s', 'delta_s', 'elapsed_s'})
_FIELDS = _IDS | _NUMBERS | {'device_id', 'revision', 'outcome', 'coverage', 'phase'}
_BASE = frozenset({'schema', 'event', 'protocol_version', 'profile', 'source'})
_OUTCOMES = frozenset({'success', 'unknown', 'conflict', 'failure', 'busy'})
_COVERAGE = frozenset({'complete', 'partial', 'none'})
MAX_LINE = 4096


class ObservationError(ValueError):
    def __init__(self, line=None):
        super().__init__('Invalid orchestration observation' +
                         (f' at line {line}' if line is not None else ''))


def _label(value):
    return type(value) is str and _LABEL.fullmatch(value) is not None


def _validate(row):
    if (type(row) is not dict or not _BASE <= row.keys()
            or row.keys() - _BASE - _FIELDS
            or type(row['schema']) is not int or row['schema'] != 1
            or type(row['event']) is not str or row['event'] not in _EVENTS
            or not all(_label(row[key]) for key in ('protocol_version', 'profile'))
            or type(row['source']) is not str
            or row['source'] not in {'runtime', 'simulation', 'counterexample'}):
        raise ObservationError()
    for key, value in row.items():
        if key in _IDS and not _label(value):
            raise ObservationError()
        if key in _NUMBERS and (type(value) not in (int, float)
                                or not 0 <= value <= 1e9 or not math.isfinite(value)):
            raise ObservationError()
        if key in {'device_id', 'revision'} and (type(value) is not int
                                                or not 0 <= value < 2**63
                                                or key == 'device_id' and value == 0):
            raise ObservationError()
        if key == 'outcome' and (type(value) is not str or value not in _OUTCOMES):
            raise ObservationError()
        if key == 'coverage' and (type(value) is not str or value not in _COVERAGE):
            raise ObservationError()
        if key == 'phase' and (type(value) is not str
                               or value not in {'command', 'session', 'metadata'}):
            raise ObservationError()
    if row['event'] in {'episode_started', 'episode_finished'}:
        if not {'episode_id', 'device_id'} <= row.keys():
            raise ObservationError()
        if row['event'] == 'episode_finished' and not {'outcome', 'coverage'} <= row.keys():
            raise ObservationError()
    return row


class Statistics:
    """A callback receives only allowlisted observations, with bounded scalars.

    Sink exceptions are counted and logged without their possibly sensitive
    message/traceback. They never propagate into accepted commands or finally.
    Invalid caller data raises before invoking the sink. Protocol adapters use
    observe() so that even a misconfigured instrumentation callback is isolated.
    """

    def __init__(self, sink, *, protocol_version, profile, source='runtime'):
        self._base = {'schema': 1, 'event': 'poll', 'protocol_version': protocol_version,
                      'profile': profile, 'source': source}
        _validate(self._base)
        if not callable(sink):
            raise ObservationError()
        self._sink = sink
        self._lock = Lock()
        self._errors = 0

    @property
    def errors(self):
        with self._lock:
            return self._errors

    def emit(self, event, **fields):
        if fields.keys() - _FIELDS:
            raise ObservationError()
        row = _validate({**self._base, 'event': event, **fields})
        try:
            self._sink(row)
        except Exception:
            with self._lock:
                self._errors += 1
            _LOG.warning('Orchestration statistics sink failed; observation missing')


def observe(statistics, event, **fields):
    """Best effort DI hook; observation failure cannot affect protocol results."""
    if statistics is not None:
        try:
            statistics.emit(event, **fields)
        except Exception:
            _LOG.warning('Orchestration statistics callback failed; observation missing')


def error_event(error):
    """Map safe exception types to categories without serializing any message."""
    from ..controller.contracts import CommandOutcomeUnknown, RateLimited
    if isinstance(error, RateLimited):
        return 'rate_limited'
    if isinstance(error, CommandOutcomeUnknown):
        return 'timeout'
    return 'error'


class JsonlSink:
    """Serialize threads sharing this sink; append/flush one complete JSON line.

    Supply an owned text stream, or a path below a chosen reports directory via
    in_reports(). This lock is process local, as is the application. Independent
    processes should use separate files. No locking/ownership state is restored.
    """

    def __init__(self, stream):
        self._stream = stream
        self._lock = Lock()

    @classmethod
    def in_reports(cls, filename, *, directory='reports'):
        if not _label(filename) or not filename.endswith('.jsonl'):
            raise ObservationError()
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        return cls((directory / filename).open('a', encoding='utf-8'))

    def __call__(self, row):
        _validate(row)
        line = json.dumps(row, ensure_ascii=True, allow_nan=False, separators=(',', ':')) + '\n'
        if len(line) > MAX_LINE:
            raise ObservationError()
        with self._lock:
            self._stream.write(line)
            self._stream.flush()

    def close(self):
        with self._lock:
            self._stream.close()


def read_jsonl(stream):
    """Reject bad/truncated/oversize lines with a safe line number, never data."""
    line_number = 0
    while True:
        raw = stream.readline(MAX_LINE + 1)
        if not raw:
            return
        line_number += 1
        try:
            if len(raw) > MAX_LINE or not raw.endswith('\n'):
                raise ObservationError()
            row = json.loads(raw)
            yield _validate(row)
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise ObservationError(line_number) from None


def analyze(rows):
    """Group protocol/profile/source; count episodes, never infer them from polls.

    Completeness must be explicitly asserted by the observer of the resource.
    Runtime grant/release alone cannot establish actual command exclusivity.
    Missing starts/finishes, duplicate boundaries and partial coverage are
    unknown. A detected overlap remains a conflict even with partial coverage.
    No reliability percentage or confidence bound is inferred from simulations.
    """
    groups = {}
    for row in rows:
        _validate(row)
        key = tuple(row[field] for field in ('protocol_version', 'profile', 'source'))
        group = groups.setdefault(key, {'events': Counter(), 'episodes': {},
                                        'wait_s': [], 'rtt_s': []})
        group['events'][row['event']] += 1
        for metric in ('wait_s', 'rtt_s'):
            if metric in row:
                group[metric].append(row[metric])
        if 'episode_id' not in row or 'device_id' not in row:
            continue
        episode = group['episodes'].setdefault((row['device_id'], row['episode_id']), [])
        episode.append(row)
    result = []
    for key, group in sorted(groups.items()):
        counts = dict.fromkeys(('total', 'complete', 'partial', 'success', 'conflict',
                                'unknown', 'failure'), 0)
        for episode in group['episodes'].values():
            counts['total'] += 1
            starts = [row for row in episode if row['event'] == 'episode_started']
            ends = [row for row in episode if row['event'] == 'episode_finished']
            complete = len(starts) == len(ends) == 1 and ends[0]['coverage'] == 'complete'
            counts['complete' if complete else 'partial'] += 1
            conflict = any(row['event'] == 'conflict' or row.get('outcome') == 'conflict'
                           for row in episode)
            unknown = any(row.get('outcome') == 'unknown' or row['event'] == 'timeout'
                          for row in episode)
            if conflict:
                outcome = 'conflict'
            elif not complete or unknown or ends[0]['outcome'] not in {'success', 'failure'}:
                outcome = 'unknown'
            else:
                outcome = ends[0]['outcome']
            counts[outcome] += 1
        report = dict(zip(('protocol_version', 'profile', 'source'), key))
        report.update(events=dict(sorted(group['events'].items())), episodes=counts)
        for metric in ('wait_s', 'rtt_s'):
            values = group[metric]
            report[metric] = {'count': len(values), 'total': sum(values),
                              'max': max(values, default=None)}
        result.append(report)
    return {'schema': 1, 'groups': result,
            'limitation': 'Observations only; missing coverage is unknown. '
                          'Simulations and poll counts do not establish ZONT reliability.'}
