"""Materialise per-ticker fundamentals for the cockpit evaluation table.

Runs the financial_facts double-scan (rev_yoy + fcf_margin per ticker) and
writes the result to data/cockpit_fundamentals.json. The GET / render reads
this cache instead of hitting the DB on every boot — cutting ~1.2s off the
render path (S12b profiling: the dominant boot offender, PR #535).

Called as Stage 0d of the morning pipeline (run_morning_pipeline.py). Can
also be run standalone after a financial_facts ingest to refresh stale data.

Usage:
    python execution/refresh_cockpit_fundamentals.py
    python execution/refresh_cockpit_fundamentals.py --repo-root . --db-path /tmp/x.db
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from cockpit_fundamentals import materialize_fundamentals  # noqa: E402

log = logging.getLogger("refresh_cockpit_fundamentals")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/portfolio.db and data/cockpit_fundamentals.json.",
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

    if not db_path.exists():
        log.error("DB not found: %s", db_path)
        return 1

    conn = sqlite3.connect(str(db_path))
    try:
        n = materialize_fundamentals(conn, repo_root)
    finally:
        conn.close()

    print(f"Cockpit fundamentals materialised · tickers={n} · cache=data/cockpit_fundamentals.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
