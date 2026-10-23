"""Bulk-extract financial / segment facts from documents (Phase 3 CLI).

Walks the documents table, dispatches each row to the correct extractor by
doc_type, and writes facts via INSERT OR IGNORE (idempotent on UNIQUE index).
Wraps the run in start_run / record_stage / end_run from src/pipeline so every
invocation produces an audit trail in ingestion_runs and stage_transitions.

Usage:
    python execution/extract_facts.py --ticker GOOG
    python execution/extract_facts.py --all
    python execution/extract_facts.py --doc-type fmp_income_statement
    python execution/extract_facts.py --all --doc-type fmp_balance_sheet
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

try:  # direct script invocation
    from _lib import PROJECT_ROOT
except ImportError:  # pragma: no cover - package import fallback
    from execution._lib import PROJECT_ROOT

from models.companies import ListType
from models.runs import StageName, StageStatus
from pipeline.fmp_batch import (
    SUPPORTED_DOC_TYPES,
    DocumentOutcome,
    extract_documents,
)
from pipeline.fmp_batch import (
    select_documents as _documents_for_extraction,
)
from pipeline.invocation_fingerprint import file_fingerprint
from pipeline.queries import (
    ANALYZED_LIST_TYPES,
    open_db,
    tracked_companies_for_user,
)
from pipeline.run_accounting import (
    JsonValue,
    PipelineRunSuppressedError,
    end_run,
    record_stage,
    start_run,
    suppression_payload,
)


def _resolve_tickers(conn: sqlite3.Connection, args: argparse.Namespace) -> list[str]:
    """Return the list of tickers to extract for, per CLI args."""
    if args.ticker:
        return [args.ticker.upper()]
    list_types = frozenset(ListType) if args.include_index_members else ANALYZED_LIST_TYPES
    companies = tracked_companies_for_user(conn, only_classified=True, list_types=list_types)
    return [c.ticker for c in companies]


def _resolve_doc_types(args: argparse.Namespace) -> list[str]:
    if args.doc_type:
        return [args.doc_type]
    return list(SUPPORTED_DOC_TYPES)


def _invocation_inputs(
    conn: sqlite3.Connection,
    docs: list[tuple[int, str, str]],
    doc_types: list[str],
    project_root: Path,
) -> dict[str, JsonValue]:
    """Fingerprint every registered immutable source document in this run."""
    documents: list[JsonValue] = []
    for doc_id, ticker, doc_type in docs:
        row = conn.execute(
            "SELECT file_path, sha256 FROM documents WHERE id = ?",
            (doc_id,),
        ).fetchone()
        raw_path = str(row["file_path"]) if row is not None and row["file_path"] else ""
        source_path = Path(raw_path)
        if not source_path.is_absolute():
            source_path = project_root / source_path
        documents.append(
            {
                "document_id": doc_id,
                "ticker": ticker,
                "doc_type": doc_type,
                "registered_sha256": (
                    str(row["sha256"]) if row is not None and row["sha256"] else None
                ),
                "source": file_fingerprint(source_path, root=project_root),
            }
        )
    return {
        "doc_types": sorted(doc_types),
        "documents": documents,
    }


def _report_document(conn: sqlite3.Connection, run_id: str, outcome: DocumentOutcome) -> None:
    error = outcome.error
    detail = (
        f"{outcome.doc_type}#{outcome.document_id}: {type(error).__name__}: {error}"[:500]
        if error is not None
        else None
    )
    try:
        record_stage(
            conn,
            run_id,
            outcome.ticker,
            StageName.COMPUTE,
            StageStatus.FAILED if error is not None else StageStatus.OK,
            error_msg=detail,
        )
    except sqlite3.Error as bookkeeping_error:
        sys.stderr.write(f"Stage failure bookkeeping failed: {type(bookkeeping_error).__name__}\n")
        raise
    if error is not None:
        sys.stderr.write(f"FAILED {outcome.ticker} {detail}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", help="Single ticker to extract for")
    group.add_argument("--all", action="store_true", help="All classified tracked companies")
    parser.add_argument(
        "--doc-type",
        choices=sorted(SUPPORTED_DOC_TYPES),
        help="Restrict to a single doc_type (default: all extractor-supported types)",
    )
    parser.add_argument(
        "--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"), help="Path to portfolio.db"
    )
    parser.add_argument(
        "--include-index-members",
        action="store_true",
        help="With --all: also extract for index_member/etf/none tickers (default: "
        "active universe — portfolio+watchlist+evaluation). Required to fan "
        "extraction over the full universe.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-extract even when the same immutable document bytes already completed.",
    )
    args = parser.parse_args()

    conn = open_db(args.db)
    try:
        tickers = _resolve_tickers(conn, args)
        doc_types = _resolve_doc_types(args)
        if not tickers:
            sys.stderr.write("No tickers resolved; nothing to do\n")
            return 0

        docs = _documents_for_extraction(conn, tickers, doc_types)
        try:
            run_id = start_run(
                conn,
                directive="extract_facts",
                ticker_scope=tickers,
                invocation_inputs=_invocation_inputs(conn, docs, doc_types, PROJECT_ROOT),
                force=bool(args.force),
                deduplicate_completed=True,
            )
        except PipelineRunSuppressedError as exc:
            print(json.dumps(suppression_payload(exc)))
            return 0
        try:
            result = extract_documents(
                conn,
                docs,
                PROJECT_ROOT,
                report=lambda outcome: _report_document(conn, run_id, outcome),
            )
            total, ok, failed = result.rows_inserted, result.succeeded, result.failed
        except sqlite3.Error as exc:
            try:
                conn.rollback()
                end_run(
                    conn,
                    run_id,
                    StageStatus.FAILED,
                    error_summary=f"{type(exc).__name__}: {exc}"[:500],
                )
            except sqlite3.Error as bookkeeping_error:
                sys.stderr.write(
                    f"Run failure bookkeeping failed: {type(bookkeeping_error).__name__}\n"
                )
            raise

        terminal = StageStatus.OK if failed == 0 else StageStatus.FAILED
        end_run(
            conn,
            run_id,
            terminal,
            error_summary=f"{failed} docs failed" if failed else None,
        )

        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "tickers": len(tickers),
                    "documents": len(docs),
                    "facts_inserted": total,
                    "documents_ok": ok,
                    "documents_failed": failed,
                },
                indent=2,
            )
        )
        return 0 if failed == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
