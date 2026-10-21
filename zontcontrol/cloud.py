"""One ordinary application per cloud invocation, with cooperative cleanup."""

from dataclasses import replace
import json
import logging
from io import StringIO
from typing import Any, cast

from . import __version__
from .application import create_application, create_controller
from .algorithms.boiler_reset import load_config
from .autonomy.catalog import BOILER_RESET_SPEC
from .autonomy.contracts import AlgorithmCancelled, AlgorithmResult, AlgorithmTimeout, Mode
from .config import ConfigurationError
from .controller.contracts import CommandOutcomeUnknown
from .logging_config import capture_console_output, configure_yc_logging


logger = logging.getLogger(__name__)


def _rotated_notes(notes: str, log: str) -> str:
    previous = json.loads(notes) if notes else []
    if not isinstance(previous, list) or any(not isinstance(item, str) for item in previous):
        raise ValueError('Notes are not a log history')
    return json.dumps([log, *previous[:2]], ensure_ascii=False)


def _response(result: AlgorithmResult) -> dict[str, Any]:
    value = cast(dict[str, Any], result.value)
    return {**value, 'version': __version__, 'algorithm_id': 'boiler_reset',
            'status': result.status.value,
            'state_at_period_end': value['assessment']['state_at_period_end']}


def _log(event, message, request_id, *, level=logging.INFO):
    try:
        logger.log(level, message, extra={'event': event, 'request_id': request_id,
                                          'algorithm_id': 'boiler_reset'})
    except Exception:
        # Diagnostics cannot interrupt application cleanup or expose an upstream error.
        pass


def handler(event: object, context: object) -> dict[str, Any]:
    """Ignore invocation payload: bindings and execution mode come from the profile.

    ZONT_TOKEN is injected by Lockbox into the environment. The existing
    composition validates credentials and fixed configs before network work.
    close() has no wait limit: it joins the worker/finally before transport close,
    even when run_once has already published a timeout or cancellation.
    """
    configure_yc_logging()
    with capture_console_output() as capture:
        report, device_id, previous_notes, notes_error = _invoke(context, capture)
    # The full invocation, including finally and transport shutdown, is now frozen.
    # Publication is separate metadata I/O; it cannot log its own log recursively.
    report.update(_save_notes(device_id, previous_notes, report['log'], notes_error))
    if report['status'] == 'error':
        raise RuntimeError(json.dumps(report, ensure_ascii=False, allow_nan=False)) from None
    return report


def _save_notes(device_id: int | None, previous: str | None, log: str,
                error: str | None) -> dict[str, Any]:
    saved = False
    if error is None:
        try:
            notes = _rotated_notes(previous, log)
        except (ValueError, TypeError, RecursionError):
            error = 'invalid_history'
        else:
            try:
                with create_controller() as controller:
                    try:
                        controller.write_notes(device_id, notes)
                        saved = True
                    except CommandOutcomeUnknown:
                        error = 'write_unknown'
                    except Exception:
                        error = 'write_failed'
            except Exception:
                error = error or ('publication_cleanup_failed' if saved else 'publication_unavailable')
    return {'notes_saved': saved, 'notes_error': error}


def _invoke(context: object, capture: StringIO) -> tuple[dict[str, Any], int | None, str | None, str | None]:
    request_id = getattr(context, 'request_id', None)
    _log('cloud_invocation_started', 'Cloud invocation started', request_id)
    failure, result = None, None
    device_id, previous_notes, notes_error = None, None, 'unavailable'
    try:
        device_id = load_config().device_id
        app = create_application(catalog=(replace(BOILER_RESET_SPEC, modes=frozenset({Mode.ONCE})),))
        try:
            try:
                result = app.run_once('boiler_reset', timeout=300)
            finally:
                # A timeout publishes a result before the worker finishes its off/finally.
                app.stop('boiler_reset', wait_timeout=None)
                try:
                    previous_notes = app.controller().read_notes(device_id)
                    notes_error = None
                except Exception:
                    notes_error = 'read_failed'
        finally:
            app.close()
        response = _response(result)
    except Exception as error:
        failure = ('timeout' if isinstance(error, AlgorithmTimeout) else
                   'cancelled' if isinstance(error, AlgorithmCancelled) else
                   'configuration_error' if isinstance(error, (ConfigurationError, ValueError)) else
                   'execution_failed')
    if failure is not None:
        _log('cloud_invocation_failed', f'Cloud once failed: {failure}', request_id,
             level=logging.ERROR)
        completed_result = None
        if result is not None:
            try:
                completed_result = _response(result)
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                pass
        report = {'version': __version__, 'algorithm_id': 'boiler_reset', 'status': 'error',
                  'error': failure, 'result': completed_result, 'log': capture.getvalue()}
    else:
        _log('cloud_invocation_finished', 'Cloud invocation finished', request_id)
        report = {**response, 'log': capture.getvalue()}
    return report, device_id, previous_notes, notes_error
