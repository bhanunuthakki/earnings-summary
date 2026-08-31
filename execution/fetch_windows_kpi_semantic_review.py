"""Fetch one complete, partitioned KPI semantic review from private Windows."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
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
    KpiSemanticReviewArtifactPointer,
    KpiSemanticReviewExport,
    KpiSemanticReviewTickerManifest,
    encoded_kpi_semantic_review_export,
    encoded_kpi_semantic_review_ticker_manifest,
    normalize_export_ticker,
)
from operations.review_bundle import OperationsReviewBundle  # noqa: E402

_ENDPOINT_PREFIX = "/api/operations/kpi-semantic-review/"
_ENDPOINT_PATH = re.compile(
    r"^/api/operations/kpi-semantic-review/"
    r"[A-Z][A-Z0-9.-]{0,14}"
    r"(?:/partitions/[0-9a-f]{64})?$"
)
_MAX_MANIFEST_BYTES = 1_000_000
_MAX_REVIEW_BUNDLE_BYTES = 2_000_000
_MAX_PINS_BYTES = 256_000


class KpiSemanticReviewFetchError(RuntimeError):
    """A closed, credential-safe semantic-review fetch failure."""


class FetchKpiSemanticReviewSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["windows_kpi_semantic_review_fetch.v2"] = (
        "windows_kpi_semantic_review_fetch.v2"
    )
    ticker: str
    observed_at: datetime
    manifest_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    total_items: int = Field(ge=0)
    manifest_output_path: str
    partition_output_paths: tuple[str, ...]


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


def _fetch(url: str, *, timeout_seconds: float, maximum_bytes: int) -> tuple[bytes, str | None]:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or _ENDPOINT_PATH.fullmatch(parsed.path) is None
    ):
        raise KpiSemanticReviewFetchError(
            "semantic review URL must be an exact credential-free HTTPS endpoint"
        )
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    opener = build_opener(_RejectRedirects())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            if response.geturl() != url:
                raise KpiSemanticReviewFetchError("semantic review endpoint redirected")
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) > maximum_bytes:
                raise KpiSemanticReviewFetchError("semantic review artifact exceeds its byte bound")
            payload = response.read(maximum_bytes + 1)
            response_etag = response.headers.get("ETag")
    except KpiSemanticReviewFetchError:
        raise
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        raise KpiSemanticReviewFetchError(f"semantic review fetch failed: {redact(exc)}") from None
    if len(payload) > maximum_bytes:
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


fetch_kpi_semantic_review_bytes = _fetch
read_bounded_input = _read_bounded


def _validated_bundle(
    *,
    payload: bytes,
    origin: str,
    now: datetime,
    max_age: timedelta,
    pins: WindowsReviewPins,
) -> OperationsReviewBundle:
    try:
        return validate_bundle(payload, origin=origin, now=now, max_age=max_age, pins=pins)
    except ReviewFetchError as exc:
        raise KpiSemanticReviewFetchError(str(exc)) from None


def _validate_authority(
    *,
    user_id: str,
    observed_at: datetime,
    code_instance_sha256: str,
    database_instance_sha256: str,
    schema_revision: str,
    expected_user_id: str,
    now: datetime,
    max_age: timedelta,
    bundle: OperationsReviewBundle,
) -> None:
    if user_id != expected_user_id:
        raise KpiSemanticReviewFetchError("semantic review owner does not match expected owner")
    if observed_at > now + timedelta(minutes=5) or now - observed_at > max_age:
        raise KpiSemanticReviewFetchError("semantic review artifact is stale or future-dated")
    if code_instance_sha256 != bundle.identity.code_instance_sha256:
        raise KpiSemanticReviewFetchError(
            "semantic review code authority does not match the bundle"
        )
    if database_instance_sha256 != bundle.identity.database_instance_sha256:
        raise KpiSemanticReviewFetchError(
            "semantic review database authority does not match the bundle"
        )
    if (
        len(bundle.schema_revision.actual_heads) != 1
        or schema_revision != bundle.schema_revision.actual_heads[0]
    ):
        raise KpiSemanticReviewFetchError(
            "semantic review schema authority does not match the bundle"
        )


def validate_semantic_review_manifest(
    payload: bytes,
    *,
    response_etag: str | None,
    ticker: str,
    expected_user_id: str,
    now: datetime,
    max_age: timedelta,
    review_bundle_payload: bytes,
    origin: str,
    pins: WindowsReviewPins,
) -> KpiSemanticReviewTickerManifest:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if not expected_user_id or len(expected_user_id) > 128:
        raise KpiSemanticReviewFetchError("expected owner identity is invalid")
    normalized_ticker = normalize_export_ticker(ticker)
    bundle = _validated_bundle(
        payload=review_bundle_payload,
        origin=origin,
        now=now,
        max_age=max_age,
        pins=pins,
    )
    try:
        manifest = KpiSemanticReviewTickerManifest.model_validate_json(payload)
    except Exception as exc:
        raise KpiSemanticReviewFetchError(
            f"semantic review manifest schema validation failed: {redact(exc)}"
        ) from None
    if response_etag != f'"{manifest.content_sha256}"':
        raise KpiSemanticReviewFetchError(
            "semantic review manifest ETag does not match its content hash"
        )
    if manifest.ticker != normalized_ticker:
        raise KpiSemanticReviewFetchError("semantic review ticker does not match the request")
    hashes = [pointer.content_sha256 for pointer in manifest.partitions]
    if len(hashes) != len(set(hashes)):
        raise KpiSemanticReviewFetchError("semantic review manifest repeats a partition hash")
    _validate_authority(
        user_id=manifest.user_id,
        observed_at=manifest.observed_at,
        code_instance_sha256=manifest.code_instance_sha256,
        database_instance_sha256=manifest.database_instance_sha256,
        schema_revision=manifest.schema_revision,
        expected_user_id=expected_user_id,
        now=now,
        max_age=max_age,
        bundle=bundle,
    )
    return manifest


def validate_semantic_review_partition(
    payload: bytes,
    *,
    response_etag: str | None,
    manifest: KpiSemanticReviewTickerManifest,
    pointer: KpiSemanticReviewArtifactPointer,
) -> KpiSemanticReviewExport:
    if len(payload) != pointer.byte_size:
        raise KpiSemanticReviewFetchError(
            "semantic review partition byte size does not match its manifest"
        )
    try:
        export = KpiSemanticReviewExport.model_validate_json(payload)
    except Exception as exc:
        raise KpiSemanticReviewFetchError(
            f"semantic review partition schema validation failed: {redact(exc)}"
        ) from None
    if response_etag != f'"{pointer.content_sha256}"':
        raise KpiSemanticReviewFetchError(
            "semantic review partition ETag does not match its manifest"
        )
    if (
        export.content_sha256 != pointer.content_sha256
        or export.ticker != manifest.ticker
        or export.partition_ordinal != pointer.ordinal
        or export.review.total_items != pointer.item_count
        or export.after_fact_id != pointer.after_fact_id
        or export.next_after_fact_id != pointer.next_after_fact_id
        or export.observed_at != manifest.observed_at
        or export.user_id != manifest.user_id
        or export.code_instance_sha256 != manifest.code_instance_sha256
        or export.database_instance_sha256 != manifest.database_instance_sha256
        or export.schema_revision != manifest.schema_revision
    ):
        raise KpiSemanticReviewFetchError(
            "semantic review partition does not match its current manifest"
        )
    return export


def validate_semantic_review_partition_set(
    manifest: KpiSemanticReviewTickerManifest,
    partitions: tuple[KpiSemanticReviewExport, ...],
) -> None:
    if len(partitions) != len(manifest.partitions):
        raise KpiSemanticReviewFetchError("semantic review partition set is incomplete")
    state_counts: dict[str, int] = {}
    total_items = 0
    for pointer, export in zip(manifest.partitions, partitions, strict=True):
        if export.content_sha256 != pointer.content_sha256:
            raise KpiSemanticReviewFetchError("semantic review partition set is out of order")
        total_items += export.review.total_items
        for state, count in export.review.state_counts.items():
            state_counts[state] = state_counts.get(state, 0) + count
    if total_items != manifest.total_items or state_counts != manifest.state_counts:
        raise KpiSemanticReviewFetchError(
            "semantic review partition contents do not match manifest totals"
        )


def _persist_bytes(
    payload: bytes,
    *,
    content_sha256: str,
    output_root: Path,
    category: Literal["manifests", "partitions"],
    maximum_bytes: int,
) -> Path:
    destination_root = output_root / category
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / f"{content_sha256}.json"
    if destination.exists():
        existing = read_bounded_input(
            destination,
            maximum_bytes=maximum_bytes,
            label=f"existing semantic review {category[:-1]}",
        )
        if existing != payload:
            raise KpiSemanticReviewFetchError(
                f"content-addressed semantic review {category[:-1]} conflicts with existing bytes"
            )
        return destination
    temporary = destination.with_suffix(".tmp")
    temporary.write_bytes(payload)
    temporary.replace(destination)
    return destination


def persist_kpi_semantic_review_manifest(
    manifest: KpiSemanticReviewTickerManifest, *, output_root: Path
) -> Path:
    return _persist_bytes(
        encoded_kpi_semantic_review_ticker_manifest(manifest),
        content_sha256=manifest.content_sha256,
        output_root=output_root,
        category="manifests",
        maximum_bytes=_MAX_MANIFEST_BYTES,
    )


def persist_kpi_semantic_review_export(
    export: KpiSemanticReviewExport, *, output_root: Path
) -> Path:
    return _persist_bytes(
        encoded_kpi_semantic_review_export(export),
        content_sha256=export.content_sha256,
        output_root=output_root,
        category="partitions",
        maximum_bytes=MAX_KPI_SEMANTIC_EXPORT_BYTES,
    )


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
    manifest_url = origin + _ENDPOINT_PREFIX + ticker
    manifest_payload, manifest_etag = fetch_kpi_semantic_review_bytes(
        manifest_url,
        timeout_seconds=args.timeout_seconds,
        maximum_bytes=_MAX_MANIFEST_BYTES,
    )
    manifest = validate_semantic_review_manifest(
        manifest_payload,
        response_etag=manifest_etag,
        ticker=ticker,
        expected_user_id=args.expected_user_id,
        now=now,
        max_age=timedelta(seconds=args.max_age_seconds),
        review_bundle_payload=review_bundle_payload,
        origin=origin,
        pins=pins,
    )

    partitions: list[KpiSemanticReviewExport] = []
    for pointer in manifest.partitions:
        partition_url = origin + _ENDPOINT_PREFIX + ticker + "/partitions/" + pointer.content_sha256
        payload, etag = fetch_kpi_semantic_review_bytes(
            partition_url,
            timeout_seconds=args.timeout_seconds,
            maximum_bytes=MAX_KPI_SEMANTIC_EXPORT_BYTES,
        )
        partitions.append(
            validate_semantic_review_partition(
                payload,
                response_etag=etag,
                manifest=manifest,
                pointer=pointer,
            )
        )
    partition_tuple = tuple(partitions)
    validate_semantic_review_partition_set(manifest, partition_tuple)

    manifest_destination = persist_kpi_semantic_review_manifest(manifest, output_root=output_root)
    partition_destinations = tuple(
        str(persist_kpi_semantic_review_export(export, output_root=output_root))
        for export in partition_tuple
    )
    summary = FetchKpiSemanticReviewSummary(
        ticker=manifest.ticker,
        observed_at=manifest.observed_at,
        manifest_content_sha256=manifest.content_sha256,
        total_items=manifest.total_items,
        manifest_output_path=str(manifest_destination),
        partition_output_paths=partition_destinations,
    )
    print(summary.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
