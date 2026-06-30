"""Extract per-quarter competitive-mention counts from earnings transcripts.

Scans ``transcripts/{processed,raw}/<TICKER>_Q<N>_<YYYY>.{txt,pdf}``, counts the
three competitive signals (displacement-of-legacy, >$1M/large-logo wins,
Cohesity/Veeam/Dell mentions) deterministically, and writes the per-quarter
counts to ``kpi_facts`` (unit=count) — so the competitive tier-2 KPIs in
``RBRK.json`` read real, chartable values.

Usage:
    python execution/extract_competitive_mentions.py --ticker RBRK
    python execution/extract_competitive_mentions.py --ticker RBRK --db /tmp/x.db
    python execution/extract_competitive_mentions.py --ticker RBRK --transcripts-root /path
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from competitive.transcript_mentions import extract_for_ticker  # noqa: E402
from pipeline.queries import open_db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"), help="Path to portfolio.db"
    )
    parser.add_argument("--ticker", default="RBRK", help="Ticker (uppercase); default RBRK")
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT, help="Repo root")
    parser.add_argument(
        "--transcripts-root",
        type=Path,
        default=None,
        help="Override the transcripts/ root (default: <repo-root>/transcripts)",
    )
    args = parser.parse_args()

    repo_root: Path = args.repo_root.resolve()
    conn = open_db(args.db)
    try:
        result = extract_for_ticker(
            conn,
            repo_root,
            args.ticker.upper(),
            transcripts_root=args.transcripts_root,
        )
        conn.commit()
    finally:
        conn.close()

    print(
        json.dumps(
            {
                "ticker": result.ticker,
                "quarters_processed": len(result.quarters),
                "quarters": [
                    {
                        "quarter": q.quarter,
                        "fiscal_year_label": q.fiscal_year_label,
                        "period_end": q.period_end,
                        "displacement": q.counts.displacement,
                        "large_win": q.counts.large_win,
                        "named_competitor": q.counts.named_competitor,
                        "vendor_breakdown": q.counts.vendor_breakdown,
                        "inserted": q.inserted,
                        "source_path": q.source_path,
                    }
                    for q in result.quarters
                ],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
