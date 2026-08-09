"""Provider-free contracts for the shared outbound HTTP client."""

from __future__ import annotations

import json
from collections.abc import Mapping
from io import BytesIO

import pytest
import requests

from net.client import (
    FmpClient,
    HostRateBudget,
    HttpAttempt,
    HttpCallError,
    HttpClient,
    HttpErrorKind,
    JsonShape,
    RetryPolicy,
    default_rate_budget,
)


def _response(
    status: int, payload: object, *, headers: Mapping[str, str] | None = None
) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    content = json.dumps(payload).encode("utf-8")
    response.encoding = "utf-8"
    response.raw = BytesIO(content)
    return response


def _client(
    session: requests.Session,
    *,
    retry: RetryPolicy = RetryPolicy(max_attempts=3, backoff_base_s=0),
    sleeps: list[float] | None = None,
    events: list[Mapping[str, object]] | None = None,
) -> HttpClient:
    return HttpClient(
        session=session,
        retry=retry,
        rate_budget=HostRateBudget({}),
        sleep=(sleeps if sleeps is not None else []).append,
        event_sink=(events if events is not None else []).append,
    )


def test_get_retries_transient_status_then_reuses_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = requests.Session()
    responses = iter([_response(503, {}), _response(200, [{"ok": True}])])
    calls: list[str] = []

    def fake_request(method: str, url: str, **_kwargs: object) -> requests.Response:
        calls.append(f"{method} {url}")
        return next(responses)

    monkeypatch.setattr(session, "request", fake_request)
    events: list[Mapping[str, object]] = []
    client = _client(session, events=events)

    result = client.request_json("GET", "https://example.test/data", expected=JsonShape.ARRAY)

    assert result.payload == [{"ok": True}]
    assert calls == ["GET https://example.test/data", "GET https://example.test/data"]
    assert [event["outcome"] for event in events] == ["retry", "ok"]


def test_attempt_hook_observes_each_retry_without_sensitive_request_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = requests.Session()
    responses = iter([_response(503, {}), _response(200, {})])

    def fake_request(*_args: object, **_kwargs: object) -> requests.Response:
        return next(responses)

    monkeypatch.setattr(session, "request", fake_request)
    attempts: list[HttpAttempt] = []

    _client(session).request_json(
        "GET",
        "https://example.test/data",
        attempt_hook=attempts.append,
    )

    assert attempts == [
        HttpAttempt(attempt=1, status_code=503),
        HttpAttempt(attempt=2, status_code=200),
    ]


def test_nonretryable_auth_status_is_classified_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = requests.Session()
    calls = 0

    def fake_request(_method: str, _url: str, **_kwargs: object) -> requests.Response:
        nonlocal calls
        calls += 1
        return _response(401, {"error": "bad key"})

    monkeypatch.setattr(session, "request", fake_request)
    client = _client(session)

    with pytest.raises(HttpCallError) as caught:
        client.request_json("GET", "https://example.test/private")

    assert calls == 1
    assert caught.value.kind is HttpErrorKind.AUTH
    assert caught.value.retryable is False


def test_non_idempotent_method_never_retries_transient_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = requests.Session()
    calls = 0

    def fake_request(_method: str, _url: str, **_kwargs: object) -> requests.Response:
        nonlocal calls
        calls += 1
        return _response(503, {})

    monkeypatch.setattr(session, "request", fake_request)
    client = _client(session)

    with pytest.raises(HttpCallError) as caught:
        client.request("POST", "https://example.test/mutate")

    assert calls == 1
    assert caught.value.kind is HttpErrorKind.TRANSIENT
    assert caught.value.retryable is False


def test_rate_budget_spaces_calls_per_provider_host() -> None:
    now = [100.0]
    sleeps: list[float] = []

    def clock() -> float:
        return now[0]

    def sleep(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    budget = HostRateBudget({"sec.gov": 0.25}, clock=clock, sleep=sleep)

    budget.acquire("data.sec.gov")
    budget.acquire("data.sec.gov")

    assert sleeps == [0.25]


def test_default_rate_budget_applies_sec_and_configured_fmp_tier() -> None:
    now = [100.0]
    sleeps: list[float] = []

    def clock() -> float:
        return now[0]

    def sleep(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    budget = default_rate_budget(
        environ={"FMP_TIER": "premium"},
        clock=clock,
        sleep=sleep,
    )

    budget.acquire("data.sec.gov")
    budget.acquire("data.sec.gov")
    budget.acquire("financialmodelingprep.com")
    budget.acquire("financialmodelingprep.com")

    assert sleeps == [0.25, pytest.approx(1.0 / 12.0)]


def test_retry_after_is_capped() -> None:
    response = _response(429, {}, headers={"Retry-After": "600"})

    delay = RetryPolicy(max_retry_after_s=20).delay_seconds(response, attempt=1)

    assert delay == 20


def test_sec_user_agent_hook_and_explicit_timeout_are_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = requests.Session()
    captured: dict[str, object] = {}

    def fake_request(_method: str, _url: str, **kwargs: object) -> requests.Response:
        captured.update(kwargs)
        return _response(200, {})

    monkeypatch.setattr(session, "request", fake_request)
    client = HttpClient(
        session=session,
        rate_budget=HostRateBudget({}),
        event_sink=lambda _event: None,
        sec_user_agent_hook=lambda: "research-client owner@example.com",
    )

    client.request_json("GET", "https://data.sec.gov/submissions/CIK1.json")

    assert captured["timeout"] == (3.05, 30.0)
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["User-Agent"] == "research-client owner@example.com"


def test_network_error_and_structured_log_redact_query_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = requests.Session()

    def fail(_method: str, _url: str, **_kwargs: object) -> requests.Response:
        raise requests.ConnectionError("failed https://x.test/data?apikey=VERY_SECRET")

    monkeypatch.setattr(session, "request", fail)
    events: list[Mapping[str, object]] = []
    client = _client(session, retry=RetryPolicy(max_attempts=1), events=events)

    with pytest.raises(HttpCallError) as caught:
        client.request_json(
            "GET",
            "https://x.test/data",
            params={"apikey": "VERY_SECRET"},
        )

    assert "VERY_SECRET" not in str(caught.value)
    assert "VERY_SECRET" not in json.dumps(events)
    assert events[0]["host"] == "x.test"
    assert events[0]["path"] == "/data"


def test_fmp_adapter_validates_expected_fallback_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = requests.Session()
    captured: dict[str, object] = {}

    def fake_request(_method: str, _url: str, **kwargs: object) -> requests.Response:
        captured.update(kwargs)
        return _response(200, {"Error Message": "plan refused"})

    monkeypatch.setattr(session, "request", fake_request)
    fmp = FmpClient(http=_client(session))

    with pytest.raises(HttpCallError) as caught:
        fmp.get_json("earnings", api_key="secret", expected=JsonShape.ARRAY)

    assert caught.value.kind is HttpErrorKind.SCHEMA
    assert caught.value.payload == {"Error Message": "plan refused"}
    params = captured["params"]
    assert isinstance(params, dict)
    assert params["apikey"] == "secret"


def test_fmp_adapter_rejects_non_fmp_url_before_transport() -> None:
    fmp = FmpClient(http=_client(requests.Session()))

    with pytest.raises(ValueError, match="FMP URL"):
        fmp.get_url_json("https://example.test/collect", api_key="secret")
