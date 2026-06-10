"""Score matured advisor memos/stances (master build P2.5).

Walks ``advisor_memos`` rows with ``score_status='pending'`` and grades the
ones whose horizon has matured — Socratic stances against the holding's
dividend-adjusted price move (SPY-relative via the tracker when reachable,
absolute otherwise), swap checks against the realized pair margin,
next-dollar memos marked unscoreable (v1). Deferred memos (immature horizon,
price cache gaps, tracker down) stay pending for the next run — the job is
idempotent.

Usage:
    python execution/score_advisor_stances.py
    python execution/score_advisor_stances.py --repo-root <path> --user-id bhanu

Runs from ``cron/weekly_score_stances.task.xml`` (Sun 06:30) and on demand.
Exit status: always 0 unless the substrate itself is unusable — deferrals
are the normal weekly state, not failures.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--user-id", default=None, help="defaults to identity.DEFAULT_USER_ID")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    from advisor.scoring import run_scoring
    from identity import DEFAULT_USER_ID

    result = run_scoring(args.repo_root.resolve(), user_id=args.user_id or DEFAULT_USER_ID)
    for outcome in result.outcomes or []:
        if outcome.scored:
            print(f"memo #{outcome.memo_id}: {outcome.verdict}", flush=True)
        else:
            print(f"memo #{outcome.memo_id}: deferred — {outcome.defer_reason}", flush=True)
    print(f"scored {result.scored} · deferred {result.deferred}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
