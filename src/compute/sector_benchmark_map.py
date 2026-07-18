"""Sector/industry -> benchmark-proxy ETF map (docs/design/comparable_sets_bottoms_up.md
section 4).

Same pattern as ``ir_url_overrides.py`` / ``comparable_set_overrides.py``: a plain,
owner-ratified dict, no DB table needed for something this small and this rarely
edited. Not consumed by anything in Phase 1 (the doc's Phase-3 "Sector context" card
is the first reader) -- shipped now so the module exists and is reviewable/extendable
as the owner ratifies entries per `directives/` §4's proposal flow, ahead of the UI
work that will need it.

Two different jobs a benchmark proxy answers -- do not conflate them (see doc §4.1):
1. Performance/relative-strength: the ETF's own price series (``etf``), read directly
   from its already-cached ``_price_chart_10y_div_adj.json`` -- no gap, no computation.
2. Multiples/fundamentals benchmark: NOT derived from the ETF's holdings (FMP
   `/stable/etf/holdings` is plan-gated/unreliable, `directives/etf_data.md:16`).
   Computed the same bottoms-up way as a per-ticker comparable set, scoped to
   `industry`/`sector` across the whole pool (`comp_set_metrics_daily` rows with
   `scope_type='industry'`/`'sector'`, Phase 2). The ETF supplies the performance
   line only, never a multiples input.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BenchmarkProxy:
    """One industry's benchmark-proxy mapping.

    ``etf`` is the tightest-fit published index ETF for the industry itself;
    ``sector_etf`` is the coarser GICS-sector fallback used when either no
    industry-specific ETF exists (``etf=None``) or the industry has no entry
    at all here (unmapped -> no benchmark line, never fabricated).
    """

    etf: str | None
    sector_etf: str | None
    note: str | None = None


# FMP profile.industry (exact string) -> benchmark proxy. Both `etf` and
# `sector_etf` must already be onboarded as list_type='etf' so the cacher has
# price history for them -- adding a row here without onboarding the ETF ticker
# gets a proxy with no price data; onboard first.
SECTOR_BENCHMARK_MAP: dict[str, BenchmarkProxy] = {
    "Semiconductors": BenchmarkProxy(
        etf="SMH", sector_etf="XLK", note="also SOX index, no direct ETF wrapper on FMP"
    ),
    "Software - Application": BenchmarkProxy(etf="IGV", sector_etf="XLK"),
    "Software - Infrastructure": BenchmarkProxy(etf="IGV", sector_etf="XLK"),
    "Banks - Regional": BenchmarkProxy(etf="KRE", sector_etf="XLF"),
    "Banks - Diversified": BenchmarkProxy(etf="KBE", sector_etf="XLF"),
    "Insurance - Diversified": BenchmarkProxy(etf="KIE", sector_etf="XLF"),
    "Biotechnology": BenchmarkProxy(etf="XBI", sector_etf="XLV"),
    "Internet Retail": BenchmarkProxy(etf="IBUY", sector_etf="XLY"),
    "Credit Services": BenchmarkProxy(
        etf=None, sector_etf="XLF", note="no clean fintech-lending ETF; sector fallback only"
    ),
    # ... owner extends per-industry as tickers are onboarded; unmapped industry
    # falls back to no benchmark line at all (never fabricated).
}


def resolve_benchmark_etf(industry: str | None) -> str | None:
    """The best available benchmark ETF for ``industry``: the industry-specific
    ``etf`` if mapped and set, else that entry's ``sector_etf``, else ``None``
    for an unmapped industry (Phase-3 ratification queue territory -- never
    guessed here)."""
    if not industry:
        return None
    entry = SECTOR_BENCHMARK_MAP.get(industry)
    if entry is None:
        return None
    return entry.etf or entry.sector_etf
