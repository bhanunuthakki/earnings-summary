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
from collections.abc import Callable
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.as_reported import extract_as_reported_facts  # noqa: E402
from compute.balance_sheet import extract_balance_sheet_facts  # noqa: E402
from compute.cashflow import extract_cashflow_facts  # noqa: E402
from compute.income_statement import extract_income_statement_facts  # noqa: E402
from compute.segment_oi_10k import extract_segment_oi_facts  # noqa: E402
from compute.segments import extract_segment_facts  # noqa: E402
from models.runs import StageName, StageStatus  # noqa: E402
from pipeline.queries import open_db, tracked_companies_for_user  # noqa: E402
from pipeline.run_accounting import end_run, record_stage, start_run  # noqa: E402

_Extractor = Callable[[sqlite3.Connection, int, Path], int]

_DISPATCH: dict[str, _Extractor] = {
    "fmp_income_statement": extract_income_statement_facts,
    "fmp_balance_sheet": extract_balance_sheet_facts,
    "fmp_cashflow": extract_cashflow_facts,
    "fmp_segment_product": extract_segment_facts,
    "fmp_segment_geographic": extract_segment_facts,
    "fmp_as_reported_income": extract_as_reported_facts,
    "fmp_as_reported_balance": extract_as_reported_facts,
    "fmp_as_reported_cashflow": extract_as_reported_facts,
    "fmp_as_reported_financial": extract_as_reported_facts,
    "fmp_10k_json": extract_segment_oi_facts,
    "fmp_10q_json": extract_segment_oi_facts,
}


def _resolve_tickers(conn: sqlite3.Connection, args: argparse.Namespace) -> list[str]:
    """Return the list of tickers to extract for, per CLI args."""
    if args.ticker:
        return [args.ticker.upper()]
    companies = tracked_companies_for_user(conn, only_classified=True)
    return [c.ticker for c in companies]


def _resolve_doc_types(args: argparse.Namespace) -> list[str]:
    if args.doc_type:
        return [args.doc_type]
    return list(_DISPATCH.keys())


def _documents_for_extraction(
    conn: sqlite3.Connection, tickers: list[str], doc_types: list[str]
) -> list[tuple[int, str, str]]:
    """Return [(document_id, ticker, doc_type), ...] for the requested scope."""
    placeholders_t = ",".join("?" for _ in tickers)
    placeholders_d = ",".join("?" for _ in doc_types)
    cur = conn.execute(
        f"SELECT id, ticker, doc_type FROM documents "
        f"WHERE ticker IN ({placeholders_t}) AND doc_type IN ({placeholders_d}) "
        f"ORDER BY ticker, doc_type, id",
        (*tickers, *doc_types),
    )
    return [(r["id"], r["ticker"], r["doc_type"]) for r in cur.fetchall()]


def _run_extraction(
    conn: sqlite3.Connection,
    run_id: str,
    docs: list[tuple[int, str, str]],
    project_root: Path,
) -> tuple[int, int, int]:
    """Execute extractors over docs; return (total_inserted, ok_count, failed_count)."""
    total_inserted = 0
    ok_count = 0
    failed_count = 0
    for doc_id, ticker, doc_type in docs:
        extractor = _DISPATCH[doc_type]
        try:
            n = extractor(conn, doc_id, project_root)
        except (ValueError, OSError, KeyError, json.JSONDecodeError) as e:
            record_stage(
                conn,
                run_id,
                ticker,
                StageName.COMPUTE,
                StageStatus.FAILED,
                error_msg=f"{doc_type}#{doc_id}: {type(e).__name__}: {e}"[:500],
            )
            failed_count += 1
            sys.stderr.write(f"FAILED {ticker} {doc_type} doc#{doc_id}: {type(e).__name__}: {e}\n")
            continue
        record_stage(conn, run_id, ticker, StageName.COMPUTE, StageStatus.OK)
        total_inserted += n
        ok_count += 1
    return (total_inserted, ok_count, failed_count)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", help="Single ticker to extract for")
    group.add_argument("--all", action="store_true", help="All classified tracked companies")
    parser.add_argument(
        "--doc-type",
        choices=sorted(_DISPATCH.keys()),
        help="Restrict to a single doc_type (default: all extractor-supported types)",
    )
    parser.add_argument(
        "--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"), help="Path to portfolio.db"
    )
    args = parser.parse_args()

    conn = open_db(args.db)
    try:
        tickers = _resolve_tickers(conn, args)
        doc_types = _resolve_doc_types(args)
        if not tickers:
            sys.stderr.write("No tickers resolved; nothing to do\n")
            return 0

        run_id = start_run(conn, directive="extract_facts", ticker_scope=tickers)
        docs = _documents_for_extraction(conn, tickers, doc_types)
        total, ok, failed = _run_extraction(conn, run_id, docs, PROJECT_ROOT)

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
