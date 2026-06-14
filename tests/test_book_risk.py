"""The single covariance→risk-contribution assembly (allocation.book_risk).

Pins the Euler identity the risk-share math leans on: the per-name risk
contributions sum to the portfolio vol, so the shares sum to ~1.0. Plus the
weight renormalization and the three hidden-reason degradations.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from allocation.book_risk import build_book_risk  # noqa: E402

TICKERS = ["AAA", "BBB", "CCC"]


def _seed_prices(repo_root: Path, n: int = 200) -> None:
    """BBB tracks AAA closely; CCC is independent (mirrors test_allocation_model)."""
    days: list[date] = []
    d = date.today() - timedelta(days=2)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    rng = np.random.default_rng(0)
    base = rng.normal(0.0005, 0.02, n)
    noise = rng.normal(0.0, 0.005, n)
    indep = rng.normal(0.0005, 0.02, n)
    series = {"AAA": base, "BBB": 0.9 * base + noise, "CCC": indep}
    fmp = repo_root / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    for ticker, rets in series.items():
        prices = 100.0 * np.exp(np.cumsum(rets))
        rows = [
            {"date": days[i].isoformat(), "adjClose": round(float(prices[i]), 6)} for i in range(n)
        ][::-1]
        (fmp / f"{ticker}_price_chart_10y_div_adj.json").write_text(
            json.dumps(rows), encoding="utf-8"
        )


def test_risk_contributions_sum_to_portfolio_vol(tmp_path: Path) -> None:
    _seed_prices(tmp_path)
    weights = {"AAA": 0.5, "BBB": 0.3, "CCC": 0.2}
    br = build_book_risk(tmp_path, TICKERS, weights)
    assert br.hidden_reason is None
    assert set(br.tickers) == set(TICKERS)
    assert br.portfolio_vol_ann is not None and br.portfolio_vol_ann > 0.0
    # Euler: sum of RC_i = portfolio vol, so the shares sum to ~1.0.
    assert sum(br.risk_contribution_ann.values()) == pytest.approx(br.portfolio_vol_ann, rel=1e-9)
    assert sum(br.risk_share.values()) == pytest.approx(1.0, rel=1e-9)
    # RC_i = w_i * marginal_vol_i, consistent with the share.
    for t in br.tickers:
        assert br.risk_contribution_ann[t] == pytest.approx(br.weights[t] * br.marginal_vol_ann[t])
    # The correlated pair (AAA/BBB) carries more marginal vol than the diversifier.
    assert br.marginal_vol_ann["AAA"] > br.marginal_vol_ann["CCC"]


def test_weights_renormalize_over_covariance_names(tmp_path: Path) -> None:
    _seed_prices(tmp_path)
    # Pass un-normalized weights (and a name with no file); the matrix names'
    # weights renormalize to sum 1.
    br = build_book_risk(tmp_path, [*TICKERS, "ZZZ"], {"AAA": 5.0, "BBB": 3.0, "CCC": 2.0})
    assert br.hidden_reason is None
    assert sum(br.weights.values()) == pytest.approx(1.0)
    assert br.weights["AAA"] == pytest.approx(0.5)
    assert "ZZZ" in br.dropped  # no price history on file


def test_equal_weight_when_passed_weights_are_zero(tmp_path: Path) -> None:
    _seed_prices(tmp_path)
    br = build_book_risk(tmp_path, TICKERS, dict.fromkeys(TICKERS, 0.0))
    assert br.hidden_reason is None
    for t in br.tickers:
        assert br.weights[t] == pytest.approx(1.0 / 3.0)


def test_hidden_when_too_few_priced_names(tmp_path: Path) -> None:
    # Only one name has a price file → no covariance.
    fmp = tmp_path / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    (fmp / "AAA_price_chart_10y_div_adj.json").write_text(
        json.dumps([{"date": "2026-06-01", "adjClose": 10.0}]), encoding="utf-8"
    )
    br = build_book_risk(tmp_path, TICKERS, {"AAA": 1.0})
    assert br.hidden_reason == "fewer than two holdings with price history on file"
    assert br.risk_share == {}
    assert "BBB" in br.dropped and "CCC" in br.dropped
