"""Generate LLM business-model peer comparables for the §4 peer-comp panel.

Usage:
    python execution/extract_peer_selection.py --ticker NU
    python execution/extract_peer_selection.py --ticker NU --refresh
    python execution/extract_peer_selection.py --all
    python execution/extract_peer_selection.py --all --refresh
    python execution/extract_peer_selection.py --ticker NU --no-fetch   # suggestions only

Caches to `data/peer_selection/{TICKER}.json` keyed by an input sha256 (name +
business description + segments + sector/industry). Re-runs are no-ops unless
the inputs changed or --refresh is passed. Also fires a best-effort, free-tier
FMP fetch of each suggested peer's fundamentals so the panel's multiples resolve.

The renderer (`src/report/sections/p3_data.py::load_peer_comp`) reads the cache
and merges the suggestions into the scored screen, riding the S5 curation
plumbing. Absent cache → the FMP sector/cap screen is used unchanged.
See directives/peer_selection_llm.md. Normally runs automatically on the
`--enable-llm` build (execution/build_artifacts.py::_ensure_peer_selection).
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
from compute.peer_selection import extract_for_ticker  # noqa: E402
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
                    ticker,
                    repo_root,
                    conn,
                    refresh=args.refresh,
                    fetch_fundamentals=not args.no_fetch,
                )
            except Exception as e:  # report per-ticker, keep going
                summary.append({"ticker": ticker, "error": f"{type(e).__name__}: {e}"})
                continue
            summary.append(
                {
                    "ticker": ticker,
                    "suggested": len(result.suggestions),
                    "peers": [s["ticker"] for s in result.suggestions],
                    "fetched": result.fetched_peers,
                    "fetched_complete": result.fetched_complete,
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
    p.add_argument("--refresh", action="store_true", help="Ignore cache and re-generate")
    p.add_argument(
        "--no-fetch",
        action="store_true",
        help="Skip the FMP fundamentals fetch (suggestions only; metric-less peers drop).",
    )
    p.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    return p.parse_args()


def _resolve_tickers(repo_root: Path, args: argparse.Namespace) -> list[str]:
    if args.ticker:
        return [args.ticker.upper()]
    db_path = repo_root / "data" / "portfolio.db"
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    cur = conn.cursor()
    # `--all` is scoped to briefed list types (portfolio + evaluation) — the
    # names that auto-produce briefs and therefore carry a peer-comp panel.
    cur.execute(
        f"SELECT DISTINCT ticker FROM tracked_companies "
        f"WHERE list_type IN {db.BRIEFED_LIST_TYPES_SQL} ORDER BY ticker"
    )
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows]


if __name__ == "__main__":
    raise SystemExit(main())
