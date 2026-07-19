"""Secondary consensus anchor: extend FMP forward years with yfinance growth.

FMP Starter's ``analyst-estimates`` truncates to 10 rows/call (history +
forward mixed), so multi-year DCF consensus anchoring often runs out of
forward years after 2-4. yfinance's free analysis tables carry the missing
out-year signal: next-fiscal-year revenue/EPS growth and Yahoo's long-term
(3-5y, EPS-based) growth consensus (``growth_estimates`` row ``LTG``).

This module extends a per-year FMP consensus dict with yfinance-DERIVED years
beyond the FMP horizon, under two hard rules (repo provenance discipline —
"log source per field, don't silently merge"):

- FMP years pass through VERBATIM. An extension year never overwrites,
  adjusts, or blends into an FMP year.
- Every year in the result is provenance-tagged (``source_by_year``:
  ``"fmp"`` | ``"yfinance"``), and the caller is expected to log the map
  (``build_redesigned_dcf.py`` emits a ``consensus_extension`` JSON event).

Extension math (documented approximations, all within the yfinance source):

- Revenue year ``y`` = revenue ``y-1`` x (1 + g_rev), where g_rev is the
  MORE CONSERVATIVE of Yahoo's +1y revenue growth and LTG (when both exist).
  LTG is EPS-based; using it as a revenue-growth ceiling is deliberately
  conservative for names whose margin still expands.
- Net income year ``y`` = NI ``y-1`` x (1 + g_ni), g_ni = LTG when present
  else +1y EPS growth. Assumes a stable share count (the standard consensus
  shorthand); EPS growth is the only NI-path signal Yahoo publishes.

Inputs come from the persisted ``data/historical/yfinance/<T>_yf_estimates.json``
written by ``execution/fetch_yf_estimates.py`` — NEVER a live network call from
the DCF build path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from models.yf_payloads import YF_PERIOD_LTG, YfEstimatesSnapshot

SOURCE_FMP = "fmp"
SOURCE_YFINANCE = "yfinance"

#: Never extend more than this many years past the FMP horizon — each extra
#: year compounds a single growth number; beyond ~3 the anchor is noise.
MAX_EXTRA_YEARS = 3

#: Growth sanity bounds — a Yahoo glitch (e.g. a 40x growth artifact on a
#: recently-listed name) must not compound into the anchor. Outside these
#: bounds the extension declines to run rather than anchoring to garbage.
_GROWTH_BOUNDS = (-0.50, 0.60)


@dataclass(frozen=True)
class YfGrowthInputs:
    """The three growth signals the extension can use, plus provenance."""

    revenue_growth_next: float | None  # revenue_estimate +1y implied YoY growth
    eps_growth_next: float | None  # earnings_estimate +1y implied YoY growth
    lt_growth: float | None  # growth_estimates LTG (3-5y, EPS-based)
    asof_date: str


def load_yf_growth(path: Path) -> YfGrowthInputs | None:
    """Read a persisted yfinance estimates snapshot -> growth inputs.
    Returns None when the file is absent/malformed or carries no usable
    growth signal — the caller then skips extension entirely (degrade,
    never guess)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        snap = YfEstimatesSnapshot.model_validate(raw)
    except Exception:
        return None
    rev_next = next((r.growth for r in snap.revenue_estimate if r.period == "+1y"), None)
    eps_next = next((r.growth for r in snap.earnings_estimate if r.period == "+1y"), None)
    ltg = next((r.stockTrend for r in snap.growth_estimates if r.period == YF_PERIOD_LTG), None)
    if rev_next is None and eps_next is None and ltg is None:
        return None
    return YfGrowthInputs(
        revenue_growth_next=rev_next,
        eps_growth_next=eps_next,
        lt_growth=ltg,
        asof_date=snap.asof_date,
    )


def _in_bounds(g: float) -> bool:
    lo, hi = _GROWTH_BOUNDS
    return lo <= g <= hi


def _revenue_growth(growth: YfGrowthInputs) -> float | None:
    """Conservative revenue extension rate: min(+1y revenue growth, LTG) when
    both exist, else whichever exists; None (no extension) out of bounds."""
    candidates = [g for g in (growth.revenue_growth_next, growth.lt_growth) if g is not None]
    if not candidates:
        return None
    g = min(candidates)
    return g if _in_bounds(g) else None


def _ni_growth(growth: YfGrowthInputs) -> float | None:
    """NI extension rate: LTG preferred (it IS an earnings-growth consensus),
    else +1y EPS growth; None out of bounds."""
    g = growth.lt_growth if growth.lt_growth is not None else growth.eps_growth_next
    if g is None:
        return None
    return g if _in_bounds(g) else None


def extend_consensus(
    cons_rev: dict[int, float],
    cons_ni: dict[int, float],
    fc_years: list[int],
    growth: YfGrowthInputs,
    *,
    max_extra_years: int = MAX_EXTRA_YEARS,
) -> tuple[dict[int, float], dict[int, float], dict[int, str]]:
    """Extend FMP consensus dicts with yfinance-derived out-years.

    Returns ``(rev_by_year, ni_by_year, source_by_year)``. FMP years are
    copied verbatim and tagged ``"fmp"``; extension years (contiguous years in
    ``fc_years`` immediately after the last FMP revenue year, at most
    ``max_extra_years``) compound the yfinance growth rate from the last FMP
    value and are tagged ``"yfinance"``. With no FMP base year or no usable
    growth rate, the inputs come back unchanged (all-"fmp" tags).
    """
    rev = dict(cons_rev)
    ni = dict(cons_ni)
    source: dict[int, str] = {y: SOURCE_FMP for y in set(rev) | set(ni)}
    if not rev:
        return rev, ni, source

    g_rev = _revenue_growth(growth)
    g_ni = _ni_growth(growth)
    last_fmp_year = max(rev)
    extension_years = [y for y in sorted(fc_years) if y > last_fmp_year][:max_extra_years]

    prev_rev = rev[last_fmp_year]
    prev_ni = ni.get(last_fmp_year)
    for offset, year in enumerate(extension_years, start=1):
        if year != last_fmp_year + offset:
            break  # only extend a contiguous run — no gap-jumping
        extended = False
        if g_rev is not None:
            prev_rev = prev_rev * (1.0 + g_rev)
            rev[year] = prev_rev
            extended = True
        if g_ni is not None and prev_ni is not None:
            prev_ni = prev_ni * (1.0 + g_ni)
            ni[year] = prev_ni
            extended = True
        if not extended:
            break
        source[year] = SOURCE_YFINANCE
    return rev, ni, source


def extension_event(
    ticker: str, source_by_year: dict[int, str], growth: YfGrowthInputs
) -> dict[str, object]:
    """The JSON-serializable provenance event the caller logs to stderr —
    per-year source tags plus the growth inputs used, so an extended anchor
    is never silent in the build log."""
    return {
        "event": "consensus_extension",
        "ticker": ticker,
        "yf_asof": growth.asof_date,
        "revenue_growth_next": growth.revenue_growth_next,
        "eps_growth_next": growth.eps_growth_next,
        "lt_growth": growth.lt_growth,
        "per_year_source": {str(y): s for y, s in sorted(source_by_year.items())},
        "yf_years": sorted(y for y, s in source_by_year.items() if s == SOURCE_YFINANCE),
    }
