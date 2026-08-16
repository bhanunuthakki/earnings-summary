"""Tests for the sensitivity-computation math (no DB).

Uses synthetic daily series — one ticker, one or more macro series — and
checks that the recovered beta is close to the underlying coefficient
used to generate the ticker series.
"""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

import pytest

from macro_store import (
    _ols_beta,  # type: ignore[reportPrivateUsage]
    _weekly_returns,  # type: ignore[reportPrivateUsage]
    compute_sensitivities,
)


def _daily_dates(n_days: int, end: date | None = None) -> list[date]:
    end = end or date(2026, 5, 25)
    return [end - timedelta(days=n_days - 1 - i) for i in range(n_days)]


def _gen_series_from_returns(rs: list[float], start: float = 100.0) -> list[float]:
    """Compose a level series from log-returns (level[i+1] = level[i] * exp(ret[i]))."""
    levels = [start]
    for r in rs:
        levels.append(levels[-1] * math.exp(r))
    return levels


def test_weekly_returns_downsample_to_iso_weeks() -> None:
    dates = _daily_dates(28)  # spans 4-5 ISO weeks depending on calendar boundary
    levels = [100.0 * (1.005) ** i for i in range(28)]
    rets = _weekly_returns(list(zip(dates, levels)))
    # 28 days touches at most 5 ISO weeks → returns are (n_weeks - 1) at most.
    assert 2 <= len(rets) <= 4
    # All returns should be positive (monotonic upward series)
    assert all(r > 0 for (_d, r) in rets)


def test_ols_beta_recovers_one_for_identity() -> None:
    xs = [0.01, 0.02, -0.01, 0.005, 0.0, -0.02, 0.015, 0.008, -0.005, 0.012, 0.003, 0.018]
    ys = list(xs)  # y = 1.0 * x + 0
    beta, r_sq, n = _ols_beta(xs, ys)
    assert beta == pytest.approx(1.0, abs=1e-9)
    assert r_sq == pytest.approx(1.0, abs=1e-9)
    assert n == 12


def test_ols_beta_recovers_two() -> None:
    xs = [0.01, 0.02, -0.01, 0.005, 0.0, -0.02, 0.015, 0.008, -0.005, 0.012, 0.003, 0.018]
    ys = [2.0 * x for x in xs]
    beta, r_sq, _ = _ols_beta(xs, ys)
    assert beta == pytest.approx(2.0, abs=1e-9)
    assert r_sq == pytest.approx(1.0, abs=1e-9)


def test_ols_handles_zero_variance() -> None:
    xs = [0.0] * 8
    ys = [0.01 * i for i in range(8)]
    beta, r_sq, _ = _ols_beta(xs, ys)
    assert beta == 0.0
    assert r_sq == 0.0


def test_compute_sensitivities_recovers_synthetic_beta() -> None:
    """End-to-end synthetic case: ticker = 0.6 × macro + noise, lookback 1 year."""
    rng = random.Random(42)
    n = 365
    dates = _daily_dates(n)
    # Macro daily returns ~ N(0, 0.005). Build levels from them.
    macro_returns = [rng.gauss(0.0, 0.005) for _ in range(n - 1)]
    macro_levels = _gen_series_from_returns(macro_returns, start=4.0)
    # Ticker: 0.6x sensitivity to macro + small idiosyncratic noise.
    tkr_returns = [0.6 * r + rng.gauss(0.0, 0.001) for r in macro_returns]
    tkr_levels = _gen_series_from_returns(tkr_returns, start=100.0)

    ticker_prices = list(zip(dates, tkr_levels))
    series_data = {"macro_x": list(zip(dates, macro_levels))}

    results = compute_sensitivities(
        ticker_prices=ticker_prices,
        series_lookups=series_data,
        lookback_days=365,
        min_observations=12,
    )
    assert "macro_x" in results
    beta, r_sq, n_obs = results["macro_x"]
    # Synthetic 0.6 beta — tolerance ~0.15 absolute for the weekly-resample noise.
    assert beta == pytest.approx(0.6, abs=0.15)
    # Should be a high-fit relationship.
    assert r_sq > 0.6
    assert n_obs >= 40  # ~52 weeks - a few drops for ISO bucketing


def test_compute_sensitivities_skips_short_windows() -> None:
    """A series with too few overlapping weeks should be dropped, not returned."""
    dates_long = _daily_dates(365)
    dates_short = _daily_dates(28)  # only ~4 weeks
    tkr_levels = [100.0 * (1.001) ** i for i in range(365)]
    macro_short = [3.0 * (1.0005) ** i for i in range(28)]
    results = compute_sensitivities(
        ticker_prices=list(zip(dates_long, tkr_levels)),
        series_lookups={"short_series": list(zip(dates_short, macro_short))},
        lookback_days=365,
        min_observations=20,
    )
    # min_observations=20 weeks but short_series gives ~3 — should be skipped.
    assert "short_series" not in results


def test_compute_sensitivities_empty_inputs_return_empty() -> None:
    assert compute_sensitivities(ticker_prices=[], series_lookups={"x": []}) == {}
    # Ticker present but no series.
    dates = _daily_dates(60)
    levels = [100.0] * 60
    assert (
        compute_sensitivities(
            ticker_prices=list(zip(dates, levels)),
            series_lookups={},
        )
        == {}
    )


def test_compute_sensitivities_multiple_series() -> None:
    """Two macro series with different real betas, recovered independently."""
    rng = random.Random(7)
    n = 365
    dates = _daily_dates(n)
    macro_a_rets = [rng.gauss(0.0, 0.004) for _ in range(n - 1)]
    macro_b_rets = [rng.gauss(0.0, 0.003) for _ in range(n - 1)]
    macro_a_lvl = _gen_series_from_returns(macro_a_rets, start=4.0)
    macro_b_lvl = _gen_series_from_returns(macro_b_rets, start=80.0)
    tkr_rets = [
        0.4 * a + (-0.8) * b + rng.gauss(0.0, 0.001) for a, b in zip(macro_a_rets, macro_b_rets)
    ]
    tkr_lvl = _gen_series_from_returns(tkr_rets, start=100.0)

    out = compute_sensitivities(
        ticker_prices=list(zip(dates, tkr_lvl)),
        series_lookups={
            "macro_a": list(zip(dates, macro_a_lvl)),
            "macro_b": list(zip(dates, macro_b_lvl)),
        },
        lookback_days=365,
    )
    # The univariate regressions for each macro should pick up its own
    # signal but be confounded by the OTHER macro — so betas won't match
    # exactly. Direction + sign should be right, though.
    assert "macro_a" in out and "macro_b" in out
    beta_a, _r_a, _n_a = out["macro_a"]
    beta_b, _r_b, _n_b = out["macro_b"]
    assert beta_a > 0  # ticker loads positively on macro_a
    assert beta_b < 0  # negatively on macro_b


def test_sensitivity_metric_version_constant() -> None:
    from macro_store import SENSITIVITY_METRIC_VERSION

    assert SENSITIVITY_METRIC_VERSION == "v2_rate_diff"


def test_weekly_returns_rate_first_difference() -> None:
    dates = _daily_dates(21)
    # Yield series: 4.00, 4.10, 4.25 (percentage points)
    yields = [4.00 + 0.05 * i for i in range(21)]
    diffs = _weekly_returns(list(zip(dates, yields)), is_rate_diff=True)
    assert len(diffs) >= 2
    # All differences should be positive and expressed in percentage points (~0.25 to 0.35 pp)
    assert all(d > 0 for (_dt, d) in diffs)
    # Values should be around (7 * 0.05) = 0.35 percentage points, not log ratios
    assert any(0.20 <= d <= 0.50 for (_dt, d) in diffs)


def test_compute_sensitivities_for_us_10y_uses_first_difference() -> None:
    """For us_10y, sensitivity should represent expected % stock return per +1.0% yield move."""
    rng = random.Random(99)
    n = 365
    dates = _daily_dates(n)

    # 10Y yield in percent (e.g. starts at 4.0%, moves in bps daily ~ N(0, 0.04%))
    yield_diffs = [rng.gauss(0.0, 0.04) for _ in range(n - 1)]
    yield_levels = [4.0]
    for d in yield_diffs:
        yield_levels.append(max(0.5, yield_levels[-1] + d))

    # Ticker: -5.0 beta (i.e. -5% return for +1.0% yield increase)
    # Weekly stock return = -5.0 * weekly_yield_diff + noise
    # We construct daily stock prices reflecting this
    tkr_returns = [-5.0 * d + rng.gauss(0.0, 0.005) for d in yield_diffs]
    tkr_levels = _gen_series_from_returns(tkr_returns, start=100.0)

    out = compute_sensitivities(
        ticker_prices=list(zip(dates, tkr_levels)),
        series_lookups={"us_10y": list(zip(dates, yield_levels))},
        lookback_days=365,
    )

    assert "us_10y" in out
    beta, r_sq, n_obs = out["us_10y"]
    # Should recover approximately -5.0
    assert beta == pytest.approx(-5.0, abs=1.0)
    assert r_sq > 0.4
    assert n_obs >= 40
