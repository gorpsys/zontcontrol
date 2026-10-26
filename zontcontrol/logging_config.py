"""Explicit stdlib logging for local entrypoints and Yandex Cloud Functions.

Only application-owned handlers are replaced. Extra data is an allowlist;
callers must use fixed diagnostic messages, never upstream text or credentials.
Exception text, stack traces and arbitrary LogRecord extras are omitted.
"""

import json
import logging
import math
import re
import sys
from datetime import datetime, timezone
from contextlib import AbstractContextManager, contextmanager, redirect_stderr, redirect_stdout
from collections.abc import Iterator
from io import StringIO
from threading import RLock
from typing import Any, TextIO


LOGGER_NAME = 'zontcontrol'
_IDENTIFIER = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        return False
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.utcoffset() == timezone.utc.utcoffset(None)
    except ValueError:
        return False


def _diagnostics(record):
    fields = {}
    for name in ('decision_reason', 'reason'):
        value = getattr(record, name, None)
        if value is None or type(value) is str and _IDENTIFIER.fullmatch(value):
            if hasattr(record, name):
                fields[name] = value
    for name in ('reset_latest_quality', 'current_reset_quality'):
        value = getattr(record, name, None)
        if hasattr(record, name) and (value is None or type(value) is str
                                     and value in ('good', 'unknown', 'missing', 'stale')):
            fields[name] = value
    for name in ('latest_valid_sample', 'reset_latest_measured_at'):
        value = getattr(record, name, None)
        if hasattr(record, name) and (value is None or _timestamp(value)):
            fields[name] = value
    for name in ('reset_history_available', 'reset_active_at_period_end', 'current_reset_active',
                 'time_truncated', 'telemetry_time_truncated', 'reset_time_truncated'):
        value = getattr(record, name, None)
        if hasattr(record, name) and (value is None or type(value) is bool):
            fields[name] = value
    for name in ('valid_temperature_samples', 'usable_samples', 'detector_buffer_size'):
        value = getattr(record, name, None)
        if type(value) is int and value >= 0:
            fields[name] = value
    age = getattr(record, 'latest_valid_sample_age_seconds', None)
    if hasattr(record, 'latest_valid_sample_age_seconds') and (age is None or (
            type(age) in (int, float) and 0 <= age <= 1e12)):
        fields['latest_valid_sample_age_seconds'] = age
    period = getattr(record, 'requested_period', None)
    if type(period) is dict and all(_timestamp(period.get(key)) for key in ('start', 'end')):
        fields['requested_period'] = {key: period[key] for key in ('start', 'end')}
    elif hasattr(record, 'requested_period') and period is None:
        fields['requested_period'] = None
    return fields


def _fields(record: logging.LogRecord) -> dict[str, Any]:
    fields = {'timestamp': datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
              'message': record.getMessage(),
              'level': {'WARNING': 'WARN', 'CRITICAL': 'FATAL'}.get(
                  record.levelname, record.levelname), 'logger': record.name}
    for name in ('event', 'request_id', 'algorithm_id', 'state'):
        value = getattr(record, name, None)
        if isinstance(value, str) and _IDENTIFIER.fullmatch(value):
            fields[name] = value
    device = getattr(record, 'device_id', None)
    if type(device) is int and device > 0:
        fields['device_id'] = device
    reset = getattr(record, 'reset_performed', None)
    if type(reset) is bool:
        fields['reset_performed'] = reset
    elapsed = getattr(record, 'elapsed_ms', None)
    if type(elapsed) in (int, float) and elapsed >= 0:
        try:
            if math.isfinite(elapsed):
                fields['elapsed_ms'] = elapsed
        except OverflowError:
            pass
    return {**fields, **_diagnostics(record)}


class YcLoggingFormatter(logging.Formatter):
    """One Unicode JSON object per line with Yandex level names."""

    def format(self, record):
        return json.dumps(_fields(record), ensure_ascii=False, allow_nan=False)


class _LocalFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = _fields(record)
        metadata = ' '.join(f'{name}={value}' for name, value in fields.items()
                            if name not in ('timestamp', 'message', 'level', 'logger'))
        return f"{fields['level']} {fields['logger']}: {fields['message']}" + (
            f' {metadata}' if metadata else '')


class _ApplicationHandler(logging.StreamHandler):
    """Distinguish our handler from handlers installed by an embedding caller."""


class _Tee:
    def __init__(self, stream: TextIO, buffer: StringIO,
                 lock: AbstractContextManager[object]) -> None:
        self.stream, self.buffer, self.lock = stream, buffer, lock

    def write(self, text: str) -> int | None:
        with self.lock:
            count = self.stream.write(text)
            self.buffer.write(text if count is None else text[:count])
            return count

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


def _stream_handlers() -> set[logging.StreamHandler]:
    loggers = [logging.getLogger(), *(logger for logger in tuple(logging.Logger.manager.loggerDict.values())
                                     if isinstance(logger, logging.Logger))]
    return {handler for logger in loggers for handler in logger.handlers
            if isinstance(handler, logging.StreamHandler)}


@contextmanager
def capture_console_output() -> Iterator[StringIO]:
    """Copy Python stdout/stderr, including already configured stream handlers."""
    with StringIO() as buffer:
        lock = RLock()
        out, err = _Tee(sys.stdout, buffer, lock), _Tee(sys.stderr, buffer, lock)
        with redirect_stdout(out), redirect_stderr(err):
            try:
                for handler in _stream_handlers():
                    for tee in (out, err):
                        if handler.stream is tee.stream:
                            handler.setStream(tee)
                            break
                yield buffer
            finally:
                # Also detach handlers created during this invocation from its buffer.
                for handler in _stream_handlers():
                    if handler.stream is out or handler.stream is err:
                        handler.setStream(handler.stream.stream)


def _configure(handler: _ApplicationHandler, formatter: logging.Formatter) -> _ApplicationHandler:
    logger = logging.getLogger(LOGGER_NAME)
    for previous in logger.handlers[:]:
        if isinstance(previous, _ApplicationHandler):
            logger.removeHandler(previous)
            previous.close()
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return handler


def configure_local_logging() -> None:
    """Human-readable diagnostics on stderr; terminal UI remains unchanged."""
    _configure(_ApplicationHandler(sys.stderr), _LocalFormatter())


def configure_yc_logging() -> _ApplicationHandler:
    """Cloud diagnostics on stdout; configure anew safely for warm invocations."""
    return _configure(_ApplicationHandler(sys.stdout), YcLoggingFormatter())
