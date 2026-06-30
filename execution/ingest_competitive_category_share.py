"""Ingest the annual 3rd-party category-share seed into kpi_facts.

Reads ``micro_thesis/competitive/<TICKER>_category_share.json`` (the manual-entry
store of Gartner Magic Quadrant / IDC data-protection position) and writes each
grounded datapoint as an ANNUAL (FY) kpi_fact via ``persist_manifest`` — so the
matching competitive tier-2 KPI in ``RBRK.json`` reads a real stored value.
Entries with a null ``value`` are "awaiting source" slots and are skipped.

Usage:
    python execution/ingest_competitive_category_share.py --ticker RBRK
    python execution/ingest_competitive_category_share.py --all
    python execution/ingest_competitive_category_share.py --ticker RBRK --db /tmp/x.db
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from competitive.category_share import ingest_category_share  # noqa: E402
from pipeline.queries import open_db  # noqa: E402


def _tickers_with_seed(repo_root: Path) -> list[str]:
    seed_dir = repo_root / "micro_thesis" / "competitive"
    if not seed_dir.exists():
        return []
    return sorted(
        p.stem.removesuffix("_category_share").upper()
        for p in seed_dir.glob("*_category_share.json")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"), help="Path to portfolio.db"
    )
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", help="Ingest one ticker (uppercase)")
    g.add_argument("--all", action="store_true", help="Ingest every ticker with a seed file")
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT, help="Repo root")
    args = parser.parse_args()

    repo_root: Path = args.repo_root.resolve()
    tickers = _tickers_with_seed(repo_root) if args.all else [args.ticker.upper()]

    conn = open_db(args.db)
    try:
        results = [ingest_category_share(conn, repo_root, t) for t in tickers]
        conn.commit()
    finally:
        conn.close()

    print(
        json.dumps(
            {
                "inserted": sum(r.inserted for r in results),
                "skipped_existing": sum(r.skipped_existing for r in results),
                "skipped_awaiting_source": sum(r.skipped_awaiting_source for r in results),
                "per_ticker": [asdict(r) for r in results],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
