"""Derive inert Work OS reader bodies from immutable legacy workspace reports."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from report.artifacts import (  # noqa: E402
    migrate_legacy_report_bodies,
    rollback_legacy_report_bodies,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--ticker",
        action="append",
        default=[],
        help="Limit extraction to one ticker; repeat for more than one.",
    )
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument(
        "--apply",
        action="store_true",
        help="Persist verified bodies and receipts, then atomically activate index updates.",
    )
    operation.add_argument(
        "--rollback",
        action="store_true",
        help="Reactivate preserved legacy manifests and remove derived reader files.",
    )
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    tickers = set(args.ticker) or None
    result = (
        rollback_legacy_report_bodies(repo_root, tickers=tickers)
        if args.rollback
        else migrate_legacy_report_bodies(repo_root, tickers=tickers, apply=bool(args.apply))
    )
    print(result.model_dump_json())
    print(
        json.dumps(
            {
                "event": "legacy_report_bodies_migrated",
                "apply": result.apply,
                "rollback": result.rollback,
                "candidates": result.candidates,
                "eligible": result.eligible,
                "migrated": result.migrated,
                "rolled_back": result.rolled_back,
                "failed": result.failed,
                "skipped_shared": result.skipped_shared,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
