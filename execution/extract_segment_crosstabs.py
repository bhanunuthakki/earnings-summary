"""Extract 2-axis revenue cross-tabs from FMP form_10k JSONs into the
segment_periods + segment_dimensions junction tables.

Usage:
    python execution/extract_segment_crosstabs.py --ticker AMZN
    python execution/extract_segment_crosstabs.py --ticker AMZN --year 2024
    python execution/extract_segment_crosstabs.py --all
    python execution/extract_segment_crosstabs.py --all --refresh

Caches to ``data/segment_crosstabs/{TICKER}.json`` with start/end timestamps,
source sha256, the parsed cross-tab list, and (periods/dimensions/cells)
insertion counts. Re-runs are no-ops unless the source sha256 changed or
``--refresh`` is passed.

Audit: each invocation writes an ``ingestion_runs`` row + per-ticker
``stage_transitions`` rows (stage=COMPUTE). On failure the run is closed
with status=FAILED and the exception summary.

Source-of-truth for SEC narrative is ``data/historical/fmp/*_form_10k_*.json``
(see memory: SEC filings source-of-truth scan).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
from compute.segment_crosstabs_llm import extract_for_ticker  # noqa: E402
from models.runs import StageName, StageStatus  # noqa: E402
from pipeline.run_accounting import end_run, record_stage, start_run  # noqa: E402


def main() -> int:
    args = _parse_args()
    repo_root = args.repo_root.resolve()
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        print(f"[error] no DB at {db_path}", file=sys.stderr)
        return 1

    tickers = _resolve_tickers(repo_root, args)
    if not tickers:
        print("[]")
        return 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    run_id = start_run(conn, directive="extract_segment_crosstabs", ticker_scope=tickers)
    summary: list[dict[str, object]] = []
    final_status = StageStatus.OK
    error_summary: str | None = None

    try:
        for ticker in tickers:
            record_stage(conn, run_id, ticker, StageName.COMPUTE, StageStatus.IN_PROGRESS)
            try:
                result = extract_for_ticker(
                    ticker, repo_root, conn, fiscal_year=args.year, refresh=args.refresh
                )
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                record_stage(
                    conn, run_id, ticker, StageName.COMPUTE, StageStatus.FAILED, error_msg=err
                )
                summary.append({"ticker": ticker, "error": err})
                final_status = StageStatus.FAILED
                error_summary = err
                continue
            if result.skipped_reason is not None:
                record_stage(
                    conn,
                    run_id,
                    ticker,
                    StageName.COMPUTE,
                    StageStatus.SKIPPED,
                    error_msg=result.skipped_reason,
                )
            else:
                record_stage(conn, run_id, ticker, StageName.COMPUTE, StageStatus.OK)
            summary.append(
                {
                    "ticker": ticker,
                    "fiscal_year": result.fiscal_year,
                    "cross_tabs": len(result.cross_tabs),
                    "periods_inserted": result.periods_inserted,
                    "dimensions_inserted": result.dimensions_inserted,
                    "cells_skipped": result.cells_skipped,
                    "elapsed_ms": result.elapsed_ms,
                    "skipped": result.skipped_reason,
                }
            )
    finally:
        end_run(conn, run_id, final_status, error_summary=error_summary)
        conn.close()

    print(json.dumps(summary, indent=2))
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", help="Single ticker")
    g.add_argument("--all", action="store_true", help="All tracked portfolio + watchlist tickers")
    p.add_argument("--year", type=int, default=None, help="Specific fiscal year (default: latest)")
    p.add_argument("--refresh", action="store_true", help="Ignore cache and re-extract")
    p.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    return p.parse_args()


def _resolve_tickers(repo_root: Path, args: argparse.Namespace) -> list[str]:
    if args.ticker:
        return [args.ticker.upper()]
    db_path = repo_root / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        f"SELECT DISTINCT ticker FROM tracked_companies "
        f"WHERE list_type IN {db.ACTIVE_LIST_TYPES_SQL} ORDER BY ticker"
    )
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows]


if __name__ == "__main__":
    raise SystemExit(main())
