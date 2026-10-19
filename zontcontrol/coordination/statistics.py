"""Bounded observations, independent of protocol state and command execution.

JSONL is an output stream, never a recovery or ownership journal. No analysis
here imports a controller, contacts ZONT, retries a write or asserts reliability.
Whole episodes need explicit observation coverage; polls are only event counts.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Iterator
import json
import logging
import math
from pathlib import Path
import re
from threading import Lock
from typing import Any, Protocol, TextIO, TypeAlias, cast


ObservationValue: TypeAlias = str | int | float
Observation: TypeAlias = dict[str, ObservationValue]
ObservationSink: TypeAlias = Callable[[Observation], None]


class Observer(Protocol):
    """Best-effort instrumentation dependency, independent of protocol state."""

    def emit(self, event: str, **fields: ObservationValue) -> None: ...


_LOG = logging.getLogger(__name__)
_LABEL = re.compile(r'[A-Za-z0-9_.-]{1,128}\Z')
_EVENTS = frozenset({
    'episode_started', 'episode_finished', 'attempt', 'poll', 'grant',
    'request', 'request_repeated', 'request_lost', 'heartbeat', 'finished',
    'leader_acquired', 'leader_renewed', 'leader_lost', 'leader_released',
    'leader_changed_active_grant', 'delta_breach', 'rtt', 'rate_limited',
    'timeout', 'error', 'cleanup', 'conflict', 'command', 'grant_ack', 'leader_attempt',
    'sync_started', 'sync_read', 'sync_write_started', 'sync_write', 'sync_confirmation', 'sync_window',
    'freeze', 'command_started', 'command_finished', 'command_effect',
})
_IDS = frozenset({'episode_id', 'run_id', 'request_id', 'grant_id', 'leader_term',
                  'window_id', 'command_observation_id', 'clock_id'})
_TIMINGS = ('read_write_s', 'sync_window_s', 'rtt_s', 'freeze_gap_s')
_NUMBERS = frozenset({'wait_s', 'delta_s', 'delta_cmd_s', 'elapsed_s', *_TIMINGS})
_STAMPS = frozenset({'started_at', 'finished_at', 'admitted_at', 'api_response_at',
                     'effect_completed_at', 'sent_at', 'expected_resume_at'})
_KINDS = frozenset({'critical_section', 'command', 'residual_effect'})
_FIELDS = _IDS | _NUMBERS | _STAMPS | {
    'device_id', 'revision', 'outcome', 'coverage', 'phase', 'role', 'stage',
    'exceeded', 'sample_kind', 'completion_basis',
}
_BASE = frozenset({'schema', 'event', 'protocol_version', 'profile', 'source'})
_OUTCOMES = frozenset({'success', 'unknown', 'conflict', 'failure', 'busy'})
_COVERAGE = frozenset({'complete', 'partial', 'none'})
MAX_LINE = 4096


class ObservationError(ValueError):
    def __init__(self, line: int | None = None) -> None:
        super().__init__('Invalid orchestration observation' +
                         (f' at line {line}' if line is not None else ''))


def _label(value: object) -> bool:
    return type(value) is str and _LABEL.fullmatch(value) is not None


def _validate(row: Any) -> Observation:
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
        if key in _STAMPS and (type(value) not in (int, float)
                              or not 0 <= value <= 1e15 or not math.isfinite(value)):
            raise ObservationError()
        if key in {'role', 'stage'} and not _label(value):
            raise ObservationError()
        if key == 'exceeded' and (type(value) is not int or value not in (0, 1)):
            raise ObservationError()
        if key == 'sample_kind' and (type(value) is not str or value not in _KINDS):
            raise ObservationError()
        if key == 'completion_basis' and (type(value) is not str
                or value not in {'physical_upper_bound', 'api_response'}):
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
    if row['event'] == 'command_effect' and 'sample_kind' not in row:
        raise ObservationError()
    return row


class Statistics:
    """A callback receives only allowlisted observations, with bounded scalars.

    Sink exceptions are counted and logged without their possibly sensitive
    message/traceback. They never propagate into accepted commands or finally.
    Invalid caller data raises before invoking the sink. Protocol adapters use
    observe() so that even a misconfigured instrumentation callback is isolated.
    """

    def __init__(self, sink: ObservationSink, *, protocol_version: str, profile: str,
                 source: str = 'runtime') -> None:
        self._base: Observation = {'schema': 1, 'event': 'poll', 'protocol_version': protocol_version,
                      'profile': profile, 'source': source}
        _validate(self._base)
        if not callable(sink):
            raise ObservationError()
        self._sink = sink
        self._lock = Lock()
        self._errors = 0

    @property
    def errors(self) -> int:
        with self._lock:
            return self._errors

    def emit(self, event: str, **fields: ObservationValue) -> None:
        if fields.keys() - _FIELDS:
            raise ObservationError()
        row = _validate({**self._base, 'event': event, **fields})
        try:
            self._sink(row)
        except Exception:
            with self._lock:
                self._errors += 1
            _LOG.warning('Orchestration statistics sink failed; observation missing')


def observe(statistics: Observer | None, event: str, **fields: ObservationValue) -> None:
    """Best effort DI hook; observation failure cannot affect protocol results."""
    if statistics is not None:
        try:
            statistics.emit(event, **fields)
        except Exception:
            _LOG.warning('Orchestration statistics callback failed; observation missing')


def error_event(error: BaseException) -> str:
    """Map safe exception types to categories without serializing any message."""
    from ..control_types import CommandOutcomeUnknown, RateLimited
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

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._lock = Lock()

    @classmethod
    def in_reports(cls, filename: str, *, directory: str | Path = 'reports') -> JsonlSink:
        if not _label(filename) or not filename.endswith('.jsonl'):
            raise ObservationError()
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        return cls((directory / filename).open('a', encoding='utf-8'))

    def __call__(self, row: Observation) -> None:
        _validate(row)
        line = json.dumps(row, ensure_ascii=True, allow_nan=False, separators=(',', ':')) + '\n'
        if len(line) > MAX_LINE:
            raise ObservationError()
        with self._lock:
            self._stream.write(line)
            self._stream.flush()

    def close(self) -> None:
        with self._lock:
            self._stream.close()


def read_jsonl(stream: TextIO) -> Iterator[Observation]:
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


def sample_summary(values: Iterable[float], *, unknown: int = 0,
                   limits: Iterable[float] | None = None) -> dict[str, object]:
    """Nearest-rank descriptive statistics; unknown samples stay in the report."""
    samples = list(values)
    ordered = sorted(samples)
    bounds = list(limits) if limits is not None else None
    exceeded = (sum(value > limit for value, limit in zip(samples, bounds))
                if bounds is not None else None)
    return {'status': 'observed' if samples else 'unknown', 'count': len(samples),
            'unknown': unknown, 'max': max(samples, default=None),
            'p99': ordered[math.ceil(.99 * len(ordered)) - 1] if ordered else None,
            'p99_9': ordered[math.ceil(.999 * len(ordered)) - 1] if ordered else None,
            'exceedances': exceeded,
            'exceedance_fraction': exceeded / len(samples) if samples and exceeded is not None else None}


def _recommend(summary: dict[str, Any]) -> float | None:
    if not summary['count']:
        return None
    return max(5., 10 * summary['p99_9'], 1.2 * summary['max'])


def _synchronization(rows: list[Observation]) -> dict[str, object]:
    metrics: dict[tuple[object, ...], list[tuple[float, float, bool]]] = {}
    windows: dict[tuple[object, ...], list[Observation]] = {}
    for row in rows:
        if str(row['event']).startswith('command'):
            continue
        for metric in _TIMINGS:
            if metric in row:
                key = (row.get('device_id'), row.get('role', 'unknown'),
                       row.get('stage', 'unknown'), metric)
                metrics.setdefault(key, []).append((cast(float, row[metric]),
                        cast(float, row.get('delta_s', 5.)),
                        row.get('outcome') in {'unknown', 'failure'}
                        # Earlier v2 records measured send, without a response
                        # outcome. Keep that elapsed lower bound, never present
                        # it as a completed READ-to-WRITE percentile.
                        or metric == 'read_write_s' and row.get('outcome') != 'success'))
        # Older first-READ failures may have only a freeze/read record. Retain
        # their missing boundaries as incomplete instead of losing the window.
        if 'window_id' in row:
            key = (row.get('device_id'), row['window_id'])
            windows.setdefault(key, []).append(row)
    reports = []
    for key, samples in sorted(metrics.items(), key=lambda item: str(item[0])):
        complete = [(value, limit) for value, limit, censored in samples if not censored]
        censored = [(value, limit) for value, limit, censored in samples if censored]
        summary = sample_summary((value for value, _ in complete), unknown=len(censored),
                                 limits=(limit for _, limit in complete))
        summary['censored_elapsed'] = sample_summary((value for value, _ in censored),
                                                    limits=(limit for _, limit in censored))
        reports.append(dict(zip(('device_id', 'role', 'stage', 'metric'), key)) | summary)
    incomplete = unknown = timeouts = 0
    for events in windows.values():
        starts = [row for row in events if row['event'] == 'sync_started']
        ends = [row for row in events if row['event'] == 'sync_window']
        complete = len(starts) == len(ends) == 1
        incomplete += not complete
        unknown += not complete or ends[0].get('outcome') != 'success'
        timeouts += any(row.get('outcome') == 'unknown' for row in ends)
    recommendations = [_recommend(report) for report in reports]
    lower_bounds = [report['censored_elapsed']['max'] for report in reports
                    if report['censored_elapsed']['count']]
    # Timeout elapsed times are lower bounds, not completion percentiles. Keep
    # their observed maxima in the recommendation floor without inventing p99.
    recommendations.extend(max(5., 1.2 * value) for value in lower_bounds)
    recommendation = max((n for n in recommendations if n is not None), default=None)
    return {'timings': reports, 'windows': {'total': len(windows), 'incomplete': incomplete,
                                          'unknown': unknown, 'timeouts': timeouts},
            'delta_recommendation_s': recommendation,
            'delta_recommendation_status': ('incomplete' if lower_bounds or incomplete
                                            else 'observed' if recommendation is not None else 'unknown')}


def analyze(rows: Iterable[Observation]) -> dict[str, object]:
    """Group protocol/profile/source; count episodes, never infer them from polls.

    Completeness must be explicitly asserted by the observer of the resource.
    Runtime grant/release alone cannot establish actual command exclusivity.
    Missing starts/finishes, duplicate boundaries and partial coverage are
    unknown. A detected overlap remains a conflict even with partial coverage.
    No reliability percentage or confidence bound is inferred from simulations.
    """
    # Group accumulators contain counters, episode lists and metric samples.
    groups: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        _validate(row)
        key = tuple(cast(str, row[field]) for field in ('protocol_version', 'profile', 'source'))
        group = groups.setdefault(key, {'events': Counter(), 'episodes': {},
                                        'wait_s': [], 'rtt_s': [], 'rows': []})
        group['rows'].append(row)
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
        report: dict[str, object] = dict(zip(('protocol_version', 'profile', 'source'), key))
        report.update(events=dict(sorted(group['events'].items())), episodes=counts)
        report.update(_synchronization(group['rows']))
        for metric in ('wait_s', 'rtt_s'):
            values = group[metric]
            report[metric] = {'count': len(values), 'total': sum(values),
                              'max': max(values, default=None)}
        result.append(report)
    return {'schema': 1, 'groups': result,
            'limitation': 'Observations only; missing coverage is unknown. '
                          'Timeout elapsed lower bounds are separate from completion percentiles. '
                          'Simulations and poll counts do not establish ZONT reliability.'}


def analyze_command_effects(rows: Iterable[Observation]) -> dict[str, object]:
    """Offline, explicitly typed samples. API response is never physical evidence.

    Physical upper bounds must refer to the exact command's admission timestamp
    and local clock domain. The producer asserts that domain and evidence; this
    analyzer cannot verify a sensor, clock synchronization or a hard maximum.
    Critical section boundaries belong to the algorithm producing the sample.
    """
    groups: dict[tuple[object, ...], list[Observation]] = {}
    for row in rows:
        _validate(row)
        if row['event'] not in {'command_started', 'command_finished', 'command_effect'}:
            continue
        key = tuple(row.get(k) for k in ('protocol_version', 'profile', 'source', 'device_id'))
        groups.setdefault(key, []).append(row)
    result = []
    for key, samples in sorted(groups.items(), key=lambda item: str(item[0])):
        values: dict[str, list[float]] = {kind: [] for kind in (*_KINDS, 'api_elapsed', 'network_to_effect')}
        unknown: Counter[str] = Counter()
        residual_limits: list[float] = []
        commands: dict[object, list[Observation]] = {}
        for row in samples:
            if row['event'] in {'command_started', 'command_finished'}:
                commands.setdefault(row.get('command_observation_id'), []).append(row)
        anchors: dict[object, Observation] = {}
        for identifier, events in commands.items():
            starts = [r for r in events if r['event'] == 'command_started']
            ends = [r for r in events if r['event'] == 'command_finished']
            if (identifier is not None and len(starts) == 1
                    and {'admitted_at', 'clock_id'} <= starts[0].keys()):
                anchors[identifier] = starts[0]
            anchor = anchors.get(identifier)
            if (anchor is None or len(ends) != 1
                    or ends[0].get('admitted_at') != anchor['admitted_at']
                    or ends[0].get('clock_id') != anchor['clock_id']
                    or 'api_response_at' not in ends[0]
                    or ends[0]['api_response_at'] < anchor['admitted_at']):
                unknown['command'] += 1
                continue
            elapsed = cast(float, ends[0]['api_response_at']) - cast(float, anchor['admitted_at'])
            values['api_elapsed'].append(elapsed)
            if ends[0].get('outcome') == 'success':
                values['command'].append(elapsed)
            else:
                unknown['command'] += 1
        for row in samples:
            if row['event'] != 'command_effect':
                continue
            kind = cast(str, row['sample_kind'])
            complete = row.get('coverage') == 'complete' and row.get('outcome') == 'success'
            if kind in {'critical_section', 'command'}:
                if (complete and {'started_at', 'finished_at'} <= row.keys()
                        and row['finished_at'] >= row['started_at']):
                    values[kind].append(cast(float, row['finished_at']) - cast(float, row['started_at']))
                else:
                    unknown[kind] += 1
                continue
            anchor = anchors.get(row.get('command_observation_id'))
            if (not complete or anchor is None
                    or row.get('completion_basis') != 'physical_upper_bound'
                    or row.get('clock_id') != anchor['clock_id']
                    or row.get('admitted_at') != anchor['admitted_at']
                    or 'effect_completed_at' not in row
                    or row['effect_completed_at'] < anchor['admitted_at']
                    or 'sent_at' in row and not (
                        anchor['admitted_at'] <= row['sent_at'] <= row['effect_completed_at'])):
                unknown[kind] += 1
                continue
            elapsed = cast(float, row['effect_completed_at']) - cast(float, anchor['admitted_at'])
            values[kind].append(elapsed)
            residual_limits.append(cast(float, row.get('delta_cmd_s', 5.)))
            if ('sent_at' in row and anchor['admitted_at'] <= row['sent_at'] <= row['effect_completed_at']):
                values['network_to_effect'].append(cast(float, row['effect_completed_at']) - cast(float, row['sent_at']))
        report = dict(zip(('protocol_version', 'profile', 'source', 'device_id'), key))
        for kind, durations in sorted(values.items()):
            report[kind] = sample_summary(durations, unknown=unknown[kind],
                    limits=residual_limits if kind == 'residual_effect' else None)
        report['delta_cmd_recommendation_s'] = _recommend(cast(dict, report['residual_effect']))
        result.append(report)
    return {'schema': 1, 'groups': result,
            'limitation': 'Descriptive samples only; API RTT is not physical completion. '
                          'Missing/incomparable physical evidence is unknown. Recommendations '
                          'do not prove a hard bound; change the shared profile manually.'}
