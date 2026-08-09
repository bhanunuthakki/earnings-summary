"""Reusable, rate-budgeted HTTP transport for deterministic data fetchers.

The client owns connection reuse, explicit connect/read timeouts, bounded
retries for read-only methods, provider-aware rate spacing, safe error
classification, and redacted JSON-line telemetry. Provider adapters keep
credentials and response-shape contracts out of individual fetch scripts.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import TypeAlias, cast
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

from log_redact import redact
from sec_identity import sec_user_agent

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
QueryValue: TypeAlias = str | int | float | bool | None
Timeout = tuple[float, float]
EventSink = Callable[[Mapping[str, object]], None]
AttemptHook = Callable[["HttpAttempt"], None]

DEFAULT_TIMEOUT: Timeout = (3.05, 30.0)
FMP_ORIGIN = "https://financialmodelingprep.com"
DEFAULT_FMP_BASE_URL = f"{FMP_ORIGIN}/stable"
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_READ_ONLY_METHODS = frozenset({"GET", "HEAD"})
_FMP_RATES_PER_SECOND: Mapping[str, float] = {
    "free": 4.0,
    "basic": 4.0,
    "starter": 5.0,
    "premium": 12.0,
}


class HttpErrorKind(StrEnum):
    """Stable failure taxonomy for callers choosing halt vs degradation."""

    AUTH = "auth"
    PLAN = "plan"
    RATE_LIMIT = "rate_limit"
    TRANSIENT = "transient"
    CLIENT = "client"
    NETWORK = "network"
    SCHEMA = "schema"


class JsonShape(StrEnum):
    ANY = "any"
    ARRAY = "array"
    OBJECT = "object"


class HttpCallError(RuntimeError):
    """Sanitized HTTP failure with machine-readable classification.

    ``payload`` is intentionally excluded from the message so provider bodies
    cannot leak credentials through uncaught exception output.
    """

    def __init__(
        self,
        *,
        kind: HttpErrorKind,
        message: str,
        retryable: bool,
        status_code: int | None = None,
        payload: JsonValue = None,
    ) -> None:
        super().__init__(redact(message))
        self.kind = kind
        self.retryable = retryable
        self.status_code = status_code
        self.payload = payload


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    backoff_base_s: float = 2.0
    max_backoff_s: float = 30.0
    max_retry_after_s: float = 60.0
    retry_statuses: frozenset[int] = _RETRYABLE_STATUSES
    retry_methods: frozenset[str] = _READ_ONLY_METHODS

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if min(self.backoff_base_s, self.max_backoff_s, self.max_retry_after_s) < 0:
            raise ValueError("retry delays cannot be negative")

    def delay_seconds(self, response: requests.Response | None, attempt: int) -> float:
        if response is not None:
            raw = response.headers.get("Retry-After", "").strip()
            try:
                retry_after = float(raw)
            except ValueError:
                retry_after = -1.0
            if retry_after >= 0:
                return min(retry_after, self.max_retry_after_s)
        return min(self.backoff_base_s * (2 ** (attempt - 1)), self.max_backoff_s)


DEFAULT_RETRY_POLICY = RetryPolicy()


@dataclass(frozen=True, slots=True)
class HttpJsonResponse:
    status_code: int
    payload: JsonValue


@dataclass(frozen=True, slots=True)
class HttpAttempt:
    attempt: int
    status_code: int | None
    network_error: bool = False


class HostRateBudget:
    """Thread-safe start-time spacing shared by all calls to the same host."""

    def __init__(
        self,
        intervals_s: Mapping[str, float],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        normalized: dict[str, float] = {}
        for host, interval in intervals_s.items():
            if interval < 0:
                raise ValueError("host rate intervals cannot be negative")
            normalized[host.strip().lower()] = interval
        self._intervals_s = normalized
        self._clock = clock
        self._sleep = sleep
        self._next_allowed: dict[str, float] = {}
        self._lock = threading.Lock()

    def set_interval(self, host: str, interval_s: float) -> None:
        """Update a host budget after runtime configuration is loaded."""
        if interval_s < 0:
            raise ValueError("host rate intervals cannot be negative")
        with self._lock:
            self._intervals_s[host.strip().lower()] = interval_s

    def _interval_for(self, host: str) -> float:
        normalized = host.lower()
        matches = [
            (domain, interval)
            for domain, interval in self._intervals_s.items()
            if normalized == domain or normalized.endswith(f".{domain}")
        ]
        if not matches:
            return 0.0
        return max(matches, key=lambda item: len(item[0]))[1]

    def acquire(self, host: str) -> None:
        interval = self._interval_for(host)
        if interval <= 0:
            return
        with self._lock:
            now = self._clock()
            scheduled = max(now, self._next_allowed.get(host.lower(), now))
            self._next_allowed[host.lower()] = scheduled + interval
        delay = scheduled - now
        if delay > 0:
            self._sleep(delay)


def _fmp_rate_per_second(environ: Mapping[str, str]) -> float:
    explicit = environ.get("FMP_RATE_LIMIT_PER_SEC", "").strip()
    if explicit:
        try:
            parsed = float(explicit)
        except ValueError:
            parsed = 0.0
        if parsed > 0:
            return parsed
    tier = environ.get("FMP_TIER", "basic").strip().lower() or "basic"
    return _FMP_RATES_PER_SECOND.get(tier, _FMP_RATES_PER_SECOND["basic"])


def default_rate_budget(
    *,
    environ: Mapping[str, str] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> HostRateBudget:
    source = os.environ if environ is None else environ
    return HostRateBudget(
        {
            "sec.gov": 0.25,
            "financialmodelingprep.com": 1.0 / _fmp_rate_per_second(source),
        },
        clock=clock,
        sleep=sleep,
    )


def _default_event_sink(event: Mapping[str, object]) -> None:
    safe = {key: redact(value) if isinstance(value, str) else value for key, value in event.items()}
    print(json.dumps(safe, sort_keys=True), file=sys.stderr)


def _classify_status(status_code: int) -> HttpErrorKind:
    if status_code in {401, 403}:
        return HttpErrorKind.AUTH
    if status_code == 402:
        return HttpErrorKind.PLAN
    if status_code == 429:
        return HttpErrorKind.RATE_LIMIT
    if status_code in _RETRYABLE_STATUSES or status_code >= 500:
        return HttpErrorKind.TRANSIENT
    return HttpErrorKind.CLIENT


def _validated_json(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_validated_json(item) for item in cast(list[object], value)]
    if isinstance(value, dict):
        output: dict[str, JsonValue] = {}
        for key, item in cast(dict[object, object], value).items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            output[key] = _validated_json(item)
        return output
    raise ValueError(f"unsupported JSON value type: {type(value).__name__}")


def _parse_json(response: requests.Response, *, expected: JsonShape) -> JsonValue:
    try:
        payload = _validated_json(response.json())
    except (ValueError, TypeError) as exc:
        raise HttpCallError(
            kind=HttpErrorKind.SCHEMA,
            message=f"response was not valid JSON: {redact(exc)}",
            retryable=False,
            status_code=response.status_code,
        ) from None
    if expected is JsonShape.ARRAY and not isinstance(payload, list):
        raise HttpCallError(
            kind=HttpErrorKind.SCHEMA,
            message=f"expected JSON array, got {type(payload).__name__}",
            retryable=False,
            status_code=response.status_code,
            payload=payload,
        ) from None
    if expected is JsonShape.OBJECT and not isinstance(payload, dict):
        raise HttpCallError(
            kind=HttpErrorKind.SCHEMA,
            message=f"expected JSON object, got {type(payload).__name__}",
            retryable=False,
            status_code=response.status_code,
            payload=payload,
        ) from None
    return payload


class HttpClient:
    """One reusable Requests session with explicit policy at the boundary."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: Timeout = DEFAULT_TIMEOUT,
        retry: RetryPolicy = DEFAULT_RETRY_POLICY,
        rate_budget: HostRateBudget | None = None,
        event_sink: EventSink = _default_event_sink,
        sleep: Callable[[float], None] = time.sleep,
        sec_user_agent_hook: Callable[[], str] = sec_user_agent,
    ) -> None:
        self._session = session or requests.Session()
        if session is None:
            adapter = HTTPAdapter(
                pool_connections=16,
                pool_maxsize=32,
                max_retries=0,
                pool_block=True,
            )
            self._session.mount("https://", adapter)
            self._session.mount("http://", adapter)
        self._timeout = timeout
        self._retry = retry
        self._rate_budget = rate_budget or default_rate_budget()
        self._event_sink = event_sink
        self._sleep = sleep
        self._sec_user_agent_hook = sec_user_agent_hook

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._session.close()

    def set_host_rate(self, host: str, requests_per_second: float) -> None:
        """Configure a provider budget without replacing the shared session."""
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self._rate_budget.set_interval(host, 1.0 / requests_per_second)

    def _emit(self, **event: object) -> None:
        self._event_sink({"event": "http_call", **event})

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, QueryValue] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: Timeout | None = None,
        retry: RetryPolicy | None = None,
        attempt_hook: AttemptHook | None = None,
    ) -> requests.Response:
        normalized_method = method.upper()
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path or "/"
        if parsed.scheme not in {"http", "https"} or not host:
            raise ValueError("url must be an absolute HTTP(S) URL")

        request_headers = dict(headers or {})
        if host == "sec.gov" or host.endswith(".sec.gov"):
            request_headers.setdefault("User-Agent", self._sec_user_agent_hook())

        policy = retry or self._retry
        retryable_method = normalized_method in policy.retry_methods
        for attempt in range(1, policy.max_attempts + 1):
            self._rate_budget.acquire(host)
            started = time.monotonic()
            try:
                response = self._session.request(
                    normalized_method,
                    url,
                    params=params,
                    headers=request_headers,
                    timeout=timeout or self._timeout,
                )
            except requests.RequestException as exc:
                if attempt_hook is not None:
                    attempt_hook(HttpAttempt(attempt=attempt, status_code=None, network_error=True))
                elapsed_ms = round((time.monotonic() - started) * 1000)
                will_retry = retryable_method and attempt < policy.max_attempts
                self._emit(
                    host=host,
                    path=path,
                    attempt=attempt,
                    duration_ms=elapsed_ms,
                    outcome="retry" if will_retry else "network_error",
                    error=redact(exc),
                )
                if will_retry:
                    self._sleep(policy.delay_seconds(None, attempt))
                    continue
                raise HttpCallError(
                    kind=HttpErrorKind.NETWORK,
                    message=f"network request failed for {host}{path}: {redact(exc)}",
                    retryable=retryable_method,
                ) from None

            elapsed_ms = round((time.monotonic() - started) * 1000)
            status = response.status_code
            if attempt_hook is not None:
                attempt_hook(HttpAttempt(attempt=attempt, status_code=status))
            retry_status = status in policy.retry_statuses
            will_retry = retryable_method and retry_status and attempt < policy.max_attempts
            self._emit(
                host=host,
                path=path,
                attempt=attempt,
                duration_ms=elapsed_ms,
                status=status,
                outcome="retry" if will_retry else ("ok" if status < 400 else "http_error"),
            )
            if will_retry:
                delay = policy.delay_seconds(response, attempt)
                response.close()
                self._sleep(delay)
                continue
            if status >= 400:
                try:
                    payload = _parse_json(response, expected=JsonShape.ANY)
                except HttpCallError:
                    payload = None
                kind = _classify_status(status)
                raise HttpCallError(
                    kind=kind,
                    message=f"HTTP {status} from {host}{path}",
                    retryable=retryable_method and retry_status,
                    status_code=status,
                    payload=payload,
                ) from None
            return response

        raise AssertionError("bounded request loop exited without a result")

    def request_json(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, QueryValue] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: Timeout | None = None,
        expected: JsonShape = JsonShape.ANY,
        retry: RetryPolicy | None = None,
        attempt_hook: AttemptHook | None = None,
    ) -> HttpJsonResponse:
        response = self.request(
            method,
            url,
            params=params,
            headers=headers,
            timeout=timeout,
            retry=retry,
            attempt_hook=attempt_hook,
        )
        payload = _parse_json(response, expected=expected)
        return HttpJsonResponse(status_code=response.status_code, payload=payload)


class FmpClient:
    """Canonical Financial Modeling Prep adapter over the shared transport."""

    def __init__(
        self,
        *,
        http: HttpClient,
        base_url: str = DEFAULT_FMP_BASE_URL,
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")

    def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, QueryValue] | None = None,
        api_key: str | None = None,
        expected: JsonShape = JsonShape.ANY,
        timeout: Timeout | None = None,
        retry: RetryPolicy | None = None,
        attempt_hook: AttemptHook | None = None,
    ) -> HttpJsonResponse:
        return self.get_url_json(
            f"{self._base_url}/{path.lstrip('/')}",
            params=params,
            api_key=api_key,
            expected=expected,
            timeout=timeout,
            retry=retry,
            attempt_hook=attempt_hook,
        )

    def get_url_json(
        self,
        url: str,
        *,
        params: Mapping[str, QueryValue] | None = None,
        api_key: str | None = None,
        expected: JsonShape = JsonShape.ANY,
        timeout: Timeout | None = None,
        retry: RetryPolicy | None = None,
        attempt_hook: AttemptHook | None = None,
    ) -> HttpJsonResponse:
        """Fetch an FMP stable/legacy URL through the same guarded transport."""
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "financialmodelingprep.com":
            raise ValueError("FMP URL must use https://financialmodelingprep.com")
        # Project dotenv files may be loaded after this module is imported.
        # Re-read the tier immediately before reserving the provider budget.
        self._http.set_host_rate(
            "financialmodelingprep.com",
            _fmp_rate_per_second(os.environ),
        )
        query = dict(params or {})
        query["apikey"] = os.environ.get("FMP_API_KEY", "") if api_key is None else api_key
        return self._http.request_json(
            "GET",
            url,
            params=query,
            expected=expected,
            timeout=timeout,
            retry=retry,
            attempt_hook=attempt_hook,
        )


HTTP_CLIENT = HttpClient()
FMP_CLIENT = FmpClient(http=HTTP_CLIENT)
