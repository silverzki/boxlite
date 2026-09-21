"""
Failures the cloud API reports.

The mapping is driven by the HTTP status, and only refined by the `code` the
body may carry. That order is the point: every cloud exception sets a status,
while most set no code at all, so a mapping led by `code` would have nothing to
work with on the common path. A body without a code leaves `code` as None — it
is never filled in with a guess, because a synthesized code reads exactly like
one the server chose, and the reader has no way to tell them apart.
"""

from __future__ import annotations

__all__ = [
    "AuthenticationError",
    "CloudConnectionError",
    "CloudError",
    "ImageInUse",
    "ImageLimitExceeded",
    "ImageNotFound",
    "InvalidRequest",
    "PermissionDenied",
    "RateLimited",
    "ServerError",
]

from ..errors import BoxliteError


class CloudError(BoxliteError):
    """
    Base for every cloud API failure.

    Attributes:
        message: What the server said, or what went wrong reaching it.
        status: HTTP status, or None when the request never got a response.
        code: The server's machine-readable code, when it named one.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
    ) -> None:
        self.message = message
        self.status = status
        self.code = code
        super().__init__(message)


class CloudConnectionError(CloudError):
    """The request never reached the server, so there is no status to report."""


class InvalidRequest(CloudError):
    """The server refused the request as malformed or not allowed."""


class ImageLimitExceeded(InvalidRequest):
    """This organization already holds as many images as it may keep."""


class AuthenticationError(CloudError):
    """The credential was missing, expired or not recognised."""


class PermissionDenied(CloudError):
    """The credential is valid but does not carry the scope this call needs."""


class ImageNotFound(CloudError):
    """No such image in this organization. Also what a second delete reports."""


class ImageInUse(CloudError):
    """Boxes that have not been destroyed can still boot from this image."""


class RateLimited(CloudError):
    """Too many requests. `retry_after` is the server's hint, in seconds."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        self.retry_after = retry_after
        super().__init__(message, status=status, code=code)


class ServerError(CloudError):
    """The server failed to answer. Nothing about the request needs changing."""


#: Codes that name something more specific than the status alone does. Anything
#: absent from here keeps the status-derived class, which is why an unfamiliar
#: code never degrades a well-understood status.
_CODE_REFINEMENTS: dict[str, type[CloudError]] = {
    "image_count_limit_reached": ImageLimitExceeded,
}


def _class_for_status(status: int) -> type[CloudError]:
    if status == 401:
        return AuthenticationError
    if status == 403:
        return PermissionDenied
    if status == 404:
        return ImageNotFound
    if status == 409:
        return ImageInUse
    if status == 429:
        return RateLimited
    if 400 <= status < 500:
        return InvalidRequest
    return ServerError


def error_from_response(
    status: int,
    body: dict[str, object] | None,
    *,
    retry_after: float | None = None,
) -> CloudError:
    """
    Build the error for a refused request.

    `body` is the server's flat error shape — `{path, timestamp, statusCode,
    error, message, code?}` — or None when the response was not JSON at all,
    which is itself a fact worth reporting rather than hiding behind a guess.
    """
    message = None
    code = None
    if body is not None:
        raw_message = body.get("message")
        if isinstance(raw_message, str):
            message = raw_message
        raw_code = body.get("code")
        if isinstance(raw_code, str) and raw_code:
            code = raw_code

    if message is None:
        message = f"Cloud API request failed with HTTP {status}"

    error_class = _class_for_status(status)
    # Only ever narrows: a refinement is consulted after the status has already
    # chosen a class, and an unknown code leaves that choice standing.
    if code is not None:
        error_class = _CODE_REFINEMENTS.get(code, error_class)

    if issubclass(error_class, RateLimited):
        return error_class(message, status=status, code=code, retry_after=retry_after)
    return error_class(message, status=status, code=code)
