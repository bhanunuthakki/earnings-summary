"""Run document-table extractors for one or more tickers.

This is the Phase 2 MVP CLI for the document-table extraction effort
(see directives/document_tables_design.md). Dispatches to the extractors
registered in src/document_table_extractor.py.

Usage:
    # Single ticker, all registered extractors, latest FY
    python execution/extract_document_tables.py --ticker GOOG

    # Single ticker, specific extractor + fiscal year
    python execution/extract_document_tables.py --ticker GOOG --table-kind lease_commitments --fiscal-year 2024

    # All portfolio holdings, latest FY each
    python execution/extract_document_tables.py --all-portfolio

    # List the registered extractors
    python execution/extract_document_tables.py --list-kinds
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _sync_db_path(repo_root: Path) -> None:
    """Mirror execution/run_lens.py:_sync_db_path so the artifact / ledger
    writers land in the caller's repo."""
    import db  # noqa: PLC0415 — lazy import after sys.path setup

    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.DB_PATH = str(repo_root / "data" / "portfolio.db")
    db.FMP_DIR = str(repo_root / "data" / "historical" / "fmp")


from document_table_extractor import (  # noqa: E402
    extract_for_ticker,
    registered_table_kinds,
)

log = logging.getLogger("extract_document_tables")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    g = parser.add_mutually_exclusive_group(required=False)
    g.add_argument("--ticker", help="Single ticker to extract.")
    g.add_argument("--all-portfolio", action="store_true", help="All portfolio holdings.")
    parser.add_argument(
        "--table-kind",
        action="append",
        choices=registered_table_kinds(),
        help="Restrict to specific extractor(s); repeatable. Omit = all registered.",
    )
    parser.add_argument("--fiscal-year", type=int, default=None)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--list-kinds", action="store_true", help="Print registered table_kinds and exit."
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.list_kinds:
        for tk in registered_table_kinds():
            print(tk)
        return 0

    if not args.ticker and not args.all_portfolio:
        parser.error("either --ticker or --all-portfolio is required")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    repo_root = args.repo_root.resolve()
    _sync_db_path(repo_root)

    if args.ticker:
        tickers = [args.ticker.upper()]
    else:
        tickers = _portfolio_tickers(repo_root)

    grand_total_inserted = 0
    for t in tickers:
        outcomes = extract_for_ticker(
            ticker=t,
            fiscal_year=args.fiscal_year,
            table_kinds=args.table_kind,
            repo_root=repo_root,
        )
        for outcome in outcomes:
            log.info(
                {
                    "event": "extract_outcome",
                    "ticker": outcome.ticker,
                    "fiscal_year": outcome.fiscal_year,
                    "table_kind": outcome.table_kind,
                    "status": outcome.status,
                    "n_rows_proposed": outcome.n_rows_proposed,
                    "n_rows_inserted": outcome.n_rows_inserted,
                    "n_mapping_proposals": outcome.n_mapping_proposals,
                    "notes": outcome.notes[:200] if outcome.notes else "",
                }
            )
            grand_total_inserted += outcome.n_rows_inserted

    print(
        f"\nDocument table extraction done · {grand_total_inserted} rows inserted across "
        f"{len(tickers)} ticker(s)."
    )
    return 0


def _portfolio_tickers(repo_root: Path) -> list[str]:
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            """
            SELECT ticker FROM tracked_companies
            WHERE archived_at IS NULL AND list_type = 'portfolio'
            ORDER BY ticker
            """
        ).fetchall()
        return [str(r[0]).upper() for r in rows]
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
