"""Reconcile recorded decisions against the tracker's fills → user_action_kind.

Closes the decision→action half of the decision→action→outcome chain. The
``decisions`` ledger captures the call (ADD/TRIM/HOLD/...) and the grading cron
captures the outcome, but ``user_action_kind`` (followed / ignored / partial /
reversed) had NO production writer — so calibration's action mix was structurally
empty. This rung matches each NULL-action decision to the tracker's subsequent
transactions (same ticker, the direction the call implies, within a window after
made_at) and writes the action (src/decision_extractor.reconcile_decision_actions).

Idempotent — only NULL-action rows are touched; a second run against the same
world is a no-op. Best-effort: a missing DB or an offline tracker is reported,
never fatal (a tracker outage just leaves the action undetermined for next run).
The morning pipeline runs it daily (stage 0c2), after the lifecycle reconciler.

Usage:
    python execution/reconcile_decision_actions.py
    python execution/reconcile_decision_actions.py --window-days 45 --lookback-days 365
    python execution/reconcile_decision_actions.py --db-path /tmp/x.db
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from decision_extractor import reconcile_decision_actions  # noqa: E402

log = logging.getLogger("reconcile_decision_actions")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--window-days",
        type=int,
        default=30,
        help="Match fills within this many days after a decision's made_at (default 30).",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=180,
        help="Only reconcile decisions made within this many days (default 180).",
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
    tally = reconcile_decision_actions(
        db_path=db_path, window_days=args.window_days, lookback_days=args.lookback_days
    )
    log.info({"event": "reconcile_decision_actions_done", **tally})
    print(
        "Decision-action reconcile · "
        f"scanned={tally['scanned']} · followed={tally['followed']} · "
        f"reversed={tally['reversed']} · partial={tally['partial']} · "
        f"ignored={tally['ignored']} · undetermined={tally['skipped_undetermined']} · "
        f"tracker={'offline' if tally['tracker_unavailable'] else 'online'} · "
        f"db_unavailable={tally['db_unavailable']}"
    )
    # A missing/pre-migration DB is a real failure for a scheduled rung; a tracker
    # outage is not (the reconciler degrades by design and retries next run).
    return 1 if tally["db_unavailable"] else 0


if __name__ == "__main__":
    sys.exit(main())
