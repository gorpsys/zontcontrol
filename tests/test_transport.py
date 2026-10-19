"""Offline transport boundaries: one attempt, finite I/O and safe failures."""

import io
import socket
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock
from urllib.error import HTTPError, URLError
from urllib.request import Request

from zontcontrol.controller.transport import (
    FailureKind, HttpTransport, MAX_RESPONSE_BYTES, NoRedirect, TransportError,
)
from tests.fakes.clock import FakeClock


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


    def test_per_call_timeout_is_local_to_concurrent_requests(self):
        barrier = Barrier(2)
        observed = []

        def open_response(request, *, timeout):
            observed.append(timeout)
            barrier.wait(timeout=2)
            return io.BytesIO(b'{"ok":true}')

        self.opener.open.side_effect = open_response
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.transport.request, self.request, timeout_seconds=budget)
                       for budget in (1, 2)]
            self.assertEqual([f.result(timeout=3) for f in futures], [{"ok": True}] * 2)
        self.assertCountEqual(observed, [1, 2])
        self.opener.open.side_effect = None
        self.opener.open.return_value = io.BytesIO(b'{"ok":true}')
        self.transport.request(self.request)
        self.assertEqual(self.opener.open.call_args.kwargs["timeout"], 3)

    def test_complete_response_including_body_and_suspend_must_fit_budget(self):
        for elapsed, succeeds in ((2, True), (2.01, False)):
            with self.subTest(elapsed=elapsed):
                clock = FakeClock()
                transport = HttpTransport(timeout=3, opener=self.opener, clock=clock)

                class SlowBody(io.BytesIO):
                    def read(self, size):
                        clock.advance(elapsed - 1)
                        return super().read(size)

                def open_response(request, *, timeout):
                    clock.advance(1)
                    return SlowBody(b'{"ok":true}')

                self.opener.open.side_effect = open_response
                if succeeds:
                    self.assertEqual(transport.request(self.request, timeout_seconds=2),
                                     {"ok": True})
                else:
                    with self.assertRaises(TransportError) as caught:
                        transport.request(self.request, timeout_seconds=2)
                    self.assertIs(caught.exception.kind, FailureKind.UNCONFIRMED)

    def test_invalid_per_call_timeout_does_not_open_request(self):
        for timeout in (-1, True, "2", float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.transport.request(self.request, timeout_seconds=timeout)
        self.opener.open.assert_not_called()

    def test_open_and_body_share_one_fixed_deadline(self):
        clock = FakeClock()
        transport = HttpTransport(timeout=3, opener=self.opener, clock=clock)
        class Body(io.BytesIO):
            def read(self, *args):
                clock.advance(.3)
                return super().read(*args)
        def opened(*args, **kwargs):
            clock.advance(.75)
            return Body(b'{"ok":true}')
        self.opener.open.side_effect = opened
        with self.assertRaises(TransportError) as caught:
            transport.request(self.request, timeout_seconds=1)
        self.assertIs(caught.exception.kind, FailureKind.UNCONFIRMED)
        self.opener.open.assert_called_once_with(self.request, timeout=1)

    def test_no_body_io_starts_when_open_has_consumed_the_window(self):
        clock = FakeClock()
        transport = HttpTransport(timeout=3, opener=self.opener, clock=clock)
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)

        def open_response(request, *, timeout):
            clock.advance(timeout)
            return stream

        self.opener.open.side_effect = open_response
        with self.assertRaises(TransportError) as caught:
            transport.request(self.request, timeout_seconds=1)
        self.assertIs(caught.exception.kind, FailureKind.UNCONFIRMED)
        stream.read.assert_not_called()
        stream.__exit__.assert_called_once()

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
                          (b'[' * 2000 + b']' * 2000, FailureKind.INVALID_RESPONSE),
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
