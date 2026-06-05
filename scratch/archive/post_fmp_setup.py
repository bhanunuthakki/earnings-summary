"""Post-FMP setup: index FMP files into documents + set fiscal_year_end.

Runs once after save_fmp_data.py completes the active-universe pull. Calls the
same two pipeline helpers that onboard_ticker.py invokes per ticker, but in
one process across all 88 tracked tickers — avoiding 88 subprocess startups.

This step is the critical bridge between raw FMP JSON on disk and the parse
stages that populate financial_facts/segment_facts/kpi_facts:

  - index_fmp_files_for_ticker: walks data/historical/fmp/<TICKER>_*.json and
    creates `documents` rows (one per logical doc, e.g. fmp_income_statement,
    fmp_10k_json FY2024). The parse stages in quarterly_refresh dispatch on
    these document rows.
  - set_fiscal_year_end_from_fmp: reads <TICKER>_income_statement_annual.json,
    extracts the most-recent period_end MM-DD, writes to
    tracked_companies.fiscal_year_end. backfill_transcripts uses this to map
    fiscal-quarter index -> calendar quarter end.

Both helpers are idempotent.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pipeline.fmp_doc_index import (  # noqa: E402
    index_fmp_files_for_ticker,
    set_fiscal_year_end_from_fmp,
)
from pipeline.queries import open_db  # noqa: E402

_DB_PATH = PROJECT_ROOT / "data" / "portfolio.db"


def main() -> int:
    started = time.time()
    conn = open_db(_DB_PATH)
    try:
        cur = conn.execute(
            "SELECT ticker FROM tracked_companies "
            "WHERE archived_at IS NULL ORDER BY ticker"
        )
        tickers = [r["ticker"] for r in cur.fetchall()]
        print(f"Processing {len(tickers)} tickers...")
        total_indexed = 0
        fye_set = 0
        for i, ticker in enumerate(tickers, 1):
            try:
                n = index_fmp_files_for_ticker(conn, ticker, PROJECT_ROOT)
                fye = set_fiscal_year_end_from_fmp(conn, ticker, PROJECT_ROOT)
                conn.commit()
                total_indexed += n
                if fye:
                    fye_set += 1
                marker = f"fye={fye}" if fye else "fye=None"
                print(f"  [{i:>3}/{len(tickers)}] {ticker:8s} indexed={n:<3} {marker}")
            except Exception as e:
                print(f"  [{i:>3}/{len(tickers)}] {ticker:8s} ERROR: {e}", file=sys.stderr)
    finally:
        conn.close()
    elapsed = time.time() - started
    print(f"\nDone in {elapsed:.1f}s. Indexed {total_indexed} docs. fiscal_year_end set on {fye_set}/{len(tickers)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
