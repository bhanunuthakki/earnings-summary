"""Ingest on-disk earnings transcripts into the new provenance schema.

Walks `transcripts/processed/` and `transcripts/raw/` for files matching
`<TICKER>_Q<N>_<YYYY>.{pdf,txt}`. Files for tickers outside the user's
portfolio + watchlist are skipped (logged). Idempotent on file sha256:
re-running is a no-op for already-ingested bytes.

Usage:
    python execution/ingest_transcripts.py            # all tracked tickers
    python execution/ingest_transcripts.py --ticker GOOG
    python execution/ingest_transcripts.py --dry-run
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sqlite3
import sys
from contextlib import suppress
from datetime import date
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
import index_manager  # noqa: E402
from compute import evidence_snapshot  # noqa: E402
from compute.transcript_ingest import (  # noqa: E402
    IngestResult,
    ParsedFilename,
    ingest_evidence_file,
    map_to_period,
    parse_transcript_filename,
)
from models.documents import DocType, SourceType  # noqa: E402
from models.runs import StageName, StageStatus  # noqa: E402
from pipeline.invocation_fingerprint import files_fingerprint  # noqa: E402
from pipeline.queries import open_db  # noqa: E402
from pipeline.run_accounting import (  # noqa: E402
    JsonValue,
    PipelineRunSuppressedError,
    end_run,
    record_stage,
    start_run,
    suppression_payload,
)
from pipeline.transcript_acquisition import (  # noqa: E402
    COMBINED_SOURCE_REGIME_IDENTITY,
    AuthorizedTranscriptArtifact,
    TranscriptAcquisitionDeniedError,
    load_authorized_transcript_receipt,
    load_authorized_transcript_replay,
    persist_authorized_transcript_artifact,
    read_authorized_transcript,
    stage_pending_issuer_transcripts,
    transcript_acquisition_receipt_id,
)
from provenance.selection import selected_transcripts_relation  # noqa: E402
from transcripts.acquisition_semantics import (  # noqa: E402
    TRANSCRIPT_ACQUISITION_POLICY_VERSION,
    ExistingArtifactBehavior,
    TranscriptAcquisitionEntrypoint,
    TranscriptAcquisitionRequest,
    TranscriptProvider,
)
from transcripts.immutable_staging import install_transcript_output  # noqa: E402

_TRANSCRIPT_DIRS = (
    PROJECT_ROOT / "transcripts" / "processed",
    PROJECT_ROOT / "transcripts" / "raw",
)


class EvidencePathConflictError(ValueError):
    """An immutable document path now contains bytes different from its receipt."""


UnsafeEvidencePathError = evidence_snapshot.UnsafeEvidencePathError
EvidenceSourceChangedError = evidence_snapshot.EvidenceSourceChangedError


def _stage_evidence_file(
    source: Path,
    project_root: Path,
    raw_root: Path,
    processed_root: Path,
    *,
    snapshot: evidence_snapshot.EvidenceSnapshot | None = None,
) -> Path:
    """Atomically bind one stable byte snapshot to a content-addressed raw path."""
    allowed_roots = (raw_root, processed_root)
    source_parent = source.parent
    if source_parent not in allowed_roots:
        raise UnsafeEvidencePathError("transcript source is outside an intake root")
    stable = snapshot or evidence_snapshot.capture_snapshot(source, source_parent)
    payload, digest = stable.payload, stable.sha256
    evidence_root = raw_root / ".evidence"
    digest_root = evidence_root / digest
    digest_root.mkdir(parents=True, exist_ok=True)
    destination = digest_root / source.name
    if destination.exists():
        if evidence_snapshot.capture_snapshot(destination, raw_root).sha256 != digest:
            raise EvidencePathConflictError("content-addressed evidence path has different bytes")
        return destination

    temporary = digest_root / f".{source.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if evidence_snapshot.capture_snapshot(destination, raw_root).sha256 != digest:
                raise EvidencePathConflictError(
                    "content-addressed evidence path has different bytes"
                ) from None
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()
    if evidence_snapshot.capture_snapshot(destination, raw_root).sha256 != digest:
        raise EvidenceSourceChangedError("staged transcript evidence failed verification")
    return destination


def _assert_evidence_path_identity(
    conn: sqlite3.Connection,
    *,
    file_path: Path,
    project_root: Path,
    current_sha: str,
) -> None:
    """Fail closed before ingest when a recorded path's bytes have changed."""
    relative = str(file_path.relative_to(project_root)).replace("\\", "/")
    recorded = {
        str(row["sha256"])
        for row in conn.execute(
            "SELECT sha256 FROM documents WHERE file_path = ?", (relative,)
        ).fetchall()
    }
    if recorded and recorded != {current_sha}:
        raise EvidencePathConflictError(f"immutable evidence path has different bytes: {relative}")


def _promote_raw_to_processed(
    result: IngestResult,
    parsed: ParsedFilename,
    conn: sqlite3.Connection,
    project_root: Path,
    *,
    commit: bool = True,
) -> Path:
    """Move a freshly-ingested `transcripts/raw/<name>` file to `transcripts/processed/<name>`.

    Side effects on success: updates `documents.file_path` for the row and
    rewrites `local_path`/`filepath` in the two on-disk indexes via
    `index_manager.update_local_path`.

    No-op (returns `result.file_path` unchanged) if:
      - the source file is not in a `raw/` directory (already promoted or
        living somewhere else like `intake_documents/`), or
      - `result.skipped_existing` is True (no fresh ingest → nothing to update).

    Conflict handling at the target slot:
      - target missing: atomic `os.replace`, then DB + index updates.
      - target exists with **matching** sha256: the raw/ duplicate is removed
        and the DB/index are pointed at the surviving processed/ file.
      - target exists with **different** sha256: both files are left in place,
        a `transcript_promotion_conflict` JSON event is written to stderr,
        and no DB write occurs. Rare; flagged for human investigation.
    """
    if result.skipped_existing:
        return result.file_path
    if result.file_path.parent.name != "raw":
        return result.file_path

    src = result.file_path
    target = project_root / "transcripts" / "processed" / src.name
    new_rel = str(target.relative_to(project_root)).replace("\\", "/")
    fiscal_quarter = f"Q{parsed.quarter_idx}"

    if target.exists():
        src_sha = evidence_snapshot.capture_snapshot(src, src.parent).sha256
        target_sha = evidence_snapshot.capture_snapshot(target, target.parent).sha256
        if src_sha == target_sha:
            os.remove(src)
        else:
            sys.stderr.write(
                json.dumps(
                    {
                        "event": "transcript_promotion_conflict",
                        "src": str(src.relative_to(project_root)).replace("\\", "/"),
                        "target": new_rel,
                        "src_sha256": src_sha,
                        "target_sha256": target_sha,
                        "document_id": result.document_id,
                    }
                )
                + "\n"
            )
            return src
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, target)

    conn.execute(
        "UPDATE documents SET file_path = ? WHERE id = ?",
        (new_rel, result.document_id),
    )
    if commit:
        conn.commit()
    index_manager.update_local_path(
        ticker=parsed.ticker,
        year=parsed.fiscal_year_label,
        quarter=fiscal_quarter,
        doc_type="transcript",
        new_path=new_rel,
    )
    return target


def _load_tracked_tickers(conn: sqlite3.Connection) -> frozenset[str]:
    """Return the set of active analyzed tickers (uppercased).

    Transcripts are ingested for everything we analyze (portfolio + watchlist +
    evaluation). ETFs are deliberately excluded — the `etf` list_type is the
    skip-signal used across report steps.
    """
    placeholders = ", ".join("?" for _ in db.ACTIVE_LIST_TYPES)
    cur = conn.execute(
        f"SELECT ticker FROM tracked_companies WHERE list_type IN ({placeholders})",
        db.ACTIVE_LIST_TYPES,
    )
    return frozenset(r["ticker"].upper() for r in cur.fetchall())


def _candidate_files(restrict_ticker: str | None) -> list[tuple[Path, ParsedFilename]]:
    """Return [(path, parsed)] across both transcript directories. Restrict if requested."""
    out: list[tuple[Path, ParsedFilename]] = []
    for d in _TRANSCRIPT_DIRS:
        if not d.exists():
            continue
        for path in sorted(d.iterdir()):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".pdf", ".txt"}:
                continue
            parsed = parse_transcript_filename(path)
            if parsed is None:
                continue
            if restrict_ticker is not None and parsed.ticker != restrict_ticker.upper():
                continue
            out.append((path, parsed))
    return out


def _receipt_scoped_candidates(
    conn: sqlite3.Connection,
    receipt_ids: list[str],
    *,
    restrict_ticker: str | None,
    expected_owner_requested: bool,
    project_root: Path,
) -> tuple[list[tuple[Path, ParsedFilename]], dict[Path, AuthorizedTranscriptArtifact]]:
    """Resolve only exact currently-authorized raw artifacts named by durable receipts."""

    if len(receipt_ids) != len(set(receipt_ids)):
        raise TranscriptAcquisitionDeniedError("duplicate transcript receipt selector")
    raw_root = project_root / "transcripts" / "raw"
    candidates: list[tuple[Path, ParsedFilename]] = []
    artifacts: dict[Path, AuthorizedTranscriptArtifact] = {}
    for receipt_id in receipt_ids:
        artifact = load_authorized_transcript_receipt(
            conn,
            receipt_id=receipt_id,
            project_root=project_root,
            trusted_staging_root=project_root / ".tmp" / "transcript-acquisition",
        )
        request = artifact.authorization.request
        if (
            request.entrypoint is not TranscriptAcquisitionEntrypoint.FETCH_QA_TRANSCRIPT
            or request.owner_requested is not expected_owner_requested
            or (restrict_ticker is not None and request.canonical_ticker != restrict_ticker.upper())
        ):
            raise TranscriptAcquisitionDeniedError(
                "transcript receipt does not match the requested ingest scope"
            )
        path = project_root / artifact.canonical_document_path
        if path.parent != raw_root:
            raise TranscriptAcquisitionDeniedError(
                "transcript receipt does not name a canonical raw artifact"
            )
        parsed = parse_transcript_filename(path)
        if parsed is None or (
            parsed.ticker != request.canonical_ticker
            or parsed.fiscal_year_label != request.fiscal_year
            or parsed.quarter_idx != request.fiscal_quarter
        ):
            raise TranscriptAcquisitionDeniedError(
                "transcript receipt filename does not match its authorized target"
            )
        candidates.append((path, parsed))
        artifacts[path] = artifact
    return candidates, artifacts


def _is_exactly_ingested_processed_candidate(
    conn: sqlite3.Connection,
    *,
    path: Path,
    parsed: ParsedFilename,
    project_root: Path,
    processed_root: Path,
) -> bool:
    """Return whether this exact processed artifact already has a transcript row.

    Historical ingests predate acquisition receipts. They may be skipped, but
    only when the immutable file path, bytes, ticker, and fiscal period all
    match their existing document/transcript rows. Raw candidates never use
    this compatibility seam and still require an authorized acquisition receipt.
    """

    if path.parent != processed_root:
        return False
    snapshot = evidence_snapshot.capture_snapshot(path, processed_root)
    relative = path.relative_to(project_root).as_posix()
    period = map_to_period(parsed)
    transcripts = selected_transcripts_relation(conn).sql
    row = conn.execute(
        "SELECT 1 FROM documents d "
        f"JOIN {transcripts} t ON t.document_id = d.id "  # nosec B608 -- trusted relation
        "WHERE d.file_path = ? AND d.sha256 = ? AND d.ticker = ? "
        "AND d.raw_bytes_size = ? AND d.doc_type = ? AND d.source_type = ? "
        "AND d.fetch_status = 'ok' AND t.ticker = ? "
        "AND t.fiscal_period_type = ? AND t.period_end = ? "
        "AND EXISTS (SELECT 1 FROM transcript_segments s WHERE s.transcript_id = t.id) "
        "LIMIT 1",
        (
            relative,
            snapshot.sha256,
            parsed.ticker,
            len(snapshot.payload),
            DocType.EARNINGS_CALL_TRANSCRIPT.value,
            SourceType.TRANSCRIPT_AUDIO.value,
            parsed.ticker,
            period.fiscal_period_type.value,
            str(period.period_end),
        ),
    ).fetchone()
    return row is not None


def _backfill_existing_ir_transcripts(
    conn: sqlite3.Connection,
    run_id: str,
    restrict_ticker: str | None,
    authorized_artifacts: dict[int, AuthorizedTranscriptArtifact],
) -> tuple[list[dict[str, object]], int, int]:
    """Walk `documents WHERE doc_type='ir_transcript'` and emit transcripts/segments rows.

    Returns (ingested_records, skipped_existing_count, failed_count).
    """
    sql = "SELECT id, ticker, file_path FROM documents WHERE doc_type = 'ir_transcript'"
    params: tuple[str, ...] = ()
    if restrict_ticker is not None:
        sql += " AND ticker = ?"
        params = (restrict_ticker.upper(),)
    sql += " ORDER BY ticker, period_end"
    cur = conn.execute(sql, params)
    rows = cur.fetchall()

    ingested: list[dict[str, object]] = []
    skipped_existing = 0
    failed = 0
    for row in rows:
        doc_id = int(row["id"])
        ticker = row["ticker"]
        rel_path = str(row["file_path"])
        location = evidence_snapshot.recorded_evidence_location(PROJECT_ROOT, rel_path)
        if location is None:
            record_stage(
                conn,
                run_id,
                ticker,
                StageName.INGEST,
                StageStatus.FAILED,
                error_msg=f"unsafe_evidence_path: doc#{doc_id}",
            )
            failed += 1
            sys.stderr.write(f"FAILED unsafe evidence path for doc#{doc_id}\n")
            continue
        abs_path, allowed_root = location
        try:
            artifact = authorized_artifacts.get(doc_id)
            if artifact is None:
                raise TranscriptAcquisitionDeniedError(
                    f"IR transcript doc#{doc_id} lacks an authorized artifact"
                )
            result = ingest_evidence_file(
                conn,
                document_id=doc_id,
                file_path=abs_path,
                allowed_root=allowed_root,
                project_root=PROJECT_ROOT,
                authorized_artifact=artifact,
            )
            assert result is not None
        except (ValueError, OSError) as e:
            record_stage(
                conn,
                run_id,
                ticker,
                StageName.INGEST,
                StageStatus.FAILED,
                error_msg=f"ir_transcript doc#{doc_id}: {type(e).__name__}",
            )
            failed += 1
            sys.stderr.write(f"FAILED doc#{doc_id}: {type(e).__name__}\n")
            continue
        if result.skipped_existing:
            skipped_existing += 1
            record_stage(
                conn,
                run_id,
                ticker,
                StageName.INGEST,
                StageStatus.SKIPPED,
                period_end=result.period_end,
            )
        else:
            record_stage(
                conn,
                run_id,
                ticker,
                StageName.INGEST,
                StageStatus.OK,
                period_end=result.period_end,
            )
            ingested.append(
                {
                    "ticker": result.ticker,
                    "period_end": result.period_end.date().isoformat(),
                    "document_id": result.document_id,
                    "transcript_id": result.transcript_id,
                    "segments": result.segment_count,
                    "file": rel_path,
                    "qa_status": result.qa_status.value,
                    "qa_signals": list(result.qa_signals),
                }
            )
    return (ingested, skipped_existing, failed)


def _ir_transcript_sources(
    conn: sqlite3.Connection, restrict_ticker: str | None
) -> list[tuple[int, str, str | None, Path]]:
    sql = "SELECT id, ticker, period_end, file_path FROM documents WHERE doc_type = 'ir_transcript'"
    params: tuple[str, ...] = ()
    if restrict_ticker is not None:
        sql += " AND ticker = ?"
        params = (restrict_ticker.upper(),)
    sql += " ORDER BY ticker, period_end, id"
    rows = conn.execute(sql, params).fetchall()
    sources: list[tuple[int, str, str | None, Path]] = []
    for row in rows:
        raw_path = str(row["file_path"]) if row["file_path"] else ""
        path = Path(raw_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        sources.append(
            (
                int(row["id"]),
                str(row["ticker"]).upper(),
                str(row["period_end"]) if row["period_end"] is not None else None,
                path,
            )
        )
    return sources


def _invocation_inputs(
    in_scope: list[tuple[Path, ParsedFilename]],
    ir_sources: list[tuple[int, str, str | None, Path]],
    *,
    include_ir_transcripts: bool,
    no_promote: bool,
    receipt_artifacts: dict[Path, AuthorizedTranscriptArtifact],
) -> dict[str, JsonValue]:
    source_paths = [path for path, _ in in_scope]
    return {
        "include_ir_transcripts": include_ir_transcripts,
        "no_promote": no_promote,
        "candidate_files": files_fingerprint(source_paths, root=PROJECT_ROOT),
        "transcript_receipts": [
            {
                "receipt_id": transcript_acquisition_receipt_id(artifact),
                "canonical_document_path": artifact.canonical_document_path.as_posix(),
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
            }
            for artifact in sorted(
                receipt_artifacts.values(),
                key=transcript_acquisition_receipt_id,
            )
        ],
        "ir_documents": [
            {"document_id": doc_id, "ticker": ticker, "period_end": period_end}
            for doc_id, ticker, period_end, _ in ir_sources
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", help="Restrict to a single ticker (case-insensitive)")
    parser.add_argument(
        "--receipt-id",
        action="append",
        default=[],
        help="Ingest only this exact durable transcript acquisition receipt (repeatable)",
    )
    parser.add_argument(
        "--automatic",
        action="store_true",
        help="Treat a --ticker restriction as scheduler scope, not an owner-requested acquisition",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print plan without DB writes")
    parser.add_argument(
        "--include-ir-transcripts",
        action="store_true",
        help="Also backfill `transcripts` + `transcript_segments` for existing "
        "documents with doc_type='ir_transcript' that haven't been parsed yet.",
    )
    parser.add_argument(
        "--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"), help="Path to portfolio.db"
    )
    parser.add_argument(
        "--no-promote",
        action="store_true",
        help="Skip the raw/→processed/ promotion step (kill switch for the auto-mover).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ingest again even when the same immutable transcript inputs already completed.",
    )
    args = parser.parse_args()

    conn = open_db(args.db)
    try:
        tracked = _load_tracked_tickers(conn)
        selected_artifacts: dict[Path, AuthorizedTranscriptArtifact] = {}
        try:
            if args.receipt_id:
                candidates, selected_artifacts = _receipt_scoped_candidates(
                    conn,
                    args.receipt_id,
                    restrict_ticker=args.ticker,
                    expected_owner_requested=bool(args.ticker) and not args.automatic,
                    project_root=PROJECT_ROOT,
                )
            else:
                candidates = _candidate_files(args.ticker)
        except TranscriptAcquisitionDeniedError as exc:
            sys.stderr.write(
                json.dumps(
                    {
                        "event": "transcript_acquisition_denied",
                        "ticker": args.ticker.upper() if args.ticker else None,
                        "error_class": type(exc).__name__,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            return 2

        in_scope = [(p, parsed) for p, parsed in candidates if parsed.ticker in tracked]
        out_of_scope = [(p, parsed) for p, parsed in candidates if parsed.ticker not in tracked]

        plan = {
            "candidates_total": len(candidates),
            "receipt_ids": list(args.receipt_id),
            "in_scope": [
                {"file": str(p.relative_to(PROJECT_ROOT)), "ticker": parsed.ticker}
                for p, parsed in in_scope
            ],
            "out_of_scope": sorted({parsed.ticker for _, parsed in out_of_scope}),
        }

        if args.dry_run:
            print(json.dumps(plan, indent=2))
            return 0

        processed_root = PROJECT_ROOT / "transcripts" / "processed"
        processed_root.mkdir(parents=True, exist_ok=True)
        already_ingested = [
            (path, parsed)
            for path, parsed in in_scope
            if _is_exactly_ingested_processed_candidate(
                conn,
                path=path,
                parsed=parsed,
                project_root=PROJECT_ROOT,
                processed_root=processed_root,
            )
        ]
        pending_scope = [item for item in in_scope if item not in already_ingested]

        raw_authorizations: dict[Path, AuthorizedTranscriptArtifact] = {}
        for path, parsed in pending_scope:
            selected = selected_artifacts.get(path)
            if selected is not None:
                raw_authorizations[path] = selected
                continue
            request = TranscriptAcquisitionRequest(
                entrypoint=TranscriptAcquisitionEntrypoint.FETCH_QA_TRANSCRIPT,
                canonical_ticker=parsed.ticker,
                fiscal_year=parsed.fiscal_year_label,
                fiscal_quarter=parsed.quarter_idx,
                as_of=date.today(),
                source_type=SourceType.IR_DOC,
                document_type=DocType.EARNINGS_CALL_TRANSCRIPT,
                provider=TranscriptProvider.ISSUER_IR,
                owner_requested=bool(args.ticker) and not args.automatic,
                existing_artifact=False,
                existing_artifact_behavior=ExistingArtifactBehavior.REFRESH,
                source_policy_version=TRANSCRIPT_ACQUISITION_POLICY_VERSION,
                source_regime_identity=COMBINED_SOURCE_REGIME_IDENTITY,
            )
            try:
                artifact = load_authorized_transcript_replay(
                    conn,
                    request=request,
                    project_root=PROJECT_ROOT,
                    trusted_staging_root=PROJECT_ROOT / ".tmp" / "transcript-acquisition",
                )
                if (
                    artifact is None
                    or read_authorized_transcript(
                        conn,
                        artifact,
                        project_root=PROJECT_ROOT,
                        trusted_staging_root=PROJECT_ROOT / ".tmp" / "transcript-acquisition",
                    )
                    != path.read_bytes()
                ):
                    raise TranscriptAcquisitionDeniedError(
                        f"{path.name} lacks its exact durable issuer acquisition receipt"
                    )
            except (OSError, ValueError, TranscriptAcquisitionDeniedError) as exc:
                sys.stderr.write(
                    json.dumps(
                        {
                            "event": "transcript_acquisition_denied",
                            "ticker": parsed.ticker,
                            "error_class": type(exc).__name__,
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
                return 2
            raw_authorizations[path] = artifact

        raw_root = PROJECT_ROOT / "transcripts" / "raw"
        snapshots: dict[Path, evidence_snapshot.EvidenceSnapshot] = {}
        staged_authorizations: dict[Path, AuthorizedTranscriptArtifact] = {}
        staged_scope: list[tuple[Path, ParsedFilename]] = []
        staging_failures: list[tuple[Path, ParsedFilename, str]] = []
        for path, parsed in pending_scope:
            try:
                stable = evidence_snapshot.capture_snapshot(path, path.parent)
                artifact = raw_authorizations[path]
                if stable.sha256 != artifact.sha256 or len(stable.payload) != artifact.size_bytes:
                    raise TranscriptAcquisitionDeniedError(
                        "transcript receipt raw artifact does not match its exact bytes"
                    )
                staged = _stage_evidence_file(
                    path,
                    PROJECT_ROOT,
                    raw_root,
                    processed_root,
                    snapshot=stable,
                )
                _assert_evidence_path_identity(
                    conn,
                    file_path=staged,
                    project_root=PROJECT_ROOT,
                    current_sha=stable.sha256,
                )
                staged_scope.append((staged, parsed))
                staged_authorizations[staged] = artifact
                snapshots[staged] = evidence_snapshot.EvidenceSnapshot(
                    path=staged,
                    payload=stable.payload,
                    sha256=stable.sha256,
                )
            except (
                OSError,
                ValueError,
                TranscriptAcquisitionDeniedError,
            ) as exc:
                sys.stderr.write(
                    json.dumps(
                        {
                            "event": "transcript_evidence_capture_failed",
                            "ticker": parsed.ticker,
                            "error_class": type(exc).__name__,
                        }
                    )
                    + "\n"
                )
                if args.receipt_id:
                    staging_failures.append((path, parsed, type(exc).__name__))
                    continue
                print(
                    json.dumps(
                        {
                            **plan,
                            "ingested": 0,
                            "skipped_existing": 0,
                            "failed": 1,
                            "terminal_status": "failed_closed",
                        }
                    )
                )
                return 1
        in_scope = staged_scope

        ir_sources = (
            _ir_transcript_sources(conn, args.ticker) if args.include_ir_transcripts else []
        )
        try:
            ir_artifacts = (
                stage_pending_issuer_transcripts(
                    conn,
                    tickers=sorted({ticker for _, ticker, _, _ in ir_sources}),
                    project_root=PROJECT_ROOT,
                    private_root=PROJECT_ROOT / ".tmp" / "transcript-acquisition",
                    entrypoint=TranscriptAcquisitionEntrypoint.INGEST_TRANSCRIPTS,
                    as_of=date.today(),
                )
                if ir_sources
                else {}
            )
        except (OSError, ValueError, TranscriptAcquisitionDeniedError) as exc:
            sys.stderr.write(
                json.dumps(
                    {
                        "event": "transcript_acquisition_denied",
                        "error_class": type(exc).__name__,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            return 2
        for artifact in ir_artifacts.values():
            persist_authorized_transcript_artifact(
                conn,
                artifact,
                project_root=PROJECT_ROOT,
                trusted_staging_root=PROJECT_ROOT / ".tmp" / "transcript-acquisition",
            )
        conn.commit()
        if not in_scope and not ir_sources:
            print(
                json.dumps(
                    {
                        **plan,
                        "ingested": 0,
                        "skipped_existing": len(already_ingested),
                        "failed": len(staging_failures),
                    },
                    indent=2,
                )
            )
            return 1 if staging_failures else 0

        ticker_scope = sorted(
            {parsed.ticker for _, parsed in in_scope}
            | {parsed.ticker for _, parsed in already_ingested}
            | {parsed.ticker for _, parsed, _ in staging_failures}
            | {ticker for _, ticker, _, _ in ir_sources}
        )
        try:
            run_id = start_run(
                conn,
                directive="ingest_transcripts",
                ticker_scope=ticker_scope,
                invocation_inputs=_invocation_inputs(
                    in_scope,
                    ir_sources,
                    include_ir_transcripts=bool(args.include_ir_transcripts),
                    no_promote=bool(args.no_promote),
                    receipt_artifacts=selected_artifacts,
                ),
                force=bool(args.force),
                deduplicate_completed=True,
            )
        except PipelineRunSuppressedError as exc:
            print(json.dumps(suppression_payload(exc)))
            return 0

        ingested: list[dict[str, object]] = []
        skipped_existing = len(already_ingested)
        failed = len(staging_failures)

        for _path, parsed in already_ingested:
            record_stage(
                conn,
                run_id,
                parsed.ticker,
                StageName.INGEST,
                StageStatus.SKIPPED,
                period_end=map_to_period(parsed).period_end,
            )

        for path, parsed, error_class in staging_failures:
            record_stage(
                conn,
                run_id,
                parsed.ticker,
                StageName.INGEST,
                StageStatus.FAILED,
                period_end=map_to_period(parsed).period_end,
                error_msg=f"{path.name}: {error_class}",
            )

        for path, parsed in in_scope:
            savepoint = (
                f"transcript_file_{parsed.ticker}_{parsed.fiscal_year_label}_{parsed.quarter_idx}"
            )
            conn.execute(f"SAVEPOINT {savepoint}")  # nosec B608 -- identifier from parsed filename
            artifact = staged_authorizations[path]
            try:
                ingest_path = path
                ingest_root = raw_root
                if not args.no_promote:
                    snapshot = snapshots[path]
                    processed_target = processed_root / path.name
                    _assert_evidence_path_identity(
                        conn,
                        file_path=processed_target,
                        project_root=PROJECT_ROOT,
                        current_sha=snapshot.sha256,
                    )
                    ingest_path = install_transcript_output(
                        snapshot.payload,
                        processed_root,
                        path.name,
                        expected_sha256=artifact.sha256,
                        expected_size_bytes=artifact.size_bytes,
                    )
                    ingest_root = processed_root
                    _assert_evidence_path_identity(
                        conn,
                        file_path=ingest_path,
                        project_root=PROJECT_ROOT,
                        current_sha=snapshot.sha256,
                    )
                result = ingest_evidence_file(
                    conn,
                    file_path=ingest_path,
                    allowed_root=ingest_root,
                    project_root=PROJECT_ROOT,
                    tracked_tickers=tracked,
                    authorized_artifact=artifact,
                    commit=False,
                )
                if result is not None and not result.skipped_existing and not args.no_promote:
                    new_path = _promote_raw_to_processed(
                        result, parsed, conn, PROJECT_ROOT, commit=False
                    )
                    if new_path != result.file_path:
                        result = dataclasses.replace(result, file_path=new_path)
            except Exception as e:
                conn.execute(f"ROLLBACK TO {savepoint}")  # nosec B608
                conn.execute(f"RELEASE {savepoint}")  # nosec B608
                record_stage(
                    conn,
                    run_id,
                    parsed.ticker,
                    StageName.INGEST,
                    StageStatus.FAILED,
                    error_msg=f"{path.name}: {type(e).__name__}",
                )
                failed += 1
                sys.stderr.write(
                    json.dumps(
                        {
                            "event": "transcript_ingest_failed",
                            "ticker": parsed.ticker,
                            "error_class": type(e).__name__,
                        }
                    )
                    + "\n"
                )
                continue

            conn.execute(f"RELEASE {savepoint}")  # nosec B608

            if result is None:
                continue
            if result.skipped_existing:
                skipped_existing += 1
                record_stage(
                    conn,
                    run_id,
                    parsed.ticker,
                    StageName.INGEST,
                    StageStatus.SKIPPED,
                    period_end=result.period_end,
                )
            else:
                record_stage(
                    conn,
                    run_id,
                    parsed.ticker,
                    StageName.INGEST,
                    StageStatus.OK,
                    period_end=result.period_end,
                )
                ingested.append(
                    {
                        "ticker": result.ticker,
                        "period_end": result.period_end.date().isoformat(),
                        "document_id": result.document_id,
                        "transcript_id": result.transcript_id,
                        "segments": result.segment_count,
                        "file": str(result.file_path.relative_to(PROJECT_ROOT)),
                        "qa_status": result.qa_status.value,
                        "qa_signals": list(result.qa_signals),
                    }
                )

        ir_ingested: list[dict[str, object]] = []
        ir_skipped = 0
        ir_failed = 0
        if args.include_ir_transcripts:
            ir_ingested, ir_skipped, ir_failed = _backfill_existing_ir_transcripts(
                conn, run_id, args.ticker, ir_artifacts
            )

        total_failed = failed + ir_failed
        terminal = StageStatus.OK if total_failed == 0 else StageStatus.FAILED
        end_run(
            conn,
            run_id,
            terminal,
            error_summary=(f"{total_failed} files failed" if total_failed else None),
        )

        missing_qa = [
            f"{d['ticker']} {d['period_end']}"
            for d in (*ingested, *ir_ingested)
            if d.get("qa_status") == "absent"
        ]
        for label in missing_qa:
            sys.stderr.write(
                f"WARN missing_qa: {label} — prepared remarks only; "
                f"re-fetch a full-call source to enable Say-Do/commitments mining.\n"
            )

        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "candidates_total": len(candidates),
                    "in_scope": len(in_scope),
                    "ingested": len(ingested),
                    "skipped_existing": skipped_existing,
                    "failed": failed,
                    "missing_qa": missing_qa,
                    "out_of_scope_tickers": plan["out_of_scope"],
                    "details": ingested,
                    "ir_transcript_backfill": {
                        "ingested": len(ir_ingested),
                        "skipped_existing": ir_skipped,
                        "failed": ir_failed,
                        "details": ir_ingested,
                    },
                },
                indent=2,
            )
        )
        return 0 if total_failed == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
