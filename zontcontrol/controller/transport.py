"""Internal bounded HTTP transport. No retries and no upstream error text.

urllib does not expose an HTTP-send acknowledgement. Only known connection
failures are classified NOT_SENT; timeouts and other ambiguous failures remain
UNCONFIRMED. A failure while reading is always after the request was opened.
"""

import json
import math
import socket
import ssl
from enum import Enum
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


API_URL = "https://my.zont.online/api/widget/v3"
FULL_API_URL = "https://my.zont.online/api"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class FailureKind(Enum):
    AUTHENTICATION = "authentication"
    RATE_LIMITED = "rate_limited"
    REJECTED = "rejected"
    NOT_SENT = "not_sent"
    UNCONFIRMED = "unconfirmed"
    INVALID_RESPONSE = "invalid_response"


class TransportError(Exception):
    """Structured outcome containing neither credentials nor response bodies."""

    def __init__(self, kind: FailureKind):
        self.kind = kind
        messages = {
            FailureKind.AUTHENTICATION: "ZONT authentication failed.",
            FailureKind.RATE_LIMITED: "ZONT API rate limit reached.",
            FailureKind.REJECTED: "ZONT rejected the request.",
            FailureKind.NOT_SENT: "ZONT request was not sent.",
            FailureKind.UNCONFIRMED: "ZONT request outcome is unconfirmed.",
            FailureKind.INVALID_RESPONSE: "ZONT returned an invalid response.",
        }
        super().__init__(messages[kind])


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpTransport:
    def __init__(self, *, timeout=15, opener=None):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError("timeout must be between 0 and 120 seconds")
        self._timeout = timeout
        self._opener = opener if opener is not None else build_opener(NoRedirect())

    def request(self, request: Request) -> dict:
        # Fixed origin: credentials must never be sent to an arbitrary URL.
        try:
            url = urlsplit(request.full_url)
            trusted = (url.scheme == "https" and url.netloc == "my.zont.online"
                       and url.path.startswith("/api/") and not url.fragment)
        except (ValueError, AttributeError):
            trusted = False
        if not trusted:
            raise TransportError(FailureKind.NOT_SENT)
        try:
            response = self._opener.open(request, timeout=self._timeout)
        except HTTPError as error:
            status = error.code
            error.close()
            kind = {401: FailureKind.AUTHENTICATION, 403: FailureKind.AUTHENTICATION,
                    429: FailureKind.RATE_LIMITED}.get(status)
            if kind is None:
                kind = FailureKind.UNCONFIRMED if status >= 500 else FailureKind.REJECTED
            raise TransportError(kind) from None
        except (OSError, HTTPException) as error:
            reason = error.reason if isinstance(error, URLError) else error
            kind = (FailureKind.NOT_SENT if isinstance(reason, (
                socket.gaierror, ConnectionRefusedError, ssl.SSLCertVerificationError,
            )) else FailureKind.UNCONFIRMED)
            raise TransportError(kind) from None
        except ValueError:
            # http.client rejects invalid headers before sending any bytes.
            raise TransportError(FailureKind.NOT_SENT) from None
        try:
            with response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, HTTPException):
            raise TransportError(FailureKind.UNCONFIRMED) from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise TransportError(FailureKind.INVALID_RESPONSE)
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise TransportError(FailureKind.INVALID_RESPONSE) from None
        if not isinstance(data, dict) or type(data.get("ok")) is not bool:
            raise TransportError(FailureKind.INVALID_RESPONSE)
        if data["ok"] is False:
            raise TransportError(FailureKind.REJECTED)
        return data
