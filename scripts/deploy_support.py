"""Bounded stdlib JSON transport and fixed diagnostics for deployment helpers."""

from http.client import HTTPException
import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_RESPONSE_BYTES = 2_000_000
HTTP_TIMEOUT = 15


class DeployError(Exception):
    """Only fixed local diagnostics; never provider bodies, arguments or credentials."""


class NoRedirect(HTTPRedirectHandler):
    """Do not forward credential-bearing requests to another origin."""

    def redirect_request(self, req: Request, fp: object, code: int, msg: str,
                         headers: object, newurl: str) -> None:
        return None


def request_json(request: Request, *, stage: str = 'HTTP request') -> object:
    try:
        with build_opener(NoRedirect()).open(request, timeout=HTTP_TIMEOUT) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise DeployError(f'{stage} response exceeds size limit')
        return json.loads(raw)
    except HTTPError as error:
        raise DeployError(f'{stage} failed (status {error.code})') from None
    except (HTTPException, OSError, URLError, ValueError):
        raise DeployError(f'{stage} or JSON response failed') from None


def record(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise DeployError('expected a JSON object')
    return value


def text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise DeployError(f'invalid {label}')
    return value


def commit_sha(value: object) -> str:
    result = text(value, 'commit SHA')
    if re.fullmatch('[0-9a-f]{40}', result) is None or result == '0' * 40:
        raise DeployError('invalid commit SHA')
    return result
