"""Credential validation and issuance do not depend on the prototype client."""

import base64
import getpass
import io
import json
import unittest
from unittest.mock import Mock, patch

from zontcontrol.controller.auth import basic_headers, create_token, token_headers
from zontcontrol.controller.contracts import AuthenticationError, CommandOutcomeUnknown, InvalidArguments
from zontcontrol.get_token import main as get_token_main


class AuthTests(unittest.TestCase):
    def test_token_and_basic_headers_are_separate(self):
        headers = token_headers("owner@example.com", "test-token")
        self.assertEqual(headers["X-ZONT-Token"], "test-token")
        self.assertNotIn("Authorization", headers)
        headers = basic_headers("owner@example.com", "user", "password")
        self.assertEqual(headers["Authorization"],
                         "Basic " + base64.b64encode(b"user:password").decode())
        self.assertNotIn("X-ZONT-Token", headers)

    def test_credentials_and_contact_are_validated_without_exposing_values(self):
        cases = [(token_headers, ("bad\r\nsecret", "token")),
                 (token_headers, ("contact", "")), (token_headers, ("contact", None)),
                 (token_headers, ("contact", "secret\n")),
                 (basic_headers, ("contact", "user:secret", "password")),
                 (basic_headers, ("contact", "", "secret")),
                 (basic_headers, ("contact", "user", ""))]
        for function, args in cases:
            with self.subTest(function=function.__name__), self.assertRaises(ValueError) as caught:
                function(*args)
            self.assertNotIn("secret", str(caught.exception))

    def test_issue_token_sends_one_basic_json_post(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(b'{"ok":true,"token":"issued-token"}')
        self.assertEqual(create_token("contact", "user", "password", client_name="my app",
                                      timeout=4, opener=opener), "issued-token")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://my.zont.online/api/widget/v3/authtokens")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"client_name": "my app"})
        self.assertTrue(request.get_header("Authorization").startswith("Basic "))
        self.assertIsNone(request.get_header("X-zont-token"))
        opener.open.assert_called_once_with(request, timeout=4)

    def test_bad_name_fails_before_send_and_malformed_issued_token_is_unknown(self):
        opener = Mock()
        for name in (None, "", " ", 1):
            with self.subTest(name=name), self.assertRaises(InvalidArguments):
                create_token("contact", "user", "password", client_name=name, opener=opener)
        opener.open.assert_not_called()
        for token in (None, "", "  ", 123, "secret\r\n"):
            opener.open.reset_mock()
            opener.open.return_value = io.BytesIO(json.dumps({"ok": True, "token": token}).encode())
            with self.subTest(token=token), self.assertRaises(CommandOutcomeUnknown):
                create_token("contact", "user", "password", opener=opener)
            opener.open.assert_called_once()


class TokenCliTests(unittest.TestCase):
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
