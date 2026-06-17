"""Base-year segment-coverage guard for the redesigned per-segment DCF.

``execution/build_redesigned_dcf.py`` models a company as the sum of its FMP product
segments, deriving the modeled set from the *latest quarter's* segmentation. When FMP
drops a chunk of those segments in the latest quarter (or a quarter goes missing from
the segmentation file), the modeled set covers only a fraction of the company — and the
whole DCF (base revenue, every forecast year, the terminal value) is then built on that
fraction, producing a fair value that's off by roughly the missing-revenue fraction.

The worked example was VEEV (2026-06): FMP's FY2026 Q4 dropped both "Veeva Research and
Development" segments (the larger ~55% half) and FY2026 Q3 was missing entirely, so the
modeled base revenue summed to ~$1.45B vs the income statement's FY2026 total of ~$3.20B
(~45% coverage) and fair value came out at $110.89/share instead of the consensus-anchored
~$229.91/share.

The bare ``len(prod) < 2`` / ``seg_base_total <= 0`` checks the builder shipped with only
caught *zero* coverage; this module generalises them to also catch *partial* coverage by
comparing the modeled segment revenue to the income statement's base-year total.

Falling back is always safe on the revenue level: when the builder downgrades to a single
whole-company line it rebuilds revenue from the *complete* income statement and anchors the
forecast to consensus revenue + consensus net income. The only thing lost is per-segment
growth differentiation — so we fall back generously rather than ship a fraction-of-company
valuation.

This is the single, testable decision point so the builder stays a one-line call and the
guard can be unit-tested without running the whole top-level build script.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# Modeled segments must cover at least this fraction of the base-year income-statement
# revenue, else fall back to a whole-company build. Deliberately generous: a few percent
# of unallocated / "Corporate & eliminations" revenue that segments legitimately omit must
# NOT trip the guard, but a dropped ~15%+ segment (the contamination signature) must.
COVERAGE_FLOOR = 0.85

# Sentinel single "segment" the builder models when it falls back to whole-company.
TOTAL_COMPANY = "Total company"


def _num(v: object) -> float | None:
    """Numeric coercion that, like the builder's ``isinstance(v, (int, float))`` checks,
    accepts ints/floats but rejects everything else (bool is excluded — a stray boolean
    must never be summed as 0/1 into a revenue total)."""
    if isinstance(v, bool):
        return None
    return float(v) if isinstance(v, (int, float)) else None


@dataclass(frozen=True)
class SegmentCoverage:
    """The product-segment modeling decision for one ticker's base fiscal year."""

    prod: list[str]  # modeled product segments (== [TOTAL_COMPANY] when single_seg)
    single_seg: bool  # True => model the whole company on one income-statement revenue line
    reason: str | None  # why whole-company was forced, or None when a per-segment build stands
    coverage: float | None  # seg_base_total / income_base_total, or None when no income base
    seg_base_total: float  # base-FY product-segment revenue sum (raw FMP units)
    income_base_total: float  # base-FY income-statement revenue (raw FMP units)


def resolve_product_segments(
    pseg_i: Mapping[tuple[int, str], Mapping[str, object] | None],
    inc_i: Mapping[tuple[int, str], Mapping[str, object] | None],
    periods: Sequence[str],
    base_fy: int,
    latest_key: tuple[int, str],
    *,
    floor: float = COVERAGE_FLOOR,
) -> SegmentCoverage:
    """Derive the modeled product segments and decide whether to fall back to whole-company.

    Mirrors the builder exactly:

    * ``prod`` = the segments carrying numeric revenue in the LATEST quarter
      (``latest_key``), ordered largest-first — this is the "current reporting structure"
      that drops legacy segment names only present in old columns.
    * ``seg_base_total`` sums those segments' revenue across the base fiscal year
      (every period in ``periods`` for ``base_fy``).
    * ``income_base_total`` is the income statement's base-FY revenue (same periods).

    Falls back to a single ``TOTAL_COMPANY`` line when:

    1. there are <2 segments (no per-segment model to build), OR
    2. the reported segments carry no base-year revenue (FMP dropped segment disclosure for
       a stretch of years — e.g. LITE FY2025 at zero), OR
    3. the segments cover materially less than the base-year income total — the partial
       contamination case the bare checks missed (VEEV FY2026 ~45% coverage).

    Inputs use raw FMP units (not /1e6); the ratio is unitless. ``pseg_i`` / ``inc_i`` are
    the builder's ``(fiscal_year, period) -> record`` indexes, with active fact_overrides
    already applied to the segment cache upstream.
    """
    latest_p = pseg_i.get(latest_key) or {}
    latest_nums: dict[str, float] = {
        s: f for s, v in latest_p.items() if (f := _num(v)) is not None
    }
    prod = sorted(latest_nums, key=lambda s: -latest_nums[s])

    seg_base_total = 0.0
    for p in periods:
        rec = pseg_i.get((base_fy, p)) or {}
        for s in prod:
            n = _num(rec.get(s))
            if n is not None:
                seg_base_total += n

    income_base_total = 0.0
    for p in periods:
        rec = inc_i.get((base_fy, p)) or {}
        n = _num(rec.get("revenue"))
        if n is not None:
            income_base_total += n

    coverage = (seg_base_total / income_base_total) if income_base_total > 0 else None
    reason: str | None = None
    if len(prod) < 2:
        reason = f"<2 segments ({len(prod)})"
    elif seg_base_total <= 0:
        reason = "zero base-year segment revenue"
    elif coverage is not None and coverage < floor:
        reason = f"coverage={coverage:.2f} < floor={floor:.2f}"

    single_seg = reason is not None
    return SegmentCoverage(
        prod=[TOTAL_COMPANY] if single_seg else prod,
        single_seg=single_seg,
        reason=reason,
        coverage=coverage,
        seg_base_total=seg_base_total,
        income_base_total=income_base_total,
    )
