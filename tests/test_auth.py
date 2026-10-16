"""Credential validation and issuance do not depend on the prototype client."""

import base64
import io
import json
import unittest
from unittest.mock import Mock

from zontcontrol.controller.auth import basic_headers, create_token, token_headers
from zontcontrol.controller.contracts import AuthenticationError, CommandOutcomeUnknown, InvalidArguments


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
