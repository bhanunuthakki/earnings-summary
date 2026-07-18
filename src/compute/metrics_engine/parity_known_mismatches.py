"""Expected-mismatch registry + tolerance bands for the parity harness.

A `(ticker, formula_key)` pair NOT in `KNOWN_MISMATCHES` that fails its
tolerance band is a real regression, not noise (docs/design/
bottoms_up_metrics_engine.md section 4). Phase 1's 15 metrics have no
FMP-vs-computed divergence expected beyond ordinary rounding/timing drift --
the registry starts empty and is extended the moment a real, understood
divergence shows up (e.g. the Phase-2 net_debt_strict-vs-FMP-net_debt case
the design doc names for AAPL).
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from models.facts import Unit

from .registry import MetricCategory


class ToleranceBand(BaseModel):
    """A computed-vs-FMP comparison passes if it clears EITHER bound
    (whichever is looser) -- `abs_bps` is an absolute bound on a
    percentage-point difference, `rel_pct` a relative bound on the ratio
    of the two values."""

    abs_bps: Decimal | None = None
    rel_pct: Decimal


# Per docs/design/bottoms_up_metrics_engine.md section 4:
#   Margins/growth/returns (%): +/-50 bps absolute OR +/-3% relative, whichever looser.
#   Leverage/liquidity ratios:  +/-5% relative (denominators can differ by
#                               rounding/currency conversion timing).
#   Per-share figures:          +/-1% relative (share-count timing drift).
#   Valuation multiples (Phase 3): +/-5% relative (price-snapshot timing).
_PERCENT_BAND = ToleranceBand(abs_bps=Decimal("50"), rel_pct=Decimal("3"))
_RATIO_BAND = ToleranceBand(rel_pct=Decimal("5"))
_PER_SHARE_BAND = ToleranceBand(rel_pct=Decimal("1"))
_VALUATION_BAND = ToleranceBand(rel_pct=Decimal("5"))

_CATEGORY_BANDS: dict[MetricCategory, ToleranceBand] = {
    MetricCategory.MARGIN: _PERCENT_BAND,
    MetricCategory.GROWTH: _PERCENT_BAND,
    MetricCategory.RETURNS: _PERCENT_BAND,
    # Phase 1's only EFFICIENCY formula (sbc_pct_revenue) is a percentage
    # output, so the category default is the percent band. Phase 2 adds
    # non-percent EFFICIENCY formulas (asset/receivables/inventory_turnover,
    # cash_conversion_cycle -- ratios/days, not percentages); `tolerance_for`
    # below overrides to _RATIO_BAND for those via the optional `unit` arg
    # rather than changing this category-only default (keeps the existing
    # Phase 1 call sites, which never pass `unit`, unaffected).
    MetricCategory.EFFICIENCY: _PERCENT_BAND,
    MetricCategory.LIQUIDITY: _RATIO_BAND,
    MetricCategory.LEVERAGE: _RATIO_BAND,
    MetricCategory.PER_SHARE: _PER_SHARE_BAND,
    MetricCategory.VALUATION: _VALUATION_BAND,
}


def tolerance_for(category: MetricCategory, *, unit: Unit | None = None) -> ToleranceBand:
    """Category-keyed lookup, with a unit-aware override for EFFICIENCY
    (see _CATEGORY_BANDS' comment) -- `unit=None` (the default) preserves
    the plain category-only lookup every Phase 1 call site relies on."""
    if category == MetricCategory.EFFICIENCY and unit is not None and unit != Unit.PERCENT:
        return _RATIO_BAND
    return _CATEGORY_BANDS[category]


def within_tolerance(
    category: MetricCategory, computed: Decimal, reference: Decimal, *, unit: Unit | None = None
) -> bool:
    """True iff `computed` clears `reference`'s tolerance band (either bound)."""
    band = tolerance_for(category, unit=unit)
    diff = abs(computed - reference)
    if band.abs_bps is not None and diff <= band.abs_bps / Decimal(100):
        return True
    if reference == 0:
        return diff == 0
    relative = diff / abs(reference) * Decimal(100)
    return relative <= band.rel_pct


# (ticker, formula_key) -> human-readable reason for an EXPECTED divergence
# from FMP's own ratios-ttm/key-metrics-ttm series. Empty in Phase 1 -- add
# an entry only after confirming the divergence is a documented method
# choice (e.g. net_debt_strict vs FMP's net_debt, which nets long-term
# marketable securities FMP includes and this engine's strict variant does
# not), never as a way to silence an unexplained failure.
KNOWN_MISMATCHES: dict[tuple[str, str], str] = {}
