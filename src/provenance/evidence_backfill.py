"""Bounded, dry-run-first bridge from legacy evidence tables to the evidence ledger.

The bridge deliberately reads legacy rows without repairing or normalizing
them.  A source document is admitted only when the bytes still on disk match
its recorded hash and size; descendants are then represented as immutable
ledger nodes under that verified document.  This is a migration aid, not a
replacement for future dual-write paths.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    EvidenceLocator,
    EvidenceNode,
    ExtractionRun,
    LedgerRecord,
    SourceObservation,
)
from provenance.evidence_links import (
    BlobLocationObservation,
    DocumentObservationLink,
    EvidenceLinkLedger,
)
from provenance.selection import selected_filing_sections_relation, selected_transcripts_relation

_BACKFILL_VERSION = "evidence-backfill@1"
_CONFIG_SHA256 = hashlib.sha256(b"legacy-evidence-backfill-config-v1").hexdigest()
_EXTRACTOR_CONFIG_SHA256 = hashlib.sha256(b"legacy-evidence-backfill-extractor-v1").hexdigest()
_QUARANTINE_EVENT_LIMIT = 100
_StateMode = Literal["apply", "dry_run"]


class BackfillRequest(BaseModel):
    """Validated operational controls for one bounded backfill invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repo_root: Path
    apply: bool = False
    batch_size: int = Field(default=100, ge=1, le=10_000)
    task_id: str = Field(
        default="evidence-ledger-backfill",
        pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$",
    )


class BackfillCheckpoint(BaseModel):
    """Durable progress only written after an apply transaction commits."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    last_document_id: int = Field(ge=0)
    updated_at: datetime


class BackfillSummary(BaseModel):
    """The CLI-safe, JSON-only accounting for a single batch."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    mode: _StateMode
    dry_run: bool
    batch_size: int
    run_at: datetime
    last_document_id_before: int
    last_document_id_after: int
    has_more: bool
    documents_considered: int = 0
    documents_backfilled: int = 0
    documents_quarantined: int = 0
    filing_sections_considered: int = 0
    filing_sections_backfilled: int = 0
    transcript_segments_considered: int = 0
    transcript_segments_backfilled: int = 0
    records_planned: int = 0
    records_created: int = 0
    records_replayed: int = 0
    finding_counts: dict[str, int] = Field(default_factory=dict[str, int])
    selection_modes: dict[str, str] = Field(default_factory=dict[str, str])


class _DocumentChain(BaseModel):
    """The stable ledger identities descendants need without exposing row writes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    legacy_document_id: int
    extraction_run_id: str
    document_node_id: str
    recorded_at: datetime
    legacy_source_ref: str


def emit_structured_event(event: str, **fields: object) -> None:
    """Emit one JSONL operational event without contaminating stdout."""

    sys.stderr.write(json.dumps({"event": event, **fields}, default=str, sort_keys=True) + "\n")


def backfill_legacy_evidence(conn: sqlite3.Connection, request: BackfillRequest) -> BackfillSummary:
    """Backfill one bounded document-id range; dry runs never write ledger/state.

    The caller owns connection acquisition.  The execution entrypoint uses a
    read-only central connection for dry runs and a writer-role connection for
    ``--apply``.  Direct callers may use this deterministic function in an
    isolated test database.
    """

    _require_table(conn, "documents")
    _require_ledger_tables(conn)
    root = request.repo_root.resolve()
    checkpoint_path = root / ".tmp" / request.task_id / "state.json"
    checkpoint = (
        _read_checkpoint(checkpoint_path)
        if request.apply
        else BackfillCheckpoint(last_document_id=0, updated_at=datetime.now(UTC))
    )
    documents = _documents_after(conn, checkpoint.last_document_id, request.batch_size)
    summary = BackfillSummary(
        task_id=request.task_id,
        mode="apply" if request.apply else "dry_run",
        dry_run=not request.apply,
        batch_size=request.batch_size,
        run_at=datetime.now(UTC),
        last_document_id_before=checkpoint.last_document_id,
        last_document_id_after=checkpoint.last_document_id,
        has_more=False,
    )
    selection_modes = _selection_modes(conn)
    summary.selection_modes = selection_modes

    try:
        if request.apply:
            conn.execute("BEGIN IMMEDIATE")
        for document in documents:
            document_id = _integer(document, "id")
            summary.documents_considered += 1
            chain = _prepare_document_chain(document, root, summary)
            if chain is None:
                continue
            filing_sections = _active_filing_sections(conn, document_id, selection_modes)
            transcript_segments = _active_transcript_segments(conn, document_id, selection_modes)
            output_sha256 = canonical_output_sha256(
                _document_node(chain, _required_text(document, "doc_type")),
                [_filing_section_node(row, chain) for row in filing_sections],
                [_transcript_segment_node(row, chain) for row in transcript_segments],
            )
            summary.documents_backfilled += 1
            if request.apply:
                _persist_document_chain(
                    conn,
                    document,
                    chain,
                    root,
                    output_sha256,
                    summary.run_at,
                    summary,
                )
            else:
                summary.records_planned += 7

            _backfill_filing_sections(conn, filing_sections, chain, request.apply, summary)
            _backfill_transcript_segments(conn, transcript_segments, chain, request.apply, summary)
    except Exception:
        if request.apply:
            conn.rollback()
        raise

    if documents:
        summary.last_document_id_after = _integer(documents[-1], "id")
    summary.has_more = _has_documents_after(conn, summary.last_document_id_after)
    if request.apply:
        conn.commit()
        _write_checkpoint(
            checkpoint_path,
            BackfillCheckpoint(
                last_document_id=summary.last_document_id_after,
                updated_at=datetime.now(UTC),
            ),
        )
        emit_structured_event(
            "evidence_ledger_backfill_completed",
            task_id=request.task_id,
            last_document_id=summary.last_document_id_after,
            records_created=summary.records_created,
            records_replayed=summary.records_replayed,
        )
    else:
        emit_structured_event(
            "evidence_ledger_backfill_dry_run",
            task_id=request.task_id,
            documents_considered=summary.documents_considered,
            records_planned=summary.records_planned,
        )
    return summary


def _prepare_document_chain(
    document: sqlite3.Row, root: Path, summary: BackfillSummary
) -> _DocumentChain | None:
    document_id = _integer(document, "id")
    path_value = _required_text(document, "file_path")
    resolved_path = _resolve_legacy_path(root, path_value)
    if resolved_path is None:
        _quarantine(summary, "path_outside_repo", document_id)
        return None
    if not resolved_path.is_file():
        _quarantine(summary, "content_missing", document_id)
        return None
    raw_bytes = resolved_path.read_bytes()
    actual_sha = hashlib.sha256(raw_bytes).hexdigest()
    recorded_sha = _required_text(document, "sha256").lower()
    if actual_sha != recorded_sha:
        _quarantine(summary, "sha256_mismatch", document_id)
        return None
    raw_size = _optional_integer(document, "raw_bytes_size")
    if raw_size is not None and raw_size != len(raw_bytes):
        _quarantine(summary, "byte_size_mismatch", document_id)
        return None
    _required_datetime(document, "fetched_at")
    _required_text(document, "ticker")
    _required_text(document, "source_type")
    _required_text(document, "doc_type")
    _note(summary, "issuer_identity_legacy_ticker")
    _note(summary, "language_und")
    return _DocumentChain(
        legacy_document_id=document_id,
        extraction_run_id=f"legacy-run-doc-{document_id}",
        document_node_id=f"legacy-node-doc-{document_id}",
        recorded_at=_required_datetime(document, "fetched_at"),
        legacy_source_ref=path_value,
    )


def _persist_document_chain(
    conn: sqlite3.Connection,
    document: sqlite3.Row,
    chain: _DocumentChain,
    root: Path,
    output_sha256: str,
    verified_at: datetime,
    summary: BackfillSummary,
) -> None:
    document_id = chain.legacy_document_id
    path = _resolve_legacy_path(root, _required_text(document, "file_path"))
    if path is None:
        raise RuntimeError("verified legacy path unexpectedly became invalid")
    raw_bytes = path.read_bytes()
    blob_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    fetched_at = _required_datetime(document, "fetched_at")
    ticker = _required_text(document, "ticker")
    source_type = _required_text(document, "source_type")
    doc_type = _required_text(document, "doc_type")
    source_url = _optional_text(document, "source_url") or _legacy_storage_source_uri(
        path, _required_text(document, "file_path")
    )
    existing_blob = _existing_blob(conn, blob_sha256)
    blob_byte_size, blob_media_type, blob_storage_uri, blob_recorded_at = existing_blob or (
        len(raw_bytes),
        _media_type(path),
        path.as_uri(),
        fetched_at,
    )
    ledger = EvidenceLedger(conn)
    records = (
        ContentBlob(
            sha256=blob_sha256,
            byte_size=blob_byte_size,
            media_type=blob_media_type,
            storage_uri=blob_storage_uri,
            recorded_at=blob_recorded_at,
        ),
        SourceObservation(
            observation_id=f"legacy-obs-{document_id}",
            idempotency_key=f"legacy-document:{document_id}:observation",
            source_kind=source_type,
            source_url=source_url,
            blob_sha256=blob_sha256,
            source_published_at=None,
            filing_at=None,
            accepted_at=None,
            observed_at=fetched_at,
            retrieved_at=fetched_at,
            retrieval_config_sha256=_CONFIG_SHA256,
            collector_code_version=_BACKFILL_VERSION,
        ),
        DocumentVersion(
            document_version_id=f"legacy-doc-{document_id}",
            document_key=f"legacy-document:{document_id}",
            version_sequence=1,
            observation_id=f"legacy-obs-{document_id}",
            blob_sha256=blob_sha256,
            issuer_id=f"legacy-ticker:{ticker}",
            ticker=ticker,
            document_type=doc_type,
            form_type=doc_type,
            accession_number=_optional_text(document, "accession_number"),
            exhibit_id=None,
            period_start=_optional_datetime(document, "period_start"),
            period_end=_optional_datetime(document, "period_end"),
            as_of_at=None,
            language="und",
            replaces_document_version_id=None,
            legacy_document_id=document_id,
            recorded_at=fetched_at,
        ),
        ExtractionRun(
            extraction_run_id=chain.extraction_run_id,
            idempotency_key=f"legacy-document:{document_id}:backfill-run",
            document_version_id=f"legacy-doc-{document_id}",
            input_sha256=blob_sha256,
            extractor_name="legacy-evidence-backfill",
            extractor_config_sha256=_EXTRACTOR_CONFIG_SHA256,
            extractor_code_version=_BACKFILL_VERSION,
            output_sha256=output_sha256,
            started_at=fetched_at,
            completed_at=fetched_at,
            outcome="succeeded",
        ),
        _document_node(chain, doc_type),
    )
    for record in records:
        _persist(record, ledger, summary)
    link_ledger = EvidenceLinkLedger(conn)
    location_uri = path.as_uri()
    location_identity = hashlib.sha256(f"{blob_sha256}\0{location_uri}".encode()).hexdigest()
    current_location = conn.execute(
        "SELECT location_observation_id, availability_state, location_sequence "
        "FROM v_evidence_blob_locations_current "
        "WHERE blob_sha256 = ? AND storage_uri = ?",
        (blob_sha256, location_uri),
    ).fetchone()
    if current_location is None or str(current_location[1]) != "present":
        sequence = 1 if current_location is None else int(current_location[2]) + 1
        parent_id = None if current_location is None else str(current_location[0])
        revision_identity = hashlib.sha256(
            f"{location_identity}\0{sequence}\0present".encode()
        ).hexdigest()
        location_result = link_ledger.persist_location(
            BlobLocationObservation(
                location_observation_id=f"legacy-location:{revision_identity}",
                idempotency_key=f"legacy-location:{revision_identity}",
                blob_sha256=blob_sha256,
                storage_uri=location_uri,
                location_kind="local",
                availability_state="present",
                location_sequence=sequence,
                verified_at=fetched_at if sequence == 1 else verified_at,
                verified_byte_size=len(raw_bytes),
                verified_sha256=blob_sha256,
                supersedes_location_observation_id=parent_id,
                recorded_at=fetched_at if sequence == 1 else verified_at,
            )
        )
        _account_link_result(location_result.created, summary)
    else:
        summary.records_replayed += 1
    primary_link = link_ledger.persist_link(
        DocumentObservationLink(
            link_id=f"legacy-primary-link:{document_id}",
            document_version_id=f"legacy-doc-{document_id}",
            observation_id=f"legacy-obs-{document_id}",
            link_kind="primary",
            linked_at=fetched_at,
        )
    )
    _account_link_result(primary_link.created, summary)


def _backfill_filing_sections(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    chain: _DocumentChain,
    apply: bool,
    summary: BackfillSummary,
) -> None:
    for row in rows:
        summary.filing_sections_considered += 1
        if apply:
            _persist(_filing_section_node(row, chain), EvidenceLedger(conn), summary)
        else:
            summary.records_planned += 1
        summary.filing_sections_backfilled += 1


def _backfill_transcript_segments(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    chain: _DocumentChain,
    apply: bool,
    summary: BackfillSummary,
) -> None:
    for row in rows:
        summary.transcript_segments_considered += 1
        if apply:
            _persist(_transcript_segment_node(row, chain), EvidenceLedger(conn), summary)
        else:
            summary.records_planned += 1
        summary.transcript_segments_backfilled += 1


def _active_filing_sections(
    conn: sqlite3.Connection, document_id: int, selection_modes: dict[str, str]
) -> list[sqlite3.Row]:
    if not _has_table(conn, "filing_sections"):
        return []
    relation = selected_filing_sections_relation(conn)
    selection_modes["filing_sections"] = relation.selection_mode
    return conn.execute(
        "SELECT * FROM " + relation.sql + " WHERE doc_id = ? ORDER BY id",  # nosec B608 -- trusted internal SQL shape; values remain bound
        (document_id,),
    ).fetchall()


def _active_transcript_segments(
    conn: sqlite3.Connection, document_id: int, selection_modes: dict[str, str]
) -> list[sqlite3.Row]:
    if not _has_table(conn, "transcripts") or not _has_table(conn, "transcript_segments"):
        return []
    relation = selected_transcripts_relation(conn)
    selection_modes["transcripts"] = relation.selection_mode
    transcript_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(transcripts)")}
    call_date = "transcript.call_date" if "call_date" in transcript_columns else "NULL"
    return conn.execute(
        "SELECT segment.*, "  # nosec B608 -- trusted internal SQL shape; values remain bound
        + call_date
        + " AS transcript_call_date FROM transcript_segments AS segment JOIN "
        + relation.sql
        + " AS transcript ON transcript.id = segment.transcript_id "
        "WHERE transcript.document_id = ? ORDER BY transcript.id, segment.seq, segment.id",
        (document_id,),
    ).fetchall()


def _document_node(chain: _DocumentChain, doc_type: str) -> EvidenceNode:
    return EvidenceNode(
        node_id=chain.document_node_id,
        evidence_key=f"legacy-document:{chain.legacy_document_id}",
        revision=1,
        extraction_run_id=chain.extraction_run_id,
        parent_node_id=None,
        supersedes_node_id=None,
        node_kind="document",
        text=f"Legacy document {chain.legacy_document_id}: {doc_type}.",
        locator=EvidenceLocator(
            source_ref=chain.legacy_source_ref,
            legacy_table="documents",
            legacy_row_id=chain.legacy_document_id,
        ),
        recorded_at=chain.recorded_at,
    )


def _filing_section_node(row: sqlite3.Row, chain: _DocumentChain) -> EvidenceNode:
    section_id = _integer(row, "id")
    return EvidenceNode(
        node_id=f"legacy-node-filing-section-{section_id}",
        evidence_key=f"legacy-filing-section:{section_id}",
        revision=1,
        extraction_run_id=chain.extraction_run_id,
        parent_node_id=chain.document_node_id,
        supersedes_node_id=None,
        node_kind="section",
        text=_required_text(row, "text"),
        locator=EvidenceLocator(
            source_ref=_required_text(row, "source_ref"),
            filing_section_key_raw=_required_text(row, "section_key_raw"),
            filing_ordinal=_integer(row, "ordinal"),
            legacy_table="filing_sections",
            legacy_row_id=section_id,
        ),
        recorded_at=_optional_datetime(row, "created_at") or chain.recorded_at,
    )


def _transcript_segment_node(row: sqlite3.Row, chain: _DocumentChain) -> EvidenceNode:
    segment_id = _integer(row, "id")
    return EvidenceNode(
        node_id=f"legacy-node-transcript-segment-{segment_id}",
        evidence_key=f"legacy-transcript-segment:{segment_id}",
        revision=1,
        extraction_run_id=chain.extraction_run_id,
        parent_node_id=chain.document_node_id,
        supersedes_node_id=None,
        node_kind="transcript_turn",
        text=_required_text(row, "text"),
        locator=EvidenceLocator(
            transcript_turn_sequence=_integer(row, "seq"),
            transcript_speaker=_optional_text(row, "speaker"),
            transcript_time_code_start=_optional_text(row, "time_code_start"),
            transcript_time_code_end=_optional_text(row, "time_code_end"),
            legacy_table="transcript_segments",
            legacy_row_id=segment_id,
        ),
        recorded_at=_optional_datetime(row, "transcript_call_date") or chain.recorded_at,
    )


def canonical_output_sha256(
    document_node: EvidenceNode,
    filing_nodes: list[EvidenceNode],
    transcript_nodes: list[EvidenceNode],
) -> str:
    """Hash the canonical immutable node payload emitted by one legacy extraction run."""

    nodes = (document_node, *filing_nodes, *transcript_nodes)
    payload = [node.model_dump(mode="json", exclude_none=True) for node in nodes]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _persist(record: LedgerRecord, ledger: EvidenceLedger, summary: BackfillSummary) -> None:
    result = ledger.persist(record)
    if result.created:
        summary.records_created += 1
    else:
        summary.records_replayed += 1


def _account_link_result(created: bool, summary: BackfillSummary) -> None:
    if created:
        summary.records_created += 1
    else:
        summary.records_replayed += 1


def _selection_modes(conn: sqlite3.Connection) -> dict[str, str]:
    modes: dict[str, str] = {}
    if _has_table(conn, "filing_sections"):
        modes["filing_sections"] = selected_filing_sections_relation(conn).selection_mode
    if _has_table(conn, "transcripts"):
        modes["transcripts"] = selected_transcripts_relation(conn).selection_mode
    return modes


def _documents_after(
    conn: sqlite3.Connection, last_document_id: int, batch_size: int
) -> list[sqlite3.Row]:
    _require_columns(
        conn,
        "documents",
        {"id", "ticker", "source_type", "doc_type", "file_path", "sha256", "fetched_at"},
    )
    return conn.execute(
        "SELECT * FROM documents WHERE id > ? ORDER BY id LIMIT ?", (last_document_id, batch_size)
    ).fetchall()


def _has_documents_after(conn: sqlite3.Connection, document_id: int) -> bool:
    row = conn.execute("SELECT 1 FROM documents WHERE id > ? LIMIT 1", (document_id,)).fetchone()
    return row is not None


def _existing_blob(conn: sqlite3.Connection, sha256: str) -> tuple[int, str, str, datetime] | None:
    row = conn.execute(
        "SELECT byte_size, media_type, storage_uri, recorded_at FROM evidence_content_blobs WHERE sha256 = ?",
        (sha256,),
    ).fetchone()
    if row is None:
        return None
    byte_size, media_type, storage_uri, value = row
    if isinstance(byte_size, bool) or not isinstance(byte_size, int):
        raise RuntimeError("Evidence blob has an invalid byte_size")
    if not isinstance(media_type, str) or not isinstance(storage_uri, str):
        raise RuntimeError("Evidence blob has invalid immutable metadata")
    if isinstance(value, datetime):
        return byte_size, media_type, storage_uri, value
    if isinstance(value, str):
        try:
            return (
                byte_size,
                media_type,
                storage_uri,
                datetime.fromisoformat(value.replace("Z", "+00:00")),
            )
        except ValueError as error:
            raise RuntimeError("Evidence blob has an invalid recorded_at clock") from error
    else:
        raise RuntimeError("Evidence blob has an invalid recorded_at clock")


def _require_ledger_tables(conn: sqlite3.Connection) -> None:
    for table in (
        "evidence_content_blobs",
        "evidence_source_observations",
        "evidence_document_versions",
        "evidence_extraction_runs",
        "evidence_nodes",
        "evidence_blob_location_observations",
        "evidence_document_observation_links",
    ):
        _require_table(conn, table)


def _require_table(conn: sqlite3.Connection, table: str) -> None:
    if not _has_table(conn, table):
        raise RuntimeError(
            f"Required table {table!r} is unavailable; apply evidence-ledger migrations through 0218"
        )


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        is not None
    )


def _require_columns(conn: sqlite3.Connection, table: str, required: set[str]) -> None:
    actual = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    missing = sorted(required - actual)
    if missing:
        raise RuntimeError(
            f"Legacy {table} schema is missing required columns: {', '.join(missing)}"
        )


def _resolve_legacy_path(root: Path, stored_path: str) -> Path | None:
    path_without_fragment, _, _ = stored_path.partition("#")
    candidate = Path(path_without_fragment)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def _legacy_storage_source_uri(path: Path, stored_path: str) -> str:
    """Retain a legacy file fragment (such as ``#accn=...``) without hashing it as a path."""

    _, separator, fragment = stored_path.partition("#")
    return path.as_uri() if not separator else f"{path.as_uri()}#{fragment}"


def _read_checkpoint(path: Path) -> BackfillCheckpoint:
    if not path.exists():
        return BackfillCheckpoint(last_document_id=0, updated_at=datetime.now(UTC))
    return BackfillCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))


def _write_checkpoint(path: Path, checkpoint: BackfillCheckpoint) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(checkpoint.model_dump_json(), encoding="utf-8")
    temporary.replace(path)


def _quarantine(summary: BackfillSummary, reason: str, document_id: int) -> None:
    summary.documents_quarantined += 1
    _note(summary, reason)
    if summary.documents_quarantined <= _QUARANTINE_EVENT_LIMIT:
        emit_structured_event(
            "evidence_ledger_backfill_quarantined",
            document_id=document_id,
            reason=reason,
        )
    elif summary.documents_quarantined == _QUARANTINE_EVENT_LIMIT + 1:
        emit_structured_event(
            "evidence_ledger_backfill_quarantine_events_suppressed",
            emitted=_QUARANTINE_EVENT_LIMIT,
        )


def _note(summary: BackfillSummary, finding: str) -> None:
    """Account for an explicit legacy limitation without changing source data."""

    counts = Counter(summary.finding_counts)
    counts[finding] += 1
    summary.finding_counts = dict(counts)


def _media_type(path: Path) -> str:
    return {
        ".html": "text/html",
        ".htm": "text/html",
        ".json": "application/json",
        ".pdf": "application/pdf",
        ".txt": "text/plain",
    }.get(path.suffix.lower(), "application/octet-stream")


def _required_text(row: sqlite3.Row, column: str) -> str:
    value = row[column]
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Legacy row has no usable {column}")
    return value


def _optional_text(row: sqlite3.Row, column: str) -> str | None:
    if column not in tuple(row.keys()):
        return None
    value = row[column]
    return value if isinstance(value, str) and value.strip() else None


def _integer(row: sqlite3.Row, column: str) -> int:
    value = row[column]
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"Legacy row has no usable integer {column}")
    return value


def _optional_integer(row: sqlite3.Row, column: str) -> int | None:
    if column not in tuple(row.keys()) or row[column] is None:
        return None
    return _integer(row, column)


def _required_datetime(row: sqlite3.Row, column: str) -> datetime:
    value = _datetime_value(row, column)
    if value is None:
        raise RuntimeError(f"Legacy row has no usable {column}")
    return value


def _optional_datetime(row: sqlite3.Row, column: str) -> datetime | None:
    if column not in tuple(row.keys()):
        return None
    return _datetime_value(row, column)


def _datetime_value(row: sqlite3.Row, column: str) -> datetime | None:
    value = row[column]
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise RuntimeError(f"Legacy row has invalid datetime {column}: {value!r}") from error
    raise RuntimeError(f"Legacy row has invalid datetime {column}")
