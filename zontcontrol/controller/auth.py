"""Credential headers and explicit token issuance, independent of algorithms."""

import base64
import json
from urllib.request import Request

from .contracts import CommandOutcomeUnknown, InvalidArguments
from .transport import API_URL, HttpTransport, TransportError
from .zont import raise_controller_error


def _header_value(value, name):
    if (not isinstance(value, str) or not value.strip()
            or any(ord(char) < 32 or ord(char) > 126 for char in value)):
        raise ValueError(f"{name} must contain nonempty printable ASCII text")
    return value


def _headers(contact):
    return {"X-ZONT-Client": _header_value(contact, "contact"), "Accept": "application/json"}


def token_headers(contact: str, token: str) -> dict[str, str]:
    return {**_headers(contact), "X-ZONT-Token": _header_value(token, "token")}


def basic_headers(contact: str, login: str, password: str) -> dict[str, str]:
    if (not isinstance(login, str) or not login or ":" in login
            or not isinstance(password, str) or not password):
        raise ValueError("login and password are required; login cannot contain ':'")
    credentials = base64.b64encode(f"{login}:{password}".encode()).decode("ascii")
    return {**_headers(contact), "Authorization": f"Basic {credentials}"}


def create_token(contact: str, login: str, password: str, *, client_name="zontcontrol",
                 timeout=15, opener=None) -> str:
    """Create once; ambiguous failure may have issued a token, so never retry."""
    if not isinstance(client_name, str) or not client_name.strip():
        raise InvalidArguments()
    headers = basic_headers(contact, login, password)
    request = Request(f"{API_URL}/authtokens",
                      data=json.dumps({"client_name": client_name}).encode("utf-8"),
                      headers={**headers, "Content-Type": "application/json"}, method="POST")
    try:
        data = HttpTransport(timeout=timeout, opener=opener).request(request)
    except TransportError as error:
        raise_controller_error(error, write=True)
    try:
        return _header_value(data.get("token"), "token")
    except ValueError:
        raise CommandOutcomeUnknown() from None
