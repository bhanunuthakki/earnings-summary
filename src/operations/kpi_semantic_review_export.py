"""Bounded, content-addressed KPI semantic-review export artifacts."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator
from pydantic_core import to_jsonable_python

from pipeline.kpi_semantic_review import KpiSemanticReviewBatch

MAX_KPI_SEMANTIC_EXPORT_BYTES = 8 * 1024 * 1024
MAX_KPI_SEMANTIC_EXPORT_ITEMS = 1_000
KPI_SEMANTIC_EXPORT_RELATIVE_ROOT = Path("data/operations/kpi_semantic_reviews")

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_TICKER_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")
_LATEST_MAX_BYTES = 1_000_000
_LOG = logging.getLogger(__name__)
OPERATIONS_GOVERNANCE_DISPOSITION = "no-surface-change: exact v2 reader and publisher contracts"


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


class _SealedModel(_FrozenModel):
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _check_seal(self) -> Self:
        if self.content_sha256 != _payload_sha256(
            self.model_dump(mode="json", exclude={"content_sha256"})
        ):
            raise ValueError("stored semantic review hash is invalid")
        return self


class _SemanticBlob(_SealedModel):
    schema_version: Literal["kpi_semantic_blob.v1"] = "kpi_semantic_blob.v1"
    payload: dict[str, JsonValue]


class _StoredPointer(_FrozenModel):
    wire_sha256: str = Field(pattern=_SHA256_PATTERN)
    semantic_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)


class _ArtifactDescriptor(_SealedModel):
    schema_version: Literal["kpi_semantic_descriptor.v1"] = "kpi_semantic_descriptor.v1"
    observed_at: datetime
    wire_sha256: str = Field(pattern=_SHA256_PATTERN)
    semantic_sha256: str = Field(pattern=_SHA256_PATTERN)


class _Allocation(_FrozenModel):
    new_blob_count: int = Field(default=0, ge=0)
    new_blob_bytes: int = Field(default=0, ge=0)
    reused_blob_count: int = Field(default=0, ge=0)
    reused_blob_bytes: int = Field(default=0, ge=0)
    new_descriptor_count: int = Field(default=0, ge=0)
    new_descriptor_bytes: int = Field(default=0, ge=0)
    predecessor_capture_bytes: int = Field(default=0, ge=0, le=_LATEST_MAX_BYTES)
    served_wire_bytes: int = Field(default=0, ge=0)


class _Capture(_SealedModel):
    schema_version: Literal["kpi_semantic_capture.v1"] = "kpi_semantic_capture.v1"
    index: KpiSemanticReviewExportIndex
    pointers: tuple[_StoredPointer, ...]
    previous_capture_index_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    allocation: _Allocation

    @model_validator(mode="after")
    def _check_pointers(self) -> Self:
        expected = tuple(
            pointer.content_sha256
            for manifest in self.index.ticker_manifests
            for pointer in manifest.partitions
        )
        if tuple(pointer.wire_sha256 for pointer in self.pointers) != expected:
            raise ValueError("capture pointers do not match the complete public index")
        allocation = self.allocation
        if allocation.served_wire_bytes != sum(
            pointer.byte_size
            for manifest in self.index.ticker_manifests
            for pointer in manifest.partitions
        ):
            raise ValueError("capture wire accounting does not match its complete public index")
        if (
            allocation.new_blob_count + allocation.reused_blob_count > len(expected)
            or allocation.new_descriptor_count > len(expected)
            or allocation.new_blob_bytes > allocation.new_blob_count * MAX_KPI_SEMANTIC_EXPORT_BYTES
            or allocation.reused_blob_bytes
            > allocation.reused_blob_count * MAX_KPI_SEMANTIC_EXPORT_BYTES
            or allocation.new_descriptor_bytes > allocation.new_descriptor_count * _LATEST_MAX_BYTES
        ):
            raise ValueError("capture allocation exceeds its bounded object counts")
        if self.previous_capture_index_sha256 == self.index.content_sha256:
            raise ValueError("capture predecessor cannot refer to itself")
        return self


class KpiSemanticReviewStorageObservation(_FrozenModel):
    """New immutable bytes attributed to one publication, not retained directory size."""

    schema_version: Literal["storage-producer-observation/v1"] = "storage-producer-observation/v1"
    id: Literal["kpi-semantic-review"] = "kpi-semantic-review"
    kind: Literal["exports"] = "exports"
    observed_at: datetime
    logical_bytes: int = Field(ge=0)
    files: int = Field(ge=0)
    publisher_mode: Literal["managed"] = "managed"
    pending_since: None = None
    metric: Literal["publication_created_logical_bytes"] = "publication_created_logical_bytes"
    new_blob_count: int
    new_blob_bytes: int
    reused_blob_count: int
    reused_blob_bytes: int
    new_descriptor_count: int
    new_descriptor_bytes: int
    capture_bytes: int
    served_wire_bytes: int
    capture_index_sha256: str = Field(pattern=_SHA256_PATTERN)
    preservation_digest: str = Field(pattern=_SHA256_PATTERN)
    previous_capture_index_sha256: str | None


class KpiSemanticReviewStoragePreservation(_FrozenModel):
    """Validated current and one tested predecessor; this grants no deletion authority."""

    current_capture_index_sha256: str
    previous_capture_index_sha256: str | None
    capture_paths: tuple[Path, ...]
    semantic_blob_paths: tuple[Path, ...]
    descriptor_paths: tuple[Path, ...]
    legacy_artifact_paths: tuple[Path, ...]
    preservation_digest: str


def _encoded_private(model: _FrozenModel, *, bound: int) -> bytes:
    encoded = model.model_dump_json().encode("utf-8") + b"\n"
    if len(encoded) > bound:
        raise KpiSemanticReviewExportError("stored semantic review exceeds its byte bound")
    return encoded


def _semantic_blob(export: KpiSemanticReviewExport) -> _SemanticBlob:
    # Only these two envelope clocks and their derived hashes are removed.
    payload = export.model_dump(mode="json", exclude={"observed_at", "content_sha256"})
    review = export.review.model_dump(mode="json", exclude={"observed_at", "content_sha256"})
    payload["review"] = review
    sealed = {"schema_version": "kpi_semantic_blob.v1", "payload": payload}
    return _SemanticBlob.model_validate({**sealed, "content_sha256": _payload_sha256(sealed)})


def _restore_export(blob: _SemanticBlob, observed_at: datetime) -> KpiSemanticReviewExport:
    payload = dict(blob.payload)
    nested = payload.get("review")
    if not isinstance(nested, dict):
        raise KpiSemanticReviewExportError("semantic blob review is invalid")
    review = dict(nested)
    review["observed_at"] = observed_at.isoformat().replace("+00:00", "Z")
    review["content_sha256"] = _payload_sha256(review)
    payload["review"] = review
    payload["observed_at"] = review["observed_at"]
    payload["content_sha256"] = _payload_sha256(payload)
    try:
        export = KpiSemanticReviewExport.model_validate(payload)
    except ValueError as exc:
        raise KpiSemanticReviewExportError("semantic blob reconstruction is invalid") from exc
    if _semantic_blob(export) != blob:
        raise KpiSemanticReviewExportError("semantic blob contains noncanonical fields")
    return export


def _check_path(path: Path, *, leaf_optional: bool = False) -> None:
    absolute = path.absolute()
    if ".." in absolute.parts:
        raise KpiSemanticReviewExportError("semantic storage path is invalid")
    for component in (*reversed(absolute.parents), absolute):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if leaf_optional:
                continue
            raise KpiSemanticReviewExportError("semantic storage is unavailable") from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise KpiSemanticReviewExportError("semantic storage link is refused")
        if component != absolute and not stat.S_ISDIR(info.st_mode):
            raise KpiSemanticReviewExportError("semantic storage ancestor is invalid")
        if component == absolute and stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise KpiSemanticReviewExportError("semantic storage hard link is refused")


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink


def _read_bounded(path: Path, bound: int) -> bytes:
    _check_path(path)
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise KpiSemanticReviewExportError("semantic storage file is invalid")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
            ) or opened.st_nlink != 1:
                raise KpiSemanticReviewExportError("semantic storage identity changed")
            payload = handle.read(bound + 1)
            after = os.fstat(handle.fileno())
        if _file_identity(opened) != _file_identity(after) or _file_identity(
            path.lstat()
        ) != _file_identity(before):
            raise KpiSemanticReviewExportError("semantic storage identity changed")
    except OSError as exc:
        raise KpiSemanticReviewExportError("semantic storage is unavailable") from exc
    if len(payload) > bound:
        raise KpiSemanticReviewExportError("semantic storage exceeds its byte bound")
    return payload


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _atomic_write(path: Path, payload: bytes) -> None:
    _check_path(path, leaf_optional=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    _check_path(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _check_path(path, leaf_optional=True)
        temporary.replace(path)
        try:
            _sync_directory(path.parent)
        except OSError:
            # Replacement already committed; report durability evidence without a false rollback.
            _LOG.warning("publication_committed_directory_sync_pending")
    finally:
        temporary.unlink(missing_ok=True)


def _write_immutable(path: Path, payload: bytes, *, bound: int) -> bool:
    _check_path(path, leaf_optional=True)
    if path.exists():
        if _read_bounded(path, bound) != payload:
            raise KpiSemanticReviewExportError(
                "content-addressed artifact conflicts with existing bytes"
            )
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    _check_path(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _check_path(path, leaf_optional=True)
        try:
            os.link(temporary, path)
        except FileExistsError:
            if _read_bounded(path, bound) != payload:
                raise KpiSemanticReviewExportError(
                    "content-addressed artifact conflicts with existing bytes"
                ) from None
            return False
        _sync_directory(path.parent)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _capture_path(root: Path, digest: str) -> Path:
    return root / "artifacts" / "captures" / f"{digest}.json"


def _read_capture(root: Path, index: KpiSemanticReviewExportIndex) -> _Capture | None:
    path = _capture_path(root, index.content_sha256)
    _check_path(path, leaf_optional=True)
    if not path.exists():
        return None
    try:
        capture = _Capture.model_validate_json(_read_bounded(path, _LATEST_MAX_BYTES))
    except ValueError as exc:
        raise KpiSemanticReviewExportError("semantic capture is invalid") from exc
    if capture.index != index:
        raise KpiSemanticReviewExportError("semantic capture does not match the public index")
    return capture


def _index_manifest(
    index: KpiSemanticReviewExportIndex, ticker: str
) -> KpiSemanticReviewTickerManifest:
    normalized = normalize_export_ticker(ticker)
    manifest = next((item for item in index.ticker_manifests if item.ticker == normalized), None)
    if manifest is None:
        raise KpiSemanticReviewExportError("ticker is outside the current portfolio export")
    return manifest


class KpiSemanticReviewStorageComponents(_FrozenModel):
    """Pure preparation. No replacement, retention or roster authority is granted."""

    semantic_sha256: str
    blob_bytes: bytes
    descriptor_bytes: bytes
    wire_bytes: bytes
    wire_sha256: str


def prepare_kpi_semantic_review_storage(
    export: KpiSemanticReviewExport,
) -> KpiSemanticReviewStorageComponents:
    export = KpiSemanticReviewExport.model_validate(export.model_dump(mode="json"))
    blob = _semantic_blob(export)
    descriptor_payload = {
        "schema_version": "kpi_semantic_descriptor.v1",
        "observed_at": export.observed_at,
        "wire_sha256": export.content_sha256,
        "semantic_sha256": blob.content_sha256,
    }
    descriptor = _ArtifactDescriptor.model_validate(
        {**descriptor_payload, "content_sha256": _payload_sha256(descriptor_payload)}
    )
    if _restore_export(blob, export.observed_at) != export:
        raise KpiSemanticReviewExportError("semantic export reconstruction conflicts")
    return KpiSemanticReviewStorageComponents(
        semantic_sha256=blob.content_sha256,
        blob_bytes=_encoded_private(blob, bound=MAX_KPI_SEMANTIC_EXPORT_BYTES),
        descriptor_bytes=_encoded_private(descriptor, bound=_LATEST_MAX_BYTES),
        wire_bytes=encoded_kpi_semantic_review_export(export),
        wire_sha256=export.content_sha256,
    )


def _decode_artifact(path: Path) -> tuple[KpiSemanticReviewExport, bytes, str | None]:
    physical = _read_bounded(path, MAX_KPI_SEMANTIC_EXPORT_BYTES)
    try:
        descriptor = _ArtifactDescriptor.model_validate_json(physical)
    except ValueError:
        try:
            export = KpiSemanticReviewExport.model_validate_json(physical)
        except ValueError as exc:
            raise KpiSemanticReviewExportError(
                "semantic review export artifact is invalid"
            ) from exc
        return export, physical, None
    if path.parent.name != "artifacts" or path.name != f"{descriptor.wire_sha256}.json":
        raise KpiSemanticReviewExportError("semantic descriptor path identity is invalid")
    try:
        blob = _SemanticBlob.model_validate_json(
            _read_bounded(
                path.parent / "semantic" / f"{descriptor.semantic_sha256}.json",
                MAX_KPI_SEMANTIC_EXPORT_BYTES,
            )
        )
    except ValueError as exc:
        raise KpiSemanticReviewExportError("semantic blob is invalid") from exc
    if blob.content_sha256 != descriptor.semantic_sha256:
        raise KpiSemanticReviewExportError("semantic blob identity is invalid")
    export = _restore_export(blob, descriptor.observed_at)
    if export.content_sha256 != descriptor.wire_sha256:
        raise KpiSemanticReviewExportError("semantic descriptor wire identity is invalid")
    return export, encoded_kpi_semantic_review_export(export), blob.content_sha256


def decode_kpi_semantic_review_artifact(*, path: Path) -> tuple[KpiSemanticReviewExport, bytes]:
    """Strict explicit-path legacy/descriptor read; does not claim current roster membership."""
    export, payload, _ = _decode_artifact(path)
    return export, payload


def _read_partition(
    root: Path,
    index: KpiSemanticReviewExportIndex,
    manifest: KpiSemanticReviewTickerManifest,
    pointer: KpiSemanticReviewArtifactPointer,
    capture: _Capture | None,
) -> tuple[KpiSemanticReviewExport, bytes]:
    artifact = root / "artifacts" / f"{pointer.content_sha256}.json"
    export, payload, semantic_sha = _decode_artifact(artifact)
    if semantic_sha is not None and capture is None:
        raise KpiSemanticReviewExportError("semantic capture is unavailable")
    if capture is not None:
        stored = next(
            item for item in capture.pointers if item.wire_sha256 == pointer.content_sha256
        )
        if stored.semantic_sha256 != semantic_sha:
            raise KpiSemanticReviewExportError("semantic artifact conflicts with its capture")
    if len(payload) != pointer.byte_size:
        raise KpiSemanticReviewExportError(
            "semantic review export artifact violates its byte bound"
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
        raise KpiSemanticReviewExportError(
            "semantic review export artifact does not match its current ticker manifest"
        )
    return export, payload


def _validate_capture_artifacts(root: Path, capture: _Capture) -> None:
    for manifest in capture.index.ticker_manifests:
        for pointer in manifest.partitions:
            _read_partition(root, capture.index, manifest, pointer, capture)


def _seal_capture(
    index: KpiSemanticReviewExportIndex,
    pointers: tuple[_StoredPointer, ...],
    previous: str | None,
    allocation: _Allocation,
) -> _Capture:
    payload = {
        "schema_version": "kpi_semantic_capture.v1",
        "index": index,
        "pointers": pointers,
        "previous_capture_index_sha256": previous,
        "allocation": allocation,
    }
    return _Capture.model_validate({**payload, "content_sha256": _payload_sha256(payload)})


def _preservation_digest(capture: _Capture) -> str:
    return _payload_sha256(
        {
            "current": capture.index.content_sha256,
            "previous": capture.previous_capture_index_sha256,
            "pointers": capture.pointers,
        }
    )


def _observation(capture: _Capture) -> KpiSemanticReviewStorageObservation:
    allocation = capture.allocation
    capture_bytes = (
        len(_encoded_private(capture, bound=_LATEST_MAX_BYTES))
        + allocation.predecessor_capture_bytes
    )
    return KpiSemanticReviewStorageObservation(
        observed_at=capture.index.observed_at,
        logical_bytes=allocation.new_blob_bytes + allocation.new_descriptor_bytes + capture_bytes,
        files=allocation.new_blob_count
        + allocation.new_descriptor_count
        + 1
        + int(allocation.predecessor_capture_bytes > 0),
        new_blob_count=allocation.new_blob_count,
        new_blob_bytes=allocation.new_blob_bytes,
        reused_blob_count=allocation.reused_blob_count,
        reused_blob_bytes=allocation.reused_blob_bytes,
        new_descriptor_count=allocation.new_descriptor_count,
        new_descriptor_bytes=allocation.new_descriptor_bytes,
        capture_bytes=capture_bytes,
        served_wire_bytes=allocation.served_wire_bytes,
        capture_index_sha256=capture.index.content_sha256,
        preservation_digest=_preservation_digest(capture),
        previous_capture_index_sha256=capture.previous_capture_index_sha256,
    )


def kpi_semantic_review_storage_observation(*, root: Path) -> KpiSemanticReviewStorageObservation:
    """Read bounded sealed capture accounting without scanning semantic payloads."""
    index = KpiSemanticReviewExportIndex.model_validate_json(
        _read_bounded(root / "latest.json", _LATEST_MAX_BYTES)
    )
    capture = _read_capture(root, index)
    if capture is None:
        raise KpiSemanticReviewExportError("semantic capture accounting is unavailable")
    return _observation(capture)


def kpi_semantic_review_storage_preservation(*, root: Path) -> KpiSemanticReviewStoragePreservation:
    index = KpiSemanticReviewExportIndex.model_validate_json(
        _read_bounded(root / "latest.json", _LATEST_MAX_BYTES)
    )
    current = _read_capture(root, index)
    if current is None:
        raise KpiSemanticReviewExportError("semantic capture preservation is unavailable")
    captures = [current]
    if current.previous_capture_index_sha256 is not None:
        try:
            previous = _Capture.model_validate_json(
                _read_bounded(
                    _capture_path(root, current.previous_capture_index_sha256), _LATEST_MAX_BYTES
                )
            )
        except ValueError as exc:
            raise KpiSemanticReviewExportError("previous semantic capture is invalid") from exc
        if previous.index.content_sha256 != current.previous_capture_index_sha256:
            raise KpiSemanticReviewExportError("previous semantic capture identity is invalid")
        captures.append(previous)
    for capture in captures:
        _validate_capture_artifacts(root, capture)
    semantic = sorted(
        {
            root / "artifacts" / "semantic" / f"{pointer.semantic_sha256}.json"
            for capture in captures
            for pointer in capture.pointers
            if pointer.semantic_sha256 is not None
        }
    )
    descriptors = sorted(
        {
            root / "artifacts" / f"{pointer.wire_sha256}.json"
            for capture in captures
            for pointer in capture.pointers
            if pointer.semantic_sha256 is not None
        }
    )
    legacy = sorted(
        {
            root / "artifacts" / f"{pointer.wire_sha256}.json"
            for capture in captures
            for pointer in capture.pointers
            if pointer.semantic_sha256 is None
        }
    )
    return KpiSemanticReviewStoragePreservation(
        current_capture_index_sha256=index.content_sha256,
        previous_capture_index_sha256=current.previous_capture_index_sha256,
        capture_paths=tuple(
            _capture_path(root, capture.index.content_sha256) for capture in captures
        ),
        semantic_blob_paths=tuple(semantic),
        descriptor_paths=tuple(descriptors),
        legacy_artifact_paths=tuple(legacy),
        preservation_digest=_preservation_digest(current),
    )


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

    try:
        return _publish_storage(root, index, encoded_index, tuple(encoded_by_export))
    except (OSError, ValueError) as exc:
        raise KpiSemanticReviewExportError(
            "semantic review publication failed before pointer commit"
        ) from exc


def _publish_storage(
    root: Path,
    index: KpiSemanticReviewExportIndex,
    encoded_index: bytes,
    encoded_exports: tuple[tuple[KpiSemanticReviewExport, bytes], ...],
) -> KpiSemanticReviewExportIndex:
    previous: _Capture | None = None
    predecessor_bytes = 0
    _check_path(root / "latest.json", leaf_optional=True)
    if (root / "latest.json").exists():
        prior_index = KpiSemanticReviewExportIndex.model_validate_json(
            _read_bounded(root / "latest.json", _LATEST_MAX_BYTES)
        )
        previous = _read_capture(root, prior_index)
        if previous is None:
            for manifest in prior_index.ticker_manifests:
                for pointer in manifest.partitions:
                    _read_partition(root, prior_index, manifest, pointer, None)
            previous = _seal_capture(
                prior_index,
                tuple(
                    _StoredPointer(wire_sha256=pointer.content_sha256)
                    for manifest in prior_index.ticker_manifests
                    for pointer in manifest.partitions
                ),
                None,
                _Allocation(
                    served_wire_bytes=sum(
                        pointer.byte_size
                        for manifest in prior_index.ticker_manifests
                        for pointer in manifest.partitions
                    )
                ),
            )
            previous_payload = _encoded_private(previous, bound=_LATEST_MAX_BYTES)
            if _write_immutable(
                _capture_path(root, prior_index.content_sha256),
                previous_payload,
                bound=_LATEST_MAX_BYTES,
            ):
                predecessor_bytes = len(previous_payload)
        _validate_capture_artifacts(root, previous)
        if prior_index == index:
            observation = _observation(previous)
            _write_observation(root, observation)
            return index
        # Test only the named predecessor; do not chase unbounded history chains.
        kpi_semantic_review_storage_preservation(root=root)
    pointers: list[_StoredPointer] = []
    new_blobs: dict[str, int] = {}
    reused_blobs: dict[str, int] = {}
    descriptor_count = descriptor_bytes = 0
    for export, wire in encoded_exports:
        components = prepare_kpi_semantic_review_storage(export)
        blob_sha = components.semantic_sha256
        blob_bytes = components.blob_bytes
        artifact = root / "artifacts" / f"{export.content_sha256}.json"
        physical = components.descriptor_bytes
        _check_path(artifact, leaf_optional=True)
        if artifact.exists() and _read_bounded(artifact, MAX_KPI_SEMANTIC_EXPORT_BYTES) == wire:
            pointers.append(_StoredPointer(wire_sha256=export.content_sha256))
            continue
        if _write_immutable(
            root / "artifacts" / "semantic" / f"{blob_sha}.json",
            blob_bytes,
            bound=MAX_KPI_SEMANTIC_EXPORT_BYTES,
        ):
            new_blobs[blob_sha] = len(blob_bytes)
        elif blob_sha not in new_blobs:
            reused_blobs[blob_sha] = len(blob_bytes)
        if _write_immutable(artifact, physical, bound=MAX_KPI_SEMANTIC_EXPORT_BYTES):
            descriptor_count += 1
            descriptor_bytes += len(physical)
        pointers.append(_StoredPointer(wire_sha256=export.content_sha256, semantic_sha256=blob_sha))
    candidate = _seal_capture(
        index,
        tuple(pointers),
        previous.index.content_sha256 if previous else None,
        _Allocation(
            new_blob_count=len(new_blobs),
            new_blob_bytes=sum(new_blobs.values()),
            reused_blob_count=len(reused_blobs),
            reused_blob_bytes=sum(reused_blobs.values()),
            new_descriptor_count=descriptor_count,
            new_descriptor_bytes=descriptor_bytes,
            predecessor_capture_bytes=predecessor_bytes,
            served_wire_bytes=sum(len(wire) for _, wire in encoded_exports),
        ),
    )
    existing = _read_capture(root, index)
    if existing is not None:
        if (
            existing.pointers != candidate.pointers
            or existing.previous_capture_index_sha256 != candidate.previous_capture_index_sha256
        ):
            raise KpiSemanticReviewExportError(
                "semantic capture conflicts with pending publication"
            )
        candidate = existing  # Preserve original admission accounting across an interrupted retry.
    _write_immutable(
        _capture_path(root, index.content_sha256),
        _encoded_private(candidate, bound=_LATEST_MAX_BYTES),
        bound=_LATEST_MAX_BYTES,
    )
    _validate_capture_artifacts(root, candidate)
    _atomic_write(root / "latest.json", encoded_index)
    _write_observation(root, _observation(candidate))
    return index


def _write_observation(root: Path, observation: KpiSemanticReviewStorageObservation) -> None:
    try:
        _atomic_write(
            root / "storage-observation.json",
            _encoded_private(observation, bound=_LATEST_MAX_BYTES),
        )
    except (OSError, KpiSemanticReviewExportError):
        # The public pointer already committed. Optional telemetry must not undo it.
        _LOG.warning("publication_committed_metadata_pending")


def _load_current_index(
    *, root: Path, now: datetime, max_age: timedelta
) -> tuple[KpiSemanticReviewExportIndex, bytes]:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    payload = _read_bounded(root / "latest.json", _LATEST_MAX_BYTES)
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
    index, _ = _load_current_index(root=root, now=now, max_age=max_age)
    manifest = _index_manifest(index, ticker)
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
    index, _ = _load_current_index(root=root, now=now, max_age=max_age)
    manifest = _index_manifest(index, ticker)
    pointer = next(
        (item for item in manifest.partitions if item.content_sha256 == content_sha256), None
    )
    if pointer is None:
        raise KpiSemanticReviewExportError(
            "semantic review partition is not referenced by the current ticker manifest"
        )
    return _read_partition(root, index, manifest, pointer, _read_capture(root, index))


def load_current_kpi_semantic_review_export(
    *,
    root: Path,
    ticker: str,
    now: datetime,
    max_age: timedelta,
) -> tuple[KpiSemanticReviewExport, bytes]:
    """Compatibility loader for a current ticker with exactly one partition."""
    index, _ = _load_current_index(root=root, now=now, max_age=max_age)
    manifest = _index_manifest(index, ticker)
    if len(manifest.partitions) != 1:
        raise KpiSemanticReviewExportError(
            "ticker has multiple semantic review partitions; load an exact partition"
        )
    return _read_partition(
        root, index, manifest, manifest.partitions[0], _read_capture(root, index)
    )
