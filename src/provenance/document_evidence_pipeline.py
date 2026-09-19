"""Bounded, deterministic continuation of stored source bytes into evidence.

Capture and extraction retain their existing transaction owners. This coordinator
never admits facts, initializes source inventories, or claims extraction completeness.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pipeline.source_policy import (
    ArtifactKind,
    CollectionSource,
    authorize_collection_target_in_connection,
)
from provenance.evidence_backfill import (
    BackfillRequest,
    backfill_legacy_evidence,
    ensure_legacy_document_evidence,
)
from provenance.fulltext_backfill import FullTextBackfillRequest, backfill_fulltext_evidence
from provenance.source_coverage_refresh import CoverageRefreshRequest, refresh_source_coverage

ItemStatus = Literal[
    "captured",
    "extracted",
    "already-covered",
    "quarantined",
    "unsupported",
    "failed",
    "not-attempted",
]


class DocumentEvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repo_root: Path
    apply: bool = False
    ticker: str | None = None
    document_id: int | None = Field(default=None, gt=0)
    before_document_id: int = Field(default=0, ge=0)
    newer_than_document_id: int | None = Field(default=None, ge=0)
    batch_size: int = Field(default=25, ge=1, le=500)
    content_roots: tuple[Path, ...] = ()


class DocumentEvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: int
    ticker: str
    status: ItemStatus = "not-attempted"
    captured: bool = False
    extracted: bool = False
    capture_planned: bool = False
    extraction_planned: bool = False
    document_version_ids: tuple[str, ...] = ()
    findings: list[str] = Field(default_factory=list)
    inventory_keys: tuple[str, ...] = ()
    coverage_statuses: tuple[str, ...] = ()
    inventory_state: Literal["linked", "inventory_uninitialized", "unsupported"] = (
        "inventory_uninitialized"
    )

    @property
    def degraded(self) -> bool:
        """Retain pending capture, extraction, or source-coverage work."""
        return (
            self.status not in {"extracted", "already-covered"}
            or self.inventory_state != "linked"
            or not self.coverage_statuses
            or not set(self.coverage_statuses).issubset({"extracted", "indexed"})
        )


class DocumentEvidenceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["plan", "apply"]
    items: list[DocumentEvidenceItem] = Field(default_factory=lambda: list[DocumentEvidenceItem]())
    status_counts: dict[str, int] = Field(default_factory=dict)
    captured: int = 0
    extracted: int = 0
    inventory_uninitialized: int = 0
    coverage_promotions_planned: int = 0
    coverage_promotions_created: int = 0
    has_more: bool = False
    next_before_document_id: int = 0
    findings: list[str] = Field(default_factory=list)
    degraded: bool = False


def process_document_evidence(
    conn: sqlite3.Connection, request: DocumentEvidenceRequest
) -> DocumentEvidenceResult:
    """Continue one page; callers own the whole-job lock, never an open transaction.

    Pagination is explicit and stateless. A caller drains using next_before_document_id,
    then starts a later reconciliation at zero so failures remain retryable.
    Default ordering is newest stored ID, not a claim of latest issuer disclosure. Existing
    fulltext proof is checked by the extraction owner against its current identity.
    """
    if conn.in_transaction:
        raise RuntimeError("document evidence pipeline requires an idle connection")
    result = DocumentEvidenceResult(
        mode="apply" if request.apply else "plan",
        next_before_document_id=request.before_document_id,
    )
    required_tables = {
        "documents",
        "tracked_companies",
        "evidence_document_versions",
        "evidence_content_blobs",
        "evidence_source_observations",
        "evidence_extraction_runs",
        "evidence_nodes",
    }
    present = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if missing := sorted(required_tables - present):
        result.findings.extend(f"unsupported_schema:{name}" for name in missing)
        result.degraded = True
        return result
    filters = ["document.id > 0"]
    values: list[str | int] = []
    if request.before_document_id:
        filters.append("document.id < ?")
        values.append(request.before_document_id)
    if request.newer_than_document_id is not None:
        filters.append("document.id > ?")
        values.append(request.newer_than_document_id)
    if request.document_id is not None:
        filters.append("document.id = ?")
        values.append(request.document_id)
    if request.ticker is not None:
        filters.append("UPPER(document.ticker) = ?")
        values.append(request.ticker.strip().upper())
    # EXISTS avoids multiplying a document when malformed roster duplicates exist;
    # the shared authorizer below rejects ambiguous or invalid stored identities.
    filters.append(
        "EXISTS (SELECT 1 FROM tracked_companies AS company "
        "WHERE UPPER(company.ticker) = UPPER(document.ticker) "
        "AND company.archived_at IS NULL "
        "AND company.list_type IN ('portfolio', 'evaluation', 'watchlist'))"
    )
    order = "ASC" if request.newer_than_document_id is not None else "DESC"
    rows = conn.execute(
        "SELECT document.id, document.ticker FROM documents AS document WHERE "
        + " AND ".join(filters)
        + f" ORDER BY document.id {order} LIMIT ?",
        (*values, request.batch_size + 1),
    ).fetchall()
    result.has_more = len(rows) > request.batch_size
    for row in rows[: request.batch_size]:
        item = DocumentEvidenceItem(document_id=int(row[0]), ticker=str(row[1]))
        result.items.append(item)
        result.next_before_document_id = item.document_id
        try:
            _process_item(conn, request, item)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
            if conn.in_transaction:
                conn.rollback()
            item.status = "failed"
            # Exception messages may contain source URLs or local paths. Preserve
            # the failure class; owning stages emit their typed diagnostic findings.
            item.findings.append(f"stage_exception:{type(error).__name__}")
        _inventory_context(conn, item)
    if not result.items and (request.document_id is not None or request.ticker is not None):
        result.findings.append("no_eligible_stored_documents")
    keys = tuple(sorted({key for item in result.items for key in item.inventory_keys}))
    if keys:
        try:
            coverage = refresh_source_coverage(
                conn,
                CoverageRefreshRequest(
                    inventory_keys=keys,
                    recorded_at=datetime.now(UTC),
                    batch_size=request.batch_size,
                    apply=request.apply,
                ),
            )
            result.coverage_promotions_planned = coverage.assessments_planned
            result.coverage_promotions_created = coverage.assessments_created
            if coverage.has_more:
                result.findings.append("coverage_promotion_batch_incomplete")
        except (ValueError, RuntimeError, sqlite3.Error) as error:
            result.findings.append(f"coverage_refresh_failed:{type(error).__name__}")
    if request.apply:
        for item in result.items:
            _inventory_context(conn, item)
    result.status_counts = dict(Counter(item.status for item in result.items))
    result.captured = sum(item.captured for item in result.items)
    result.extracted = sum(item.extracted for item in result.items)
    result.inventory_uninitialized = sum(
        item.inventory_state == "inventory_uninitialized" for item in result.items
    )
    result.degraded = bool(
        result.has_more or result.findings or any(item.degraded for item in result.items)
    )
    return result


def _versions(conn: sqlite3.Connection, document_id: int) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in conn.execute(
            "SELECT document_version_id FROM evidence_document_versions "
            "WHERE legacy_document_id = ? ORDER BY version_sequence",
            (document_id,),
        )
    )


def _process_item(
    conn: sqlite3.Connection, request: DocumentEvidenceRequest, item: DocumentEvidenceItem
) -> None:
    authorization = authorize_collection_target_in_connection(
        conn,
        item.ticker,
        requested=False,
        source=CollectionSource.IR,
        artifact_kind=ArtifactKind.IR_DOCUMENT,
    )
    if not authorization.allowed:
        item.findings.append(f"stored_identity_denied:{authorization.status.value}")
        return
    item.document_version_ids = _versions(conn, item.document_id)
    # Existing extraction proof does not prove that the current legacy pointer
    # still resolves to its recorded bytes. Audit it before accepting coverage.
    plan = backfill_legacy_evidence(
        conn, BackfillRequest(repo_root=request.repo_root, document_id=item.document_id)
    )
    if plan.documents_quarantined:
        item.status = "quarantined"
        item.findings.extend(sorted(plan.finding_counts))
        return
    if (
        item.document_version_ids
        and conn.execute(
            "SELECT 1 FROM evidence_document_versions AS version "
            "JOIN documents AS document ON document.id = version.legacy_document_id "
            "LEFT JOIN evidence_content_blobs AS blob ON blob.sha256 = version.blob_sha256 "
            "LEFT JOIN evidence_source_observations AS observation "
            "ON observation.observation_id = version.observation_id "
            "WHERE document.id = ? AND (version.blob_sha256 <> lower(document.sha256) "
            "OR version.ticker <> document.ticker OR version.document_type <> document.doc_type "
            "OR blob.sha256 IS NULL OR observation.observation_id IS NULL "
            "OR observation.blob_sha256 <> version.blob_sha256) LIMIT 1",
            (item.document_id,),
        ).fetchone()
        is not None
    ):
        item.status = "quarantined"
        item.findings.append("immutable_document_identity_mismatch")
        return
    if not item.document_version_ids:
        item.capture_planned = True
        if not request.apply:
            item.findings.append("extraction_waiting_for_byte_anchor")
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            ensure_legacy_document_evidence(
                conn, repo_root=request.repo_root, document_id=item.document_id
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        item.captured = True
        item.status = "captured"
        item.document_version_ids = _versions(conn, item.document_id)
    extraction = backfill_fulltext_evidence(
        conn,
        FullTextBackfillRequest(
            repo_root=request.repo_root,
            content_roots=request.content_roots,
            document_id=item.document_id,
            batch_size=1,
            apply=request.apply,
        ),
    )
    item.extraction_planned = extraction.documents_planned > 0
    item.extracted = extraction.documents_extracted > 0
    item.findings.extend(sorted(extraction.finding_counts))
    if extraction.documents_quarantined:
        item.status = (
            "unsupported"
            if "unsupported_format" in extraction.finding_counts
            or "unsupported_archive_member_format" in extraction.finding_counts
            else "quarantined"
        )
    elif extraction.documents_extracted:
        item.status = "extracted"
    elif extraction.documents_skipped_covered == extraction.documents_considered:
        item.status = "already-covered"
    else:
        item.status = "not-attempted"
        item.findings.append("extraction_planned_apply_required")


def _inventory_context(conn: sqlite3.Connection, item: DocumentEvidenceItem) -> None:
    try:
        rows = conn.execute(
            "SELECT DISTINCT inventory.inventory_key, coverage.coverage_status "
            "FROM evidence_document_versions AS version "
            "JOIN v_source_coverage_current AS coverage "
            "ON coverage.document_version_id = version.document_version_id "
            "JOIN v_expected_documents_current AS expected "
            "ON expected.expected_document_id = coverage.expected_document_id "
            "JOIN v_source_inventory_current AS inventory "
            "ON inventory.snapshot_id = expected.snapshot_id "
            "WHERE version.legacy_document_id = ? ORDER BY inventory.inventory_key",
            (item.document_id,),
        ).fetchall()
        item.inventory_keys = tuple(sorted({str(row[0]) for row in rows}))
        item.coverage_statuses = tuple(sorted({str(row[1]) for row in rows}))
    except sqlite3.Error:
        item.inventory_state = "unsupported"
        item.findings.append("source_inventory_schema_unavailable")
        return
    if item.inventory_keys:
        item.inventory_state = "linked"
