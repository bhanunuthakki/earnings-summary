"""Distil the owner's graded decision corpus into candidate behavioral rules
(src/synthesis/behavior_distill.py — tenet-2 Phase 4).

Reads ``decisions`` (``decided_by='owner'``, graded, non-pending) plus his
captured musings and proposes 0-6 behavioral ``owner_profile_facts`` rows
(category='behavioral', provenance='derived', always ``status='proposed'`` —
never auto-affirmed, per §7.1 gated assertion). The packet walk is the
ratification surface; this script only stages candidates.

$0 LLM spend when the graded corpus is empty. On-demand CLI; also invoked as
the last stage of the weekly grading orchestrator
(``execution/run_calibration_grading.py``) so behavioral rules re-derive
right after each grading batch, per §3.3's "behavioral: after each grading
batch" review-horizon rule.

Usage:
    python execution/run_behavior_distill.py
    python execution/run_behavior_distill.py --repo-root . --db-path /tmp/x.db
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from synthesis.behavior_distill import run_behavior_distill  # noqa: E402

log = logging.getLogger("run_behavior_distill")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
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
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    repo_root: Path = args.repo_root.resolve()
    db_path: Path = (
        args.db_path if args.db_path is not None else repo_root / "data" / "portfolio.db"
    )

    if not db_path.exists():
        print(f"No DB at {db_path} — nothing to distil.")
        return 0

    counts = run_behavior_distill(db_path)
    print(
        "Behavior distill: "
        f"{counts['candidates']} graded decisions in corpus · "
        f"{counts['proposed']} rule(s) staged · "
        f"{counts['tensions']} superseding an affirmed rule · "
        f"{counts['skipped_uncited']} dropped (uncited/miscited) · "
        f"{counts['skipped_invalid']} dropped (schema) · "
        f"{counts['deferred_transient']} deferred (transient failure, retry next run)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
