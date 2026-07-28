"""Bounded, dry-run-first publication of exact PDF-table extraction artifacts.

The operational boundary is deliberately narrow: callers select immutable
evidence document-version identities, raw bytes are resolved only from the
content blob row bound to each version, and parsing completes before the
database writer lock is acquired.  Quarantined detector outputs are retained
as explicit artifacts but are never published as document-processing evidence.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from provenance.document_processing_evidence import (
    PdfTableArtifactPersistenceReceipt,
    record_pdf_table_extraction_artifact,
)
from provenance.evidence_ledger import EvidenceLedger, ExtractionRun
from provenance.evidence_native_candidates import resolve_local_storage_uri
from provenance.fulltext_extractor_identity import (
    PDF_TABLE_EXTRACTOR_NAME,
    pdf_table_extractor_code_version,
)
from provenance.pdf_table_extraction import PdfTableExtractionArtifact, extract_pdf_tables
from runtime.job_runtime import JobLock
from schema_compat import require_current_for_write
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_Mode = Literal["apply", "dry_run"]
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class PdfTableBackfillError(RuntimeError):
    """Base class for deterministic PDF-table backfill failures."""


class PdfTableBackfillIntegrityError(PdfTableBackfillError):
    """Recorded evidence identity does not match its local bytes."""


class PdfTableBackfillRequest(BaseModel):
    """Closed controls for one exact, bounded PDF-table batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    db_path: Path
    repo_root: Path
    content_roots: tuple[Path, ...] = Field(min_length=1, max_length=32)
    document_version_ids: tuple[str, ...] = Field(min_length=1, max_length=10_000)
    recorded_at: datetime
    apply: bool = False
    batch_size: int = Field(default=25, ge=1, le=1_000)
    maximum_pdf_bytes: int = Field(
        default=100 * 1024 * 1024,
        ge=1,
        le=512 * 1024 * 1024,
    )
    task_id: str = Field(
        default="pdf-table-evidence-backfill",
        pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$",
    )

    @field_validator("recorded_at")
    @classmethod
    def _aware_recorded_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("recorded_at must include an explicit UTC offset")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _closed_selection(self) -> Self:
        if len(set(self.document_version_ids)) != len(self.document_version_ids):
            raise ValueError("document_version_ids must be unique")
        if self.document_version_ids != tuple(sorted(self.document_version_ids)):
            raise ValueError("document_version_ids must be sorted")
        return self

    @property
    def selection_sha256(self) -> str:
        payload = {
            "document_version_ids": list(self.document_version_ids),
            "recorded_at": self.recorded_at.isoformat(),
            "maximum_pdf_bytes": self.maximum_pdf_bytes,
            "content_roots": [str(root.resolve()) for root in sorted(self.content_roots, key=str)],
        }
        return _digest(payload)


class PdfTableBackfillCheckpoint(BaseModel):
    """Keyset progress bound to one immutable request selection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    selection_sha256: str = Field(pattern=_SHA256_PATTERN)
    last_evidence_rowid: int = Field(default=0, ge=0)
    last_document_version_id: str | None = Field(default=None, min_length=1, max_length=128)
    updated_at: datetime


class PdfTableBackfillItem(BaseModel):
    """One selected document's closed processing result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_rowid: int = Field(gt=0)
    document_version_id: str
    blob_sha256: str = Field(pattern=_SHA256_PATTERN)
    extraction_run_id: str
    disposition: Literal["sealed", "quarantined"]
    quarantine_reason: str | None
    artifact_id: str | None
    member_count: int = Field(ge=0)
    exact_replay: bool


class PdfTableBackfillSummary(BaseModel):
    """CLI-safe accounting for one deterministic batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    mode: _Mode
    dry_run: bool
    selection_sha256: str = Field(pattern=_SHA256_PATTERN)
    batch_size: int
    last_evidence_rowid_before: int = Field(ge=0)
    last_evidence_rowid_after: int = Field(ge=0)
    has_more: bool
    documents_considered: int = Field(ge=0)
    documents_sealed: int = Field(ge=0)
    documents_quarantined: int = Field(ge=0)
    extraction_runs_created: int = Field(ge=0)
    artifacts_created: int = Field(ge=0)
    artifacts_replayed: int = Field(ge=0)
    admitted_count: Literal[0] = 0
    items: tuple[PdfTableBackfillItem, ...]


class _Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_rowid: int = Field(gt=0)
    document_version_id: str = Field(min_length=1, max_length=128)
    blob_sha256: str = Field(pattern=_SHA256_PATTERN)
    byte_size: int = Field(ge=0)
    media_type: str = Field(min_length=1)
    storage_uri: str = Field(min_length=1)


class _Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    candidate: _Candidate
    raw_pdf_bytes: bytes
    artifact: PdfTableExtractionArtifact
    extraction_run_id: str
    extractor_code_version: str


def emit_pdf_table_backfill_event(event: str, **fields: object) -> None:
    """Write one structured operational event without contaminating stdout."""

    sys.stderr.write(json.dumps({"event": event, **fields}, default=str, sort_keys=True) + "\n")


def backfill_pdf_table_evidence(
    request: PdfTableBackfillRequest,
) -> PdfTableBackfillSummary:
    """Parse one selected keyset batch, then optionally persist it atomically."""

    checkpoint_path = request.repo_root.resolve() / ".tmp" / request.task_id / "state.json"
    checkpoint = _read_checkpoint(checkpoint_path, request)
    read_conn = connect_sqlite(request.db_path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        # The existing guard is read-only despite its name and prevents even a
        # dry-run from reporting a plan against a stale migration contract.
        require_current_for_write(read_conn)
        candidates = _select_candidates(read_conn, request, checkpoint)
        has_more = _has_more_candidates(
            read_conn,
            request,
            candidates[-1].evidence_rowid if candidates else checkpoint.last_evidence_rowid,
        )
    finally:
        read_conn.close()

    plans = tuple(_prepare_plan(candidate, request) for candidate in candidates)
    if request.apply and plans:
        items, runs_created, artifacts_created, artifacts_replayed = _persist_plans(
            request,
            plans,
            checkpoint_path=checkpoint_path,
        )
    else:
        items = tuple(_planned_item(plan) for plan in plans)
        runs_created = 0
        artifacts_created = 0
        artifacts_replayed = 0

    after = plans[-1].candidate.evidence_rowid if plans else checkpoint.last_evidence_rowid
    summary = PdfTableBackfillSummary(
        task_id=request.task_id,
        mode="apply" if request.apply else "dry_run",
        dry_run=not request.apply,
        selection_sha256=request.selection_sha256,
        batch_size=request.batch_size,
        last_evidence_rowid_before=checkpoint.last_evidence_rowid,
        last_evidence_rowid_after=after,
        has_more=has_more,
        documents_considered=len(plans),
        documents_sealed=sum(plan.artifact.disposition == "sealed" for plan in plans),
        documents_quarantined=sum(plan.artifact.disposition == "quarantined" for plan in plans),
        extraction_runs_created=runs_created,
        artifacts_created=artifacts_created,
        artifacts_replayed=artifacts_replayed,
        items=items,
    )
    emit_pdf_table_backfill_event(
        "pdf_table_evidence_backfill_completed",
        task_id=request.task_id,
        mode=summary.mode,
        documents_considered=summary.documents_considered,
        sealed=summary.documents_sealed,
        quarantined=summary.documents_quarantined,
        admitted=summary.admitted_count,
        has_more=summary.has_more,
    )
    return summary


def _select_candidates(
    conn: sqlite3.Connection,
    request: PdfTableBackfillRequest,
    checkpoint: PdfTableBackfillCheckpoint,
) -> tuple[_Candidate, ...]:
    placeholders = ",".join("?" for _ in request.document_version_ids)
    parameters: tuple[object, ...] = (
        *request.document_version_ids,
        checkpoint.last_evidence_rowid,
        request.batch_size,
    )
    rows = conn.execute(
        "SELECT document.rowid AS evidence_rowid, document.document_version_id, "
        "lower(document.blob_sha256) AS blob_sha256, blob.byte_size, "
        "blob.media_type, blob.storage_uri "
        "FROM evidence_document_versions AS document "
        "JOIN evidence_content_blobs AS blob ON blob.sha256=document.blob_sha256 "
        f"WHERE document.document_version_id IN ({placeholders}) "
        "AND document.rowid > ? ORDER BY document.rowid LIMIT ?",
        parameters,
    ).fetchall()
    found_any = {
        str(row[0])
        for row in conn.execute(
            "SELECT document_version_id FROM evidence_document_versions "
            f"WHERE document_version_id IN ({placeholders})",
            request.document_version_ids,
        )
    }
    missing = sorted(set(request.document_version_ids) - found_any)
    if missing:
        raise PdfTableBackfillIntegrityError(
            "selected document versions are missing: " + ", ".join(missing)
        )
    return tuple(
        _Candidate(
            evidence_rowid=_positive_int(row["evidence_rowid"], "evidence_rowid"),
            document_version_id=_text(row["document_version_id"], "document_version_id"),
            blob_sha256=_text(row["blob_sha256"], "blob_sha256").lower(),
            byte_size=_nonnegative_int(row["byte_size"], "byte_size"),
            media_type=_text(row["media_type"], "media_type"),
            storage_uri=_text(row["storage_uri"], "storage_uri"),
        )
        for row in rows
    )


def _has_more_candidates(
    conn: sqlite3.Connection,
    request: PdfTableBackfillRequest,
    after_rowid: int,
) -> bool:
    placeholders = ",".join("?" for _ in request.document_version_ids)
    return (
        conn.execute(
            "SELECT 1 FROM evidence_document_versions "
            f"WHERE document_version_id IN ({placeholders}) AND rowid > ? LIMIT 1",
            (*request.document_version_ids, after_rowid),
        ).fetchone()
        is not None
    )


def _prepare_plan(
    candidate: _Candidate,
    request: PdfTableBackfillRequest,
) -> _Plan:
    if candidate.media_type.lower() != "application/pdf":
        raise PdfTableBackfillIntegrityError(
            f"{candidate.document_version_id}: selected blob is not application/pdf"
        )
    if candidate.byte_size > request.maximum_pdf_bytes:
        raise PdfTableBackfillIntegrityError(
            f"{candidate.document_version_id}: recorded PDF exceeds maximum_pdf_bytes"
        )
    resolved = resolve_local_storage_uri(
        candidate.storage_uri,
        allowed_roots=request.content_roots,
    )
    if resolved is None:
        raise PdfTableBackfillIntegrityError(
            f"{candidate.document_version_id}: storage_uri is outside allowed roots"
        )
    if not resolved.is_file():
        raise PdfTableBackfillIntegrityError(
            f"{candidate.document_version_id}: recorded content blob is unavailable"
        )
    raw_pdf = resolved.read_bytes()
    if len(raw_pdf) != candidate.byte_size:
        raise PdfTableBackfillIntegrityError(
            f"{candidate.document_version_id}: recorded byte size mismatch"
        )
    actual_sha256 = hashlib.sha256(raw_pdf).hexdigest()
    if actual_sha256 != candidate.blob_sha256:
        raise PdfTableBackfillIntegrityError(
            f"{candidate.document_version_id}: recorded content SHA-256 mismatch"
        )
    artifact = extract_pdf_tables(raw_pdf)
    code_version = pdf_table_extractor_code_version(
        detector_version=artifact.detector.detector_version,
        pymupdf_version=artifact.detector.pymupdf_version,
        mupdf_version=artifact.detector.mupdf_version,
    )
    run_identity = _digest(
        {
            "document_version_id": candidate.document_version_id,
            "blob_sha256": candidate.blob_sha256,
            "detector_identity_sha256": artifact.detector.detector_identity_sha256,
        }
    )
    return _Plan(
        candidate=candidate,
        raw_pdf_bytes=raw_pdf,
        artifact=artifact,
        extraction_run_id=f"pdf-table-run-{run_identity}",
        extractor_code_version=code_version,
    )


def _persist_plans(
    request: PdfTableBackfillRequest,
    plans: tuple[_Plan, ...],
    *,
    checkpoint_path: Path,
) -> tuple[tuple[PdfTableBackfillItem, ...], int, int, int]:
    items: list[PdfTableBackfillItem] = []
    runs_created = 0
    artifacts_created = 0
    artifacts_replayed = 0
    write_sets = [
        f"sqlite:{request.db_path.resolve()}",
        f"pdf-table-evidence-checkpoint:{request.task_id}",
    ]
    with JobLock(request.repo_root.resolve(), "pdf-table-evidence-backfill", write_sets):
        conn = connect_sqlite(
            request.db_path,
            role=SQLiteConnectionRole.WRITER,
            schema_preflight=True,
        )
        try:
            # The 0240+ commitment triggers intentionally require the sole
            # writer boundary to supply this deterministic scalar.
            conn.create_function("fact_sha256", 1, _sql_sha256, deterministic=True)
            conn.execute("BEGIN IMMEDIATE")
            ledger = EvidenceLedger(conn)
            for plan in plans:
                run = ExtractionRun(
                    extraction_run_id=plan.extraction_run_id,
                    idempotency_key=f"pdf-table-extraction:{plan.extraction_run_id}",
                    document_version_id=plan.candidate.document_version_id,
                    input_sha256=plan.candidate.blob_sha256,
                    extractor_name=PDF_TABLE_EXTRACTOR_NAME,
                    extractor_config_sha256=(plan.artifact.detector.configuration_sha256),
                    extractor_code_version=plan.extractor_code_version,
                    output_sha256=plan.artifact.ordered_page_table_seal_sha256,
                    started_at=request.recorded_at,
                    completed_at=request.recorded_at,
                    outcome="succeeded",
                )
                run_result = ledger.persist(run)
                runs_created += int(run_result.created)
                receipt = record_pdf_table_extraction_artifact(
                    conn,
                    document_version_id=plan.candidate.document_version_id,
                    extraction_run_id=plan.extraction_run_id,
                    raw_pdf_bytes=plan.raw_pdf_bytes,
                    artifact=plan.artifact,
                    recorded_at=request.recorded_at,
                )
                artifacts_created += int(not receipt.exact_replay)
                artifacts_replayed += int(receipt.exact_replay)
                items.append(_persisted_item(plan, receipt))
            conn.commit()
            # The checkpoint is advanced while the same cross-process write
            # lock is still held, and only after the transaction is durable.
            # A file-write failure leaves safe idempotent replay as recovery.
            _write_checkpoint(
                checkpoint_path,
                PdfTableBackfillCheckpoint(
                    selection_sha256=request.selection_sha256,
                    last_evidence_rowid=plans[-1].candidate.evidence_rowid,
                    last_document_version_id=plans[-1].candidate.document_version_id,
                    updated_at=datetime.now(UTC),
                ),
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return tuple(items), runs_created, artifacts_created, artifacts_replayed


def _planned_item(plan: _Plan) -> PdfTableBackfillItem:
    return PdfTableBackfillItem(
        evidence_rowid=plan.candidate.evidence_rowid,
        document_version_id=plan.candidate.document_version_id,
        blob_sha256=plan.candidate.blob_sha256,
        extraction_run_id=plan.extraction_run_id,
        disposition=plan.artifact.disposition,
        quarantine_reason=plan.artifact.quarantine_reason,
        artifact_id=None,
        member_count=sum(
            1 + sum(1 + sum(1 + len(row.cells) for row in table.rows) for table in page.tables)
            for page in plan.artifact.pages
        ),
        exact_replay=False,
    )


def _persisted_item(
    plan: _Plan,
    receipt: PdfTableArtifactPersistenceReceipt,
) -> PdfTableBackfillItem:
    return PdfTableBackfillItem(
        evidence_rowid=plan.candidate.evidence_rowid,
        document_version_id=plan.candidate.document_version_id,
        blob_sha256=plan.candidate.blob_sha256,
        extraction_run_id=plan.extraction_run_id,
        disposition=receipt.disposition,
        quarantine_reason=plan.artifact.quarantine_reason,
        artifact_id=receipt.artifact_id,
        member_count=receipt.member_count,
        exact_replay=receipt.exact_replay,
    )


def _read_checkpoint(
    path: Path,
    request: PdfTableBackfillRequest,
) -> PdfTableBackfillCheckpoint:
    if not path.exists():
        return PdfTableBackfillCheckpoint(
            selection_sha256=request.selection_sha256,
            last_evidence_rowid=0,
            last_document_version_id=None,
            updated_at=request.recorded_at,
        )
    checkpoint = PdfTableBackfillCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))
    if checkpoint.selection_sha256 != request.selection_sha256:
        raise PdfTableBackfillIntegrityError("checkpoint selection identity mismatch")
    return checkpoint


def _write_checkpoint(path: Path, checkpoint: PdfTableBackfillCheckpoint) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        checkpoint.model_dump_json(exclude_none=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _digest(value: object) -> str:
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sql_sha256(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise PdfTableBackfillIntegrityError(f"{field} must be non-empty text")
    return value


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PdfTableBackfillIntegrityError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PdfTableBackfillIntegrityError(f"{field} must be a non-negative integer")
    return value
