"""Bounded, content-addressed KPI semantic-review export artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import to_jsonable_python

from pipeline.kpi_semantic_review import KpiSemanticReviewBatch

MAX_KPI_SEMANTIC_EXPORT_BYTES = 8 * 1024 * 1024
MAX_KPI_SEMANTIC_EXPORT_ITEMS = 1_000
KPI_SEMANTIC_EXPORT_RELATIVE_ROOT = Path("data/operations/kpi_semantic_reviews")

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_TICKER_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")
_LATEST_MAX_BYTES = 1_000_000


class KpiSemanticReviewExportError(RuntimeError):
    """A closed validation, publication, or artifact-read failure."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def normalize_export_ticker(value: str) -> str:
    ticker = value.strip().upper()
    if _TICKER_PATTERN.fullmatch(ticker) is None:
        raise KpiSemanticReviewExportError("ticker is invalid")
    return ticker


def _payload_sha256(value: object) -> str:
    canonical = json.dumps(
        to_jsonable_python(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


payload_sha256 = _payload_sha256


class KpiSemanticReviewExport(_FrozenModel):
    schema_version: Literal["windows_kpi_semantic_review_export.v1"] = (
        "windows_kpi_semantic_review_export.v1"
    )
    observed_at: datetime
    user_id: str = Field(min_length=1, max_length=128)
    ticker: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,14}$")
    code_instance_sha256: str = Field(pattern=_SHA256_PATTERN)
    database_instance_sha256: str = Field(pattern=_SHA256_PATTERN)
    schema_revision: str = Field(min_length=1, max_length=160)
    review: KpiSemanticReviewBatch
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("observed_at")
    @classmethod
    def _observed_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("semantic review export timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _validate_contract(self) -> Self:
        if self.review.schema_version != "kpi_semantic_review.v3":
            raise ValueError("semantic review export requires kpi_semantic_review.v3")
        if self.review.ticker != self.ticker:
            raise ValueError("semantic review export ticker does not match its review batch")
        if self.review.user_id != self.user_id:
            raise ValueError("semantic review export user does not match its review batch")
        if self.review.observed_at != self.observed_at:
            raise ValueError("semantic review export timestamp does not match its review batch")
        if self.review.truncated:
            raise ValueError("truncated semantic review batches cannot be exported")
        if any(
            item.evidence_candidates_truncated or item.evidence_search_incomplete
            for item in self.review.items
        ):
            raise ValueError("incomplete semantic review evidence cannot be exported")
        if self.review.total_items > MAX_KPI_SEMANTIC_EXPORT_ITEMS:
            raise ValueError("semantic review export exceeds the item bound")
        expected = _payload_sha256(self.model_dump(mode="json", exclude={"content_sha256"}))
        if self.content_sha256 != expected:
            raise ValueError("semantic review export content hash does not match its payload")
        return self


class KpiSemanticReviewArtifactPointer(_FrozenModel):
    ticker: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,14}$")
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    byte_size: int = Field(gt=0, le=MAX_KPI_SEMANTIC_EXPORT_BYTES)


class KpiSemanticReviewExportIndex(_FrozenModel):
    schema_version: Literal["windows_kpi_semantic_review_index.v1"] = (
        "windows_kpi_semantic_review_index.v1"
    )
    observed_at: datetime
    user_id: str = Field(min_length=1, max_length=128)
    code_instance_sha256: str = Field(pattern=_SHA256_PATTERN)
    database_instance_sha256: str = Field(pattern=_SHA256_PATTERN)
    schema_revision: str = Field(min_length=1, max_length=160)
    artifacts: tuple[KpiSemanticReviewArtifactPointer, ...]
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("observed_at")
    @classmethod
    def _observed_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("semantic review index timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _validate_contract(self) -> Self:
        tickers = [artifact.ticker for artifact in self.artifacts]
        if tickers != sorted(tickers) or len(tickers) != len(set(tickers)):
            raise ValueError("semantic review index tickers must be unique and sorted")
        expected = _payload_sha256(self.model_dump(mode="json", exclude={"content_sha256"}))
        if self.content_sha256 != expected:
            raise ValueError("semantic review index content hash does not match its payload")
        return self


def seal_kpi_semantic_review_export(
    *,
    review: KpiSemanticReviewBatch,
    code_instance_sha256: str,
    database_instance_sha256: str,
    schema_revision: str,
) -> KpiSemanticReviewExport:
    ticker = normalize_export_ticker(review.ticker or "")
    payload = {
        "schema_version": "windows_kpi_semantic_review_export.v1",
        "observed_at": review.observed_at,
        "user_id": review.user_id,
        "ticker": ticker,
        "code_instance_sha256": code_instance_sha256,
        "database_instance_sha256": database_instance_sha256,
        "schema_revision": schema_revision,
        "review": review,
    }
    return KpiSemanticReviewExport.model_validate(
        {**payload, "content_sha256": _payload_sha256(payload)}
    )


def encoded_kpi_semantic_review_export(export: KpiSemanticReviewExport) -> bytes:
    encoded = export.model_dump_json(indent=2).encode("utf-8") + b"\n"
    if len(encoded) > MAX_KPI_SEMANTIC_EXPORT_BYTES:
        raise KpiSemanticReviewExportError("semantic review export exceeds the byte bound")
    return encoded


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def publish_kpi_semantic_review_exports(
    *, root: Path, exports: tuple[KpiSemanticReviewExport, ...]
) -> KpiSemanticReviewExportIndex:
    if not exports:
        raise KpiSemanticReviewExportError("semantic review export set is empty")
    ordered = tuple(sorted(exports, key=lambda item: item.ticker))
    first = ordered[0]
    identities = {
        (
            item.observed_at,
            item.user_id,
            item.code_instance_sha256,
            item.database_instance_sha256,
            item.schema_revision,
        )
        for item in ordered
    }
    if len(identities) != 1 or len({item.ticker for item in ordered}) != len(ordered):
        raise KpiSemanticReviewExportError(
            "semantic review exports must have one authority identity and unique tickers"
        )
    pointers: list[KpiSemanticReviewArtifactPointer] = []
    artifact_root = root / "artifacts"
    for export in ordered:
        encoded = encoded_kpi_semantic_review_export(export)
        destination = artifact_root / f"{export.content_sha256}.json"
        if destination.exists():
            if destination.read_bytes() != encoded:
                raise KpiSemanticReviewExportError(
                    "content-addressed semantic review artifact conflicts with existing bytes"
                )
        else:
            _atomic_write(destination, encoded)
        pointers.append(
            KpiSemanticReviewArtifactPointer(
                ticker=export.ticker,
                content_sha256=export.content_sha256,
                byte_size=len(encoded),
            )
        )
    index_payload = {
        "schema_version": "windows_kpi_semantic_review_index.v1",
        "observed_at": first.observed_at,
        "user_id": first.user_id,
        "code_instance_sha256": first.code_instance_sha256,
        "database_instance_sha256": first.database_instance_sha256,
        "schema_revision": first.schema_revision,
        "artifacts": tuple(pointers),
    }
    index = KpiSemanticReviewExportIndex.model_validate(
        {**index_payload, "content_sha256": _payload_sha256(index_payload)}
    )
    encoded_index = index.model_dump_json(indent=2).encode("utf-8") + b"\n"
    if len(encoded_index) > _LATEST_MAX_BYTES:
        raise KpiSemanticReviewExportError("semantic review export index exceeds its byte bound")
    _atomic_write(root / "latest.json", encoded_index)
    return index


def load_current_kpi_semantic_review_export(
    *, root: Path, ticker: str, now: datetime, max_age: timedelta
) -> tuple[KpiSemanticReviewExport, bytes]:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    normalized = normalize_export_ticker(ticker)
    latest = root / "latest.json"
    try:
        with latest.open("rb") as handle:
            index_payload = handle.read(_LATEST_MAX_BYTES + 1)
    except OSError as exc:
        raise KpiSemanticReviewExportError("semantic review export index is unavailable") from exc
    if len(index_payload) > _LATEST_MAX_BYTES:
        raise KpiSemanticReviewExportError("semantic review export index exceeds its byte bound")
    try:
        index = KpiSemanticReviewExportIndex.model_validate_json(index_payload)
    except Exception as exc:
        raise KpiSemanticReviewExportError("semantic review export index is invalid") from exc
    if index.observed_at > now + timedelta(minutes=5) or now - index.observed_at > max_age:
        raise KpiSemanticReviewExportError("semantic review export index is stale or future-dated")
    pointer = next((item for item in index.artifacts if item.ticker == normalized), None)
    if pointer is None:
        raise KpiSemanticReviewExportError("ticker is outside the current portfolio export")
    artifact = root / "artifacts" / f"{pointer.content_sha256}.json"
    try:
        with artifact.open("rb") as handle:
            payload = handle.read(MAX_KPI_SEMANTIC_EXPORT_BYTES + 1)
    except OSError as exc:
        raise KpiSemanticReviewExportError(
            "semantic review export artifact is unavailable"
        ) from exc
    if len(payload) > MAX_KPI_SEMANTIC_EXPORT_BYTES or len(payload) != pointer.byte_size:
        raise KpiSemanticReviewExportError(
            "semantic review export artifact violates its byte bound"
        )
    try:
        export = KpiSemanticReviewExport.model_validate_json(payload)
    except Exception as exc:
        raise KpiSemanticReviewExportError("semantic review export artifact is invalid") from exc
    if (
        export.content_sha256 != pointer.content_sha256
        or export.ticker != normalized
        or export.observed_at != index.observed_at
        or export.user_id != index.user_id
        or export.code_instance_sha256 != index.code_instance_sha256
        or export.database_instance_sha256 != index.database_instance_sha256
        or export.schema_revision != index.schema_revision
    ):
        raise KpiSemanticReviewExportError(
            "semantic review export artifact does not match its current index"
        )
    return export, payload
