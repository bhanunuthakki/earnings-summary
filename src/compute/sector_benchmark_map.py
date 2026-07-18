"""Owner-ratified FMP-industry -> benchmark-ETF proxy map
(docs/design/comparable_sets_bottoms_up.md §4).

Same pattern as ``ir_pipeline.ir_url_overrides``: a plain, version-controlled
dict, hand-edited, no DB table needed for something this small and this
rarely edited. Two distinct jobs live behind one row, per §4.1 — **do not
conflate them**:

- ``etf`` / ``sector_etf`` (this module) answer "which published index ETF
  tracks this industry/sector" for the **performance/relative-strength**
  line — a pure price-ratio question, read straight from that ETF ticker's
  own ``_price_chart_10y_div_adj.json`` once it's onboarded as
  ``list_type='etf'``.
- The **multiples benchmark** (industry/sector PE, EV/EBITDA, growth) is
  NEVER derived from the ETF's holdings (``directives/etf_data.md:16``: FMP
  ``/stable/etf/holdings`` is plan-gated, 402s tolerated, unreliable
  coverage). It is computed the same bottoms-up way as a per-ticker
  comparable set, scoped to the whole pool's `industry`/`sector` instead of
  a market-cap band around one subject (`comp_set_metrics_daily` rows with
  `scope_type='industry'`/`'sector'` — Phase 2, not built here). The ETF
  ticker never enters that computation's inputs.

Ratification flow for an unmapped industry (Phase 3, not built here): a
small, separate LLM purpose (`sector_benchmark_proposal`) proposes
`{etf, sector_etf, why}` for review; the owner reviews and hand-pastes the
ratified line into ``SECTOR_BENCHMARK_MAP`` below. Never auto-applied.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BenchmarkProxy:
    """One industry's benchmark-ETF proxy. ``etf`` is the tightest-fit
    published index ETF (may be ``None`` if none exists); ``sector_etf`` is
    the coarser GICS-sector fallback used when ``etf`` is ``None`` or the
    industry has no dedicated ETF wrapper."""

    etf: str | None
    sector_etf: str | None
    note: str = ""


# FMP profile.industry (exact string) -> benchmark proxy. Both `etf` and
# `sector_etf` must already be onboarded as list_type='etf' (so the cacher
# has price history for them) before a row here is useful — adding a row
# without onboarding the ticker gets a proxy with no price data; onboard
# first. Owner extends per-industry as tickers are onboarded; an unmapped
# industry falls back to `sector_etf`, then to no benchmark line at all
# (never fabricated).
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
    # ... owner extends per-industry as tickers are onboarded; unmapped
    # industry falls back to sector_etf, then to no benchmark line at all.
}


def get_benchmark_proxy(industry: str | None) -> BenchmarkProxy | None:
    """Return the ratified proxy for ``industry`` (exact-string match), or
    ``None`` when unmapped. Callers fall back to sector-level resolution
    (a separate lookup, since this map is keyed by industry only) or omit
    the benchmark line entirely — never fabricate a proxy."""
    if industry is None:
        return None
    return SECTOR_BENCHMARK_MAP.get(industry)
