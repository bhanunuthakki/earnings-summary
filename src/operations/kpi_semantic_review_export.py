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


def _validate_state_counts(state_counts: dict[str, int], *, total_items: int) -> None:
    if any(not state or count <= 0 for state, count in state_counts.items()):
        raise ValueError("semantic review state counts must contain positive named counts")
    if sum(state_counts.values()) != total_items:
        raise ValueError("semantic review state counts do not match the item total")


class KpiSemanticReviewExport(_FrozenModel):
    """One immutable, bounded page in a ticker's complete review export."""

    schema_version: Literal["windows_kpi_semantic_review_export.v2"] = (
        "windows_kpi_semantic_review_export.v2"
    )
    observed_at: datetime
    user_id: str = Field(min_length=1, max_length=128)
    ticker: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,14}$")
    code_instance_sha256: str = Field(pattern=_SHA256_PATTERN)
    database_instance_sha256: str = Field(pattern=_SHA256_PATTERN)
    schema_revision: str = Field(min_length=1, max_length=160)
    partition_ordinal: int = Field(ge=0)
    after_fact_id: int = Field(ge=0)
    next_after_fact_id: int | None = Field(default=None, gt=0)
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
        if self.review.total_items > MAX_KPI_SEMANTIC_EXPORT_ITEMS:
            raise ValueError("semantic review export exceeds the item bound")
        if (self.partition_ordinal == 0) != (self.after_fact_id == 0):
            raise ValueError("semantic review partition origin is inconsistent")
        fact_ids = [item.fact_id for item in self.review.items]
        if fact_ids != sorted(fact_ids) or len(fact_ids) != len(set(fact_ids)):
            raise ValueError("semantic review partition fact IDs must strictly increase")
        if any(fact_id <= self.after_fact_id for fact_id in fact_ids):
            raise ValueError("semantic review partition contains a fact at or before its cursor")
        if self.review.truncated:
            if not fact_ids or self.next_after_fact_id != fact_ids[-1]:
                raise ValueError("nonterminal semantic review partition has an invalid next cursor")
        elif self.next_after_fact_id is not None:
            raise ValueError("terminal semantic review partition cannot have a next cursor")
        expected = _payload_sha256(self.model_dump(mode="json", exclude={"content_sha256"}))
        if self.content_sha256 != expected:
            raise ValueError("semantic review export content hash does not match its payload")
        return self


class KpiSemanticReviewArtifactPointer(_FrozenModel):
    ticker: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,14}$")
    ordinal: int = Field(ge=0)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    byte_size: int = Field(gt=0, le=MAX_KPI_SEMANTIC_EXPORT_BYTES)
    item_count: int = Field(ge=0, le=MAX_KPI_SEMANTIC_EXPORT_ITEMS)
    after_fact_id: int = Field(ge=0)
    next_after_fact_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _validate_cursor(self) -> Self:
        if self.next_after_fact_id is not None and (
            self.item_count == 0 or self.next_after_fact_id <= self.after_fact_id
        ):
            raise ValueError("semantic review partition pointer cursor does not progress")
        return self


class KpiSemanticReviewTickerManifest(_FrozenModel):
    """Complete current partition manifest for one portfolio ticker."""

    schema_version: Literal["windows_kpi_semantic_review_ticker_manifest.v1"] = (
        "windows_kpi_semantic_review_ticker_manifest.v1"
    )
    observed_at: datetime
    user_id: str = Field(min_length=1, max_length=128)
    ticker: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,14}$")
    code_instance_sha256: str = Field(pattern=_SHA256_PATTERN)
    database_instance_sha256: str = Field(pattern=_SHA256_PATTERN)
    schema_revision: str = Field(min_length=1, max_length=160)
    total_items: int = Field(ge=0)
    state_counts: dict[str, int]
    partitions: tuple[KpiSemanticReviewArtifactPointer, ...] = Field(min_length=1)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("observed_at")
    @classmethod
    def _observed_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("semantic review manifest timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _validate_contract(self) -> Self:
        _validate_state_counts(self.state_counts, total_items=self.total_items)
        if self.total_items > 0 and any(pointer.item_count == 0 for pointer in self.partitions):
            raise ValueError("nonempty ticker exports cannot contain an empty partition")
        if sum(pointer.item_count for pointer in self.partitions) != self.total_items:
            raise ValueError("semantic review partition counts do not match ticker total")
        if self.total_items == 0 and (
            len(self.partitions) != 1 or self.partitions[0].item_count != 0
        ):
            raise ValueError("empty ticker exports require exactly one empty partition")
        if any(pointer.ticker != self.ticker for pointer in self.partitions):
            raise ValueError("semantic review partition ticker does not match its manifest")
        if [pointer.ordinal for pointer in self.partitions] != list(range(len(self.partitions))):
            raise ValueError("semantic review partition ordinals must be contiguous")
        if self.partitions[0].after_fact_id != 0:
            raise ValueError("semantic review ticker manifest must begin at cursor zero")
        for position, pointer in enumerate(self.partitions):
            is_last = position == len(self.partitions) - 1
            if is_last != (pointer.next_after_fact_id is None):
                raise ValueError("only the final semantic review partition may be terminal")
            if not is_last:
                following = self.partitions[position + 1]
                if pointer.next_after_fact_id != following.after_fact_id:
                    raise ValueError("semantic review partition cursor chain is not contiguous")
        expected = _payload_sha256(self.model_dump(mode="json", exclude={"content_sha256"}))
        if self.content_sha256 != expected:
            raise ValueError("semantic review ticker manifest hash does not match its payload")
        return self


class KpiSemanticReviewExportIndex(_FrozenModel):
    schema_version: Literal["windows_kpi_semantic_review_index.v2"] = (
        "windows_kpi_semantic_review_index.v2"
    )
    observed_at: datetime
    user_id: str = Field(min_length=1, max_length=128)
    code_instance_sha256: str = Field(pattern=_SHA256_PATTERN)
    database_instance_sha256: str = Field(pattern=_SHA256_PATTERN)
    schema_revision: str = Field(min_length=1, max_length=160)
    total_items: int = Field(ge=0)
    state_counts: dict[str, int]
    ticker_manifests: tuple[KpiSemanticReviewTickerManifest, ...] = Field(min_length=1)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("observed_at")
    @classmethod
    def _observed_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("semantic review index timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _validate_contract(self) -> Self:
        tickers = [manifest.ticker for manifest in self.ticker_manifests]
        if tickers != sorted(tickers) or len(tickers) != len(set(tickers)):
            raise ValueError("semantic review index tickers must be unique and sorted")
        authority = (
            self.observed_at,
            self.user_id,
            self.code_instance_sha256,
            self.database_instance_sha256,
            self.schema_revision,
        )
        if any(
            (
                manifest.observed_at,
                manifest.user_id,
                manifest.code_instance_sha256,
                manifest.database_instance_sha256,
                manifest.schema_revision,
            )
            != authority
            for manifest in self.ticker_manifests
        ):
            raise ValueError("semantic review ticker manifests do not share index authority")
        if sum(manifest.total_items for manifest in self.ticker_manifests) != self.total_items:
            raise ValueError("semantic review ticker totals do not match the index total")
        _validate_state_counts(self.state_counts, total_items=self.total_items)
        aggregate_counts: dict[str, int] = {}
        for manifest in self.ticker_manifests:
            for state, count in manifest.state_counts.items():
                aggregate_counts[state] = aggregate_counts.get(state, 0) + count
        if self.state_counts != aggregate_counts:
            raise ValueError("semantic review ticker state counts do not match the index")
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
    partition_ordinal: int = 0,
    after_fact_id: int = 0,
    next_after_fact_id: int | None = None,
) -> KpiSemanticReviewExport:
    ticker = normalize_export_ticker(review.ticker or "")
    payload = {
        "schema_version": "windows_kpi_semantic_review_export.v2",
        "observed_at": review.observed_at,
        "user_id": review.user_id,
        "ticker": ticker,
        "code_instance_sha256": code_instance_sha256,
        "database_instance_sha256": database_instance_sha256,
        "schema_revision": schema_revision,
        "partition_ordinal": partition_ordinal,
        "after_fact_id": after_fact_id,
        "next_after_fact_id": next_after_fact_id,
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


def encoded_kpi_semantic_review_ticker_manifest(
    manifest: KpiSemanticReviewTickerManifest,
) -> bytes:
    encoded = manifest.model_dump_json(indent=2).encode("utf-8") + b"\n"
    if len(encoded) > _LATEST_MAX_BYTES:
        raise KpiSemanticReviewExportError("semantic review ticker manifest exceeds its byte bound")
    return encoded


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _state_counts(exports: tuple[KpiSemanticReviewExport, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for export in exports:
        for state, count in export.review.state_counts.items():
            counts[state] = counts.get(state, 0) + count
    return {state: counts[state] for state in sorted(counts)}


def _ticker_manifest(
    *,
    exports: tuple[KpiSemanticReviewExport, ...],
    pointers: tuple[KpiSemanticReviewArtifactPointer, ...],
) -> KpiSemanticReviewTickerManifest:
    first = exports[0]
    payload = {
        "schema_version": "windows_kpi_semantic_review_ticker_manifest.v1",
        "observed_at": first.observed_at,
        "user_id": first.user_id,
        "ticker": first.ticker,
        "code_instance_sha256": first.code_instance_sha256,
        "database_instance_sha256": first.database_instance_sha256,
        "schema_revision": first.schema_revision,
        "total_items": sum(export.review.total_items for export in exports),
        "state_counts": _state_counts(exports),
        "partitions": pointers,
    }
    return KpiSemanticReviewTickerManifest.model_validate(
        {**payload, "content_sha256": _payload_sha256(payload)}
    )


def publish_kpi_semantic_review_exports(
    *,
    root: Path,
    exports: tuple[KpiSemanticReviewExport, ...],
    expected_tickers: tuple[str, ...],
) -> KpiSemanticReviewExportIndex:
    if not exports:
        raise KpiSemanticReviewExportError("semantic review export set is empty")
    normalized_expected = tuple(
        sorted(normalize_export_ticker(ticker) for ticker in expected_tickers)
    )
    if not normalized_expected or len(normalized_expected) != len(set(normalized_expected)):
        raise KpiSemanticReviewExportError("expected portfolio tickers must be present and unique")
    validated_exports = tuple(
        KpiSemanticReviewExport.model_validate(item.model_dump(mode="json")) for item in exports
    )
    ordered = tuple(
        sorted(validated_exports, key=lambda item: (item.ticker, item.partition_ordinal))
    )
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
    if len(identities) != 1:
        raise KpiSemanticReviewExportError(
            "semantic review exports must have one authority identity"
        )
    actual_tickers = tuple(sorted({item.ticker for item in ordered}))
    if actual_tickers != normalized_expected:
        raise KpiSemanticReviewExportError(
            "semantic review exports do not cover the complete expected portfolio"
        )

    encoded_by_export: list[tuple[KpiSemanticReviewExport, bytes]] = []
    manifests: list[KpiSemanticReviewTickerManifest] = []
    for ticker in normalized_expected:
        ticker_exports = tuple(item for item in ordered if item.ticker == ticker)
        encoded_ticker: list[tuple[KpiSemanticReviewExport, bytes]] = []
        pointers: list[KpiSemanticReviewArtifactPointer] = []
        for export in ticker_exports:
            encoded = encoded_kpi_semantic_review_export(export)
            encoded_ticker.append((export, encoded))
            pointers.append(
                KpiSemanticReviewArtifactPointer(
                    ticker=export.ticker,
                    ordinal=export.partition_ordinal,
                    content_sha256=export.content_sha256,
                    byte_size=len(encoded),
                    item_count=export.review.total_items,
                    after_fact_id=export.after_fact_id,
                    next_after_fact_id=export.next_after_fact_id,
                )
            )
        manifests.append(_ticker_manifest(exports=ticker_exports, pointers=tuple(pointers)))
        encoded_by_export.extend(encoded_ticker)

    global_counts: dict[str, int] = {}
    for manifest in manifests:
        for state, count in manifest.state_counts.items():
            global_counts[state] = global_counts.get(state, 0) + count
    index_payload = {
        "schema_version": "windows_kpi_semantic_review_index.v2",
        "observed_at": first.observed_at,
        "user_id": first.user_id,
        "code_instance_sha256": first.code_instance_sha256,
        "database_instance_sha256": first.database_instance_sha256,
        "schema_revision": first.schema_revision,
        "total_items": sum(manifest.total_items for manifest in manifests),
        "state_counts": {state: global_counts[state] for state in sorted(global_counts)},
        "ticker_manifests": tuple(manifests),
    }
    index = KpiSemanticReviewExportIndex.model_validate(
        {**index_payload, "content_sha256": _payload_sha256(index_payload)}
    )
    encoded_index = index.model_dump_json(indent=2).encode("utf-8") + b"\n"
    if len(encoded_index) > _LATEST_MAX_BYTES:
        raise KpiSemanticReviewExportError("semantic review export index exceeds its byte bound")

    artifact_root = root / "artifacts"
    for export, encoded in encoded_by_export:
        destination = artifact_root / f"{export.content_sha256}.json"
        if destination.exists():
            try:
                with destination.open("rb") as handle:
                    existing = handle.read(MAX_KPI_SEMANTIC_EXPORT_BYTES + 1)
            except OSError as exc:
                raise KpiSemanticReviewExportError(
                    "content-addressed semantic review artifact is unavailable"
                ) from exc
            if len(existing) > MAX_KPI_SEMANTIC_EXPORT_BYTES:
                raise KpiSemanticReviewExportError(
                    "content-addressed semantic review artifact exceeds its byte bound"
                )
            if existing != encoded:
                raise KpiSemanticReviewExportError(
                    "content-addressed semantic review artifact conflicts with existing bytes"
                )
    for export, encoded in encoded_by_export:
        destination = artifact_root / f"{export.content_sha256}.json"
        if not destination.exists():
            _atomic_write(destination, encoded)
    _atomic_write(root / "latest.json", encoded_index)
    return index


def _load_current_index(
    *, root: Path, now: datetime, max_age: timedelta
) -> tuple[KpiSemanticReviewExportIndex, bytes]:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    latest = root / "latest.json"
    try:
        with latest.open("rb") as handle:
            payload = handle.read(_LATEST_MAX_BYTES + 1)
    except OSError as exc:
        raise KpiSemanticReviewExportError("semantic review export index is unavailable") from exc
    if len(payload) > _LATEST_MAX_BYTES:
        raise KpiSemanticReviewExportError("semantic review export index exceeds its byte bound")
    try:
        index = KpiSemanticReviewExportIndex.model_validate_json(payload)
    except Exception as exc:
        raise KpiSemanticReviewExportError("semantic review export index is invalid") from exc
    if index.observed_at > now + timedelta(minutes=5) or now - index.observed_at > max_age:
        raise KpiSemanticReviewExportError("semantic review export index is stale or future-dated")
    return index, payload


def load_current_kpi_semantic_review_ticker_manifest(
    *, root: Path, ticker: str, now: datetime, max_age: timedelta
) -> tuple[KpiSemanticReviewTickerManifest, bytes]:
    normalized = normalize_export_ticker(ticker)
    index, _ = _load_current_index(root=root, now=now, max_age=max_age)
    manifest = next((item for item in index.ticker_manifests if item.ticker == normalized), None)
    if manifest is None:
        raise KpiSemanticReviewExportError("ticker is outside the current portfolio export")
    return manifest, encoded_kpi_semantic_review_ticker_manifest(manifest)


def load_current_kpi_semantic_review_partition(
    *,
    root: Path,
    ticker: str,
    content_sha256: str,
    now: datetime,
    max_age: timedelta,
) -> tuple[KpiSemanticReviewExport, bytes]:
    if re.fullmatch(_SHA256_PATTERN, content_sha256) is None:
        raise KpiSemanticReviewExportError("semantic review partition hash is invalid")
    manifest, _ = load_current_kpi_semantic_review_ticker_manifest(
        root=root, ticker=ticker, now=now, max_age=max_age
    )
    pointer = next(
        (item for item in manifest.partitions if item.content_sha256 == content_sha256), None
    )
    if pointer is None:
        raise KpiSemanticReviewExportError(
            "semantic review partition is not referenced by the current ticker manifest"
        )
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
        raise KpiSemanticReviewExportError(
            "semantic review export artifact does not match its current ticker manifest"
        )
    return export, payload


def load_current_kpi_semantic_review_export(
    *, root: Path, ticker: str, now: datetime, max_age: timedelta
) -> tuple[KpiSemanticReviewExport, bytes]:
    """Compatibility loader for a current ticker with exactly one partition."""

    manifest, _ = load_current_kpi_semantic_review_ticker_manifest(
        root=root, ticker=ticker, now=now, max_age=max_age
    )
    if len(manifest.partitions) != 1:
        raise KpiSemanticReviewExportError(
            "ticker has multiple semantic review partitions; load an exact partition"
        )
    return load_current_kpi_semantic_review_partition(
        root=root,
        ticker=manifest.ticker,
        content_sha256=manifest.partitions[0].content_sha256,
        now=now,
        max_age=max_age,
    )
