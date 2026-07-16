"""Run the Triage second-pass route sweep (user_state.triage_suggest).

Backfills route suggestions for parked ``needs_triage`` comments that predate
the tap: high-confidence verdicts auto-route (the note leaves the queue as if
the owner picked it, receipt in ``context['auto_routed']``); low-confidence
ones store a one-tap suggestion the Triage row leads with; a park verdict (or
any classify failure) leaves the row a manual pick.

``--suggest-only`` stores every verdict without auto-routing. Idempotent: a
note carrying any second-pass stamp is never re-classified.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from user_state.triage_suggest import sweep_unsuggested


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=50, help="max notes to classify (default 50)")
    parser.add_argument(
        "--suggest-only",
        action="store_true",
        help="store suggestions but never auto-route, even at high confidence",
    )
    parser.add_argument("--db-path", type=Path, default=None, help="override the portfolio DB")
    args = parser.parse_args()
    stats = sweep_unsuggested(
        db_path=args.db_path, limit=args.limit, apply_high=not args.suggest_only
    )
    print(json.dumps(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
