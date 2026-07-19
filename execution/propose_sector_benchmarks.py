"""One-off CLI: propose a benchmark-ETF row for an unmapped FMP industry
(docs/design/comparable_sets_bottoms_up.md §4, Phase 3 ratification flow).

**Never writes to ``sector_benchmark_map.py``.** Writes a review-queue JSON
proposal to ``data/sector_benchmark_proposals/{industry_key}.json``; the owner
reads it and hand-pastes the ratified line into ``SECTOR_BENCHMARK_MAP``
themselves. Deliberately a manual, on-demand CLI, not a standing pipeline
stage -- the doc explicitly rules against over-engineering this (~50-150
total industries, one-time-per-industry).

Usage:
    python execution/propose_sector_benchmarks.py --industry "Credit Services"
    python execution/propose_sector_benchmarks.py --all-unmapped
    python execution/propose_sector_benchmarks.py --all-unmapped --refresh
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.comparable_sets import load_pool  # noqa: E402
from compute.sector_benchmark_map import SECTOR_BENCHMARK_MAP  # noqa: E402
from compute.sector_benchmark_proposal import extract_for_industry  # noqa: E402
from llm.cli import is_hard_stop  # noqa: E402
from pipeline.queries import open_db  # noqa: E402


def _unmapped_pool_industries(conn: sqlite3.Connection, repo_root: Path) -> list[str]:
    """Every distinct industry in the pool (§2) with no ratified entry yet,
    sorted for a deterministic run order."""
    pool = load_pool(conn, repo_root)
    industries = {m.industry for m in pool.values() if m.industry}
    return sorted(i for i in industries if i not in SECTOR_BENCHMARK_MAP)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--industry", help="Propose a benchmark for one industry string")
    group.add_argument(
        "--all-unmapped",
        action="store_true",
        help="Propose for every pool industry (§2) not yet in SECTOR_BENCHMARK_MAP",
    )
    parser.add_argument("--refresh", action="store_true", help="Ignore any cached proposal")
    parser.add_argument(
        "--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"), help="Path to portfolio.db"
    )
    parser.add_argument(
        "--repo-root",
        default=str(PROJECT_ROOT),
        help="Root containing data/historical/fmp (override for read-only validation)",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root)
    if args.industry:
        industries = [args.industry]
    else:
        conn = open_db(args.db)
        try:
            industries = _unmapped_pool_industries(conn, repo_root)
        finally:
            conn.close()

    if not industries:
        print(json.dumps({"industries": 0, "results": []}, indent=2))
        return 0

    results: list[dict[str, object]] = []
    for industry in industries:
        try:
            proposal = extract_for_industry(industry, repo_root, refresh=args.refresh)
        except Exception as e:
            if is_hard_stop(e):
                print(
                    f"HARD STOP proposing {industry!r}: {type(e).__name__}: {e}",
                    file=sys.stderr,
                )
                return 1
            raise
        results.append(
            {
                "industry": proposal.industry,
                "industry_key": proposal.industry_key,
                "etf": proposal.etf,
                "sector_etf": proposal.sector_etf,
                "why": proposal.why,
                "skipped_reason": proposal.skipped_reason,
                "cache_path": str(
                    repo_root
                    / "data"
                    / "sector_benchmark_proposals"
                    / f"{proposal.industry_key}.json"
                ),
            }
        )

    print(
        json.dumps(
            {
                "industries": len(industries),
                "results": results,
                "next_step": (
                    "Review each cache_path above and hand-paste the ratified line into "
                    "src/sector_benchmark_map.py's SECTOR_BENCHMARK_MAP -- never auto-applied."
                ),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
