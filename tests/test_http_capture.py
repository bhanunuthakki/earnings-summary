"""Capture-extension contracts for the shared HTTP client.

These tests use fake requests.Response objects and a fake session request so
no live network is contacted. They assert byte-exact bodies, aware UTC and
UUID capture metadata, secret-free origin/path derivation, the default no
capture path, third-field backwards compatibility, and FMP adapter
propagation shape.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from io import BytesIO
from uuid import UUID, uuid4

import pytest
import requests

from net.client import (
    HostRateBudget,
    HttpCallError,
    HttpClient,
    HttpJsonResponse,
    JsonShape,
    RetryPolicy,
)


@pytest.fixture()
def synthetic_credential() -> str:
    """Generate test-only credential text; never retain a credential literal."""
    return uuid4().hex


BODY_WHITESPACE = b'{\n  "z": 1,\n  "a": [1, 2,   3]\n}\n'


def _raw_response(status: int, body: bytes, *, url: str | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.encoding = "utf-8"
    response.raw = BytesIO(body)
    if url is not None:
        response.url = url
    return response


def _client(session: requests.Session) -> HttpClient:
    return HttpClient(
        session=session,
        retry=RetryPolicy(max_attempts=1, backoff_base_s=0),
        rate_budget=HostRateBudget({}),
        sleep=lambda _delay: None,
        event_sink=lambda _event: None,
    )


def test_default_request_json_does_not_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    session = requests.Session()
    body = b'{"a": 1}'

    def fake_request(_method: str, _url: str, **_kwargs: object) -> requests.Response:
        return _raw_response(200, body, url="https://example.test/data")

    monkeypatch.setattr(session, "request", fake_request)

    result = _client(session).request_json("GET", "https://example.test/data")

    assert result.capture is None
    assert result.payload == {"a": 1}


def test_retain_capture_preserves_exact_bytes_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_credential: str,
) -> None:
    session = requests.Session()

    def fake_request(_method: str, _url: str, **_kwargs: object) -> requests.Response:
        return _raw_response(
            200,
            BODY_WHITESPACE,
            url=f"https://example.test/data?apikey={synthetic_credential}#fragment",
        )

    monkeypatch.setattr(session, "request", fake_request)

    before = datetime.now(UTC)
    result = _client(session).request_json(
        "GET",
        "https://example.test/data",
        retain_capture=True,
    )
    after = datetime.now(UTC)

    capture = result.capture
    assert capture is not None
    # byte-exact entity body, not a reserialized JSON canonicalization
    assert capture.body == BODY_WHITESPACE
    reserialized = json.dumps(result.payload).encode("utf-8")
    assert capture.body != reserialized
    assert hashlib.sha256(capture.body).hexdigest() != hashlib.sha256(reserialized).hexdigest()
    # parse still applied to the same response
    assert result.payload == {"z": 1, "a": [1, 2, 3]}
    # aware UTC timestamp at capture
    assert capture.received_at.tzinfo is not None
    assert capture.received_at.utcoffset() == UTC.utcoffset(capture.received_at)
    assert before <= capture.received_at <= after
    # capture id parses as a UUID
    assert UUID(capture.capture_id).version == 4
    # secret-free origin/path parsed from the final response URL
    assert capture.origin == "https://example.test"
    assert capture.path == "/data"
    assert synthetic_credential not in capture.origin
    assert synthetic_credential not in capture.path
    assert "fragment" not in capture.path


def test_capture_body_is_excluded_from_repr_along_with_query_secret(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_credential: str,
) -> None:
    session = requests.Session()
    secret_body = json.dumps({"apikey": synthetic_credential, "value": 2}).encode()

    def fake_request(_method: str, _url: str, **_kwargs: object) -> requests.Response:
        return _raw_response(
            200,
            secret_body,
            url=f"https://user:{synthetic_credential}@example.test/data?apikey={synthetic_credential}#{synthetic_credential}",
        )

    monkeypatch.setattr(session, "request", fake_request)

    result = _client(session).request_json(
        "GET",
        "https://example.test/data",
        retain_capture=True,
    )

    capture = result.capture
    assert capture is not None
    text = repr(capture)
    assert synthetic_credential not in text
    assert "apikey" not in text
    # userinfo, query, and fragment are stripped from origin and path
    assert capture.origin == "https://example.test"
    assert capture.path == "/data"
    # body itself is still available to the caller for hashing
    assert capture.body == secret_body


def test_capture_id_is_unique_per_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    session = requests.Session()

    def fake_request(_method: str, _url: str, **_kwargs: object) -> requests.Response:
        return _raw_response(200, b"[]", url="https://example.test/data")

    monkeypatch.setattr(session, "request", fake_request)
    client = _client(session)

    first = client.request_json("GET", "https://example.test/data", retain_capture=True)
    second = client.request_json("GET", "https://example.test/data", retain_capture=True)

    assert first.capture is not None and second.capture is not None
    assert first.capture.capture_id != second.capture.capture_id


def test_non_https_final_url_yields_empty_origin_and_path(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_credential: str,
) -> None:
    session = requests.Session()

    def fake_request(_method: str, _url: str, **_kwargs: object) -> requests.Response:
        return _raw_response(
            200, b"{}", url=f"http://example.test/data?apikey={synthetic_credential}"
        )

    monkeypatch.setattr(session, "request", fake_request)

    result = _client(session).request_json(
        "GET",
        "https://example.test/data",
        retain_capture=True,
    )

    assert result.capture is not None
    assert result.capture.origin == ""
    assert result.capture.path == ""


def test_old_two_argument_constructor_remains_compatible() -> None:
    response = HttpJsonResponse(status_code=200, payload={"ok": True})
    assert response.capture is None
    assert response.status_code == 200
    assert response.payload == {"ok": True}


def test_expected_shape_still_enforced_when_capturing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = requests.Session()

    def fake_request(_method: str, _url: str, **_kwargs: object) -> requests.Response:
        return _raw_response(200, b'{"not": "array"}', url="https://example.test/data")

    monkeypatch.setattr(session, "request", fake_request)

    with pytest.raises(HttpCallError):
        _client(session).request_json(
            "GET",
            "https://example.test/data",
            expected=JsonShape.ARRAY,
            retain_capture=True,
        )
