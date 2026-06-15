"""Offline audit of the cached FMP segment files against income-statement revenue.

Shared core for two callers:
  - ``execution/check_segment_cache_sanity.py`` — standalone CLI sweep.
  - the ``validate_segment_cache`` stage of ``pipeline.quarterly_refresh`` — a
    post-fetch gate that runs at the front of each ticker's refresh DAG.

Both flag any cached quarter whose sum-of-segments runs materially above the
period's reported income-statement revenue — the signature of FMP's recurring
"revenue-product-segmentation / revenue-geographic-segments returns a Q4/FY
record contaminated with the prior FY's annual figures" bug (e.g. GOOG
2025-12-31 once reported Google Cloud at $20.9B / +75% YoY vs the press
release's $17.7B / +48%).

The reconciliation predicate + tolerance are the SAME ones the DB-ingest gate
uses (``compute.segments.segment_sum_exceeds_revenue``), so a quarter this audit
flags is exactly the record ``extract_segment_facts()`` would drop — but the
audit runs against the RAW CACHE, catching contamination before the direct-JSON
readers (``execution/build_redesigned_dcf.py``, ``dcf_opus_assumptions.py``)
consume it.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import cast

from compute.segments import segment_sum_exceeds_revenue

# Quarterly segment caches audited for contamination. Annual rollups are
# excluded: FMP's annual figures reconcile to total revenue even when an
# individual quarter is corrupt (the GOOG case), so they don't carry the
# over-cap signature this audit keys on.
SEGMENT_SUFFIXES = ("product_segments_quarterly", "geo_segments_quarterly")


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


def segment_cache_present(data_dir: str, ticker: str) -> bool:
    """True when at least one product/geo segment cache file exists for the ticker."""
    t = ticker.upper()
    return any(
        os.path.exists(os.path.join(data_dir, f"{t}_{suffix}.json")) for suffix in SEGMENT_SUFFIXES
    )


def audit_ticker_cache(data_dir: str, ticker: str) -> list[SegmentFlag]:
    """Return one flag per over-cap segment record in the ticker's cache files.

    A quarter is flagged only when the income-statement cache carries a non-zero
    revenue for the same period_end to reconcile against — mirroring the ingest
    gate's "can't disprove without revenue, so accept" None branch.
    """
    ticker = ticker.upper()
    revenue_by_date = _revenue_by_period_end(data_dir, ticker)
    flags: list[SegmentFlag] = []
    for suffix in SEGMENT_SUFFIXES:
        recs = _load_json(os.path.join(data_dir, f"{ticker}_{suffix}.json"))
        if not recs:
            continue
        for rec in recs:
            date = str(rec.get("date") or "")
            data = rec.get("data")
            values = data.values() if isinstance(data, dict) else []
            revenue = revenue_by_date.get(date)
            if revenue is None or revenue == 0:
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
