"""Logging uses stdlib, explicit entrypoint configuration and safe metadata."""

import contextlib
import io
import json
import logging
import unittest

from zontcontrol.logging_config import configure_local_logging, configure_yc_logging


class LoggingConfigTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger('zontcontrol')
        previous = (self.logger.handlers[:], self.logger.level, self.logger.propagate)
        self.logger.handlers = []
        def restore():
            for handler in self.logger.handlers:
                handler.close()
            self.logger.handlers, self.logger.level, self.logger.propagate = previous
        self.addCleanup(restore)

    def test_cloud_stdout_levels_unicode_and_safe_fields_without_exception_details(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            configure_yc_logging()
            for level in (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR,
                          logging.CRITICAL):
                try:
                    raise RuntimeError('private-exception-payload')
                except RuntimeError:
                    self.logger.log(level, 'Проверка котла', exc_info=True, extra={
                        'event': 'once_finished', 'algorithm_id': 'boiler_reset',
                        'device_id': 42, 'state': 'unknown', 'reset_performed': False,
                        'elapsed_ms': 25, 'request_id': 'req-123',
                        'token': 'private-token', 'payload': {'secret': 'private-payload'}})
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([row['level'] for row in rows], ['INFO', 'WARN', 'ERROR', 'FATAL'])
        self.assertIn('Проверка котла', output.getvalue())
        self.assertNotIn('private', output.getvalue())
        for row in rows:
            self.assertEqual(set(row), {'message', 'level', 'logger', 'event', 'algorithm_id',
                'device_id', 'state', 'reset_performed', 'elapsed_ms', 'request_id'})
            self.assertEqual(row['logger'], 'zontcontrol')
            self.assertIs(row['reset_performed'], False)

    def test_configuration_is_idempotent_preserves_other_handlers_and_root(self):
        foreign = logging.NullHandler()
        self.logger.addHandler(foreign)
        root = logging.getLogger()
        root_state = (root.handlers[:], root.level)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            configure_yc_logging()
            configure_yc_logging()
            self.logger.info('one')
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertEqual(len(self.logger.handlers), 2)
        self.assertIs(self.logger.handlers[0], foreign)
        self.assertEqual((root.handlers, root.level), root_state)
        self.assertEqual(self.logger.level, logging.INFO)
        self.assertFalse(self.logger.propagate)

    def test_local_text_and_switch_to_cloud_leave_one_owned_handler(self):
        local, cloud = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(local):
            configure_local_logging()
            configure_local_logging()
            self.logger.warning('Локальное событие', extra={'event': 'once_started'})
        self.assertEqual(len(local.getvalue().splitlines()), 1)
        self.assertIn('Локальное событие', local.getvalue())
        self.assertIn('WARN', local.getvalue())
        with contextlib.redirect_stdout(cloud):
            configure_yc_logging()
            self.logger.info('Облачное событие')
        self.assertEqual(len(self.logger.handlers), 1)
        self.assertEqual(json.loads(cloud.getvalue())['message'], 'Облачное событие')

    def test_unknown_extra_objects_and_invalid_metadata_are_not_serialized(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            configure_yc_logging()
            self.logger.info('safe', extra={'event': object(), 'request_id': 'private\npayload',
                'algorithm_id': [], 'device_id': True, 'state': {},
                'reset_performed': 'private', 'elapsed_ms': float('nan'), 'config': object()})
        self.assertEqual(json.loads(output.getvalue()),
                         {'message': 'safe', 'level': 'INFO', 'logger': 'zontcontrol'})
