"""Backfill exact Full Brief fiscal periods from immutable report selectors."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from _lib import PROJECT_ROOT
except ImportError:
    from execution._lib import PROJECT_ROOT

from operations.paths import configured_product_state_root
from report.artifacts import backfill_report_fiscal_periods


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        help="Explicit artifact root; defaults to configured product state",
    )
    parser.add_argument(
        "--ticker",
        action="append",
        default=[],
        help="Limit extraction to one ticker; repeat for more than one.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Atomically activate only checksum-verified, exact-period index updates.",
    )
    args = parser.parse_args()
    repo_root = (
        args.repo_root.resolve()
        if args.repo_root is not None
        else configured_product_state_root(PROJECT_ROOT)
    )
    result = backfill_report_fiscal_periods(
        repo_root,
        tickers=set(args.ticker) or None,
        apply=bool(args.apply),
    )
    print(result.model_dump_json())
    print(
        json.dumps(
            {
                "event": "report_fiscal_periods_backfilled",
                "apply": result.apply,
                "candidates": result.candidates,
                "eligible": result.eligible,
                "applied": result.applied,
                "skipped_existing": result.skipped_existing,
                "unresolved": result.unresolved,
                "unresolved_reasons": result.unresolved_reasons,
                "failed": result.failed,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
