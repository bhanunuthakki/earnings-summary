"""Fetch one precomputed KPI semantic-review artifact from the private Windows origin."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import BaseModel, ConfigDict, Field

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from execution.fetch_windows_review_bundle import (  # noqa: E402
    ReviewFetchError,
    WindowsReviewPins,
    exact_https_origin,
    validate_bundle,
)
from log_redact import redact  # noqa: E402
from operations.kpi_semantic_review_export import (  # noqa: E402
    MAX_KPI_SEMANTIC_EXPORT_BYTES,
    KpiSemanticReviewExport,
    encoded_kpi_semantic_review_export,
    normalize_export_ticker,
)

_ENDPOINT_PREFIX = "/api/operations/kpi-semantic-review/"
_MAX_REVIEW_BUNDLE_BYTES = 2_000_000
_MAX_PINS_BYTES = 256_000


class KpiSemanticReviewFetchError(RuntimeError):
    """A closed, credential-safe semantic-review fetch failure."""


class FetchKpiSemanticReviewSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["windows_kpi_semantic_review_fetch.v1"] = (
        "windows_kpi_semantic_review_fetch.v1"
    )
    ticker: str
    observed_at: datetime
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_path: str


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> Request | None:
        return None


def _fetch(url: str, *, timeout_seconds: float) -> tuple[bytes, str | None]:
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    opener = build_opener(_RejectRedirects())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            if response.geturl() != url:
                raise KpiSemanticReviewFetchError("semantic review endpoint redirected")
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) > MAX_KPI_SEMANTIC_EXPORT_BYTES:
                raise KpiSemanticReviewFetchError("semantic review artifact exceeds its byte bound")
            payload = response.read(MAX_KPI_SEMANTIC_EXPORT_BYTES + 1)
            response_etag = response.headers.get("ETag")
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        raise KpiSemanticReviewFetchError(f"semantic review fetch failed: {redact(exc)}") from None
    if len(payload) > MAX_KPI_SEMANTIC_EXPORT_BYTES:
        raise KpiSemanticReviewFetchError("semantic review artifact exceeds its byte bound")
    return payload, response_etag


def _read_bounded(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    try:
        with path.open("rb") as handle:
            payload = handle.read(maximum_bytes + 1)
    except OSError as exc:
        raise KpiSemanticReviewFetchError(f"{label} is unavailable: {redact(exc)}") from None
    if len(payload) > maximum_bytes:
        raise KpiSemanticReviewFetchError(f"{label} exceeds its byte bound")
    return payload


# Public seams for deterministic transport tests and other bounded local callers.
fetch_kpi_semantic_review_bytes = _fetch
read_bounded_input = _read_bounded


def validate_semantic_review_export(
    payload: bytes,
    *,
    ticker: str,
    expected_user_id: str,
    now: datetime,
    max_age: timedelta,
    review_bundle_payload: bytes,
    origin: str,
    pins: WindowsReviewPins,
) -> KpiSemanticReviewExport:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if not expected_user_id or len(expected_user_id) > 128:
        raise KpiSemanticReviewFetchError("expected owner identity is invalid")
    normalized_ticker = normalize_export_ticker(ticker)
    try:
        bundle = validate_bundle(
            review_bundle_payload,
            origin=origin,
            now=now,
            max_age=max_age,
            pins=pins,
        )
    except ReviewFetchError as exc:
        raise KpiSemanticReviewFetchError(str(exc)) from None
    try:
        export = KpiSemanticReviewExport.model_validate_json(payload)
    except Exception as exc:
        raise KpiSemanticReviewFetchError(
            f"semantic review schema validation failed: {redact(exc)}"
        ) from None
    if export.ticker != normalized_ticker:
        raise KpiSemanticReviewFetchError("semantic review ticker does not match the request")
    if export.user_id != expected_user_id:
        raise KpiSemanticReviewFetchError("semantic review owner does not match expected owner")
    if export.observed_at > now + timedelta(minutes=5) or now - export.observed_at > max_age:
        raise KpiSemanticReviewFetchError("semantic review artifact is stale or future-dated")
    if export.code_instance_sha256 != bundle.identity.code_instance_sha256:
        raise KpiSemanticReviewFetchError(
            "semantic review code authority does not match the bundle"
        )
    if export.database_instance_sha256 != bundle.identity.database_instance_sha256:
        raise KpiSemanticReviewFetchError(
            "semantic review database authority does not match the bundle"
        )
    if (
        len(bundle.schema_revision.actual_heads) != 1
        or export.schema_revision != bundle.schema_revision.actual_heads[0]
    ):
        raise KpiSemanticReviewFetchError(
            "semantic review schema authority does not match the bundle"
        )
    return export


def _persist(export: KpiSemanticReviewExport, *, output_root: Path) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    destination = output_root / f"{export.content_sha256}.json"
    encoded = encoded_kpi_semantic_review_export(export)
    if destination.exists():
        existing = read_bounded_input(
            destination,
            maximum_bytes=MAX_KPI_SEMANTIC_EXPORT_BYTES,
            label="existing semantic review artifact",
        )
        if existing != encoded:
            raise KpiSemanticReviewFetchError(
                "content-addressed semantic review artifact conflicts with existing bytes"
            )
        return destination
    temporary = destination.with_suffix(".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(destination)
    return destination


persist_kpi_semantic_review_export = _persist


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--expected-user-id", required=True)
    parser.add_argument("--review-bundle", type=Path, required=True)
    parser.add_argument("--pins", type=Path, required=True)
    parser.add_argument("--max-age-seconds", type=int, default=1200)
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=PROJECT_ROOT / ".tmp" / "windows_review" / "kpi_semantic_reviews",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_age_seconds <= 0 or args.timeout_seconds <= 0:
        raise KpiSemanticReviewFetchError("timeouts and maximum age must be positive")
    output_root = args.output_root.resolve()
    allowed_root = (PROJECT_ROOT / ".tmp").resolve()
    if not output_root.is_relative_to(allowed_root):
        raise KpiSemanticReviewFetchError("semantic review output must remain under .tmp")
    origin = exact_https_origin(args.origin)
    ticker = normalize_export_ticker(args.ticker)
    pins = WindowsReviewPins.model_validate_json(
        read_bounded_input(args.pins, maximum_bytes=_MAX_PINS_BYTES, label="Windows review pins")
    )
    review_bundle_payload = read_bounded_input(
        args.review_bundle,
        maximum_bytes=_MAX_REVIEW_BUNDLE_BYTES,
        label="Operations review bundle",
    )
    now = datetime.now(UTC)
    url = origin + _ENDPOINT_PREFIX + ticker
    payload, response_etag = fetch_kpi_semantic_review_bytes(
        url, timeout_seconds=args.timeout_seconds
    )
    export = validate_semantic_review_export(
        payload,
        ticker=ticker,
        expected_user_id=args.expected_user_id,
        now=now,
        max_age=timedelta(seconds=args.max_age_seconds),
        review_bundle_payload=review_bundle_payload,
        origin=origin,
        pins=pins,
    )
    if response_etag != f'"{export.content_sha256}"':
        raise KpiSemanticReviewFetchError(
            "semantic review endpoint ETag does not match its artifact"
        )
    destination = persist_kpi_semantic_review_export(export, output_root=output_root)
    summary = FetchKpiSemanticReviewSummary(
        ticker=export.ticker,
        observed_at=export.observed_at,
        content_sha256=export.content_sha256,
        output_path=str(destination),
    )
    print(summary.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
