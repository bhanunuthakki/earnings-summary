"""Value / size / momentum style loadings from free ETF-proxy return spreads.

``portfolio_risk.factor_exposure_rollup`` covers market beta (SPY), growth
tilt (QQQ - SPY), correlation crowding, and the 10Y rate leg — but value /
size / momentum need a factor-RETURN series the tracker doesn't expose, so
they were historically omitted rather than faked. This module supplies the
series and the loadings:

* each style factor is a LONG-minus-SHORT daily log-return spread of two
  liquid ETF proxies (``factor_proxies`` store — VTV-VUG for value-vs-growth,
  IWM-SPY for size, MTUM-SPY for momentum);
* each holding's loading is the univariate OLS beta of its daily log returns
  on that spread — the same univariate-beta idiom as
  ``macro_store.compute_sensitivities`` (an interpretable "tilt vs the
  spread"), deliberately NOT a joint multi-factor fit;
* the book read is the weight-renormalized mean over the names that HAVE the
  estimate, mirroring ``factor_exposure_rollup``'s coverage discipline, with
  the top |weight x loading| contributors named per leg.

The regression math is pure stdlib over already-loaded return series (tests
need no disk, no numpy). ``build_style_rollup_from_disk`` assembles the inputs
from the FMP price-chart cache (holdings) + the factor_proxies store (ETFs),
so the whole surface renders with the tracker OFFLINE.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from portfolio_risk import CrowdedName

__all__ = [
    "LOOKBACK_OBS",
    "MIN_OBS",
    "STYLE_FACTORS",
    "StyleFactorDef",
    "StyleFactorLeg",
    "StyleFactorRollup",
    "StyleLoading",
    "build_style_rollup",
    "build_style_rollup_from_disk",
    "factor_spread_returns",
    "regress_loading",
]

# Same window discipline as the next-dollar covariance (price_history):
# at most a year of common daily observations, and no estimate at all below
# ~6 months — a thin overlap prints noise, not a loading.
LOOKBACK_OBS = 252
MIN_OBS = 120


@dataclass(frozen=True, slots=True)
class StyleFactorDef:
    """One style factor as a long-minus-short ETF return spread."""

    key: str
    label: str
    long: str
    short: str
    spread_label: str  # display form, e.g. "VTV - VUG"


STYLE_FACTORS: tuple[StyleFactorDef, ...] = (
    StyleFactorDef(key="value", label="Value", long="VTV", short="VUG", spread_label="VTV - VUG"),
    StyleFactorDef(
        key="size", label="Size (small)", long="IWM", short="SPY", spread_label="IWM - SPY"
    ),
    StyleFactorDef(
        key="momentum", label="Momentum", long="MTUM", short="SPY", spread_label="MTUM - SPY"
    ),
)


def factor_spread_returns(
    proxy_returns: Mapping[str, Mapping[date, float]], factor: StyleFactorDef
) -> dict[date, float]:
    """The factor's daily long-minus-short return spread over the dates both
    proxy legs share; ``{}`` when either leg's series is absent."""
    long_r = proxy_returns.get(factor.long.upper())
    short_r = proxy_returns.get(factor.short.upper())
    if not long_r or not short_r:
        return {}
    return {d: long_r[d] - short_r[d] for d in long_r.keys() & short_r.keys()}


@dataclass(frozen=True, slots=True)
class StyleLoading:
    """One holding's univariate OLS fit against one factor spread."""

    beta: float
    r_squared: float
    n_obs: int


def regress_loading(
    asset_returns: Mapping[date, float],
    factor_returns: Mapping[date, float],
    *,
    min_obs: int = MIN_OBS,
    lookback_obs: int = LOOKBACK_OBS,
) -> StyleLoading | None:
    """OLS beta (with intercept) of the asset's daily returns on the factor
    spread over their latest ``lookback_obs`` common dates. ``None`` below
    ``min_obs`` common observations or on a degenerate (zero-variance) factor.
    """
    common = sorted(asset_returns.keys() & factor_returns.keys())[-lookback_obs:]
    if len(common) < min_obs:
        return None
    n = len(common)
    xs = [factor_returns[d] for d in common]
    ys = [asset_returns[d] for d in common]
    mean_x = math.fsum(xs) / n
    mean_y = math.fsum(ys) / n
    sxx = math.fsum((x - mean_x) ** 2 for x in xs)
    syy = math.fsum((y - mean_y) ** 2 for y in ys)
    sxy = math.fsum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    if sxx <= 0.0 or not math.isfinite(sxx):
        return None
    beta = sxy / sxx
    r_squared = (sxy * sxy) / (sxx * syy) if syy > 0.0 else 0.0
    return StyleLoading(beta=beta, r_squared=r_squared, n_obs=n)


@dataclass(slots=True)
class StyleFactorLeg:
    """The book's read on one style factor: the weight-renormalized mean beta
    over the names that have the estimate, with coverage + top contributors
    (``CrowdedName`` reused so the panel's contributor renderer applies)."""

    key: str
    label: str
    spread_label: str
    book_beta: float | None
    names_priced: int
    top: list[CrowdedName] = field(default_factory=list[CrowdedName])


@dataclass(slots=True)
class StyleFactorRollup:
    """All style legs plus the provenance a reader needs to trust (or
    discount) them: coverage, the proxy-series freshness stamp, and any proxy
    whose series is missing from the local store."""

    legs: list[StyleFactorLeg]
    names_total: int
    lookback_obs: int
    proxies_through: date | None  # stalest last-date across the proxies used
    missing_proxies: list[str] = field(default_factory=list[str])


def _weighted_mean(pairs: list[tuple[float, float]]) -> float | None:
    """Weight-renormalized mean of ``(weight, value)`` pairs; None if no weight.
    (Same contract as ``portfolio_risk._weighted_mean``.)"""
    total_w = sum(w for w, _v in pairs if w > 0)
    if total_w <= 0:
        return None
    return sum(w * v for w, v in pairs if w > 0) / total_w


def build_style_rollup(
    returns_by_ticker: Mapping[str, Mapping[date, float]],
    proxy_returns: Mapping[str, Mapping[date, float]],
    weights: Mapping[str, float] | None = None,
    *,
    top_n: int = 3,
) -> StyleFactorRollup | None:
    """Roll the per-holding style regressions into the book read.

    ``weights`` is ticker (upper) -> fraction-of-book; holdings missing from it
    (or an absent/empty mapping) fall back to equal weight over the holdings,
    matching the next-dollar model's degrade. Returns ``None`` when there are
    no holdings with returns or no factor spread can be built at all (nothing
    honest to show — the caller renders the fetch hint instead).
    """
    holdings = {t.upper(): rets for t, rets in returns_by_ticker.items() if rets}
    if not holdings:
        return None
    weights = weights or {}
    positive = {t: w for t, w in weights.items() if w > 0}
    equal_w = 1.0 / len(holdings)

    def weight_of(ticker: str) -> float:
        return positive.get(ticker, 0.0) if positive else equal_w

    missing = sorted(
        {
            etf.upper()
            for f in STYLE_FACTORS
            for etf in (f.long, f.short)
            if not proxy_returns.get(etf.upper())
        }
    )
    legs: list[StyleFactorLeg] = []
    used_proxies: set[str] = set()
    for f in STYLE_FACTORS:
        spread = factor_spread_returns(proxy_returns, f)
        if not spread:
            continue
        used_proxies.update((f.long.upper(), f.short.upper()))
        pairs: list[tuple[float, float]] = []
        contributors: list[CrowdedName] = []
        for t in sorted(holdings):
            loading = regress_loading(holdings[t], spread)
            if loading is None:
                continue
            w = weight_of(t)
            pairs.append((w, loading.beta))
            if w > 0:
                contributors.append(
                    CrowdedName(ticker=t, weight_pct=w * 100.0, loading=loading.beta)
                )
        contributors.sort(key=lambda c: abs(c.weight_pct * c.loading), reverse=True)
        legs.append(
            StyleFactorLeg(
                key=f.key,
                label=f.label,
                spread_label=f.spread_label,
                book_beta=_weighted_mean(pairs),
                names_priced=len(pairs),
                top=contributors[:top_n],
            )
        )
    if not legs:
        return None
    # The freshness stamp is the STALEST leg among the proxies actually used —
    # honest about the weakest series feeding the read.
    last_dates = [max(proxy_returns[p]) for p in used_proxies if proxy_returns.get(p)]
    return StyleFactorRollup(
        legs=legs,
        names_total=len(holdings),
        lookback_obs=LOOKBACK_OBS,
        proxies_through=min(last_dates) if last_dates else None,
        missing_proxies=missing,
    )


def build_style_rollup_from_disk(
    repo_root: Path,
    tickers: Sequence[str],
    weights: Mapping[str, float] | None = None,
) -> StyleFactorRollup | None:
    """Assemble the rollup from the on-disk substrates: holdings' daily returns
    from the FMP price-chart cache, factor spreads from the factor_proxies
    store. Pure disk reads — never the network — so the Risk panel renders this
    with the tracker down."""
    from allocation.price_history import daily_log_returns, load_daily_closes
    from factor_proxies import load_proxy_returns

    returns_by_ticker: dict[str, dict[date, float]] = {}
    for t in {t.upper() for t in tickers}:
        rets = daily_log_returns(load_daily_closes(t, repo_root))
        if rets:
            returns_by_ticker[t] = rets
    if not returns_by_ticker:
        return None
    return build_style_rollup(returns_by_ticker, load_proxy_returns(repo_root), weights)
