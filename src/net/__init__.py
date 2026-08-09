"""Shared outbound HTTP clients and provider adapters."""

from net.client import (
    FMP_CLIENT,
    FMP_ORIGIN,
    HTTP_CLIENT,
    FmpClient,
    HostRateBudget,
    HttpAttempt,
    HttpCallError,
    HttpErrorKind,
    HttpJsonResponse,
    JsonShape,
    JsonValue,
    QueryValue,
    RetryPolicy,
    default_rate_budget,
)

__all__ = [
    "FMP_CLIENT",
    "FMP_ORIGIN",
    "HTTP_CLIENT",
    "FmpClient",
    "HostRateBudget",
    "HttpAttempt",
    "HttpCallError",
    "HttpErrorKind",
    "HttpJsonResponse",
    "JsonShape",
    "JsonValue",
    "QueryValue",
    "RetryPolicy",
    "default_rate_budget",
]
