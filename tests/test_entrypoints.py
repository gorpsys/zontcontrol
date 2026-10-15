import contextlib
import io
import runpy
import unittest
from unittest.mock import patch

from zontcontrol.application import main


class EntrypointTests(unittest.TestCase):
    def test_help_version_need_no_config_environment_or_network(self):
        for option in ('--help', '--version'):
            with self.subTest(option=option), patch('zontcontrol.application.create_application') as build, \
                    patch('builtins.input') as prompt, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    main([option])
                self.assertEqual(caught.exception.code, 0)
                build.assert_not_called()
                prompt.assert_not_called()

    def test_dispatch_and_close_single_process_application(self):
        for argv, method, args in [(['list'], 'list_algorithms', ()),
                                   (['once', 'sample'], 'run_once', ('sample',)),
                                   (['all'], 'run_all_once', ()),
                                   (['start', 'sample'], 'start', ('sample',)),
                                   (['host'], 'host', ())]:
            with self.subTest(argv=argv), patch('zontcontrol.application.create_application') as build, \
                    contextlib.redirect_stdout(io.StringIO()):
                app = build.return_value.__enter__.return_value
                app.list_algorithms.return_value = ()
                self.assertEqual(main(argv), 0)
                getattr(app, method).assert_called_once_with(*args)
                build.return_value.__exit__.assert_called_once()

    def test_missing_token_safe_instruction(self):
        with patch.dict('os.environ', {}, clear=True), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(['list']), 1)
        self.assertIn('ZONT_TOKEN', err.getvalue())
        self.assertIn('python3 -m zontcontrol.get_token', err.getvalue())

    def test_module_entry_uses_the_same_composition_root(self):
        with patch('zontcontrol.application.main', return_value=0) as entry:
            with self.assertRaises(SystemExit) as caught:
                runpy.run_module('zontcontrol', run_name='__main__')
            self.assertEqual(caught.exception.code, 0)
            entry.assert_called_once_with()

    def test_host_explicit_start_and_shutdown_are_one_application(self):
        with patch('zontcontrol.application.create_application') as build:
            app = build.return_value.__enter__.return_value
            self.assertEqual(main(['host', '--start', 'sample']), 0)
            app.start.assert_called_once_with('sample')
            app.host.assert_called_once_with()
            build.return_value.__exit__.assert_called_once()

    def test_arbitrary_failure_text_is_not_printed_and_all_errors_are_nonzero(self):
        with patch('zontcontrol.application.create_application', side_effect=OSError('fake-secret')), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(['list']), 1)
            self.assertNotIn('fake-secret', err.getvalue())
        with patch('zontcontrol.application.create_application') as build, \
                contextlib.redirect_stdout(io.StringIO()):
            build.return_value.__enter__.return_value.run_all_once.return_value = {'sample': ValueError()}
            self.assertEqual(main(['all']), 1)

    def test_removed_flags_and_remote_stop_are_rejected_before_composition(self):
        for argv in (['--history'], ['--config', 'x'], ['stop', 'sample'], ['--execute']):
            with self.subTest(argv=argv), patch('zontcontrol.application.create_application') as build, \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    main(argv)
                self.assertEqual(caught.exception.code, 2)
                build.assert_not_called()
