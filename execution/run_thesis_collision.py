"""Refresh the whole-book thesis-collision audit (src/thesis_collision.py).

Reads every portfolio holding's thesis + tier-1 break-rule drivers from
``micro_thesis/holdings/<T>.json`` and makes ONE governed LLM call flagging
shared-driver concentration ("these names are really one bet") and
contradictory theses. Cached in ``llm_artifacts`` (scope ``portfolio``,
purpose ``thesis_collision``) keyed by a thesis-set hash — a re-run is a
no-op (no LLM spend) unless a thesis or tier-1 break condition actually
changed since the last run.

Low-frequency by design (the book's thesis set changes rarely, and this is
an LLM call with real cost) — run on demand or from a slow cron, NOT the
daily morning pipeline. The Risk tab's "Thesis collisions" section only ever
reads the cached result back; it never triggers a call itself.

Usage:
    python execution/run_thesis_collision.py
    python execution/run_thesis_collision.py --repo-root . --db-path /tmp/x.db
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402
from thesis_collision import refresh_thesis_collision  # noqa: E402

log = logging.getLogger("run_thesis_collision")


def _portfolio_tickers(db_path: Path) -> list[str]:
    if not db_path.exists():
        return []
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    try:
        rows = conn.execute(
            "SELECT ticker FROM tracked_companies "
            "WHERE list_type = 'portfolio' AND archived_at IS NULL ORDER BY ticker"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [str(r[0]).upper() for r in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/portfolio.db and micro_thesis/holdings/.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Override the portfolio DB path (wins over --repo-root derivation).",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    repo_root: Path = args.repo_root.resolve()
    db_path: Path = (
        args.db_path if args.db_path is not None else repo_root / "data" / "portfolio.db"
    )

    tickers = _portfolio_tickers(db_path)
    if len(tickers) < 2:
        print(f"Fewer than 2 portfolio tickers ({len(tickers)}) — nothing to audit.")
        return 0

    result = refresh_thesis_collision(db_path, repo_root, tickers)
    if result is None:
        print("Fewer than 2 names had a usable thesis on file — nothing to audit.")
        return 0

    spend = "cache hit (thesis set unchanged, no LLM call)" if result.cache_hit else "regenerated"
    print(
        f"Thesis collision audit: {spend} · "
        f"{len(result.report.tickers_analyzed)} names · "
        f"{len(result.report.clusters)} shared-driver clusters · "
        f"{len(result.report.contradictions)} contradictions"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
