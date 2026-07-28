"""execution/build_confidence_observations.py
-----------------------------------------------
Seed the ``confidence_observations`` ledger (Close-the-Loops L10) from the
ground truth already sitting in the DB:

* every ``supersedes_id`` restatement chain (financial_facts + kpi_facts) —
  grading the OLD fact's stored confidence against whether the later filing
  confirmed or materially restated it;
* every RESOLVED cross-source ``source_disagreement`` — grading the lower-tier
  fact against the higher-tier authority.

Forward capture (the four restatement-detection insert sites) records new
restatements as they happen; this backfill seeds everything that predates that
wiring. Both write the same idempotent table (``UNIQUE(fact_table, fact_id,
kind)``), so this is safe to re-run after each validation pass — a second run
over an unchanged DB inserts nothing new.

Usage:
    python execution/build_confidence_observations.py                 # dry-run
    python execution/build_confidence_observations.py --apply         # write
    python execution/build_confidence_observations.py --db C:/path/portfolio.db --apply
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from credibility.observations import (  # noqa: E402
    backfill_disagreement_observations,
    backfill_restatement_observations,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[2] if __doc__ else "")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/portfolio.db (default: this checkout).",
    )
    parser.add_argument(
        "--db", type=Path, default=None, help="Explicit DB path (wins over --repo-root)."
    )
    parser.add_argument(
        "--user-id", type=str, default="bhanu", help="Owner of the observation rows."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the observations. Without it: dry-run (rolled back, counts only).",
    )
    args = parser.parse_args(argv)

    db_path = args.db if args.db is not None else args.repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        print(f"ERROR: no DB at {db_path}", file=sys.stderr)
        return 2

    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    conn.row_factory = sqlite3.Row
    try:
        restatements = backfill_restatement_observations(conn, user_id=args.user_id)
        disagreements = backfill_disagreement_observations(conn, user_id=args.user_id)
        if args.apply:
            conn.commit()
        else:
            conn.rollback()
    finally:
        conn.close()

    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"confidence-observation backfill [{mode}] db={db_path}")
    print(f"  restatements: {'inserted' if args.apply else 'would-insert'}={restatements}")
    print(f"  disagreements: {'inserted' if args.apply else 'would-insert'}={disagreements}")
    if not args.apply:
        print("re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
