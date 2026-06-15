"""
execution/check_segment_cache_sanity.py
---------------------------------------
Standalone CLI sweep over the FMP segment caches. Flags any quarter whose
sum-of-segments runs materially above the period's reported income-statement
revenue — the signature of FMP's recurring "revenue-product-segmentation /
revenue-geographic-segments returns a Q4/FY record contaminated with the prior
FY's annual figures" bug (e.g. GOOG 2025-12-31 once reported Google Cloud at
$20.9B / +75% YoY when the press release was $17.7B / +48%).

Why sum-vs-revenue rather than a per-segment YoY-growth heuristic: a single
inflated or duplicated cell blows up the *sum* far past the income-statement
total, which is mechanically impossible and so has ~no false positives. A raw
"segment grew too fast YoY" rule fires on legitimately fast movers (GCP +48%,
AWS, etc.).

The scan core lives in ``pipeline.segment_cache_audit`` so the quarterly-refresh
``validate_segment_cache`` stage applies the EXACT same rule/threshold as this
CLI (and as the DB-ingest gate). This catches corruption at the cache layer —
*before* the direct-JSON readers (build_redesigned_dcf.py, dcf_opus_assumptions.py)
consume it.

Usage:
  python execution/check_segment_cache_sanity.py --ticker GOOG
  python execution/check_segment_cache_sanity.py --all
Exit code is non-zero when any quarter is flagged, so it can gate a pipeline.
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
DEFAULT_DATA_DIR = os.path.join(PROJECT_ROOT, "data", "historical", "fmp")
sys.path.append(SRC_DIR)

from compute.segments import RECONCILE_TOLERANCE_OVER  # noqa: E402
from pipeline.segment_cache_audit import SegmentFlag, audit_ticker_cache  # noqa: E402


def _tracked_tickers() -> list[str]:
    # Imported lazily: only --all needs the DB; single-ticker runs stay DB-free.
    import db

    return [str(c["ticker"]) for c in db.get_tracked_companies()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", help="Specific ticker (e.g. GOOG)")
    group.add_argument("--all", action="store_true", help="Scan all tracked companies")
    parser.add_argument(
        "--data-dir",
        default=DEFAULT_DATA_DIR,
        help=f"FMP cache dir (default: {DEFAULT_DATA_DIR})",
    )
    args = parser.parse_args()

    tickers = _tracked_tickers() if args.all else [args.ticker.upper()]
    print(
        f"Segment-cache sanity sweep - {len(tickers)} ticker(s); "
        f"flag when sum(segments) > revenue x {1 + RECONCILE_TOLERANCE_OVER:.2f}"
    )

    all_flags: list[SegmentFlag] = []
    for t in tickers:
        all_flags.extend(audit_ticker_cache(args.data_dir, t))

    if not all_flags:
        print("  [OK] no over-cap segment records found.")
        sys.exit(0)

    print(f"\n  [FLAG] {len(all_flags)} over-cap segment record(s) - likely FMP contamination:")
    for fl in sorted(all_flags, key=lambda r: (r.ticker, r.period_end)):
        print(
            f"    {fl.ticker:6s} {fl.period_end[:10]} {fl.period:3s} "
            f"{fl.file:36s} sum=${fl.segment_sum / Decimal(1_000_000):,.0f}M "
            f"revenue=${fl.revenue / Decimal(1_000_000):,.0f}M ratio={fl.ratio:.3f}"
        )
    sys.exit(1)


if __name__ == "__main__":
    main()
