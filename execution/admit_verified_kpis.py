"""Dry-run or apply append-only batch semantic admission for verified retained KPI facts."""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime
from pathlib import Path

from db_paths import require_db_path
from pipeline.kpi_batch_admission import run_batch_kpi_admission
from pipeline.kpi_semantic_scope import portfolio_tickers
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

OPERATIONS_GOVERNANCE_DISPOSITION = "no_surface_change_internal_append_only_kpi_batch_admission"
OPERATIONS_GOVERNANCE_PRESERVED_CONTRACT = (
    "src/operations/registry.py:OperationsRegistry",
    "src/pipeline/operations_panel.py:visible_surface_dispositions",
    "src/operations/review_bundle.py:ReviewKpiCensus",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Explicit portfolio DB path (defaults to configured authority; checkout-default forbidden).",
    )
    parser.add_argument(
        "--tickers",
        default="",
        help="Comma-separated list of tickers to scan (default: all active KPI tickers unless --portfolio).",
    )
    parser.add_argument(
        "--portfolio",
        action="store_true",
        help="Restrict scope to active portfolio companies (excluding ETFs).",
    )
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=datetime.now(UTC).date(),
        help="Cutoff ISO date; facts with period_end > as_of are skipped.",
    )
    parser.add_argument(
        "--include-llm-with-excerpt",
        action="store_true",
        help="Also qualify LLM-extracted rows that carry a non-empty source_excerpt.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write append-only admitted semantic context revisions. Omit for read-only dry-run.",
    )
    parser.add_argument(
        "--reviewed-by",
        default="batch_admission:deterministic_v1",
        help="Reviewer attribution stored in reviewed_by when --apply is set.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    db_path = require_db_path(args.db_path)
    role = SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY
    conn = connect_sqlite(db_path, role=role)
    try:
        if not args.apply:
            conn.execute("PRAGMA query_only=1")
        selected_tickers: list[str] | None = None
        if args.tickers.strip():
            selected_tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        elif args.portfolio:
            selected_tickers = list(portfolio_tickers(conn))

        report = run_batch_kpi_admission(
            conn,
            tickers=selected_tickers,
            as_of=args.as_of,
            apply=args.apply,
            include_llm_with_excerpt=args.include_llm_with_excerpt,
            reviewed_by=args.reviewed_by,
        )
    finally:
        conn.close()

    print(report.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
