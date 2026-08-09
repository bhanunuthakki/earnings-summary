"""Bounded full-text extraction for canonical document versions.

The legacy bridge deliberately represents a document before it knows how to
parse every file type.  This module closes that gap without rewriting the
bridge: it emits a separate, deterministic extraction run for each eligible
document version and only after re-verifying the local bytes that produced the
version.  Failed or unsupported files remain explicitly uncovered; this is not
an OCR or guess-text fallback.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import stat
import sys
import unicodedata
import zipfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import quote, urlparse

from pydantic import BaseModel, ConfigDict, Field

from provenance.evidence_ledger import EvidenceLedger, EvidenceLocator, EvidenceNode, ExtractionRun
from provenance.evidence_native_candidates import (
    EvidenceNativeDocumentCandidate,
    has_evidence_native_after,
    resolve_local_storage_uri,
    select_evidence_native_candidates,
)
from provenance.fulltext_extractor_identity import (
    BASE_FULLTEXT_EXTRACTOR,
    STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR,
    FulltextExtractorIdentity,
    resolve_fulltext_extractor_identity,
)
from provenance.ooxml_extraction import (
    OfficeExtractionError,
    classify_office_format,
    extract_office_nodes,
)

_PLAIN_SUFFIXES = frozenset({".txt", ".text", ".md", ".csv", ".xml", ".xhtml"})
_HTML_SUFFIXES = frozenset({".html", ".htm", ".xhtml"})
_ARCHIVE_SUFFIXES = frozenset({".zip"})
_ARCHIVE_MEDIA_TYPES = frozenset({"application/zip", "application/x-zip-compressed"})
_ARCHIVE_MEMBER_SUFFIXES = frozenset(
    {
        ".txt",
        ".text",
        ".md",
        ".csv",
        ".json",
        ".xml",
        ".xsd",
        ".xbrl",
        ".svg",
        ".xhtml",
        ".html",
        ".htm",
    }
)
_ARCHIVE_XML_SUFFIXES = frozenset({".xml", ".xsd", ".xbrl", ".svg"})
_ARCHIVE_BINARY_IMAGE_SUFFIXES = frozenset(
    {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
)
_MAX_ARCHIVE_COMPRESSED_BYTES = 100 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 2_048
_MAX_ARCHIVE_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_ARCHIVE_TOTAL_BYTES = 256 * 1024 * 1024
_MAX_ARCHIVE_COMPRESSION_RATIO = 1_000
_MAX_ARCHIVE_NODES = 100_000
_MAX_HTML_BYTES = 64 * 1024 * 1024
_MAX_HTML_DEPTH = 256
_MAX_HTML_NODES = 100_000
_MAX_HTML_TAG_NAME_LENGTH = 128
_MAX_DOM_LOCATOR_LENGTH = 512
_HTML_ROW_CELL_SEPARATOR = " \u241f "
_VOID_HTML_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_NON_REPORT_TEXT_ELEMENTS = frozenset({"script", "style"})
_StateMode = Literal["apply", "dry_run"]
_SourceLane = Literal["legacy", "evidence_native"]
_FormatScope = Literal["all", "office"]


class FullTextBackfillRequest(BaseModel):
    """Validated controls for a single bounded full-text backfill batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repo_root: Path
    content_roots: tuple[Path, ...] = ()
    apply: bool = False
    batch_size: int = Field(default=100, ge=1, le=10_000)
    max_records_per_batch: int = Field(default=50_000, ge=1, le=1_000_000)
    max_nodes_per_batch: int = Field(default=50_000, ge=1, le=1_000_000)
    source_lane: _SourceLane = "legacy"
    format_scope: _FormatScope = "all"
    task_id: str = Field(
        default="fulltext-evidence-backfill", pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$"
    )


class FullTextBackfillCheckpoint(BaseModel):
    """Progress persisted only after its matching SQLite transaction commits."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_lane: _SourceLane = "legacy"
    format_scope: _FormatScope = "all"
    last_document_id: int = Field(default=0, ge=0)
    last_evidence_rowid: int = Field(default=0, ge=0)
    last_document_version_id: str | None = Field(default=None, min_length=1, max_length=128)
    updated_at: datetime


class FullTextBackfillSummary(BaseModel):
    """Closed JSON accounting for an extraction batch."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    source_lane: _SourceLane
    format_scope: _FormatScope
    mode: _StateMode
    dry_run: bool
    batch_size: int
    max_records_per_batch: int
    max_nodes_per_batch: int
    run_at: datetime
    last_document_id_before: int
    last_document_id_after: int
    last_evidence_rowid_before: int = 0
    last_evidence_rowid_after: int = 0
    last_document_version_id_after: str | None = None
    has_more: bool
    budget_exhausted: bool = False
    oversized_document_admitted: bool = False
    documents_sized: int = 0
    documents_deferred_by_budget: int = 0
    records_budget_used: int = 0
    nodes_budget_used: int = 0
    records_budget_utilization: float = 0.0
    nodes_budget_utilization: float = 0.0
    documents_considered: int = 0
    documents_planned: int = 0
    documents_extracted: int = 0
    documents_skipped_covered: int = 0
    documents_quarantined: int = 0
    substantive_nodes_planned: int = 0
    substantive_nodes_created: int = 0
    reference_nodes_planned: int = 0
    reference_nodes_created: int = 0
    substantive_node_kind_counts: dict[str, int] = Field(default_factory=dict[str, int])
    largest_document_node_count: int = 0
    largest_document_ref: str | None = None
    records_planned: int = 0
    records_created: int = 0
    records_replayed: int = 0
    finding_counts: dict[str, int] = Field(default_factory=dict[str, int])


class _DocumentCandidate(BaseModel):
    """The minimum verified legacy/document-version join needed to extract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: int | None = Field(default=None, gt=0)
    evidence_rowid: int | None = Field(default=None, gt=0)
    file_path: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    media_type: str | None = Field(default=None, min_length=1, max_length=255)
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_bytes_size: int | None = Field(default=None, ge=0)
    document_version_id: str | None = Field(default=None, max_length=128)
    blob_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    document_recorded_at: datetime | None = None


class _NodeText(BaseModel):
    """A substantive extracted unit before it is assigned ledger revisions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    local_key: str = Field(min_length=1, max_length=256)
    parent_local_key: str | None = Field(default=None, min_length=1, max_length=256)
    node_kind: Literal[
        "document", "section", "passage", "table", "table_row", "table_cell", "pdf_page"
    ]
    text: str = Field(min_length=1)
    locator: EvidenceLocator


_PlanStatus = Literal[
    "ignored",
    "covered",
    "quarantined_unrecorded",
    "failed",
    "succeeded",
]


@dataclass(frozen=True, slots=True)
class _CandidatePlan:
    candidate: _DocumentCandidate
    identity: FulltextExtractorIdentity
    status: _PlanStatus
    node_texts: tuple[_NodeText, ...] = ()
    reason: str | None = None

    @property
    def node_count(self) -> int:
        return len(self.node_texts)

    @property
    def record_count(self) -> int:
        if self.status == "succeeded":
            return self.node_count + 2
        if self.status == "failed":
            return 1
        return 0


@dataclass(frozen=True, slots=True)
class _ArchiveBinaryReplica:
    document_version_id: str
    member_sha256: str


_ArchiveReplicaResolver = Callable[[str, str], _ArchiveBinaryReplica]


class _ExtractionError(Exception):
    """A deliberate, user-visible coverage failure for untrusted source bytes."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def emit_structured_event(event: str, **fields: object) -> None:
    """Emit one JSONL event without contaminating a CLI's JSON stdout."""

    sys.stderr.write(json.dumps({"event": event, **fields}, default=str, sort_keys=True) + "\n")


def backfill_fulltext_evidence(
    conn: sqlite3.Connection, request: FullTextBackfillRequest
) -> FullTextBackfillSummary:
    """Backfill one bounded set of substantive document text nodes.

    Dry runs never write either the evidence ledger or a checkpoint.  Apply
    owns exactly one SQLite transaction for the selected bounded batch and
    advances its checkpoint only after that transaction commits.
    """

    _require_tables(conn, request.source_lane)
    root = request.repo_root.resolve()
    allowed_roots = _allowed_content_roots(request, root)
    checkpoint_path = _checkpoint_path(root, request)
    checkpoint = _read_checkpoint(
        checkpoint_path,
        request.source_lane,
        request.format_scope,
    )
    if request.source_lane == "evidence_native":
        candidates = _evidence_native_candidates_after(
            conn, checkpoint.last_evidence_rowid, request.batch_size
        )
    else:
        candidates = _candidates_after(conn, checkpoint.last_document_id, request.batch_size)
    summary = FullTextBackfillSummary(
        task_id=request.task_id,
        source_lane=request.source_lane,
        format_scope=request.format_scope,
        mode="apply" if request.apply else "dry_run",
        dry_run=not request.apply,
        batch_size=request.batch_size,
        max_records_per_batch=request.max_records_per_batch,
        max_nodes_per_batch=request.max_nodes_per_batch,
        run_at=datetime.now(UTC),
        last_document_id_before=checkpoint.last_document_id,
        last_document_id_after=checkpoint.last_document_id,
        last_evidence_rowid_before=checkpoint.last_evidence_rowid,
        last_evidence_rowid_after=checkpoint.last_evidence_rowid,
        last_document_version_id_after=checkpoint.last_document_version_id,
        has_more=False,
    )
    if request.apply and conn.in_transaction:
        raise RuntimeError("full-text backfill requires an idle SQLite connection")
    plans = _bounded_candidate_plans(
        conn,
        candidates,
        allowed_roots,
        request,
        summary,
    )
    if request.apply:
        # All source reads and parsing happened before the writer lock.  The
        # transaction now owns only the explicitly budgeted immutable writes.
        conn.execute("BEGIN IMMEDIATE")
    try:
        for plan in plans:
            _execute_candidate_plan(conn, plan, request.apply, summary)
    except Exception:
        if request.apply and conn.in_transaction:
            conn.rollback()
        raise

    if plans:
        final_candidate = plans[-1].candidate
        if final_candidate.document_id is not None:
            summary.last_document_id_after = final_candidate.document_id
        if final_candidate.evidence_rowid is not None:
            summary.last_evidence_rowid_after = final_candidate.evidence_rowid
            summary.last_document_version_id_after = final_candidate.document_version_id
    summary.has_more = (
        has_evidence_native_after(conn, summary.last_evidence_rowid_after)
        if request.source_lane == "evidence_native"
        else _has_documents_after(conn, summary.last_document_id_after)
    )
    if request.apply:
        conn.commit()
        _write_checkpoint(
            checkpoint_path,
            FullTextBackfillCheckpoint(
                source_lane=request.source_lane,
                format_scope=request.format_scope,
                last_document_id=summary.last_document_id_after,
                last_evidence_rowid=summary.last_evidence_rowid_after,
                last_document_version_id=summary.last_document_version_id_after,
                updated_at=datetime.now(UTC),
            ),
        )
        emit_structured_event(
            "fulltext_evidence_backfill_completed",
            task_id=request.task_id,
            records_created=summary.records_created,
            documents_extracted=summary.documents_extracted,
        )
    else:
        emit_structured_event(
            "fulltext_evidence_backfill_dry_run",
            task_id=request.task_id,
            documents_planned=summary.documents_planned,
            records_planned=summary.records_planned,
        )
    return summary


def _bounded_candidate_plans(
    conn: sqlite3.Connection,
    candidates: list[_DocumentCandidate],
    allowed_roots: tuple[Path, ...],
    request: FullTextBackfillRequest,
    summary: FullTextBackfillSummary,
) -> list[_CandidatePlan]:
    plans: list[_CandidatePlan] = []
    records_used = 0
    nodes_used = 0
    for candidate in candidates:
        plan = _plan_candidate(
            conn,
            candidate,
            allowed_roots,
            request.format_scope,
        )
        summary.documents_sized += 1
        no_write_cost = plan.record_count == 0 and plan.node_count == 0
        fits = (
            records_used + plan.record_count <= request.max_records_per_batch
            and nodes_used + plan.node_count <= request.max_nodes_per_batch
        )
        if not no_write_cost and not fits:
            if records_used == 0 and nodes_used == 0:
                summary.oversized_document_admitted = True
            else:
                summary.budget_exhausted = True
                break
        plans.append(plan)
        records_used += plan.record_count
        nodes_used += plan.node_count
    summary.documents_deferred_by_budget = len(candidates) - len(plans)
    summary.records_budget_used = records_used
    summary.nodes_budget_used = nodes_used
    summary.records_budget_utilization = records_used / request.max_records_per_batch
    summary.nodes_budget_utilization = nodes_used / request.max_nodes_per_batch
    return plans


def _plan_candidate(
    conn: sqlite3.Connection,
    candidate: _DocumentCandidate,
    allowed_roots: tuple[Path, ...],
    format_scope: _FormatScope,
) -> _CandidatePlan:
    identity = resolve_fulltext_extractor_identity(candidate.source_ref, candidate.media_type)
    if (
        format_scope == "office"
        and classify_office_format(candidate.source_ref, candidate.media_type) is None
    ):
        return _CandidatePlan(candidate=candidate, identity=identity, status="ignored")
    if candidate.document_version_id is None or candidate.blob_sha256 is None:
        return _CandidatePlan(
            candidate=candidate,
            identity=identity,
            status="quarantined_unrecorded",
            reason="missing_document_version",
        )
    if _has_substantive_coverage(conn, candidate.document_version_id, identity):
        return _CandidatePlan(candidate=candidate, identity=identity, status="covered")
    try:
        raw_bytes = _verified_bytes(conn, candidate, allowed_roots)
    except _ExtractionError as error:
        # The canonical input bytes were not available, so recording a run
        # against the version's hash would falsely claim it read those bytes.
        return _CandidatePlan(
            candidate=candidate,
            identity=identity,
            status="quarantined_unrecorded",
            reason=error.reason,
        )
    try:
        suffix = Path(urlparse(candidate.source_ref).path).suffix.lower()
        normalized_media_type = (
            None if candidate.media_type is None else candidate.media_type.partition(";")[0].lower()
        )
        replica_resolver: _ArchiveReplicaResolver | None = None
        if _is_archive_format(suffix, normalized_media_type):

            def _resolve_replica(digest: str, member_name: str) -> _ArchiveBinaryReplica:
                return _resolve_archive_binary_replica(
                    conn,
                    candidate,
                    digest,
                    member_name,
                )

            replica_resolver = _resolve_replica
        node_texts = _extract_nodes(
            candidate.source_ref,
            raw_bytes,
            candidate.media_type,
            archive_replica_resolver=replica_resolver,
        )
    except _ExtractionError as error:
        return _CandidatePlan(
            candidate=candidate,
            identity=identity,
            status="failed",
            reason=error.reason,
        )
    return _CandidatePlan(
        candidate=candidate,
        identity=identity,
        status="succeeded",
        node_texts=tuple(node_texts),
    )


def _resolve_archive_binary_replica(
    conn: sqlite3.Connection,
    archive_candidate: _DocumentCandidate,
    member_sha256: str,
    member_name: str,
) -> _ArchiveBinaryReplica:
    """Resolve one image only through current, sealed same-package coverage."""

    del member_name  # The hash is authoritative; the member path remains in its locator.
    archive_document_version_id = archive_candidate.document_version_id
    if archive_document_version_id is None:
        raise _ExtractionError("archive_binary_replica_package_unresolved")
    required_relations = (
        "v_source_coverage_current",
        "v_expected_documents_current",
        "v_source_inventory_sealed_complete",
    )
    if any(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'view' AND name = ?",
            (relation,),
        ).fetchone()
        is None
        for relation in required_relations
    ):
        raise _ExtractionError("archive_binary_replica_package_unresolved")
    context_rows = conn.execute(
        "SELECT document.issuer_id, document.accession_number, expected.snapshot_id "
        "FROM evidence_document_versions AS document "
        "JOIN v_source_coverage_current AS coverage "
        "ON coverage.document_version_id = document.document_version_id "
        "AND coverage.coverage_status IN ('captured', 'extracted', 'indexed') "
        "JOIN v_expected_documents_current AS expected "
        "ON expected.expected_document_id = coverage.expected_document_id "
        "JOIN v_source_inventory_sealed_complete AS inventory "
        "ON inventory.snapshot_id = expected.snapshot_id "
        "WHERE document.document_version_id = ? "
        "AND expected.issuer_id = document.issuer_id "
        "AND expected.accession_number = document.accession_number "
        "AND inventory.issuer_id = document.issuer_id "
        "ORDER BY expected.snapshot_id",
        (archive_document_version_id,),
    ).fetchall()
    contexts = {
        (str(row[0]), None if row[1] is None else str(row[1]), str(row[2])) for row in context_rows
    }
    if len(contexts) != 1:
        raise _ExtractionError("archive_binary_replica_package_unresolved")
    issuer_id, accession_number, snapshot_id = next(iter(contexts))
    if accession_number is None:
        raise _ExtractionError("archive_binary_replica_package_unresolved")

    rows = conn.execute(
        "SELECT replica.document_version_id, replica.issuer_id, "
        "replica.accession_number, expected.issuer_id, expected.accession_number, "
        "expected.snapshot_id, coverage.coverage_status, inventory.snapshot_id, "
        "inventory.issuer_id, blob.media_type "
        "FROM evidence_document_versions AS replica "
        "JOIN evidence_content_blobs AS blob ON blob.sha256 = replica.blob_sha256 "
        "LEFT JOIN v_source_coverage_current AS coverage "
        "ON coverage.document_version_id = replica.document_version_id "
        "LEFT JOIN v_expected_documents_current AS expected "
        "ON expected.expected_document_id = coverage.expected_document_id "
        "LEFT JOIN v_source_inventory_sealed_complete AS inventory "
        "ON inventory.snapshot_id = expected.snapshot_id "
        "WHERE replica.blob_sha256 = ? AND replica.document_version_id <> ? "
        "ORDER BY replica.document_version_id, expected.expected_document_id",
        (member_sha256, archive_document_version_id),
    ).fetchall()
    if not rows:
        raise _ExtractionError("archive_binary_replica_missing")
    same_issuer = [row for row in rows if str(row[1]) == issuer_id]
    if not same_issuer:
        raise _ExtractionError("archive_binary_replica_wrong_issuer")
    same_accession = [
        row for row in same_issuer if row[2] is not None and str(row[2]) == accession_number
    ]
    if not same_accession:
        raise _ExtractionError("archive_binary_replica_wrong_accession")
    exact = [
        row
        for row in same_accession
        if row[3] is not None
        and str(row[3]) == issuer_id
        and row[4] is not None
        and str(row[4]) == accession_number
        and row[5] is not None
        and str(row[5]) == snapshot_id
        and row[6] in {"captured", "extracted", "indexed"}
        and row[7] is not None
        and str(row[7]) == snapshot_id
        and row[8] is not None
        and str(row[8]) == issuer_id
        and row[9] is not None
        and str(row[9]).partition(";")[0].lower().startswith("image/")
    ]
    if exact:
        replica_document_version_id = min(str(row[0]) for row in exact)
        return _ArchiveBinaryReplica(
            document_version_id=replica_document_version_id,
            member_sha256=member_sha256,
        )
    covered_snapshots = {
        str(row[5]) for row in same_accession if row[5] is not None and row[7] is not None
    }
    if covered_snapshots and snapshot_id not in covered_snapshots:
        raise _ExtractionError("archive_binary_replica_wrong_snapshot")
    raise _ExtractionError("archive_binary_replica_uncovered")


def _execute_candidate_plan(
    conn: sqlite3.Connection,
    plan: _CandidatePlan,
    apply: bool,
    summary: FullTextBackfillSummary,
) -> None:
    summary.documents_considered += 1
    if plan.status == "ignored":
        return
    if plan.status == "covered":
        summary.documents_skipped_covered += 1
        return
    if plan.status in {"quarantined_unrecorded", "failed"}:
        if plan.reason is None:
            raise RuntimeError("quarantined extraction plan is missing its reason")
        _quarantine(summary, _candidate_ref(plan.candidate), plan.reason)
        summary.records_planned += plan.record_count
        if plan.status == "failed":
            _persist_failed_run(
                conn,
                plan.candidate,
                plan.identity,
                apply,
                summary,
                plan.reason,
            )
        return
    node_texts = list(plan.node_texts)
    reference_count = sum(
        _is_archive_replica_reference(node.node_kind, node.text) for node in node_texts
    )
    substantive_count = sum(node.node_kind != "document" for node in node_texts)
    summary.documents_planned += 1
    summary.substantive_nodes_planned += substantive_count
    summary.reference_nodes_planned += reference_count
    kind_counts = Counter(summary.substantive_node_kind_counts)
    kind_counts.update(node.node_kind for node in node_texts if node.node_kind != "document")
    summary.substantive_node_kind_counts = dict(sorted(kind_counts.items()))
    if len(node_texts) > summary.largest_document_node_count:
        summary.largest_document_node_count = len(node_texts)
        summary.largest_document_ref = _candidate_ref(plan.candidate)
    if not apply:
        summary.records_planned += plan.record_count
        return
    summary.records_planned += plan.record_count
    _persist_success(conn, plan.candidate, plan.identity, node_texts, summary)
    summary.documents_extracted += 1


def _is_archive_replica_reference(node_kind: str, text: str) -> bool:
    return node_kind == "document" and '"record_kind":"archive_binary_replica_reference"' in text


def _verified_bytes(
    conn: sqlite3.Connection,
    candidate: _DocumentCandidate,
    allowed_roots: tuple[Path, ...],
) -> bytes:
    path = resolve_local_storage_uri(candidate.file_path, allowed_roots=allowed_roots)
    if path is None:
        raise _ExtractionError("storage_uri_not_allowed_local_file")
    if not path.is_file():
        raise _ExtractionError("content_missing")
    raw_bytes = path.read_bytes()
    digest = hashlib.sha256(raw_bytes).hexdigest()
    if digest != candidate.document_sha256 or digest != candidate.blob_sha256:
        raise _ExtractionError("sha256_mismatch")
    if candidate.raw_bytes_size is not None and candidate.raw_bytes_size != len(raw_bytes):
        raise _ExtractionError("byte_size_mismatch")
    blob = conn.execute(
        "SELECT byte_size FROM evidence_content_blobs WHERE sha256 = ?", (candidate.blob_sha256,)
    ).fetchone()
    if blob is None or not isinstance(blob[0], int) or blob[0] != len(raw_bytes):
        raise _ExtractionError("evidence_blob_size_mismatch")
    return raw_bytes


def _extract_nodes(
    source_ref: str,
    raw_bytes: bytes,
    media_type: str | None = None,
    *,
    archive_replica_resolver: _ArchiveReplicaResolver | None = None,
) -> list[_NodeText]:
    suffix = Path(urlparse(source_ref).path).suffix.lower()
    normalized_media_type = None if media_type is None else media_type.partition(";")[0].lower()
    office_format = classify_office_format(source_ref, media_type)
    if office_format is not None:
        try:
            return [
                _NodeText(
                    local_key=node.local_key,
                    parent_local_key=node.parent_local_key,
                    node_kind=node.node_kind,
                    text=node.text,
                    locator=node.locator,
                )
                for node in extract_office_nodes(source_ref, raw_bytes, office_format)
            ]
        except OfficeExtractionError as error:
            raise _ExtractionError(error.reason) from error
    if normalized_media_type == "application/pdf" or suffix == ".pdf":
        return _extract_pdf_pages(raw_bytes, source_ref)
    if _is_archive_format(suffix, normalized_media_type):
        return _extract_zip_archive(
            raw_bytes,
            source_ref,
            replica_resolver=archive_replica_resolver,
        )
    if normalized_media_type in {"text/html", "application/xhtml+xml"} or suffix in _HTML_SUFFIXES:
        return _extract_html(raw_bytes, source_ref)
    if (
        (
            normalized_media_type is not None
            and (
                normalized_media_type.startswith("text/")
                or normalized_media_type
                in {"application/json", "application/xml", "application/xhtml+xml"}
            )
        )
        or suffix in _PLAIN_SUFFIXES
        or suffix == ".json"
    ):
        text = raw_bytes.decode("utf-8", errors="replace")
        if not text.strip():
            raise _ExtractionError("no_substantive_text")
        return [
            _NodeText(
                local_key="passage:1",
                node_kind="passage",
                text=text,
                locator=EvidenceLocator(source_ref=source_ref, char_start=0, char_end=len(text)),
            )
        ]
    raise _ExtractionError("unsupported_format")


def _extract_html(raw_bytes: bytes, source_ref: str) -> list[_NodeText]:
    if len(raw_bytes) > _MAX_HTML_BYTES:
        raise _ExtractionError("html_too_large")
    source = raw_bytes.decode("utf-8", errors="replace")
    parser = _SourceAnchoredHtmlParser(source, source_ref)
    try:
        parser.feed(source)
        parser.close()
        parser.finish()
    except _ExtractionError:
        raise
    except Exception as error:
        raise _ExtractionError("unreadable_html") from error
    if not parser.has_substantive_text:
        raise _ExtractionError("no_substantive_text")
    return parser.nodes


def _is_archive_format(suffix: str, media_type: str | None) -> bool:
    return suffix in _ARCHIVE_SUFFIXES or media_type in _ARCHIVE_MEDIA_TYPES


def _extract_zip_archive(
    raw_bytes: bytes,
    source_ref: str,
    *,
    replica_resolver: _ArchiveReplicaResolver | None,
) -> list[_NodeText]:
    """Extract a fail-closed, bounded archive without writing members to disk."""

    if len(raw_bytes) > _MAX_ARCHIVE_COMPRESSED_BYTES:
        raise _ExtractionError("archive_compressed_size_limit_exceeded")
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as archive:
            members = archive.infolist()
            if len(members) > _MAX_ARCHIVE_MEMBERS:
                raise _ExtractionError("archive_member_count_limit_exceeded")
            normalized_members = _validated_archive_members(members)
            nodes: list[_NodeText] = []
            total_uncompressed = 0
            for normalized_name, member in normalized_members:
                if member.is_dir():
                    continue
                total_uncompressed += member.file_size
                if total_uncompressed > _MAX_ARCHIVE_TOTAL_BYTES:
                    raise _ExtractionError("archive_total_size_limit_exceeded")
                member_bytes = _read_archive_member(archive, member)
                suffix = PurePosixPath(normalized_name).suffix.lower()
                member_ref = _archive_member_source_ref(source_ref, normalized_name)
                if suffix in _ARCHIVE_BINARY_IMAGE_SUFFIXES:
                    if replica_resolver is None:
                        raise _ExtractionError("archive_binary_replica_package_unresolved")
                    member_sha256 = hashlib.sha256(member_bytes).hexdigest()
                    replica = replica_resolver(member_sha256, normalized_name)
                    prefix = hashlib.sha256(normalized_name.encode("utf-8")).hexdigest()[:24]
                    nodes.append(
                        _NodeText(
                            local_key=(f"archive:{prefix}:replica:{replica.member_sha256[:24]}"),
                            node_kind="document",
                            text=json.dumps(
                                {
                                    "archive_member": normalized_name,
                                    "member_sha256": replica.member_sha256,
                                    "record_kind": "archive_binary_replica_reference",
                                    "replica_document_version_id": (replica.document_version_id),
                                },
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            locator=EvidenceLocator(
                                source_ref=member_ref,
                                filing_section_key_raw=(
                                    f"archive-member-sha256:{replica.member_sha256}"
                                ),
                            ),
                        )
                    )
                    if len(nodes) > _MAX_ARCHIVE_NODES:
                        raise _ExtractionError("archive_node_count_limit_exceeded")
                    continue
                try:
                    member_bytes.decode("utf-8", errors="strict")
                except UnicodeDecodeError as error:
                    raise _ExtractionError("archive_member_invalid_utf8") from error
                if b"\x00" in member_bytes:
                    raise _ExtractionError("archive_member_binary_text")
                try:
                    member_nodes = _extract_nodes(
                        member_ref,
                        member_bytes,
                        (
                            "application/xml"
                            if PurePosixPath(normalized_name).suffix.lower()
                            in _ARCHIVE_XML_SUFFIXES
                            else None
                        ),
                    )
                except _ExtractionError as error:
                    if error.reason == "unsupported_format":
                        raise _ExtractionError("unsupported_archive_member_format") from error
                    raise _ExtractionError(f"archive_member_{error.reason}") from error
                prefix = hashlib.sha256(normalized_name.encode("utf-8")).hexdigest()[:24]
                for node in member_nodes:
                    nodes.append(
                        node.model_copy(
                            update={
                                "local_key": f"archive:{prefix}:{node.local_key}",
                                "parent_local_key": (
                                    None
                                    if node.parent_local_key is None
                                    else f"archive:{prefix}:{node.parent_local_key}"
                                ),
                            }
                        )
                    )
                    if len(nodes) > _MAX_ARCHIVE_NODES:
                        raise _ExtractionError("archive_node_count_limit_exceeded")
    except _ExtractionError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise _ExtractionError("unreadable_archive") from error
    if not nodes:
        raise _ExtractionError("archive_no_substantive_text")
    return nodes


def _validated_archive_members(
    members: list[zipfile.ZipInfo],
) -> list[tuple[str, zipfile.ZipInfo]]:
    normalized: list[tuple[str, zipfile.ZipInfo]] = []
    seen: set[str] = set()
    for member in members:
        name = _safe_archive_member_name(member.filename)
        if name in seen:
            raise _ExtractionError("archive_duplicate_member_path")
        seen.add(name)
        unix_mode = member.external_attr >> 16
        if stat.S_IFMT(unix_mode) == stat.S_IFLNK:
            raise _ExtractionError("archive_symbolic_link_member")
        if member.flag_bits & 0x1:
            raise _ExtractionError("archive_encrypted_member")
        if member.file_size < 0 or member.compress_size < 0:
            raise _ExtractionError("archive_invalid_member_size")
        if member.file_size > _MAX_ARCHIVE_MEMBER_BYTES:
            raise _ExtractionError("archive_member_size_limit_exceeded")
        if member.file_size > 0 and member.compress_size == 0:
            raise _ExtractionError("archive_compression_ratio_exceeded")
        if (
            member.compress_size > 0
            and member.file_size / member.compress_size > _MAX_ARCHIVE_COMPRESSION_RATIO
        ):
            raise _ExtractionError("archive_compression_ratio_exceeded")
        if not member.is_dir():
            suffix = PurePosixPath(name).suffix.lower()
            if (
                suffix not in _ARCHIVE_MEMBER_SUFFIXES
                and suffix not in _ARCHIVE_BINARY_IMAGE_SUFFIXES
            ):
                raise _ExtractionError("unsupported_archive_member_format")
        normalized.append((name, member))
    return sorted(normalized, key=lambda item: item[0].encode("utf-8"))


def _safe_archive_member_name(raw_name: str) -> str:
    if not raw_name or "\x00" in raw_name:
        raise _ExtractionError("archive_unsafe_member_path")
    portable_name = unicodedata.normalize("NFC", raw_name.replace("\\", "/"))
    path = PurePosixPath(portable_name)
    if (
        path.is_absolute()
        or portable_name.startswith("/")
        or (len(portable_name) >= 2 and portable_name[1] == ":")
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise _ExtractionError("archive_unsafe_member_path")
    normalized = path.as_posix()
    if len(normalized) > 1_024:
        raise _ExtractionError("archive_member_path_too_long")
    return normalized


def _read_archive_member(archive: zipfile.ZipFile, member: zipfile.ZipInfo) -> bytes:
    try:
        with archive.open(member, "r") as stream:
            data = stream.read(_MAX_ARCHIVE_MEMBER_BYTES + 1)
    except (OSError, RuntimeError, zipfile.BadZipFile) as error:
        raise _ExtractionError("unreadable_archive_member") from error
    if len(data) > _MAX_ARCHIVE_MEMBER_BYTES:
        raise _ExtractionError("archive_member_size_limit_exceeded")
    if len(data) != member.file_size:
        raise _ExtractionError("archive_member_size_mismatch")
    return data


def _archive_member_source_ref(source_ref: str, member_name: str) -> str:
    # Encode the parent reference as data so its query/fragment cannot hide the
    # member suffix from deterministic inner-format classification.
    member_ref = f"archive:{quote(source_ref, safe='')}!/{quote(member_name, safe='/')}"
    if len(member_ref) > 2_048:
        raise _ExtractionError("archive_member_source_ref_too_long")
    return member_ref


@dataclass(slots=True)
class _HtmlFrame:
    tag: str
    path: str
    child_counts: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    local_key: str | None = None
    node_index: int | None = None
    start_offset: int | None = None
    table_path: str | None = None
    row_index: int | None = None
    column_index: int | None = None
    next_row_index: int = 0
    next_column_index: int = 0
    heading_level: int | None = None
    section_local_key: str | None = None
    cell_text_parts: list[str] = field(default_factory=lambda: list[str]())
    row_cell_texts: list[str] = field(default_factory=lambda: list[str]())


class _SourceAnchoredHtmlParser(HTMLParser):
    """Emit exact decoded-source spans with deterministic source-DOM paths."""

    def __init__(self, source: str, source_ref: str) -> None:
        super().__init__(convert_charrefs=False)
        self.source = source
        self.source_ref = source_ref
        self.nodes: list[_NodeText] = []
        self.has_substantive_text = False
        self._stack: list[_HtmlFrame] = []
        self._root_counts: dict[str, int] = {}
        self._line_starts = [0]
        self._line_starts.extend(
            index + 1 for index, character in enumerate(source) if character == "\n"
        )
        self._ordinal = 0
        self._active_sections: dict[int, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        self._push_start_tag(tag, self.get_starttag_text() or f"<{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        self._push_start_tag(tag, self.get_starttag_text() or f"<{tag}/>")
        if self._stack and self._stack[-1].tag == tag.lower():
            frame = self._stack.pop()
            self._finalize_frame(
                frame, self._absolute_offset() + len(self.get_starttag_text() or "")
            )

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index].tag == normalized:
                close_start = self._absolute_offset()
                close_end = self.source.find(">", close_start)
                frame_end = len(self.source) if close_end < 0 else close_end + 1
                for frame in reversed(self._stack[index:]):
                    self._finalize_frame(frame, frame_end)
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        start = self._absolute_offset()
        self._emit_text(data, start, start + len(data))

    def handle_entityref(self, name: str) -> None:
        self._emit_reference(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._emit_reference(f"&#{name};")

    def _push_start_tag(self, tag: str, source_text: str) -> None:
        normalized = tag.lower()
        if len(normalized) > _MAX_HTML_TAG_NAME_LENGTH:
            raise _ExtractionError("html_tag_name_too_long")
        counts = self._stack[-1].child_counts if self._stack else self._root_counts
        child_index = counts.get(normalized, 0) + 1
        counts[normalized] = child_index
        parent_path = self._stack[-1].path if self._stack else ""
        path = f"{parent_path}/{normalized}[{child_index}]"
        if len(self._stack) >= _MAX_HTML_DEPTH:
            raise _ExtractionError("html_depth_limit_exceeded")
        frame = _HtmlFrame(tag=normalized, path=path)
        frame.start_offset = self._absolute_offset()
        if normalized == "table":
            frame.table_path = path
            frame.local_key, frame.node_index = self._emit_structure_node(
                "table",
                "HTML table structure; child rows carry reported content.",
                path,
                table_path=path,
                source_span_text=source_text,
            )
        elif normalized == "tr":
            table = self._nearest_frame("table")
            if table is not None:
                table.next_row_index += 1
                frame.table_path = table.path
                frame.row_index = table.next_row_index
                frame.local_key, frame.node_index = self._emit_structure_node(
                    "table_row",
                    "HTML table row structure; cell aggregation pending.",
                    path,
                    parent_local_key=table.local_key,
                    table_path=table.path,
                    row_index=frame.row_index,
                    source_span_text=source_text,
                )
        elif normalized in {"td", "th"}:
            row = self._nearest_frame("tr")
            if row is not None:
                row.next_column_index += 1
                frame.table_path = row.table_path
                frame.row_index = row.row_index
                frame.column_index = row.next_column_index
        elif len(normalized) == 2 and normalized[0] == "h" and normalized[1] in "123456":
            frame.heading_level = int(normalized[1])
        if normalized not in _VOID_HTML_ELEMENTS:
            self._stack.append(frame)

    def _emit_structure_node(
        self,
        node_kind: Literal["table", "table_row"],
        text: str,
        path: str,
        *,
        parent_local_key: str | None = None,
        table_path: str,
        row_index: int | None = None,
        source_span_text: str,
    ) -> tuple[str, int]:
        start = self._absolute_offset()
        if self.source[start : start + len(source_span_text)] != source_span_text:
            raise _ExtractionError("html_source_span_mismatch")
        local_key = self._next_local_key(node_kind)
        self.nodes.append(
            _NodeText(
                local_key=local_key,
                parent_local_key=parent_local_key,
                node_kind=node_kind,
                text=text,
                locator=EvidenceLocator(
                    source_ref=self.source_ref,
                    filing_section_key_raw=_bounded_dom_locator(path),
                    filing_ordinal=self._ordinal,
                    char_start=start,
                    char_end=start + len(source_span_text),
                    table_name=_table_identifier(table_path),
                    table_row_index=row_index,
                ),
            )
        )
        self._check_node_limit()
        return local_key, len(self.nodes) - 1

    def _emit_text(self, text: str, start: int, end: int) -> None:
        if not text.strip() or self._inside_non_report_element():
            return
        if self.source[start:end] != text:
            raise _ExtractionError("html_source_span_mismatch")
        self.has_substantive_text = True
        frame = self._stack[-1] if self._stack else None
        path = "/document-text[1]" if frame is None else frame.path
        if (
            frame is not None
            and frame.heading_level is not None
            and frame.section_local_key is None
        ):
            parent = self._nearest_active_section(frame.heading_level)
            local_key = self._next_local_key("section")
            self.nodes.append(
                _NodeText(
                    local_key=local_key,
                    parent_local_key=parent,
                    node_kind="section",
                    text=text,
                    locator=self._text_locator(path, start, end),
                )
            )
            frame.section_local_key = local_key
            self._active_sections = {
                level: key
                for level, key in self._active_sections.items()
                if level < frame.heading_level
            }
            self._active_sections[frame.heading_level] = local_key
            self._check_node_limit()
            return
        row = self._nearest_frame("tr")
        cell = self._nearest_cell_frame()
        if cell is not None and row is not None and cell.table_path is not None:
            cell.cell_text_parts.append(text)
            node_kind: Literal["passage", "table_cell"] = "table_cell"
            local_key = self._next_local_key(node_kind)
            parent = row.local_key
            locator = self._text_locator(
                path,
                start,
                end,
                table_path=cell.table_path,
                row_index=cell.row_index,
                column_index=cell.column_index,
            )
        else:
            node_kind = "passage"
            local_key = self._next_local_key(node_kind)
            table = self._nearest_frame("table")
            parent = (
                frame.section_local_key
                if frame is not None and frame.section_local_key is not None
                else row.local_key
                if row is not None
                else table.local_key
                if table is not None
                else self._current_section()
            )
            locator = self._text_locator(path, start, end)
        self.nodes.append(
            _NodeText(
                local_key=local_key,
                parent_local_key=parent,
                node_kind=node_kind,
                text=text,
                locator=locator,
            )
        )
        self._check_node_limit()

    def _emit_reference(self, expected_token: str) -> None:
        start = self._absolute_offset()
        token = expected_token
        if not self.source.startswith(token, start):
            without_semicolon = token[:-1]
            if self.source.startswith(without_semicolon, start):
                token = without_semicolon
            else:
                raise _ExtractionError("html_source_span_mismatch")
        self._emit_text(token, start, start + len(token))

    def _text_locator(
        self,
        path: str,
        start: int,
        end: int,
        *,
        table_path: str | None = None,
        row_index: int | None = None,
        column_index: int | None = None,
    ) -> EvidenceLocator:
        return EvidenceLocator(
            source_ref=self.source_ref,
            filing_section_key_raw=_bounded_dom_locator(path),
            filing_ordinal=self._ordinal,
            char_start=start,
            char_end=end,
            table_name=None if table_path is None else _table_identifier(table_path),
            table_row_index=row_index,
            table_column_index=column_index,
        )

    def _next_local_key(self, kind: str) -> str:
        self._ordinal += 1
        return f"html:{self._ordinal}:{kind}"

    def _absolute_offset(self) -> int:
        line, column = self.getpos()
        if line < 1 or line > len(self._line_starts):
            raise _ExtractionError("html_source_position_invalid")
        return self._line_starts[line - 1] + column

    def _nearest_frame(self, tag: str) -> _HtmlFrame | None:
        return next((frame for frame in reversed(self._stack) if frame.tag == tag), None)

    def _nearest_cell_frame(self) -> _HtmlFrame | None:
        return next(
            (frame for frame in reversed(self._stack) if frame.tag in {"td", "th"}),
            None,
        )

    def _inside_non_report_element(self) -> bool:
        return any(frame.tag in _NON_REPORT_TEXT_ELEMENTS for frame in self._stack)

    def _nearest_active_section(self, level: int) -> str | None:
        parents = [candidate for candidate in self._active_sections if candidate < level]
        return None if not parents else self._active_sections[max(parents)]

    def _current_section(self) -> str | None:
        return (
            None if not self._active_sections else self._active_sections[max(self._active_sections)]
        )

    def _check_node_limit(self) -> None:
        if len(self.nodes) > _MAX_HTML_NODES:
            raise _ExtractionError("html_node_count_limit_exceeded")

    def finish(self) -> None:
        """Finalize any source frames left open by malformed-but-parseable HTML."""

        for frame in reversed(self._stack):
            self._finalize_frame(frame, len(self.source))
        self._stack.clear()

    def _finalize_frame(self, frame: _HtmlFrame, end: int) -> None:
        if frame.tag in {"td", "th"}:
            row = self._nearest_frame("tr")
            cell_text = "".join(frame.cell_text_parts)
            if row is not None and cell_text.strip():
                row.row_cell_texts.append(cell_text)
            return
        if frame.node_index is None or frame.start_offset is None:
            return
        existing = self.nodes[frame.node_index]
        locator = existing.locator.model_copy(
            update={"char_start": frame.start_offset, "char_end": end}
        )
        if frame.tag == "tr":
            escaped_cells = [
                cell.replace("\u241f", "\u241f\u241f") for cell in frame.row_cell_texts
            ]
            text = (
                _HTML_ROW_CELL_SEPARATOR.join(escaped_cells)
                if escaped_cells
                else "HTML empty table row structure."
            )
            node_kind: Literal["document", "table_row"] = (
                "table_row" if escaped_cells else "document"
            )
            self.nodes[frame.node_index] = existing.model_copy(
                update={"node_kind": node_kind, "text": text, "locator": locator}
            )
            return
        if frame.tag == "table":
            self.nodes[frame.node_index] = existing.model_copy(update={"locator": locator})


def _table_identifier(table_path: str) -> str:
    if len(table_path) <= 255:
        return table_path
    return f"dom-table:{hashlib.sha256(table_path.encode('utf-8')).hexdigest()}"


def _bounded_dom_locator(path: str) -> str:
    """Commit a full source-DOM path while respecting the closed locator size.

    Exact decoded-source offsets remain the primary click-through address.
    Long paths retain a readable suffix and a SHA-256 commitment to every
    omitted ancestor, so a verifier can recompute and compare the complete
    parser path without storing an invalid over-width locator.
    """

    if len(path) <= _MAX_DOM_LOCATOR_LENGTH:
        return path
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()
    prefix = f"dom-path-sha256:{digest}:suffix:"
    suffix_budget = _MAX_DOM_LOCATOR_LENGTH - len(prefix)
    return f"{prefix}{path[-suffix_budget:]}"


def _extract_pdf_pages(raw_bytes: bytes, source_ref: str) -> list[_NodeText]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(raw_bytes))
        if reader.is_encrypted:
            raise _ExtractionError("encrypted_pdf")
        nodes = [
            _NodeText(
                local_key=f"page:{index}",
                node_kind="pdf_page",
                text=text,
                locator=EvidenceLocator(source_ref=source_ref, page_number=index),
            )
            for index, page in enumerate(reader.pages, start=1)
            if (text := (page.extract_text() or "")).strip()
        ]
    except _ExtractionError:
        raise
    except Exception as error:
        # ``pypdf`` parses untrusted issuer bytes and has a broad exception tree.
        raise _ExtractionError("unreadable_pdf") from error
    if not nodes:
        raise _ExtractionError("no_substantive_text")
    return nodes


def _persist_success(
    conn: sqlite3.Connection,
    candidate: _DocumentCandidate,
    identity: FulltextExtractorIdentity,
    node_texts: list[_NodeText],
    summary: FullTextBackfillSummary,
) -> None:
    document_version_id = _require_document_version(candidate)
    recorded_at = _require_recorded_at(candidate)
    run_id = _run_id(document_version_id, identity)
    document_node = _new_node(
        conn,
        evidence_key=_document_evidence_key(document_version_id, identity),
        node_id=_node_id(run_id, "document"),
        extraction_run_id=run_id,
        parent_node_id=None,
        node_kind="document",
        text=f"Full-text extraction for evidence document version {document_version_id}.",
        locator=EvidenceLocator(source_ref=candidate.source_ref),
        recorded_at=recorded_at,
    )
    child_nodes: list[EvidenceNode] = []
    local_node_ids: dict[str, str] = {}
    for index, node in enumerate(node_texts, start=1):
        if node.local_key in local_node_ids:
            raise RuntimeError(f"duplicate extracted node local key: {node.local_key}")
        parent_node_id = document_node.node_id
        if node.parent_local_key is not None:
            parent_node_id = local_node_ids.get(node.parent_local_key, "")
            if not parent_node_id:
                raise RuntimeError(
                    "extracted node parent must precede its child: "
                    f"{node.parent_local_key} -> {node.local_key}"
                )
        child = _new_node(
            conn,
            evidence_key=_content_evidence_key(
                document_version_id, index, node.local_key, identity
            ),
            node_id=_node_id(run_id, f"content-{index}"),
            extraction_run_id=run_id,
            parent_node_id=parent_node_id,
            node_kind=node.node_kind,
            text=node.text,
            locator=node.locator,
            recorded_at=recorded_at,
        )
        child_nodes.append(child)
        local_node_ids[node.local_key] = child.node_id
    output_sha256 = _canonical_output_sha256([document_node, *child_nodes])
    run = ExtractionRun(
        extraction_run_id=run_id,
        idempotency_key=(f"{identity.idempotency_namespace}:{_stable_token(document_version_id)}"),
        document_version_id=document_version_id,
        input_sha256=_require_blob_sha(candidate),
        extractor_name=identity.name,
        extractor_config_sha256=identity.config_sha256,
        extractor_code_version=identity.code_version,
        output_sha256=output_sha256,
        started_at=recorded_at,
        completed_at=recorded_at,
        outcome="succeeded",
    )
    ledger = EvidenceLedger(conn)
    _account(ledger.persist(run).created, summary)
    _account(ledger.persist(document_node).created, summary)
    for child_node in child_nodes:
        created = ledger.persist(child_node).created
        _account(created, summary)
        if created:
            if _is_archive_replica_reference(child_node.node_kind, child_node.text):
                summary.reference_nodes_created += 1
            elif child_node.node_kind != "document":
                summary.substantive_nodes_created += 1


def _persist_failed_run(
    conn: sqlite3.Connection,
    candidate: _DocumentCandidate,
    identity: FulltextExtractorIdentity,
    apply: bool,
    summary: FullTextBackfillSummary,
    reason: str,
) -> None:
    if candidate.document_version_id is None or candidate.blob_sha256 is None:
        return
    if not apply:
        return
    recorded_at = _require_recorded_at(candidate)
    document_version_id = candidate.document_version_id
    failure_payload = json.dumps(
        {"document_version_id": document_version_id, "reason": reason},
        sort_keys=True,
        separators=(",", ":"),
    )
    run = ExtractionRun(
        extraction_run_id=_run_id(document_version_id, identity),
        idempotency_key=(f"{identity.idempotency_namespace}:{_stable_token(document_version_id)}"),
        document_version_id=document_version_id,
        input_sha256=candidate.blob_sha256,
        extractor_name=identity.name,
        extractor_config_sha256=identity.config_sha256,
        extractor_code_version=identity.code_version,
        output_sha256=hashlib.sha256(failure_payload.encode("utf-8")).hexdigest(),
        started_at=recorded_at,
        completed_at=recorded_at,
        outcome="failed",
    )
    _account(EvidenceLedger(conn).persist(run).created, summary)


def _new_node(
    conn: sqlite3.Connection,
    *,
    evidence_key: str,
    node_id: str,
    extraction_run_id: str,
    parent_node_id: str | None,
    node_kind: Literal[
        "document",
        "section",
        "passage",
        "table",
        "table_row",
        "table_cell",
        "pdf_page",
    ],
    text: str,
    locator: EvidenceLocator,
    recorded_at: datetime,
) -> EvidenceNode:
    prior = conn.execute(
        "SELECT node_id, revision FROM evidence_nodes WHERE evidence_key = ? "
        "ORDER BY revision DESC LIMIT 1",
        (evidence_key,),
    ).fetchone()
    revision = 1 if prior is None else int(prior[1]) + 1
    supersedes = None if prior is None else str(prior[0])
    return EvidenceNode(
        node_id=node_id,
        evidence_key=evidence_key,
        revision=revision,
        extraction_run_id=extraction_run_id,
        parent_node_id=parent_node_id,
        supersedes_node_id=supersedes,
        node_kind=node_kind,
        text=text,
        locator=locator,
        recorded_at=recorded_at,
    )


def _has_substantive_coverage(
    conn: sqlite3.Connection,
    document_version_id: str,
    identity: FulltextExtractorIdentity,
) -> bool:
    node_predicate = (
        "(node.node_kind <> 'document' OR "
        "(node.parent_node_id IS NOT NULL AND node.text LIKE "
        '\'%"record_kind":"archive_binary_replica_reference"%\'))'
        if identity is STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR
        else "node.node_kind <> 'document'"
    )
    return (
        conn.execute(
            "SELECT 1 FROM evidence_nodes AS node JOIN evidence_extraction_runs AS run "  # nosec B608 -- trusted internal SQL shape; values remain bound
            "ON run.extraction_run_id = node.extraction_run_id "
            "WHERE run.document_version_id = ? AND run.outcome = 'succeeded' "
            "AND run.extractor_name = ? AND run.extractor_config_sha256 = ? "
            f"AND run.extractor_code_version = ? AND {node_predicate} "
            "AND length(trim(node.text)) > 0 LIMIT 1",
            (
                document_version_id,
                identity.name,
                identity.config_sha256,
                identity.code_version,
            ),
        ).fetchone()
        is not None
    )


def _candidates_after(
    conn: sqlite3.Connection, last_document_id: int, batch_size: int
) -> list[_DocumentCandidate]:
    rows = conn.execute(
        "WITH selected_documents AS ("
        "SELECT * FROM documents WHERE id > ? ORDER BY id LIMIT ?"
        ") SELECT document.id AS document_id, document.file_path, lower(document.sha256) "
        "AS document_sha256, document.raw_bytes_size, document_version.document_version_id, "
        "document_version.blob_sha256, document_version.recorded_at AS document_recorded_at "
        "FROM selected_documents AS document LEFT JOIN evidence_document_versions AS document_version "
        "ON document_version.legacy_document_id = document.id "
        "ORDER BY document.id, document_version.version_sequence",
        (last_document_id, batch_size),
    ).fetchall()
    return [_candidate_from_row(row) for row in rows]


def _evidence_native_candidates_after(
    conn: sqlite3.Connection, last_evidence_rowid: int, batch_size: int
) -> list[_DocumentCandidate]:
    return [
        _candidate_from_evidence_native(candidate)
        for candidate in select_evidence_native_candidates(
            conn, after_rowid=last_evidence_rowid, batch_size=batch_size
        )
    ]


def _candidate_from_evidence_native(
    candidate: EvidenceNativeDocumentCandidate,
) -> _DocumentCandidate:
    return _DocumentCandidate(
        document_id=None,
        evidence_rowid=candidate.evidence_rowid,
        file_path=candidate.storage_uri,
        source_ref=candidate.source_ref,
        media_type=candidate.media_type,
        document_sha256=candidate.blob_sha256,
        raw_bytes_size=candidate.byte_size,
        document_version_id=candidate.document_version_id,
        blob_sha256=candidate.blob_sha256,
        document_recorded_at=_datetime_value(candidate.recorded_at),
    )


def _candidate_from_row(row: sqlite3.Row) -> _DocumentCandidate:
    raw_size = row["raw_bytes_size"]
    if raw_size is not None and (isinstance(raw_size, bool) or not isinstance(raw_size, int)):
        raise RuntimeError("documents.raw_bytes_size must be a non-negative integer when present")
    recorded_at = row["document_recorded_at"]
    return _DocumentCandidate(
        document_id=_int_value(row["document_id"], "document.id"),
        file_path=_text_value(row["file_path"], "documents.file_path"),
        source_ref=_text_value(row["file_path"], "documents.file_path"),
        media_type=None,
        document_sha256=_text_value(row["document_sha256"], "documents.sha256").lower(),
        raw_bytes_size=raw_size,
        document_version_id=_optional_text_value(row["document_version_id"]),
        blob_sha256=_optional_text_value(row["blob_sha256"]),
        document_recorded_at=_datetime_value(recorded_at),
    )


def _has_documents_after(conn: sqlite3.Connection, last_document_id: int) -> bool:
    return (
        conn.execute("SELECT 1 FROM documents WHERE id > ? LIMIT 1", (last_document_id,)).fetchone()
        is not None
    )


def _allowed_content_roots(request: FullTextBackfillRequest, repo_root: Path) -> tuple[Path, ...]:
    roots = (repo_root, *(root.resolve() for root in request.content_roots))
    return tuple(dict.fromkeys(roots))


def _checkpoint_path(root: Path, request: FullTextBackfillRequest) -> Path:
    filename = "state.json" if request.source_lane == "legacy" else "evidence-native-state.json"
    return root / ".tmp" / request.task_id / filename


def _require_tables(conn: sqlite3.Connection, source_lane: _SourceLane) -> None:
    tables = [
        "evidence_content_blobs",
        "evidence_source_observations",
        "evidence_document_versions",
        "evidence_extraction_runs",
        "evidence_nodes",
    ]
    if source_lane == "legacy":
        tables.append("documents")
    for table in tables:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if exists is None:
            raise RuntimeError(f"Required table {table!r} is unavailable for full-text backfill")
    if source_lane == "evidence_native":
        return
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(documents)")}
    required = {"id", "file_path", "sha256", "raw_bytes_size"}
    if missing := sorted(required - columns):
        raise RuntimeError(f"documents schema is missing required columns: {', '.join(missing)}")


def _read_checkpoint(
    path: Path,
    source_lane: _SourceLane,
    format_scope: _FormatScope,
) -> FullTextBackfillCheckpoint:
    if not path.exists():
        return FullTextBackfillCheckpoint(
            source_lane=source_lane,
            format_scope=format_scope,
            updated_at=datetime.now(UTC),
        )
    checkpoint = FullTextBackfillCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))
    if checkpoint.source_lane != source_lane:
        raise RuntimeError("full-text checkpoint source lane does not match request")
    if checkpoint.format_scope != format_scope:
        raise RuntimeError("full-text checkpoint format scope does not match request")
    return checkpoint


def _write_checkpoint(path: Path, checkpoint: FullTextBackfillCheckpoint) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(checkpoint.model_dump_json(), encoding="utf-8")
    temporary.replace(path)


def _candidate_ref(candidate: _DocumentCandidate) -> str:
    if candidate.document_version_id is not None:
        return candidate.document_version_id
    if candidate.document_id is not None:
        return str(candidate.document_id)
    return "unknown"


def _canonical_output_sha256(nodes: list[EvidenceNode]) -> str:
    payload = [node.model_dump(mode="json", exclude_none=True) for node in nodes]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stable_token(document_version_id: str) -> str:
    return hashlib.sha256(document_version_id.encode("utf-8")).hexdigest()[:48]


def _run_id(document_version_id: str, identity: FulltextExtractorIdentity) -> str:
    semantic = "\0".join(
        (
            document_version_id,
            identity.name,
            identity.config_sha256,
            identity.code_version,
        )
    )
    return f"fulltext-run-{hashlib.sha256(semantic.encode('utf-8')).hexdigest()[:48]}"


def _node_id(run_id: str, suffix: str) -> str:
    semantic = "\0".join((run_id, suffix))
    return f"fulltext-node-{hashlib.sha256(semantic.encode()).hexdigest()[:48]}"


def _document_evidence_key(document_version_id: str, identity: FulltextExtractorIdentity) -> str:
    if identity is STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR:
        # The structured web extractor replaces the former one-node HTML
        # extraction instead of leaving two current document assertions.
        return f"fulltext-document:{_stable_token(document_version_id)}"
    return f"{identity.evidence_namespace}-document:{_stable_token(document_version_id)}"


def _content_evidence_key(
    document_version_id: str,
    ordinal: int,
    local_key: str,
    identity: FulltextExtractorIdentity,
) -> str:
    if identity is BASE_FULLTEXT_EXTRACTOR or (
        identity is STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR and ordinal == 1
    ):
        # Structured HTML's first source-anchored node supersedes the legacy
        # monolithic passage at ordinal one; all additional precise nodes use
        # locator-derived keys below.
        return f"fulltext-content:{_stable_token(document_version_id)}:{ordinal}"
    location_token = hashlib.sha256(local_key.encode("utf-8")).hexdigest()[:24]
    return (
        f"{identity.evidence_namespace}-content:"
        f"{_stable_token(document_version_id)}:{location_token}"
    )


def _require_document_version(candidate: _DocumentCandidate) -> str:
    if candidate.document_version_id is None:
        raise RuntimeError("candidate missing evidence document version")
    return candidate.document_version_id


def _require_blob_sha(candidate: _DocumentCandidate) -> str:
    if candidate.blob_sha256 is None:
        raise RuntimeError("candidate missing evidence blob hash")
    return candidate.blob_sha256


def _require_recorded_at(candidate: _DocumentCandidate) -> datetime:
    if candidate.document_recorded_at is None:
        raise RuntimeError("candidate document version has no recorded_at clock")
    return candidate.document_recorded_at


def _quarantine(summary: FullTextBackfillSummary, document_ref: str, reason: str) -> None:
    summary.documents_quarantined += 1
    counts = Counter(summary.finding_counts)
    counts[reason] += 1
    summary.finding_counts = dict(counts)
    emit_structured_event(
        "fulltext_evidence_backfill_quarantined",
        document_ref=document_ref,
        reason=reason,
    )


def _account(created: bool, summary: FullTextBackfillSummary) -> None:
    if created:
        summary.records_created += 1
    else:
        summary.records_replayed += 1


def _int_value(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"{name} must be an integer")
    return value


def _text_value(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{name} must be non-empty text")
    return value


def _optional_text_value(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _datetime_value(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise RuntimeError("document version recorded_at is invalid") from error
    raise RuntimeError("document version recorded_at is invalid")
