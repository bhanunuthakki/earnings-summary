"""Stage 2 of the KPI cascade: extract tier_1 KPIs from `.tmp/*_summary.txt`.

Reads the holdings JSON's tier_1_kpis for each ticker, asks Claude Haiku to
pull values from the existing per-quarter LLM summary files (already generated
by `python src/main.py --company <T>`), and persists into kpi_facts via the
existing manifest pipeline. Idempotent on (ticker, period, KPI name).

Usage:
    python execution/extract_kpis_from_summaries.py --ticker NU
    python execution/extract_kpis_from_summaries.py --all
    python execution/extract_kpis_from_summaries.py --all --refresh

Per-run telemetry lands in `data/kpi_extraction_log.json` keyed by ticker.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.kpi_extract_summaries import (  # noqa: E402
    extract_for_ticker,
    write_log,
)


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
    results = []
    summary_lines: list[dict[str, object]] = []
    try:
        for ticker in tickers:
            log = extract_for_ticker(ticker, repo_root, conn, refresh=args.refresh)
            results.append(log)
            summary_lines.append(
                {
                    "ticker": log.ticker,
                    "extracted_quarters": len(log.quarters_extracted),
                    "skipped_quarters": len(log.quarters_skipped_no_missing),
                    "kpis_inserted": log.kpis_inserted_total,
                    "elapsed_ms": log.elapsed_ms,
                    "error": log.error,
                }
            )
            print(
                f"[{log.ticker}] extracted={len(log.quarters_extracted)} "
                f"skipped={len(log.quarters_skipped_no_missing)} "
                f"kpis_inserted={log.kpis_inserted_total} "
                f"elapsed={log.elapsed_ms}ms"
                + (f" error={log.error}" if log.error else ""),
                file=sys.stderr,
            )
    finally:
        conn.close()

    log_path = write_log(repo_root, results)
    print(json.dumps({"summary": summary_lines, "log": str(log_path)}, indent=2))
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", help="Single ticker")
    g.add_argument("--all", action="store_true", help="All portfolio + watchlist tickers")
    p.add_argument("--refresh", action="store_true", help="Re-extract even if all KPIs already present")
    p.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    return p.parse_args()


def _resolve_tickers(repo_root: Path, args: argparse.Namespace) -> list[str]:
    if args.ticker:
        return [args.ticker.upper()]
    db_path = repo_root / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT ticker FROM tracked_companies WHERE list_type IN ('portfolio','watchlist') ORDER BY ticker"
    )
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows]


if __name__ == "__main__":
    raise SystemExit(main())
