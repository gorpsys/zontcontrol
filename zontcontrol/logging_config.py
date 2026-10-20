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


LOGGER_NAME = 'zontcontrol'
_IDENTIFIER = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
_STATES = frozenset({'unknown', 'normal', 'warning', 'suppressed', 'confirmed_pattern'})


def _fields(record):
    fields = {'message': record.getMessage(),
              'level': {'WARNING': 'WARN', 'CRITICAL': 'FATAL'}.get(
                  record.levelname, record.levelname), 'logger': record.name}
    for name in ('event', 'request_id', 'algorithm_id'):
        value = getattr(record, name, None)
        if isinstance(value, str) and _IDENTIFIER.fullmatch(value):
            fields[name] = value
    device = getattr(record, 'device_id', None)
    if type(device) is int and device > 0:
        fields['device_id'] = device
    state = getattr(record, 'state', None)
    if isinstance(state, str) and state in _STATES:
        fields['state'] = state
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
                            if name not in ('message', 'level', 'logger'))
        return f"{fields['level']} {fields['logger']}: {fields['message']}" + (
            f' {metadata}' if metadata else '')


class _ApplicationHandler(logging.StreamHandler):
    """Distinguish our handler from handlers installed by an embedding caller."""


def _configure(stream, formatter):
    logger = logging.getLogger(LOGGER_NAME)
    for handler in logger.handlers[:]:
        if isinstance(handler, _ApplicationHandler):
            logger.removeHandler(handler)
            handler.close()
    handler = _ApplicationHandler(stream)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def configure_local_logging():
    """Human-readable diagnostics on stderr; terminal UI remains unchanged."""
    _configure(sys.stderr, _LocalFormatter())


def configure_yc_logging():
    """Cloud diagnostics on stdout; configure anew safely for warm invocations."""
    _configure(sys.stdout, YcLoggingFormatter())
