"""Extract the §2 "Platform overview" ASCII diagram from 10-K + profile + recent transcripts.

Usage:
    python execution/extract_platform_diagram.py --ticker NU
    python execution/extract_platform_diagram.py --ticker NU --year 2022
    python execution/extract_platform_diagram.py --all
    python execution/extract_platform_diagram.py --all --refresh

Caches to `data/platform_diagram/{TICKER}.json` keyed by a sha256 over the
combined inputs (10-K bytes + profile description + transcript text).
Re-runs are no-ops unless any source changed or --refresh is passed.

The brief renderer (`src/report/sections/company_description.py`) reads the
cache and renders a "### Platform overview" block right under the elevator
pitch in §2. If the cache is missing, the section degrades gracefully (the
visual is simply omitted) — no MISSING_DATA banner.
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
from compute.platform_diagram import extract_for_ticker  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


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

    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
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
                    "diagram_present": bool(result.diagram),
                    "caption_present": bool(result.caption),
                    "transcripts_used": len(result.transcript_paths),
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
    g.add_argument(
        "--all",
        action="store_true",
        help="All tickers that produce briefs (portfolio + evaluation).",
    )
    p.add_argument("--year", type=int, default=None, help="Specific fiscal year (default: latest)")
    p.add_argument("--refresh", action="store_true", help="Ignore cache and re-extract")
    p.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    return p.parse_args()


def _resolve_tickers(repo_root: Path, args: argparse.Namespace) -> list[str]:
    if args.ticker:
        return [args.ticker.upper()]
    db_path = repo_root / "data" / "portfolio.db"
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    cur = conn.cursor()
    # `--all` is scoped to BRIEFED_LIST_TYPES (portfolio + evaluation) — these
    # are the names that auto-produce briefs and therefore need a §2 platform
    # diagram. Watchlist names are a holding pen; backfill explicitly via --ticker.
    cur.execute(
        f"SELECT DISTINCT ticker FROM tracked_companies "
        f"WHERE list_type IN {db.BRIEFED_LIST_TYPES_SQL} ORDER BY ticker"
    )
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows]


if __name__ == "__main__":
    raise SystemExit(main())
