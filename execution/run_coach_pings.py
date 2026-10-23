"""Run the daily dashboard findings and reconciliation pass.

Collect deterministic findings, apply the governor's freshness and mute rules,
and retain eligible findings in the dashboard digest. Capacity findings also
refresh the existing owner_capacity_breach alerts. This operation writes state
and makes no Telegram calls.

    python execution/run_coach_pings.py
"""

from __future__ import annotations

import argparse
import sys
from contextlib import nullcontext
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Deprecated compatibility alias; reconciliation and digest state are still written.",
    )
    args = parser.parse_args()
    if args.dry_run:
        print(
            "--dry-run is deprecated: this pass writes reconciliation and digest state; "
            "Telegram delivery is retired.",
            file=sys.stderr,
        )
    repo_root = args.repo_root.resolve()
    db_path = repo_root / "data" / "portfolio.db"

    from runtime.job_runtime import JobLock, inherited_lock_is_valid

    # One findings pass at a time: the scheduler already holds this lane for
    # scheduled runs; a manual run takes the same lane, so finding admission
    # and row creation never interleave with another run.
    inherited = inherited_lock_is_valid(repo_root, "notification-delivery")
    with (
        nullcontext() if inherited else JobLock(repo_root, "coach-pings", ["notification-delivery"])
    ):
        return _run(repo_root, db_path)


def _run(repo_root: Path, db_path: Path) -> int:
    from research.governor import run_governor
    from synthesis.auto_reconcile import auto_reconcile

    # Housekeeping first — derive, don't ask: anything software can resolve
    # (played-out history, moot falsifiers) never reaches the owner's queue.
    auto_tally = auto_reconcile(db_path)
    print(f"auto_reconcile: {auto_tally}", file=sys.stderr)

    tally = run_governor(db_path, repo_root=repo_root)
    print(f"run_coach_pings: {tally}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
