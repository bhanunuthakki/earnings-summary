"""Extract the §2 Company description from the latest 10-K + profile.json.

Usage:
    python execution/extract_company_description.py --ticker GOOG
    python execution/extract_company_description.py --ticker GOOG --year 2024
    python execution/extract_company_description.py --all
    python execution/extract_company_description.py --all --refresh

Caches to `data/company_description/{TICKER}.json` keyed by source sha256.
Re-runs are no-ops unless the 10-K changed or --refresh is passed.

The brief renderer (`src/report/sections/company_description.py`) reads the
cache. Skipping this step renders §2 in MISSING_DATA mode with a fix command
pointing back to this script.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.company_description import extract_for_ticker  # noqa: E402


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
    summary: list[dict[str, object]] = []
    try:
        for ticker in tickers:
            try:
                result = extract_for_ticker(
                    ticker, repo_root, conn, fiscal_year=args.year, refresh=args.refresh
                )
            except Exception as e:
                summary.append({"ticker": ticker, "error": f"{type(e).__name__}: {e}"})
                continue
            summary.append(
                {
                    "ticker": ticker,
                    "fiscal_year": result.fiscal_year,
                    "elevator_pitch_present": bool(result.elevator_pitch),
                    "segments_described": len(result.segments),
                    "geographies_described": len(result.geographies),
                    "elapsed_ms": result.elapsed_ms,
                    "skipped": result.skipped_reason,
                }
            )
    finally:
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
        "SELECT DISTINCT ticker FROM tracked_companies "
        "WHERE list_type IN ('portfolio','watchlist') ORDER BY ticker"
    )
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows]


if __name__ == "__main__":
    raise SystemExit(main())
