"""
The one HTTP path the cloud client takes.

Built on `urllib.request` so that installing `boxlite` still pulls in nothing:
the package declares no runtime dependencies, and a catalog client is not worth
the first one. Every method here blocks; the async face wraps it with
`asyncio.to_thread` rather than keeping a second implementation, so the two
faces cannot answer differently.
"""

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .errors import CloudConnectionError, CloudError, error_from_response

__all__ = ["Transport"]

#: Where the API lives, shared with `BoxliteRestOptions.from_env()` so one
#: deployment needs one address. It already includes the `/api` prefix.
BASE_URL_ENV = "BOXLITE_REST_URL"
API_KEY_ENV = "BOXLITE_API_KEY"

DEFAULT_TIMEOUT_SECONDS = 30.0

#: The server refuses anything longer, and a value this size is a probe rather
#: than a name. Checked here so it never reaches a URL.
MAX_SEGMENT_LENGTH = 512


def _require(value: str | None, env: str, what: str) -> str:
    if value:
        return value
    from_env = os.environ.get(env)
    if from_env:
        return from_env
    raise ValueError(f"{what} is required: pass it explicitly or set {env}")


def quote_segment(value: str) -> str:
    """
    Percent-encode one path segment, refusing what should never be sent.

    A reference contains slashes, so it has to be encoded whole rather than
    joined — `quay.io/acme/app` is one segment, not three. The refusals happen
    before anything is built: a traversal that reached the URL would be a
    request this client had no reason to make, and the server rejecting it is
    one round trip too late to be the only guard.
    """
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(
            "Image reference must be a non-empty string without surrounding whitespace"
        )
    if len(value) > MAX_SEGMENT_LENGTH:
        raise ValueError(f"Image reference exceeds {MAX_SEGMENT_LENGTH} characters")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError("Image reference must not contain control characters")
    # Any `..` component, not just a leading one: `quay.io/acme/..` is as
    # much a traversal as `../etc`, and the check reads as one rule rather
    # than a list of spellings to keep extending.
    if value.startswith("/") or any(
        part == ".." for part in value.replace("\\", "/").split("/")
    ):
        raise ValueError(f"Image reference '{value}' is not a valid name")
    return urllib.parse.quote(value, safe="")


def _base_url_of(value: str) -> str:
    """
    The base URL, checked for the one thing that matters here.

    `urlopen` honours whatever scheme it is given, so a `file://` or
    `ftp://` base would make this client read a path or reach a host the
    caller never meant — with the key attached. `http` is allowed because
    a local stack serves it; anything outside the pair is refused.
    """
    scheme = urllib.parse.urlparse(value).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"Base URL must be http or https, got {scheme or 'no scheme'}")
    return value.rstrip("/")


class Transport:
    """Issues requests and turns refusals into typed errors."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._api_key = _require(api_key, API_KEY_ENV, "An API key")
        self._base_url = _base_url_of(_require(base_url, BASE_URL_ENV, "A base URL"))
        self._timeout = timeout

    @property
    def base_url(self) -> str:
        return self._base_url

    def request(self, method: str, path: str) -> Any:
        return self._send(method, path)[1]

    def request_list(self, method: str, path: str) -> list[Any]:
        """
        The same request, for a route whose answer is a list.

        The check lives here rather than in the caller because this is where
        the status is still in scope: a caller that found the wrong shape
        could only report `status=None`, which this package uses to mean the
        request never got a response.
        """
        status, body = self._send(method, path)
        if not isinstance(body, list):
            raise CloudError(
                f"Expected a list, got {type(body).__name__}", status=status
            )
        return body

    def _send(self, method: str, path: str) -> tuple[int, Any]:
        url = f"{self._base_url}/{path.lstrip('/')}"

        request = urllib.request.Request(url, method=method)
        # The key carries the organization, so no organization header is sent.
        # Sending one would invite a caller to think it selects the
        # organization, when the key is what decides.
        #
        # Unredirected because urllib copies `headers` onto a 30x target and
        # leaves `unredirected_hdrs` behind: this is what keeps the key from
        # following a redirect to a host that was never asked for.
        request.add_unredirected_header("Authorization", f"Bearer {self._api_key}")
        request.add_header("Accept", "application/json")

        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return response.status, _decode(response.status, response.read())
        except urllib.error.HTTPError as error:
            body = _load_json(error.read())
            raise error_from_response(
                error.code,
                body if isinstance(body, dict) else None,
                retry_after=_retry_after(error.headers.get("Retry-After")),
            ) from error
        except urllib.error.URLError as error:
            raise CloudConnectionError(
                f"Could not reach the cloud API at {self._base_url}: {error.reason}"
            ) from error


def _decode(status: int, payload: bytes) -> Any:
    # 204 is what a delete answers with, and an empty body is not a parse
    # failure — it is the whole answer.
    if status == 204 or not payload:
        return None
    try:
        return json.loads(payload)
    except ValueError as error:
        # A refusal that cannot be read already becomes a typed failure. A
        # success that cannot be read is the same thing to a caller, and a
        # bare JSONDecodeError is outside every name this package documents.
        raise CloudError(
            f"Could not read the server's answer: {error}", status=status
        ) from error


def _load_json(payload: bytes) -> Any:
    try:
        return json.loads(payload)
    except (ValueError, TypeError):
        # A refusal that is not JSON still has a status, and the status is what
        # the mapping runs on. Losing the body loses the message, not the class.
        return None


def _retry_after(header: str | None) -> float | None:
    if not header:
        return None
    try:
        seconds = float(header)
    except ValueError:
        # Only the delta-seconds form is understood. An HTTP-date is valid but
        # nothing here sends one, and half-parsing it would invent a deadline.
        return None
    # `float()` also accepts "-1", "inf" and "nan". None says "no usable
    # hint", which is what those are; a caller sleeping on them would not.
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds
