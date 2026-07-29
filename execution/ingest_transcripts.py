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
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
import index_manager  # noqa: E402
from compute.transcript_ingest import (  # noqa: E402
    IngestResult,
    ParsedFilename,
    ingest_existing_ir_transcript,
    ingest_one,
    parse_transcript_filename,
    sha256_of,
)
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

_TRANSCRIPT_DIRS = (
    PROJECT_ROOT / "transcripts" / "processed",
    PROJECT_ROOT / "transcripts" / "raw",
)


def _promote_raw_to_processed(
    result: IngestResult,
    parsed: ParsedFilename,
    conn: sqlite3.Connection,
    project_root: Path,
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
        src_sha = sha256_of(src)
        target_sha = sha256_of(target)
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


def _backfill_existing_ir_transcripts(
    conn: sqlite3.Connection, run_id: str, restrict_ticker: str | None
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
        rel_path = row["file_path"]
        abs_path = (PROJECT_ROOT / rel_path).resolve()
        if not abs_path.exists():
            record_stage(
                conn,
                run_id,
                ticker,
                StageName.INGEST,
                StageStatus.FAILED,
                error_msg=f"missing_file: {rel_path}",
            )
            failed += 1
            sys.stderr.write(f"FAILED missing file for doc#{doc_id}: {rel_path}\n")
            continue
        try:
            result: IngestResult = ingest_existing_ir_transcript(
                conn, document_id=doc_id, file_path=abs_path
            )
        except (ValueError, OSError) as e:
            record_stage(
                conn,
                run_id,
                ticker,
                StageName.INGEST,
                StageStatus.FAILED,
                error_msg=f"ir_transcript doc#{doc_id}: {type(e).__name__}: {e}"[:500],
            )
            failed += 1
            sys.stderr.write(f"FAILED doc#{doc_id} ({rel_path}): {type(e).__name__}: {e}\n")
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
) -> dict[str, JsonValue]:
    source_paths = [path for path, _ in in_scope]
    source_paths.extend(path for _, _, _, path in ir_sources)
    return {
        "include_ir_transcripts": include_ir_transcripts,
        "no_promote": no_promote,
        "candidate_files": files_fingerprint(source_paths, root=PROJECT_ROOT),
        "ir_documents": [
            {"document_id": doc_id, "ticker": ticker, "period_end": period_end}
            for doc_id, ticker, period_end, _ in ir_sources
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", help="Restrict to a single ticker (case-insensitive)")
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
        candidates = _candidate_files(args.ticker)

        in_scope = [(p, parsed) for p, parsed in candidates if parsed.ticker in tracked]
        out_of_scope = [(p, parsed) for p, parsed in candidates if parsed.ticker not in tracked]

        plan = {
            "candidates_total": len(candidates),
            "in_scope": [
                {"file": str(p.relative_to(PROJECT_ROOT)), "ticker": parsed.ticker}
                for p, parsed in in_scope
            ],
            "out_of_scope": sorted({parsed.ticker for _, parsed in out_of_scope}),
        }

        if args.dry_run:
            print(json.dumps(plan, indent=2))
            return 0

        ir_sources = (
            _ir_transcript_sources(conn, args.ticker) if args.include_ir_transcripts else []
        )
        if not in_scope and not ir_sources:
            print(json.dumps({**plan, "ingested": 0, "skipped_existing": 0}, indent=2))
            return 0

        ticker_scope = sorted(
            {parsed.ticker for _, parsed in in_scope} | {ticker for _, ticker, _, _ in ir_sources}
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
                ),
                force=bool(args.force),
                deduplicate_completed=True,
            )
        except PipelineRunSuppressedError as exc:
            print(json.dumps(suppression_payload(exc)))
            return 0

        ingested: list[dict[str, object]] = []
        skipped_existing = 0
        failed = 0

        for path, parsed in in_scope:
            try:
                result = ingest_one(
                    conn,
                    file_path=path,
                    project_root=PROJECT_ROOT,
                    tracked_tickers=tracked,
                )
            except Exception as e:
                record_stage(
                    conn,
                    run_id,
                    parsed.ticker,
                    StageName.INGEST,
                    StageStatus.FAILED,
                    error_msg=f"{path.name}: {type(e).__name__}: {e}"[:500],
                )
                failed += 1
                sys.stderr.write(f"FAILED {path.name}: {type(e).__name__}: {e}\n")
                continue

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
                if not args.no_promote:
                    new_path = _promote_raw_to_processed(result, parsed, conn, PROJECT_ROOT)
                    if new_path != result.file_path:
                        result = dataclasses.replace(result, file_path=new_path)
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
                conn, run_id, args.ticker
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
