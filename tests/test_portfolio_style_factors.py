"""Style-factor loadings (value/size/momentum via ETF-proxy spreads) — the
pure regression + rollup math, and the disk assembler over fixture caches."""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path

from factor_proxies import store_proxy_series
from portfolio_style_factors import (
    STYLE_FACTORS,
    StyleFactorDef,
    build_style_rollup,
    build_style_rollup_from_disk,
    factor_spread_returns,
    regress_loading,
)

# ---------------------------------------------------------------------------
# Synthetic series helpers — deterministic, no randomness.
# ---------------------------------------------------------------------------


def _trading_days(n: int, start: date = date(2025, 1, 6)) -> list[date]:
    """n consecutive weekdays from start (a Monday)."""
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _factor_series(days: list[date]) -> dict[date, float]:
    """A deterministic, zero-drift-ish oscillating factor return series."""
    return {d: 0.01 * math.sin(i * 0.7) + 0.002 * math.cos(i * 1.3) for i, d in enumerate(days)}


def _asset_from_factor(
    factor: dict[date, float], beta: float, *, alpha: float = 0.0003
) -> dict[date, float]:
    return {d: alpha + beta * v for d, v in factor.items()}


def test_regress_loading_recovers_known_beta() -> None:
    days = _trading_days(200)
    factor = _factor_series(days)
    asset = _asset_from_factor(factor, 0.8)
    loading = regress_loading(asset, factor)
    assert loading is not None
    assert abs(loading.beta - 0.8) < 1e-9
    assert loading.r_squared > 0.999  # noiseless construction
    assert loading.n_obs == 200


def test_regress_loading_respects_min_obs_and_lookback() -> None:
    days = _trading_days(400)
    factor = _factor_series(days)
    asset = _asset_from_factor(factor, 1.5)
    thin = {d: asset[d] for d in days[:100]}
    assert regress_loading(thin, factor) is None  # below MIN_OBS
    capped = regress_loading(asset, factor)
    assert capped is not None
    assert capped.n_obs == 252  # the lookback caps the window


def test_regress_loading_degenerate_factor_is_none() -> None:
    days = _trading_days(150)
    factor = dict.fromkeys(days, 0.0)  # zero variance
    asset = _factor_series(days)
    assert regress_loading(asset, factor) is None


def test_regress_loading_nan_in_asset_series_returns_none() -> None:
    """A single NaN from a price-gap artifact in the cached asset series must
    not silently poison the beta via math.fsum (which propagates NaN without
    raising) — the whole window is rejected instead."""
    days = _trading_days(150)
    factor = _factor_series(days)
    asset = _asset_from_factor(factor, 0.8)
    asset[days[10]] = math.nan
    assert regress_loading(asset, factor) is None


def test_regress_loading_inf_in_factor_series_returns_none() -> None:
    """Same contamination guard, on the factor leg instead of the asset leg."""
    days = _trading_days(150)
    factor = _factor_series(days)
    asset = _asset_from_factor(factor, 0.8)  # clean asset series, derived first
    factor[days[75]] = math.inf
    assert regress_loading(asset, factor) is None


def test_factor_spread_returns_intersects_and_requires_both_legs() -> None:
    days = _trading_days(10)
    f = StyleFactorDef(key="t", label="T", long="AAA", short="BBB", spread_label="AAA - BBB")
    long_r = {d: 0.01 for d in days}
    short_r = {d: 0.004 for d in days[2:]}
    spread = factor_spread_returns({"AAA": long_r, "BBB": short_r}, f)
    assert set(spread) == set(days[2:])
    assert all(abs(v - 0.006) < 1e-12 for v in spread.values())
    assert factor_spread_returns({"AAA": long_r}, f) == {}


# ---------------------------------------------------------------------------
# Rollup
# ---------------------------------------------------------------------------


def _full_proxy_returns(days: list[date]) -> dict[date, float]:
    return _factor_series(days)


def _proxies_for_all_factors(days: list[date]) -> dict[str, dict[date, float]]:
    """Proxy series such that each factor spread is a distinct known series."""
    base = _factor_series(days)
    spy = {d: 0.001 for d in days}
    # value spread = base; size spread = 2*base; momentum spread = -base.
    return {
        "SPY": spy,
        "VUG": {d: 0.002 for d in days},
        "VTV": {d: 0.002 + base[d] for d in days},
        "IWM": {d: spy[d] + 2.0 * base[d] for d in days},
        "MTUM": {d: spy[d] - base[d] for d in days},
    }


def test_build_style_rollup_weights_and_tops() -> None:
    days = _trading_days(252)
    proxies = _proxies_for_all_factors(days)
    base = _factor_series(days)
    holdings = {
        "AAA": {d: 1.0 * base[d] for d in days},  # value beta 1, size 0.5, mom -1
        "BBB": {d: -1.0 * base[d] for d in days},
    }
    rollup = build_style_rollup(holdings, proxies, {"AAA": 0.75, "BBB": 0.25})
    assert rollup is not None
    assert rollup.names_total == 2
    assert rollup.missing_proxies == []
    assert rollup.proxies_through == days[-1]
    legs = {leg.key: leg for leg in rollup.legs}
    assert set(legs) == {"value", "size", "momentum"}
    # value: 0.75*1 + 0.25*(-1) = 0.5; size betas halve; momentum negates.
    value_beta = legs["value"].book_beta
    assert value_beta is not None and abs(value_beta - 0.5) < 1e-9
    size_beta = legs["size"].book_beta
    assert size_beta is not None and abs(size_beta - 0.25) < 1e-9
    mom_beta = legs["momentum"].book_beta
    assert mom_beta is not None and abs(mom_beta - (-0.5)) < 1e-9
    # Top contributor on the value leg is the 75% name.
    assert legs["value"].top[0].ticker == "AAA"
    assert abs(legs["value"].top[0].weight_pct - 75.0) < 1e-9


def test_build_style_rollup_equal_weight_fallback_and_coverage() -> None:
    days = _trading_days(252)
    proxies = _proxies_for_all_factors(days)
    base = _factor_series(days)
    holdings = {
        "AAA": {d: base[d] for d in days},
        "BBB": {d: base[d] for d in days[:60]},  # too thin — dropped from legs
    }
    rollup = build_style_rollup(holdings, proxies, weights=None)
    assert rollup is not None
    legs = {leg.key: leg for leg in rollup.legs}
    assert legs["value"].names_priced == 1  # BBB below MIN_OBS
    v = legs["value"].book_beta
    assert v is not None and abs(v - 1.0) < 1e-9  # renormalized over AAA alone


def test_build_style_rollup_missing_proxy_drops_leg_and_reports() -> None:
    days = _trading_days(252)
    proxies = _proxies_for_all_factors(days)
    del proxies["MTUM"]
    base = _factor_series(days)
    holdings = {"AAA": {d: base[d] for d in days}}
    rollup = build_style_rollup(holdings, proxies, {"AAA": 1.0})
    assert rollup is not None
    assert {leg.key for leg in rollup.legs} == {"value", "size"}
    assert rollup.missing_proxies == ["MTUM"]


def test_build_style_rollup_none_without_holdings_or_proxies() -> None:
    days = _trading_days(252)
    proxies = _proxies_for_all_factors(days)
    assert build_style_rollup({}, proxies, {}) is None
    base = _factor_series(days)
    holdings = {"AAA": {d: base[d] for d in days}}
    assert build_style_rollup(holdings, {}, {"AAA": 1.0}) is None


# ---------------------------------------------------------------------------
# Disk assembler — FMP price-chart fixtures + the factor_proxies store.
# ---------------------------------------------------------------------------


def _write_price_chart(repo_root: Path, ticker: str, closes: list[tuple[date, float]]) -> None:
    fmp = repo_root / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    rows = [{"date": d.isoformat(), "adjClose": v} for d, v in closes]
    (fmp / f"{ticker}_price_chart_10y_div_adj.json").write_text(json.dumps(rows), encoding="utf-8")


def test_build_style_rollup_from_disk(tmp_path: Path) -> None:
    days = _trading_days(300)
    base = _factor_series(days)
    # Holding price path: cumulative product of exp(returns) — beta 1 on value.
    level = 100.0
    closes: list[tuple[date, float]] = []
    for d in days:
        level *= math.exp(base[d])
        closes.append((d, level))
    _write_price_chart(tmp_path, "AAA", closes)
    # Proxies: VTV compounds the same spread over a flat VUG.
    for etf, rets in _proxies_for_all_factors(days).items():
        lvl = 50.0
        series: list[tuple[date, float]] = []
        for d in days:
            lvl *= math.exp(rets[d])
            series.append((d, lvl))
        store_proxy_series(tmp_path, etf, series)
    rollup = build_style_rollup_from_disk(tmp_path, ["AAA"], {"AAA": 1.0})
    assert rollup is not None
    legs = {leg.key: leg for leg in rollup.legs}
    v = legs["value"].book_beta
    assert v is not None and abs(v - 1.0) < 1e-6
    assert rollup.proxies_through == days[-1]


def test_build_style_rollup_from_disk_no_prices(tmp_path: Path) -> None:
    assert build_style_rollup_from_disk(tmp_path, ["ZZZ"], {"ZZZ": 1.0}) is None


def test_style_factor_registry_shape() -> None:
    """The registry stays value/size/momentum over the 5 stored proxies."""
    assert [f.key for f in STYLE_FACTORS] == ["value", "size", "momentum"]
    etfs = {e for f in STYLE_FACTORS for e in (f.long, f.short)}
    assert etfs == {"VTV", "VUG", "IWM", "SPY", "MTUM"}
