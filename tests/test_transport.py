"""Offline transport boundaries: one attempt, finite I/O and safe failures."""

import io
import socket
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError, URLError
from urllib.request import Request

from zontcontrol.controller.transport import (
    FailureKind, HttpTransport, MAX_RESPONSE_BYTES, NoRedirect, TransportError,
)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.opener = Mock()
        self.transport = HttpTransport(timeout=3, opener=self.opener)
        self.request = Request("https://my.zont.online/api/devices", data=b"{}")

    def assert_failure(self, error, kind):
        self.opener.open.reset_mock()
        self.opener.open.side_effect = error
        with self.assertRaises(TransportError) as caught:
            self.transport.request(self.request)
        self.assertIs(caught.exception.kind, kind)
        self.assertNotIn("secret", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.opener.open.assert_called_once_with(self.request, timeout=3)

    def test_json_read_and_finite_timeout(self):
        self.opener.open.return_value = io.BytesIO(b'{"ok":true,"value":0}')
        self.assertEqual(self.transport.request(self.request), {"ok": True, "value": 0})
        self.opener.open.assert_called_once_with(self.request, timeout=3)
        for timeout in (0, -1, True, None, "3", float("nan"), float("inf"), 121):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                HttpTransport(timeout=timeout)

    def test_http_errors_are_typed_without_response_body_or_credentials(self):
        for status, kind in ((401, FailureKind.AUTHENTICATION),
                             (403, FailureKind.AUTHENTICATION),
                             (429, FailureKind.RATE_LIMITED),
                             (400, FailureKind.REJECTED), (302, FailureKind.REJECTED),
                             (500, FailureKind.UNCONFIRMED)):
            with self.subTest(status=status):
                body = io.BytesIO(b"secret")
                self.assert_failure(HTTPError("secret", status, "secret", {}, body), kind)
                self.assertTrue(body.closed)

    def test_proven_connect_failure_and_ambiguous_timeout_are_distinct(self):
        for reason in (socket.gaierror("secret"), ConnectionRefusedError("secret")):
            self.assert_failure(URLError(reason), FailureKind.NOT_SENT)
        for error in (TimeoutError("secret"), URLError("secret"), OSError("secret")):
            self.assert_failure(error, FailureKind.UNCONFIRMED)
        self.assert_failure(ValueError("secret"), FailureKind.NOT_SENT)

    def test_read_failure_after_open_is_never_classed_as_pre_send(self):
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        stream.read.side_effect = ConnectionRefusedError("secret")
        self.opener.open.return_value = stream
        with self.assertRaises(TransportError) as caught:
            self.transport.request(self.request)
        self.assertIs(caught.exception.kind, FailureKind.UNCONFIRMED)

    def test_invalid_and_oversized_response_are_distinct_from_explicit_rejection(self):
        for raw, kind in ((b"secret", FailureKind.INVALID_RESPONSE),
                          (b"[]", FailureKind.INVALID_RESPONSE),
                          (b'{"ok":1}', FailureKind.INVALID_RESPONSE),
                          (b'{"ok":false,"error":"secret"}', FailureKind.REJECTED),
                          (b"x" * (MAX_RESPONSE_BYTES + 1), FailureKind.INVALID_RESPONSE)):
            with self.subTest(kind=kind):
                self.opener.open.return_value = io.BytesIO(raw)
                with self.assertRaises(TransportError) as caught:
                    self.transport.request(self.request)
                self.assertIs(caught.exception.kind, kind)
                self.assertNotIn("secret", str(caught.exception))

    def test_reject_untrusted_url_before_network(self):
        for url in ("http://my.zont.online/api/devices", "https://other.example/api/devices",
                    "https://my.zont.online@other.example/api/devices"):
            with self.subTest(url=url), self.assertRaises(TransportError) as caught:
                self.transport.request(Request(url))
            self.assertIs(caught.exception.kind, FailureKind.NOT_SENT)
        self.opener.open.assert_not_called()

    def test_default_opener_refuses_redirects(self):
        handlers = HttpTransport()._opener.handlers
        self.assertTrue(any(isinstance(handler, NoRedirect) for handler in handlers))
        self.assertIsNone(NoRedirect().redirect_request(
            None, None, 302, "", {}, "https://other.example"))
