"""execution/backfill_seed_decisions.py — seed the Brier denominator.

Lands the 22 seed-corpus decisions (``data/ledger_seed/seed.json``) as
``decided_by='owner'`` rows in the ``decisions`` calibration ledger (0130
shape), then optionally runs the standing price grader so they get outcome
labels with the SAME methodology as every advisor row (no hand-coded hindsight
grades). Idempotent — re-running skips already-landed items.

    python execution/backfill_seed_decisions.py            # land + grade
    python execution/backfill_seed_decisions.py --no-grade # land only

Requires the 0130 migration on the target DB (``alembic upgrade head``).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from synthesis.seed_decisions import backfill_seed_decisions  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--seed-json", type=Path, default=None)
    parser.add_argument("--no-grade", action="store_true")
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    db_path = repo_root / "data" / "portfolio.db"
    seed_path = args.seed_json or (repo_root / "data" / "ledger_seed" / "seed.json")
    if not seed_path.exists():
        print(f"backfill_seed_decisions: no seed at {seed_path}", file=sys.stderr)
        return 2
    tally = backfill_seed_decisions(db_path, seed_path)
    print(f"backfill_seed_decisions: {tally}", file=sys.stderr)
    if not args.no_grade and tally["inserted"]:
        # Same grader, same methodology as the advisor stream (weekly cron).
        result = subprocess.run(
            [sys.executable, str(repo_root / "execution" / "grade_decisions.py")],
            cwd=str(repo_root),
            check=False,
        )
        return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
