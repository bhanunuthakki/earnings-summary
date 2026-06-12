"""Reconcile the position-lifecycle ledger against the current portfolio.

State-based (src/position_lifecycle.py): compares tracked_companies'
``list_type='portfolio'`` membership + the tracker's live holdings against
open ``position_entries`` rows; opens rows for new names, closes rows for
departed ones, enriches entry prices when the tracker is online. Idempotent —
the morning pipeline runs it daily (stage 0c); a quiet day is a no-op.

``--backfill`` seeds rows for positions held BEFORE this ledger existed:
``source='backfill'`` and ``entry_date`` stays NULL unless an opening buy is
actually visible in the tracker's transaction window (honest "held, opening
unknown"). Run it once per deployment.

Usage:
    python execution/sync_position_lifecycle.py
    python execution/sync_position_lifecycle.py --backfill
    python execution/sync_position_lifecycle.py --repo-root <MAIN> --db-path /tmp/x.db
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from identity import DEFAULT_USER_ID  # noqa: E402
from position_lifecycle import sync_position_lifecycle  # noqa: E402

log = logging.getLogger("sync_position_lifecycle")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Treat newly-opened rows as pre-existing holdings (source='backfill', "
        "entry_date NULL unless a buy transaction is visible). One-time seeding.",
    )
    parser.add_argument(
        "--user-id",
        default=DEFAULT_USER_ID,
        help=f"Owner of the ledger rows (default {DEFAULT_USER_ID!r}).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/portfolio.db.",
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

    db_path: Path = (
        args.db_path
        if args.db_path is not None
        else args.repo_root.resolve() / "data" / "portfolio.db"
    )
    tally = sync_position_lifecycle(
        db_path=db_path, user_id=args.user_id, assume_preexisting=args.backfill
    )
    log.info({"event": "sync_position_lifecycle_done", **tally})
    print(
        "Position lifecycle sync complete · "
        f"opened={tally['opened']} · closed={tally['closed']} · "
        f"unchanged={tally['unchanged']} · "
        f"tracker={'online' if tally['tracker_available'] else 'offline'} · "
        f"db_unavailable={tally['db_unavailable']}"
    )
    # A missing/pre-migration DB is a real failure for a scheduled rung; a
    # tracker outage is not (the reconciler degrades by design).
    return 1 if tally["db_unavailable"] else 0


if __name__ == "__main__":
    sys.exit(main())
