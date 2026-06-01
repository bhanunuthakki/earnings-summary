"""Macro-series registry — 12 macro slugs the scenario engine depends on.

Each entry declares one OR more provider configs. The fetcher tries them in
order; the first one that returns parseable rows wins. If all fail, the
series is logged + skipped — the fetcher never raises and never blocks the
rest of the universe.

Provider config shape:
    {
      "kind":         "fmp_historical" | "fmp_treasury" | "fmp_economic" | "fmp_fx",
      "path":         "<endpoint path, relative to FMP_BASE>",
      "params":       {dict of query string params},
      "row_field":    optional jq-style path inside the response to the list,
      "date_key":     name of the date column in each row (default "date"),
      "value_key":    name of the value column in each row (default "close"),
      "source":       label persisted in macro_series.source (default "FMP")
    }

All providers are on FMP `/stable` (the legacy `/api/v3` `historical-price-full`
fallbacks were retired in the v3->stable migration — they 403 as "Legacy
Endpoint" for non-legacy accounts). A few symbols still live behind premium
tiers (see the ^SOX note below); when a series' provider 402/403s it is logged
+ skipped, fail-soft. The fetcher takes the first provider that parses; the
registry enumerates candidates in priority order. Add a new fallback by
extending the list (stable paths only); do NOT special-case in
fetch_macro_series.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def _empty_params() -> dict[str, str]:
    return {}


@dataclass(slots=True, frozen=True)
class ProviderSpec:
    """One candidate endpoint for a series. Tried in order, first hit wins."""

    kind: str
    path: str
    params: dict[str, str] = field(default_factory=_empty_params)
    row_field: str | None = None
    date_key: str = "date"
    value_key: str = "close"
    source: str = "FMP"


@dataclass(slots=True, frozen=True)
class SeriesSpec:
    """One macro series — its identity, what it measures, and where to fetch."""

    series_id: str
    display: str
    units: str  # e.g. "pct", "level", "fx_rate", "usd_per_bbl"
    category: str  # "rates" | "fx" | "commodity" | "index"
    providers: tuple[ProviderSpec, ...]


# ---------------------------------------------------------------------------
# Registry — 12 series mandated by the spec
# ---------------------------------------------------------------------------

# Notes on endpoint choices:
#   - Rates: FMP exposes treasury yields at /stable/treasury-rates. Effective
#     fed funds is best-effort from FMP's economic indicator endpoint; we list
#     a 13W T-bill fallback as a proxy when the EFFR series is not in the
#     plan.
#   - FX: USDxxx pairs flow through FMP's historical-price-eod feed which
#     accepts a symbol like "USDBRL" and returns OHLC. The `close` field
#     is the daily fix.
#   - Commodities: Brent (BZUSD), Copper (HGUSD), Gold (GCUSD) on FMP's
#     stable commodities feed. Premium-only on some accounts; the
#     index-quote endpoint can serve as a free fallback.
#   - Indices: ^SOX (Philadelphia Semi) and ^VIX trade via FMP's historical
#     stock price endpoint — symbols carry the caret prefix. NOTE (2026-06-01
#     stable probe): ^VIX resolves on /stable, but ^SOX returns 402 "Premium
#     Query Parameter" on /stable AND its /api/v3 feed is a dead Legacy Endpoint
#     (403), so the `sox` series has no working FMP source on the current
#     subscription and fail-softs (logged + skipped) until the tier includes it.


REGISTRY: dict[str, SeriesSpec] = {
    "fed_funds": SeriesSpec(
        series_id="fed_funds",
        display="Effective Federal Funds Rate",
        units="pct",
        category="rates",
        providers=(
            ProviderSpec(
                kind="fmp_economic",
                path="economic-indicators",
                params={"name": "federalFunds"},
                value_key="value",
            ),
            ProviderSpec(
                kind="fmp_treasury",
                path="treasury-rates",
                params={},
                value_key="month1",
            ),
        ),
    ),
    "us_10y": SeriesSpec(
        series_id="us_10y",
        display="US 10-Year Treasury Yield",
        units="pct",
        category="rates",
        providers=(
            ProviderSpec(
                kind="fmp_treasury",
                path="treasury-rates",
                params={},
                value_key="year10",
            ),
        ),
    ),
    "vix": SeriesSpec(
        series_id="vix",
        display="CBOE Volatility Index",
        units="level",
        category="index",
        providers=(
            ProviderSpec(
                kind="fmp_historical",
                path="historical-price-eod/full",
                params={"symbol": "^VIX"},
            ),
        ),
    ),
    "usd_brl": SeriesSpec(
        series_id="usd_brl",
        display="USD / BRL exchange rate",
        units="fx_rate",
        category="fx",
        providers=(
            ProviderSpec(
                kind="fmp_fx",
                path="historical-price-eod/full",
                params={"symbol": "USDBRL"},
            ),
        ),
    ),
    "usd_inr": SeriesSpec(
        series_id="usd_inr",
        display="USD / INR exchange rate",
        units="fx_rate",
        category="fx",
        providers=(
            ProviderSpec(
                kind="fmp_fx",
                path="historical-price-eod/full",
                params={"symbol": "USDINR"},
            ),
        ),
    ),
    "usd_eur": SeriesSpec(
        series_id="usd_eur",
        display="USD / EUR exchange rate",
        units="fx_rate",
        category="fx",
        providers=(
            ProviderSpec(
                kind="fmp_fx",
                path="historical-price-eod/full",
                params={"symbol": "USDEUR"},
            ),
        ),
    ),
    "usd_cad": SeriesSpec(
        series_id="usd_cad",
        display="USD / CAD exchange rate",
        units="fx_rate",
        category="fx",
        providers=(
            ProviderSpec(
                kind="fmp_fx",
                path="historical-price-eod/full",
                params={"symbol": "USDCAD"},
            ),
        ),
    ),
    "usd_twd": SeriesSpec(
        series_id="usd_twd",
        display="USD / TWD exchange rate",
        units="fx_rate",
        category="fx",
        providers=(
            ProviderSpec(
                kind="fmp_fx",
                path="historical-price-eod/full",
                params={"symbol": "USDTWD"},
            ),
        ),
    ),
    "brent": SeriesSpec(
        series_id="brent",
        display="Brent Crude (USD/bbl)",
        units="usd_per_bbl",
        category="commodity",
        providers=(
            ProviderSpec(
                kind="fmp_historical",
                path="historical-price-eod/full",
                params={"symbol": "BZUSD"},
            ),
        ),
    ),
    "copper": SeriesSpec(
        series_id="copper",
        display="Copper futures (USD/lb)",
        units="usd_per_lb",
        category="commodity",
        providers=(
            ProviderSpec(
                kind="fmp_historical",
                path="historical-price-eod/full",
                params={"symbol": "HGUSD"},
            ),
        ),
    ),
    "gold": SeriesSpec(
        series_id="gold",
        display="Gold (USD/oz)",
        units="usd_per_oz",
        category="commodity",
        providers=(
            ProviderSpec(
                kind="fmp_historical",
                path="historical-price-eod/full",
                params={"symbol": "GCUSD"},
            ),
        ),
    ),
    "sox": SeriesSpec(
        series_id="sox",
        display="Philadelphia Semiconductor Index",
        units="level",
        category="index",
        providers=(
            ProviderSpec(
                kind="fmp_historical",
                path="historical-price-eod/full",
                params={"symbol": "^SOX"},
            ),
        ),
    ),
}


def all_series_ids() -> list[str]:
    """Series ids in registry order."""
    return list(REGISTRY.keys())


def get(series_id: str) -> SeriesSpec | None:
    return REGISTRY.get(series_id)


def by_category(category: str) -> list[SeriesSpec]:
    return [s for s in REGISTRY.values() if s.category == category]
