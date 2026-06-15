"""
execution/extract_8k_overrides.py
---------------------------------
Automated SEC 8-K -> ``fact_overrides`` populator. Fetches a company's earnings
press-release exhibit (EX-99.1) from EDGAR by accession number, LLM-extracts the
segment revenue table, and (with ``--apply``) records a durable record-level
``segment`` override so the company-published figures supersede FMP's segmentation
at read/resolve time — durably, even across an FMP re-fetch.

See ``directives/provenance_override_2026_06.md``. The override mechanism is P1-P3;
this is the automated front-end (vs. hand-seeding with ``record_fact_override.py``).

Examples
--------
  # Propose (dry-run) the GOOG Q4'25 product-segment override from the 8-K:
  python execution/extract_8k_overrides.py \
      --ticker GOOG --accession 0001652044-26-000012 \
      --period-end 2025-12-31 --fiscal-period-type Q4 --dim-type product

  # Same, but record it into the DB:
  python execution/extract_8k_overrides.py --ticker GOOG \
      --accession 0001652044-26-000012 --period-end 2025-12-31 \
      --fiscal-period-type Q4 --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
sys.path.append(SRC_DIR)

import db  # noqa: E402
from provenance import edgar_8k, overrides  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--ticker", required=True)
    parser.add_argument(
        "--accession", required=True, help="EDGAR accession, e.g. 0001652044-26-000012"
    )
    parser.add_argument("--period-end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--fiscal-period-type", required=True, help="Q1..Q4 | FY | H1 ...")
    parser.add_argument("--dim-type", default="product", choices=["product", "geography"])
    parser.add_argument("--exhibit", help="Exhibit filename (auto-discovered if omitted)")
    parser.add_argument("--cik", help="Override CIK (auto-resolved from ticker if omitted)")
    parser.add_argument(
        "--apply", action="store_true", help="Record the override (default: dry-run)"
    )
    parser.add_argument("--db-path", help="Target DB (defaults to the project portfolio.db)")
    args = parser.parse_args()

    proposal = edgar_8k.extract_8k_segment_override(
        ticker=args.ticker,
        accession=args.accession,
        period_end=args.period_end,
        fiscal_period_type=args.fiscal_period_type,
        dim_type=args.dim_type,
        exhibit=args.exhibit,
        cik=args.cik,
    )
    if proposal is None:
        print(
            f"  [no-op] no {args.dim_type} segment breakdown extracted for "
            f"{args.ticker.upper()} {args.accession}"
        )
        return 1

    print(
        f"  [extracted] {proposal.ticker} {proposal.period_end} {proposal.fiscal_period_type} "
        f"{proposal.dim_type} <- {proposal.source_exhibit}"
    )
    print(f"    {json.dumps(proposal.segments, indent=2)}")
    print(f"    source: {proposal.source_url}")

    if not args.apply:
        print("  [dry-run] pass --apply to record this override")
        return 0

    if args.db_path:
        db.set_db_path(args.db_path)
    conn = db.get_connection()
    try:
        new_id = overrides.record_override(
            conn,
            ticker=proposal.ticker,
            period_end=proposal.period_end,
            fiscal_period_type=proposal.fiscal_period_type,
            fact_kind=overrides.SEGMENT,
            fact_key=overrides.segment_record_key(proposal.dim_type),
            action=overrides.OverrideAction.REPLACE,
            value_json=dict(proposal.segments),
            source_doc_type="sec_8k",
            source_accession=proposal.source_accession,
            source_exhibit=proposal.source_exhibit,
            source_url=proposal.source_url,
            source_excerpt=proposal.source_excerpt,
            rationale=f"Auto-extracted from 8-K exhibit {proposal.source_exhibit}",
            created_by="agent:extract_8k_overrides",
        )
        conn.commit()
        print(f"  [ok] recorded override id={new_id}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
