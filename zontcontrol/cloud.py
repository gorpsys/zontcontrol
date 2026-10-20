"""One ordinary application per cloud invocation, with cooperative cleanup."""

from dataclasses import replace
import logging

from . import __version__
from .application import create_application
from .autonomy.catalog import BOILER_RESET_SPEC
from .autonomy.contracts import AlgorithmCancelled, AlgorithmTimeout, Mode
from .config import ConfigurationError
from .logging_config import configure_yc_logging


logger = logging.getLogger(__name__)
_STATES = frozenset({'normal', 'warning', 'confirmed_pattern', 'unknown', 'suppressed'})
_REASONS = frozenset({'cancelled', 'already_on', 'current_reset_unknown'})


def _response(result):
    value = result.value
    state = value['assessment']['state_at_period_end']
    reason = value['reason']
    if (state not in _STATES or reason is not None and reason not in _REASONS
            or type(value['reset_performed']) is not bool):
        raise ValueError('Invalid boiler_reset result')
    return {'version': __version__, 'algorithm_id': 'boiler_reset',
            'status': result.status.value, 'state_at_period_end': state,
            'reset_performed': value['reset_performed'], 'reason': reason}


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
    configure_yc_logging()
    request_id = getattr(context, 'request_id', None)
    _log('cloud_invocation_started', 'Cloud invocation started', request_id)
    failure = None
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
        raise RuntimeError(f'Cloud once failed: {failure}') from None
    _log('cloud_invocation_finished', 'Cloud invocation finished', request_id)
    return response
