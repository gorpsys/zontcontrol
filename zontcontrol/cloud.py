"""One ordinary application per cloud invocation, with cooperative cleanup."""

from dataclasses import replace
import json
import logging

from . import __version__
from .application import create_application
from .autonomy.catalog import BOILER_RESET_SPEC
from .autonomy.contracts import AlgorithmCancelled, AlgorithmTimeout, Mode
from .config import ConfigurationError
from .logging_config import configure_yc_logging


logger = logging.getLogger(__name__)


def _response(result):
    return {**result.value, 'version': __version__, 'algorithm_id': 'boiler_reset',
            'status': result.status.value,
            'state_at_period_end': result.value['assessment']['state_at_period_end']}


def _log(event, message, request_id, *, level=logging.INFO):
    try:
        logger.log(level, message, extra={'event': event, 'request_id': request_id,
                                          'algorithm_id': 'boiler_reset'})
    except Exception:
        # Diagnostics cannot interrupt application cleanup or expose an upstream error.
        pass


def handler(event, context):
    """Ignore invocation payload: bindings and execution mode come from the profile.

    ZONT_TOKEN is injected by Lockbox into the environment. The existing
    composition validates credentials and fixed configs before network work.
    close() has no wait limit: it joins the worker/finally before transport close,
    even when run_once has already published a timeout or cancellation.
    """
    capture = configure_yc_logging(capture=True)
    try:
        return _invoke(context, capture)
    finally:
        # Includes timeout, cancellation and exceptions during report preparation.
        capture.stop_capture()


def _invoke(context, capture):
    request_id = getattr(context, 'request_id', None)
    _log('cloud_invocation_started', 'Cloud invocation started', request_id)
    failure, result = None, None
    try:
        app = create_application(catalog=(replace(BOILER_RESET_SPEC, modes=frozenset({Mode.ONCE})),))
        try:
            result = app.run_once('boiler_reset', timeout=300)
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
                  'error': failure, 'result': completed_result, 'log': capture.stop_capture()}
        raise RuntimeError(json.dumps(report, ensure_ascii=False, allow_nan=False)) from None
    _log('cloud_invocation_finished', 'Cloud invocation finished', request_id)
    response['log'] = capture.stop_capture()
    return response
