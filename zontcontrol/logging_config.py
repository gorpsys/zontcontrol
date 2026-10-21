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


class _CloudHandler(_ApplicationHandler):
    def __init__(self, stream, *, capture):
        super().__init__(stream)
        self._records = [] if capture else None
        self._root_level = None

    def capture_root(self):
        root = logging.getLogger()
        self._root_level = root.level
        root.addHandler(self)
        root.setLevel(min(root.level, logging.INFO))

    def emit(self, record):
        try:
            line = self.format(record) + self.terminator
            if self._records is not None:
                self._records.append(line)
            self.stream.write(line)
            self.flush()
        except Exception:
            self.handleError(record)

    def stop_capture(self):
        """Transfer records to the caller and release invocation memory."""
        self.acquire()
        try:
            records, self._records = self._records, None
            if self._root_level is not None:
                root = logging.getLogger()
                root.removeHandler(self)
                root.setLevel(self._root_level)
                self._root_level = None
            return ''.join(records) if records is not None else ''
        finally:
            self.release()

    def close(self):
        self.stop_capture()
        super().close()


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


def configure_yc_logging(*, capture=False):
    """Cloud diagnostics on stdout; configure anew safely for warm invocations."""
    handler = _configure(_CloudHandler(sys.stdout, capture=capture), YcLoggingFormatter())
    if capture:
        handler.capture_root()
    return handler
