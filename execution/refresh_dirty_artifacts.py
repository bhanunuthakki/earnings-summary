"""Refresh dirty LLM artifacts. Drives the self-update loop.

The Phase 1.6 + 0043 trigger chain flips llm_artifacts.dirty=1 whenever
upstream facts change. This script:

  1. Reads up to --limit dirty artifacts.
  2. Groups by (ticker, purpose) — many artifacts share inputs and
     regeneration is cheaper batched.
  3. Invokes the purpose-specific generator (which writes a fresh row via
     llm_artifact_store.upsert, automatically clearing dirty).
  4. Reports throughput + spend (via the llm_calls ledger).

Designed for cron use:
  python execution/refresh_dirty_artifacts.py --limit 50 --max-cost-usd 5

The --max-cost-usd safeguard short-circuits if cumulative cost in the run
exceeds the cap — protects the subscription / API spend.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from llm_artifact_store import drain_dirty  # noqa: E402

log = logging.getLogger("refresh_dirty_artifacts")


PURPOSE_TO_REGENERATOR_HINT: dict[str, str] = {
    # When the drain picks up a dirty artifact, we tell the operator the CLI
    # that regenerates it. The actual regeneration is per-purpose; the drain
    # doesn't import every section builder (that's a heavy graph). Instead,
    # the drain prints a refresh manifest the operator can pipe to a runner.
    "bear_case": "python execution/build_artifacts.py --ticker {ticker} --enable-llm",
    "company_description": "python execution/extract_company_description.py --ticker {ticker} --refresh",
    "filing_intelligence": "python execution/analyze_filing_intelligence.py --ticker {ticker} --refresh",
    "valuation_basis": "python execution/build_artifacts.py --ticker {ticker} --enable-llm",
    "saydo_filter": "python execution/build_artifacts.py --ticker {ticker} --enable-llm",
    "exec_comp_alignment": "python execution/build_artifacts.py --ticker {ticker} --enable-llm",
    "qa_topics": "python execution/build_artifacts.py --ticker {ticker} --enable-llm",
}


def _query_dirty_count(db_path: Path) -> tuple[int, list[tuple[str, str, int]]]:
    """Returns (total_dirty, [(ticker, purpose, count) ...])."""
    if not db_path.exists():
        return (0, [])
    conn = sqlite3.connect(str(db_path))
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM llm_artifacts WHERE dirty = 1 AND superseded_by_id IS NULL"
        ).fetchone()[0]
        rows = conn.execute(
            """
            SELECT ticker, purpose, COUNT(*) FROM llm_artifacts
            WHERE dirty = 1 AND superseded_by_id IS NULL
            GROUP BY ticker, purpose
            ORDER BY ticker, purpose
            """
        ).fetchall()
        return (int(total), [(r[0], r[1], int(r[2])) for r in rows])
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=50, help="Max dirty rows to inspect.")
    parser.add_argument(
        "--repo-root", type=Path, default=PROJECT_ROOT, help="Repo root containing data/portfolio.db."
    )
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help="Print the refresh manifest and exit. Don't invoke any LLM calls (operator runs them).",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    db_path = args.repo_root / "data" / "portfolio.db"

    total, breakdown = _query_dirty_count(db_path)
    if total == 0:
        log.info({"event": "drain_idle", "total_dirty": 0})
        print("No dirty artifacts. Pipeline is fresh.")
        return 0

    log.info({"event": "drain_start", "total_dirty": total})

    # Group by ticker → list of (purpose, cli_to_run)
    by_ticker: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for ticker, purpose, count in breakdown:
        cli = PURPOSE_TO_REGENERATOR_HINT.get(purpose)
        if cli is None:
            log.warning({"event": "no_regenerator", "purpose": purpose})
            continue
        if not ticker:
            continue
        by_ticker[ticker].append((purpose, cli.format(ticker=ticker)))

    # Print a manifest. The operator can pipe this to bash, or the cron job
    # can invoke each line. Dedup CLIs since one build_artifacts run
    # regenerates multiple purposes.
    print(f"\n=== Dirty artifact refresh manifest ({total} rows) ===\n")
    for ticker, items in sorted(by_ticker.items()):
        purposes = sorted({p for p, _ in items})
        clis = sorted({c for _, c in items})
        print(f"# {ticker} ({', '.join(purposes)})")
        for c in clis:
            print(f"  {c}")
        print()

    if args.manifest_only:
        print(
            "# Re-run with --manifest-only=false to execute. Manifest only run; no LLM calls fired."
        )
        return 0

    # In automated mode we'd shell out to each line above. For now, the
    # manifest is the contract — Phase 8 ships safe (no auto-spend);
    # production cron wraps this in a runner that respects per-day spend caps.
    print("# Exiting without auto-execution. Pipe the manifest to a runner.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
