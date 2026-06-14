"""Pure whole-book risk derivations (L5): drawdown + factor/style roll-up.

Both functions are pure transforms over the tracker's already-parsed payloads —
no network, no DB. These pin the math the Risk cockpit renders: the underwater
curve / trough / time-to-recovery from a cumulative TWR series, and the
value-weighted factor loadings (with renormalization over the priced names)
from the per-ticker correlation/beta rows.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from integrations.portfolio_tracker_client import (  # noqa: E402
    PerformancePoint,
    PositionCorrelationRow,
)
from portfolio_risk import (  # noqa: E402
    compute_drawdown,
    factor_exposure_rollup,
)


def _pt(date: str, ret: float | None) -> PerformancePoint:
    return PerformancePoint(
        date=date,
        portfolio_return_pct=ret,
        spy_return_pct=None,
        qqq_return_pct=None,
        policy_return_pct=None,
    )


def _row(
    ticker: str,
    weight: float,
    *,
    beta_spy: float | None,
    beta_qqq: float | None,
    corr_spy: float | None,
    sample: int = 250,
) -> PositionCorrelationRow:
    return PositionCorrelationRow(
        security_id=None,
        ticker=ticker,
        name=ticker,
        value=weight,
        weight_pct=weight,
        sample_size=sample,
        correlation_spy=corr_spy,
        beta_spy=beta_spy,
        correlation_qqq=None,
        beta_qqq=beta_qqq,
        correlation_policy=None,
        beta_policy=None,
    )


# ----- drawdown -----


def test_drawdown_recovers_to_a_new_high() -> None:
    # equity: 1.00, 1.10, 1.05, 0.98, 1.08, 1.12 → trough at d4, recovers at d6.
    points = [
        _pt("2026-01-01", 0.0),
        _pt("2026-02-01", 10.0),
        _pt("2026-03-01", 5.0),
        _pt("2026-04-01", -2.0),
        _pt("2026-05-01", 8.0),
        _pt("2026-06-01", 12.0),
    ]
    dd = compute_drawdown(points)
    assert dd is not None
    # 0.98 / 1.10 - 1 = -10.909%
    assert dd.max_drawdown_pct == pytest.approx(-10.909, abs=0.01)
    assert dd.trough_date == "2026-04-01"
    assert dd.peak_date == "2026-02-01"  # the high that PRECEDED the trough
    assert dd.recovery_date == "2026-06-01"
    assert dd.days_to_recovery == 61  # Apr 1 → Jun 1
    assert dd.recovered is True
    assert dd.current_drawdown_pct == pytest.approx(0.0, abs=0.001)  # ends at a high
    assert len(dd.underwater) == 6
    assert dd.underwater[3].drawdown_pct == pytest.approx(-10.909, abs=0.01)


def test_drawdown_still_underwater() -> None:
    points = [_pt("2026-01-01", 0.0), _pt("2026-02-01", 10.0), _pt("2026-03-01", -5.0)]
    dd = compute_drawdown(points)
    assert dd is not None
    # 0.95 / 1.10 - 1 = -13.636%
    assert dd.max_drawdown_pct == pytest.approx(-13.636, abs=0.01)
    assert dd.recovered is False
    assert dd.recovery_date is None
    assert dd.days_to_recovery is None
    assert dd.current_drawdown_pct == pytest.approx(-13.636, abs=0.01)


def test_drawdown_skips_missing_points_and_needs_two() -> None:
    # A None return is skipped; one usable point is not enough for a curve.
    assert compute_drawdown([_pt("2026-01-01", None), _pt("2026-02-01", 5.0)]) is None
    assert compute_drawdown([]) is None
    # Monotonic-up series never goes underwater.
    dd = compute_drawdown([_pt("2026-01-01", 0.0), _pt("2026-02-01", 4.0), _pt("2026-03-01", 9.0)])
    assert dd is not None
    assert dd.max_drawdown_pct == pytest.approx(0.0)
    assert dd.recovered is False  # never fell, so never "recovered"


# ----- factor / style roll-up -----


def test_factor_rollup_value_weights_and_renormalizes() -> None:
    rows = [
        _row("NU", 50.0, beta_spy=1.4, beta_qqq=1.2, corr_spy=0.6),
        _row("AAPL", 30.0, beta_spy=1.0, beta_qqq=1.5, corr_spy=0.9),
        # A cash-like name with no price history: weight present, betas None,
        # sample 0 → counted in names_total but not names_priced, and excluded
        # from every weighted mean (renormalized over the priced 80%).
        _row("CASH", 20.0, beta_spy=None, beta_qqq=None, corr_spy=None, sample=0),
    ]
    f = factor_exposure_rollup(rows, {"NU": -2.0})
    assert f is not None
    assert f.spy_beta == pytest.approx(100.0 / 80.0)  # (50*1.4+30*1.0)/80 = 1.25
    assert f.qqq_beta == pytest.approx(105.0 / 80.0)  # (50*1.2+30*1.5)/80 = 1.3125
    assert f.growth_tilt == pytest.approx(105.0 / 80.0 - 100.0 / 80.0)  # 0.0625
    assert f.avg_correlation_spy == pytest.approx(57.0 / 80.0)  # 0.7125
    # Rate leg: only NU has a 10Y beta → its own value (weight cancels).
    assert f.rate_beta_10y == pytest.approx(-2.0)
    assert f.names_total == 3
    assert f.names_priced == 2
    # Top contributors by |weight x loading|.
    assert [c.ticker for c in f.top_market] == ["NU", "AAPL"]
    # AAPL's +0.5 gap x 30 beats NU's -0.2 x 50.
    assert [c.ticker for c in f.top_growth] == ["AAPL", "NU"]
    assert [c.ticker for c in f.top_crowding] == ["NU", "AAPL"]


def test_factor_rollup_empty_and_no_rate() -> None:
    assert factor_exposure_rollup([]) is None
    f = factor_exposure_rollup([_row("X", 100.0, beta_spy=1.1, beta_qqq=None, corr_spy=0.5)])
    assert f is not None
    assert f.spy_beta == pytest.approx(1.1)
    assert f.qqq_beta is None  # no QQQ beta anywhere
    assert f.growth_tilt is None  # needs both legs
    assert f.rate_beta_10y is None  # no rate betas passed
