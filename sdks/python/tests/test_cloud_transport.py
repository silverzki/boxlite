"""
The one HTTP path `boxlite.cloud` takes: what it sends, and how it reads a
refusal. Exercised through the real `Transport` with only the socket replaced,
because the URL, the headers and the mapping are the parts that can be wrong.
"""

import io
import json
import urllib.error
import urllib.request

import pytest

from boxlite.cloud import (
    AuthenticationError,
    CloudConnectionError,
    CloudError,
    ImageInUse,
    ImageLimitExceeded,
    ImageNotFound,
    InvalidRequest,
    PermissionDenied,
    RateLimited,
    ServerError,
)
from boxlite.cloud._transport import Transport, quote_segment

BASE = "https://api.test/api"


class _Response:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture(monkeypatch, status=200, payload=b"{}"):
    """Replace the socket, keep the transport. Returns the requests it made."""
    sent = []

    def fake_urlopen(request, timeout=None):
        sent.append(request)
        return _Response(status, payload)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return sent


def _refuse(monkeypatch, status, body, headers=None):
    def fake_urlopen(request, timeout=None):
        message = urllib.error.HTTPError(
            BASE,
            status,
            "refused",
            _headers(headers or {}),
            io.BytesIO(body),
        )
        raise message

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def _headers(values):
    import http.client

    message = http.client.HTTPMessage()
    for key, value in values.items():
        message.add_header(key, value)
    return message


def _transport():
    return Transport(api_key="blk_live_secret", base_url=BASE)


def test_sends_the_key_as_a_bearer_token(monkeypatch):
    sent = _capture(monkeypatch)

    _transport().request("GET", "images")

    assert sent[0].get_header("Authorization") == "Bearer blk_live_secret"


def test_the_key_does_not_follow_a_redirect(monkeypatch):
    """
    urllib copies a request's `headers` onto a 30x target and leaves
    `unredirected_hdrs` behind. A key on the first is one cross-host redirect
    away from being handed to a host nobody asked for, so this drives the
    stdlib's own redirect logic over a request the transport built.
    """
    sent = _capture(monkeypatch)

    _transport().request("GET", "images")

    redirected = urllib.request.HTTPRedirectHandler().redirect_request(
        sent[0],
        io.BytesIO(b""),
        302,
        "Found",
        _headers({}),
        "https://elsewhere.test/api/images",
    )

    assert redirected.get_header("Authorization") is None


def test_an_unreadable_success_is_a_cloud_error(monkeypatch):
    """
    The refusal path already turns an unreadable body into a typed failure.
    A 200 that is not JSON is the same thing to a caller, and a bare
    JSONDecodeError is outside every name this package documents.
    """
    _capture(monkeypatch, status=200, payload=b"<html>not json</html>")

    with pytest.raises(CloudError) as caught:
        _transport().request("GET", "images")

    assert caught.value.status == 200


def test_a_non_list_answer_is_not_an_empty_catalog(monkeypatch):
    """
    `[]` would be indistinguishable from a genuinely empty catalog, so a
    caller would be told it holds no images when what actually happened is
    that the answer could not be read. The status is carried because this
    reached the server — `status=None` means it never did.
    """
    _capture(monkeypatch, status=200, payload=b'{"images": []}')

    with pytest.raises(CloudError) as caught:
        _transport().request_list("GET", "images")

    assert caught.value.status == 200


def test_a_list_answer_comes_back_whole(monkeypatch):
    _capture(monkeypatch, status=200, payload=b'[{"name": "python"}]')

    assert _transport().request_list("GET", "images") == [{"name": "python"}]


def test_sends_no_organization_header(monkeypatch):
    """
    The key carries the organization. A header that looked like it selected one
    would invite callers to set it and expect it to matter.
    """
    sent = _capture(monkeypatch)

    _transport().request("GET", "images")

    header_names = {name.lower() for name, _ in sent[0].header_items()}
    assert "x-boxlite-organization-id" not in header_names


def test_builds_the_url_under_the_configured_base(monkeypatch):
    sent = _capture(monkeypatch)

    _transport().request("GET", "images/usage")

    assert sent[0].full_url == f"{BASE}/images/usage"
    assert sent[0].get_method() == "GET"


def test_reads_an_empty_delete_response_as_no_content(monkeypatch):
    _capture(monkeypatch, status=204, payload=b"")

    assert _transport().request("DELETE", "images/x") is None


def test_reports_an_unreachable_server_without_inventing_a_status(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(CloudConnectionError) as caught:
        _transport().request("GET", "images")

    assert caught.value.status is None


class TestBaseUrl:
    """
    The key travels over whatever this URL names, and `urlopen` honours any
    scheme it is handed.
    """

    @pytest.mark.parametrize(
        "value",
        ["file:///etc/passwd", "ftp://host/api", "api.test/api", ""],
    )
    def test_refuses_a_scheme_that_is_not_http(self, value):
        with pytest.raises(ValueError):
            Transport(api_key="k", base_url=value)

    @pytest.mark.parametrize(
        "value", ["http://localhost:3000/api", "https://api.test/api"]
    )
    def test_accepts_http_because_a_local_stack_serves_it(self, value):
        assert Transport(api_key="k", base_url=value).base_url == value


class TestReferenceEncoding:
    def test_encodes_a_reference_as_one_segment(self):
        assert quote_segment("quay.io/acme/app") == "quay.io%2Facme%2Fapp"

    def test_keeps_a_digest_intact(self):
        encoded = quote_segment("quay.io/acme/app@sha256:" + "a" * 64)
        assert "%40sha256%3A" in encoded

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "  ",
            "../etc/passwd",
            "quay.io/../../etc",
            "/absolute",
            "..",
            # A `..` anywhere, not only at the front: the guard is one rule
            # about components, not a list of prefixes.
            "quay.io/acme/..",
            "quay.io\\..\\acme",
            "a" * 513,
            "with\nnewline",
        ],
    )
    def test_refuses_before_building_a_url(self, value):
        with pytest.raises(ValueError):
            quote_segment(value)


class TestErrorMapping:
    """
    Status first, `code` only refines. Most cloud exceptions set no code at
    all, so a mapping led by `code` would have nothing to work with on the
    common path — and a synthesized one would hide the status behind it.
    """

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (400, InvalidRequest),
            (401, AuthenticationError),
            (403, PermissionDenied),
            (404, ImageNotFound),
            (409, ImageInUse),
            (422, InvalidRequest),
            (500, ServerError),
            (503, ServerError),
        ],
    )
    def test_maps_a_body_without_a_code(self, monkeypatch, status, expected):
        body = json.dumps(
            {
                "path": "/api/images",
                "timestamp": "2026-01-01T00:00:00.000Z",
                "statusCode": status,
                "error": "Error",
                "message": "refused",
            }
        ).encode()
        _refuse(monkeypatch, status, body)

        with pytest.raises(expected) as caught:
            _transport().request("GET", "images")

        assert caught.value.status == status
        assert caught.value.message == "refused"
        # Never filled in with a guess: a synthesized code is indistinguishable
        # from one the server chose.
        assert caught.value.code is None

    def test_a_known_code_narrows_the_status(self, monkeypatch):
        body = json.dumps(
            {
                "statusCode": 400,
                "message": "holds its limit",
                "code": "image_count_limit_reached",
            }
        ).encode()
        _refuse(monkeypatch, 400, body)

        with pytest.raises(ImageLimitExceeded) as caught:
            _transport().request("GET", "images")

        assert caught.value.code == "image_count_limit_reached"
        # The refinement is still the status's class, narrowed.
        assert isinstance(caught.value, InvalidRequest)

    def test_an_unknown_code_leaves_the_status_standing(self, monkeypatch):
        body = json.dumps(
            {"statusCode": 409, "message": "in use", "code": "something_new"}
        ).encode()
        _refuse(monkeypatch, 409, body)

        with pytest.raises(ImageInUse) as caught:
            _transport().request("DELETE", "images/x")

        assert caught.value.code == "something_new"

    def test_a_refusal_that_is_not_json_still_maps_on_its_status(self, monkeypatch):
        _refuse(monkeypatch, 502, b"<html>gateway</html>")

        with pytest.raises(ServerError) as caught:
            _transport().request("GET", "images")

        assert caught.value.status == 502
        assert caught.value.code is None

    def test_carries_the_retry_hint_of_a_rate_limit(self, monkeypatch):
        body = json.dumps(
            {
                "statusCode": 429,
                "message": "too many",
                "code": "image_cold_pull_rate_limited",
            }
        ).encode()
        _refuse(monkeypatch, 429, body, headers={"Retry-After": "42"})

        with pytest.raises(RateLimited) as caught:
            _transport().request("GET", "images")

        assert caught.value.retry_after == 42.0

    @pytest.mark.parametrize("header", ["-1", "inf", "-inf", "nan", "soon"])
    def test_drops_a_retry_hint_that_is_not_a_wait(self, monkeypatch, header):
        """
        `float()` takes "-1", "inf" and "nan" as readily as "42". None says
        there is no usable hint, which is the truth about all three; a caller
        that slept on them would not be waiting, it would be guessing.
        """
        body = json.dumps({"statusCode": 429, "message": "too many"}).encode()
        _refuse(monkeypatch, 429, body, headers={"Retry-After": header})

        with pytest.raises(RateLimited) as caught:
            _transport().request("GET", "images")

        assert caught.value.retry_after is None


class TestConfiguration:
    def test_reads_the_key_and_address_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("BOXLITE_API_KEY", "env-key")
        monkeypatch.setenv("BOXLITE_REST_URL", "https://env.test/api/")
        sent = _capture(monkeypatch)

        Transport().request("GET", "images")

        assert sent[0].full_url == "https://env.test/api/images"
        assert sent[0].get_header("Authorization") == "Bearer env-key"

    def test_says_which_variable_is_missing(self, monkeypatch):
        monkeypatch.delenv("BOXLITE_API_KEY", raising=False)

        with pytest.raises(ValueError, match="BOXLITE_API_KEY"):
            Transport(base_url=BASE)
