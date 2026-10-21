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
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from threading import RLock


LOGGER_NAME = 'zontcontrol'
_IDENTIFIER = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')


def _fields(record):
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
    return fields


class YcLoggingFormatter(logging.Formatter):
    """One Unicode JSON object per line with Yandex level names."""

    def format(self, record):
        return json.dumps(_fields(record), ensure_ascii=False, allow_nan=False)


class _LocalFormatter(logging.Formatter):
    def format(self, record):
        fields = _fields(record)
        metadata = ' '.join(f'{name}={value}' for name, value in fields.items()
                            if name not in ('timestamp', 'message', 'level', 'logger'))
        return f"{fields['level']} {fields['logger']}: {fields['message']}" + (
            f' {metadata}' if metadata else '')


class _ApplicationHandler(logging.StreamHandler):
    """Distinguish our handler from handlers installed by an embedding caller."""


class _Tee:
    def __init__(self, stream, buffer, lock):
        self.stream, self.buffer, self.lock = stream, buffer, lock

    def write(self, text):
        with self.lock:
            count = self.stream.write(text)
            self.buffer.write(text if count is None else text[:count])
            return count

    def __getattr__(self, name):
        return getattr(self.stream, name)


def _stream_handlers():
    loggers = [logging.getLogger(), *(logger for logger in tuple(logging.Logger.manager.loggerDict.values())
                                     if isinstance(logger, logging.Logger))]
    return {handler for logger in loggers for handler in logger.handlers
            if isinstance(handler, logging.StreamHandler)}


@contextmanager
def capture_console_output():
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


def _configure(handler, formatter):
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


def configure_local_logging():
    """Human-readable diagnostics on stderr; terminal UI remains unchanged."""
    _configure(_ApplicationHandler(sys.stderr), _LocalFormatter())


def configure_yc_logging():
    """Cloud diagnostics on stdout; configure anew safely for warm invocations."""
    return _configure(_ApplicationHandler(sys.stdout), YcLoggingFormatter())
