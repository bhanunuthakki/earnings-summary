"""Derive standard KPIs from FMP financial_facts and persist into kpi_facts.

Operating Margin (GAAP), Net Income Margin (GAAP), Gross Margin (GAAP),
and Revenue YoY Growth (USD) are computed for every quarterly observation
where the underlying line items exist. Idempotent: re-running over the same
data inserts no duplicates (UNIQUE index on kpi_facts).

Usage:
    python execution/derive_kpis_from_fmp.py --ticker MELI
    python execution/derive_kpis_from_fmp.py --all
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.fmp_derived_kpis import derive_for_ticker  # noqa: E402
from models.runs import StageName, StageStatus  # noqa: E402
from pipeline.queries import open_db, tracked_companies_for_user  # noqa: E402
from pipeline.run_accounting import end_run, record_stage, start_run  # noqa: E402


def _resolve_tickers(conn, args: argparse.Namespace) -> list[str]:
    """Return list of tickers. --ticker overrides --all."""
    if args.ticker:
        return [args.ticker.upper()]
    companies = tracked_companies_for_user(conn, only_classified=True)
    return [c.ticker for c in companies]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", help="Single ticker to derive for")
    group.add_argument(
        "--all", action="store_true", help="All classified tracked companies"
    )
    parser.add_argument(
        "--db", default=str(PROJECT_ROOT / "data" / "portfolio.db")
    )
    args = parser.parse_args()

    conn = open_db(args.db)
    try:
        tickers = _resolve_tickers(conn, args)
        if not tickers:
            print(json.dumps({"warning": "no tickers"}, indent=2))
            return 0

        run_id = start_run(conn, directive="derive_kpis_from_fmp", ticker_scope=tickers)
        per_ticker: list[dict[str, object]] = []
        total_inserted = 0
        failed = 0

        for ticker in tickers:
            try:
                emitted, inserted = derive_for_ticker(conn, ticker)
            except (ValueError, KeyError) as e:
                failed += 1
                record_stage(
                    conn, run_id, ticker, StageName.COMPUTE, StageStatus.FAILED,
                    error_msg=f"{type(e).__name__}: {e}"[:500],
                )
                sys.stderr.write(f"FAILED {ticker}: {type(e).__name__}: {e}\n")
                continue

            status = StageStatus.OK if emitted > 0 else StageStatus.SKIPPED
            record_stage(conn, run_id, ticker, StageName.COMPUTE, status)
            total_inserted += inserted
            per_ticker.append({
                "ticker": ticker,
                "rows_emitted": emitted,
                "rows_inserted": inserted,
            })

        terminal = StageStatus.OK if failed == 0 else StageStatus.FAILED
        end_run(conn, run_id, terminal, error_summary=f"{failed} failed" if failed else None)

        print(json.dumps({
            "run_id": run_id,
            "tickers_processed": len(per_ticker),
            "total_kpi_rows_inserted": total_inserted,
            "failed": failed,
            "per_ticker": per_ticker,
        }, indent=2))
        return 0 if failed == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
