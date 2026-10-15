"""Offline token utility: closed password input and dedicated auth DI."""

import getpass
import io
import unittest
from unittest.mock import patch

from zontcontrol.controller.contracts import AuthenticationError
from zontcontrol.get_token import main as get_token_main


class TokenUtilityTests(unittest.TestCase):
    @patch("zontcontrol.get_token.create_token")
    @patch("zontcontrol.get_token.getpass.getpass", return_value="password-secret")
    @patch("builtins.input", return_value="user")
    def test_prompts_for_credentials_and_prints_only_token(self, login, password, client):
        client.return_value = "issued-token"
        with patch("sys.argv", ["zontcontrol.get_token", "--contact", "owner@example.com"]), \
                patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(get_token_main(), 0)
        self.assertEqual(stdout.getvalue(), "issued-token\n")
        self.assertIn("Логин ZONT:", stderr.getvalue())
        self.assertNotIn("issued-token", stderr.getvalue())
        self.assertNotIn("password-secret", stdout.getvalue() + stderr.getvalue())
        password.assert_called_once_with("Пароль ZONT: ")
        login.assert_called_once_with()
        client.assert_called_once_with("owner@example.com", "user", "password-secret",
                                       client_name="zontcontrol", timeout=15)

    @patch("zontcontrol.get_token.create_token")
    @patch("zontcontrol.get_token.getpass.getpass", return_value="password-secret")
    @patch("builtins.input", side_effect=["owner@example.com", "user"])
    def test_contact_prompt_and_custom_name(self, login, password, client):
        client.return_value = "issued-token"
        with patch.dict("os.environ", {}, clear=True), \
                patch("sys.argv", ["zontcontrol.get_token", "--client-name", "my app"]), \
                patch("sys.stdout", new_callable=io.StringIO), \
                patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(get_token_main(), 0)
        self.assertEqual(login.call_count, 2)
        client.assert_called_once_with("owner@example.com", "user", "password-secret",
                                       client_name="my app", timeout=15)

    @patch("zontcontrol.get_token.create_token")
    @patch("builtins.input", return_value="user")
    def test_cancellation_does_not_print_token(self, login, client):
        for error in [EOFError(), KeyboardInterrupt()]:
            with self.subTest(error=type(error).__name__), \
                    patch("sys.argv", ["zontcontrol.get_token", "--contact", "owner@example.com"]), \
                    patch("zontcontrol.get_token.getpass.getpass", side_effect=error), \
                    patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                    patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(get_token_main(), 1)
                self.assertEqual(stdout.getvalue(), "")
        client.assert_not_called()

    @patch("zontcontrol.get_token.create_token")
    @patch("builtins.input", return_value="user")
    @patch("zontcontrol.get_token.getpass.getpass", return_value="password-secret")
    def test_api_failure_returns_nonzero_and_empty_stdout(self, password, login, client):
        client.side_effect = AuthenticationError()
        with patch("sys.argv", ["zontcontrol.get_token", "--contact", "owner@example.com"]), \
                patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(get_token_main(), 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn(AuthenticationError.message, stderr.getvalue())
            self.assertNotIn("password-secret", stderr.getvalue())

    @patch("zontcontrol.get_token.create_token")
    @patch("builtins.input", return_value="user")
    def test_password_plaintext_fallback_is_refused(self, login, client):
        import warnings

        def plaintext_fallback(*args, **kwargs):
            warnings.warn("no terminal", getpass.GetPassWarning)
            self.fail("plaintext fallback was reached")

        with patch("sys.argv", ["zontcontrol.get_token", "--contact", "owner@example.com"]), \
                patch("zontcontrol.get_token.getpass.getpass", side_effect=plaintext_fallback), \
                patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(get_token_main(), 1)
            self.assertEqual(stdout.getvalue(), "")
        client.assert_not_called()
