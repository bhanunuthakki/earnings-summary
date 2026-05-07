"""Batch baseline DCF for all tracked tickers (or a subset).

Pulls each ticker's trailing-4Q growth + margin from kpi_facts, applies a
linearly-decaying growth profile to terminal_growth over 10 years, persists
to dcf_runs. Skips tickers whose inputs are missing (FLKR, etc.).

Usage:
    python execution/batch_dcf.py                       # all tracked tickers
    python execution/batch_dcf.py --ticker MELI         # single ticker
    python execution/batch_dcf.py --wacc 0.10 --terminal-growth 0.03
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.batch_dcf import run_batch_dcf_for_ticker  # noqa: E402
from models.runs import StageStatus  # noqa: E402
from pipeline.queries import open_db, tracked_companies_for_user  # noqa: E402
from pipeline.run_accounting import end_run, start_run  # noqa: E402


def _resolve_tickers(conn, args: argparse.Namespace) -> list[str]:
    if args.ticker:
        return [args.ticker.upper()]
    companies = tracked_companies_for_user(conn, only_classified=True)
    return [c.ticker for c in companies]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", help="Restrict to one ticker")
    parser.add_argument("--wacc", type=float, default=0.09, help="Default 0.09")
    parser.add_argument(
        "--terminal-growth", type=float, default=0.025, help="Default 0.025",
    )
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"))
    args = parser.parse_args()

    conn = open_db(args.db)
    try:
        tickers = _resolve_tickers(conn, args)
        run_id = start_run(conn, directive="batch_dcf", ticker_scope=tickers)

        rows: list[dict[str, object]] = []
        successes = 0
        for ticker in tickers:
            try:
                result = run_batch_dcf_for_ticker(
                    conn, ticker=ticker, wacc=args.wacc,
                    terminal_growth=args.terminal_growth, run_id=run_id,
                )
            except (ValueError, KeyError) as e:
                rows.append({
                    "ticker": ticker, "skipped_reason": f"{type(e).__name__}: {e}",
                    "dcf_run_id": None,
                })
                continue
            if result.dcf_run_id is None:
                rows.append({
                    "ticker": result.ticker, "skipped_reason": result.skipped_reason,
                    "dcf_run_id": None,
                })
            else:
                successes += 1
                rows.append({
                    "ticker": result.ticker, "dcf_run_id": result.dcf_run_id,
                    "base_revenue_billions": result.base_revenue_billions,
                    "npv_billions": result.npv_billions,
                    "npv_per_share": result.npv_per_share,
                })

        end_run(conn, run_id, StageStatus.OK, error_summary=None)

        print(json.dumps({
            "run_id": run_id, "ticker_count": len(tickers),
            "successes": successes, "skipped": len(tickers) - successes,
            "rows": rows,
        }, indent=2))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
