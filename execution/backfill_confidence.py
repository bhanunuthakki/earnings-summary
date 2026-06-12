"""execution/backfill_confidence.py
-----------------------------------
Re-runnable backfill of the ``confidence`` column on ``financial_facts`` and
``kpi_facts`` using the documented deterministic formula in
``src/pipeline/confidence.py``:

    confidence = clamp( tier_base + method_delta − unresolved-issue penalties )

Historically every row carried the schema default 1.0 — an S-1 extraction
badged identically to an SEC XBRL fact. This script rescores every row from
its document's ``source_quality_tier``, the row's ``extracted_by`` method,
and unresolved ``validation_issues`` targeting the fact, and UPDATEs rows
whose stored confidence differs.

Idempotent by construction: the formula is pure, so a second run over an
unchanged DB updates zero rows. Re-run after each validation-engine pass to
fold fresh cross-source-disagreement penalties into the scores (ingest-time
writers can only store the tier+method prior — issues don't exist yet for
rows being written).

LLM-extracted rows whose confidence is already ≠ 1.0 were self-scored at
extraction time (model self-report folded in by kpi_persistence); the
original self-report is not recoverable from the stored product, so those
rows are preserved, never rescored (tallied as "preserved").

Usage:
    python execution/backfill_confidence.py                    # dry-run, all tickers
    python execution/backfill_confidence.py --apply            # write updates
    python execution/backfill_confidence.py --ticker NU --apply
    python execution/backfill_confidence.py --repo-root C:/path/to/repo --apply
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pipeline.confidence import RescoreOutcome, apply_confidence_scores  # noqa: E402


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
    parser.add_argument("--ticker", type=str, default=None, help="Restrict to one ticker.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the recomputed scores. Without it: dry-run (counts only).",
    )
    args = parser.parse_args(argv)

    db_path = args.db if args.db is not None else args.repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        print(f"ERROR: no DB at {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path))
    try:
        outcomes: list[RescoreOutcome] = [
            apply_confidence_scores(
                conn, table="financial_facts", ticker=args.ticker, apply=args.apply
            ),
            apply_confidence_scores(conn, table="kpi_facts", ticker=args.ticker, apply=args.apply),
        ]
    finally:
        conn.close()

    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"confidence backfill [{mode}] db={db_path}")
    for o in outcomes:
        print(
            f"  {o.table}: examined={o.examined} "
            f"{'updated' if args.apply else 'would-update'}={o.updated} "
            f"preserved-self-scored={o.preserved_self_scored}"
        )
    if not args.apply:
        print("re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
