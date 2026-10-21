"""Logging uses stdlib, explicit entrypoint configuration and safe metadata."""

import contextlib
import io
import json
import logging
import sys
from threading import Barrier, Thread
from datetime import datetime
import unittest

from zontcontrol.logging_config import capture_console_output, configure_local_logging, configure_yc_logging


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
            self.assertEqual(set(row), {'timestamp', 'message', 'level', 'logger', 'event', 'algorithm_id',
                'device_id', 'state', 'reset_performed', 'elapsed_ms', 'request_id'})
            self.assertEqual(datetime.fromisoformat(row['timestamp']).utcoffset().total_seconds(), 0)
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
        row = json.loads(output.getvalue())
        self.assertIsNotNone(datetime.fromisoformat(row.pop('timestamp')).utcoffset())
        self.assertEqual(row,
                         {'message': 'safe', 'level': 'INFO', 'logger': 'zontcontrol'})

    def test_capture_matches_stdout_and_stops_without_retaining_invocation(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            configure_yc_logging()
            with capture_console_output() as capture:
                self.logger.info('first', extra={'token': 'private'})
                records = capture.getvalue()
            self.logger.info('after response')
        self.assertEqual(records, output.getvalue().splitlines(keepends=True)[0])
        self.assertTrue(capture.closed)
        self.assertNotIn('private', json.dumps(records))

    def test_warm_invocation_capture_is_new_and_default_does_not_collect(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            configure_yc_logging()
            with capture_console_output() as first:
                self.logger.info('first')
            with capture_console_output() as second:
                self.logger.info('second')
                self.assertEqual(json.loads(second.getvalue())['message'], 'second')
            self.assertTrue(first.closed)
            self.assertTrue(second.closed)
            configure_yc_logging()
            self.logger.info('plain')

    def test_arbitrary_logger_and_formatter_output_are_copied_exactly(self):
        root = logging.getLogger()
        root_state = root.handlers[:], root.level
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            handler = configure_yc_logging()
            handler.setFormatter(logging.Formatter('%(name)s %(message)s %(new_field)s'))
            with capture_console_output() as capture:
                self.logger.info('any application message', extra={'new_field': 'another value'})
                rendered = capture.getvalue()
        self.assertEqual(rendered, output.getvalue())
        self.assertEqual(rendered, 'zontcontrol any application message another value\n')
        self.assertEqual((root.handlers, root.level), root_state)

    def test_tee_preserves_write_counts_and_flush_and_combines_console_streams(self):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            with capture_console_output() as capture:
                self.assertEqual(sys.stdout.write('stdout'), 6)
                sys.stdout.flush()
                self.assertEqual(sys.stderr.write('stderr'), 6)
                sys.stderr.flush()
                self.assertEqual(capture.getvalue(), 'stdoutstderr')
            self.assertIs(sys.stdout, output)
            self.assertIs(sys.stderr, errors)
        self.assertEqual(output.getvalue(), 'stdout')
        self.assertEqual(errors.getvalue(), 'stderr')

    def test_concurrent_independent_loggers_keep_console_and_capture_in_same_order(self):
        output = io.StringIO()
        start = Barrier(3)
        loggers = [logging.getLogger(f'parallel_output.{name}') for name in ('first', 'second')]
        for logger in loggers:
            previous = logger.handlers[:], logger.level, logger.propagate
            self.addCleanup(setattr, logger, 'handlers', previous[0])
            self.addCleanup(setattr, logger, 'level', previous[1])
            self.addCleanup(setattr, logger, 'propagate', previous[2])
            logger.handlers = [logging.StreamHandler(output)]
            logger.setLevel(logging.INFO)
            logger.propagate = False
        def write(logger, prefix):
            start.wait(3)
            for index in range(1000):
                logger.info('%s%s', prefix, index)
        previous_interval = sys.getswitchinterval()
        self.addCleanup(sys.setswitchinterval, previous_interval)
        sys.setswitchinterval(1e-6)
        with contextlib.redirect_stdout(output), capture_console_output() as capture:
            workers = [Thread(target=write, args=(logger, prefix))
                       for logger, prefix in zip(loggers, ('A', 'B'))]
            for worker in workers:
                worker.start()
            start.wait(3)
            for worker in workers:
                worker.join(3)
                self.assertFalse(worker.is_alive())
            rendered = capture.getvalue()
        self.assertEqual(len(output.getvalue().splitlines()), 2000)
        self.assertEqual(rendered, output.getvalue())
