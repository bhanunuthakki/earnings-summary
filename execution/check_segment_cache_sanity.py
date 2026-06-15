"""
execution/check_segment_cache_sanity.py
---------------------------------------
Offline sanity sweep over the FMP segment caches. Flags any quarter whose
sum-of-segments runs materially above the period's reported income-statement
revenue — the signature of FMP's recurring "revenue-product-segmentation /
revenue-geographic-segments returns a Q4/FY record contaminated with the prior
FY's annual figures" bug (e.g. GOOG 2025-12-31 once reported Google Cloud at
$20.9B / +75% YoY when the press release was $17.7B / +48%).

Why sum-vs-revenue rather than a per-segment YoY-growth heuristic: a single
inflated or duplicated cell blows up the *sum* far past the income-statement
total, which is mechanically impossible and so has ~no false positives. A raw
"segment grew too fast YoY" rule fires on legitimately fast movers (GCP +48%,
AWS, etc.). This sweep applies the EXACT predicate and tolerance the DB-ingest
gate uses (compute.segments.segment_sum_exceeds_revenue), so a record this
scanner flags is the same record extract_segment_facts() would drop.

This catches corruption at the cache layer — *before* the direct-JSON readers
(execution/build_redesigned_dcf.py, execution/dcf_opus_assumptions.py) consume
it. The DB path is already protected by the ingest gate; those raw-JSON readers
are not.

Usage:
  python execution/check_segment_cache_sanity.py --ticker GOOG
  python execution/check_segment_cache_sanity.py --all
Exit code is non-zero when any quarter is flagged, so it can gate a pipeline.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import cast

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
DEFAULT_DATA_DIR = os.path.join(PROJECT_ROOT, "data", "historical", "fmp")
sys.path.append(SRC_DIR)

from compute.segments import (  # noqa: E402
    RECONCILE_TOLERANCE_OVER,
    segment_sum_exceeds_revenue,
)

_SEGMENT_SUFFIXES = ("product_segments_quarterly", "geo_segments_quarterly")


@dataclass(frozen=True)
class SegmentFlag:
    """One over-cap segment record (sum-of-segments exceeds reported revenue)."""

    ticker: str
    file: str
    period_end: str
    period: str
    segment_sum: Decimal
    revenue: Decimal
    ratio: float


def _load_json(path: str) -> list[dict[str, object]] | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            body = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  [skip] unreadable {os.path.basename(path)}: {e}", file=sys.stderr)
        return None
    if not isinstance(body, list):
        return None
    # FMP caches are JSON arrays of record objects; cast at the JSON boundary.
    return cast("list[dict[str, object]]", body)


def _revenue_by_period_end(data_dir: str, ticker: str) -> dict[str, Decimal]:
    """date -> standalone-quarter revenue from the income-statement cache."""
    recs = _load_json(os.path.join(data_dir, f"{ticker}_income_statement_quarterly.json"))
    out: dict[str, Decimal] = {}
    for rec in recs or []:
        date = rec.get("date")
        rev = rec.get("revenue")
        if date and rev is not None:
            try:
                out[str(date)] = Decimal(str(rev))
            except (ArithmeticError, ValueError):
                continue
    return out


def check_ticker(data_dir: str, ticker: str) -> list[SegmentFlag]:
    """Return one flag per over-cap segment record for the ticker."""
    ticker = ticker.upper()
    revenue_by_date = _revenue_by_period_end(data_dir, ticker)
    flags: list[SegmentFlag] = []
    for suffix in _SEGMENT_SUFFIXES:
        recs = _load_json(os.path.join(data_dir, f"{ticker}_{suffix}.json"))
        if not recs:
            continue
        for rec in recs:
            date = str(rec.get("date") or "")
            data = rec.get("data")
            values = data.values() if isinstance(data, dict) else []
            revenue = revenue_by_date.get(date)
            if revenue is None or revenue == 0:
                # No income-statement revenue to reconcile against - can't
                # disprove, so don't flag (mirrors the ingest gate's None branch).
                continue
            exceeds, seg_sum = segment_sum_exceeds_revenue(values, revenue)
            if exceeds:
                flags.append(
                    SegmentFlag(
                        ticker=ticker,
                        file=f"{ticker}_{suffix}.json",
                        period_end=date,
                        period=str(rec.get("period") or ""),
                        segment_sum=seg_sum,
                        revenue=revenue,
                        ratio=float(seg_sum / revenue),
                    )
                )
    return flags


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
        all_flags.extend(check_ticker(args.data_dir, t))

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
