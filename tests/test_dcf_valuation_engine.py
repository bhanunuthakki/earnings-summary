"""Tests for the Damodaran valuation engine (valuation.compute_valuation):
the FCFF -> operating value -> equity bridge -> value/share walk, the
company-specific EV exit multiple, and the implied price-multiple readout.
"""

from __future__ import annotations

import pytest

from dcf import valuation as val

_FCFF = [100.0, 110.0, 120.0, 130.0, 140.0]
_YEARS = [2027, 2028, 2029, 2030, 2031]
_WACC = 0.10
_TERMINAL = val.TerminalMetrics(
    revenue=1000.0, ebit=180.0, ebitda=200.0, fcf=150.0, net_income=80.0
)


def _base(**over: object) -> val.DamodaranValuation:
    kw: dict[str, object] = {
        "basis": "EV/EBITDA",
        "terminal_multiple": 10.0,
        "terminal": _TERMINAL,
        "cash_and_nonop": 300.0,
        "total_debt": 500.0,
        "diluted_shares_M": 100.0,
    }
    kw.update(over)
    return val.compute_valuation(_FCFF, _YEARS, _WACC, **kw)  # type: ignore[arg-type]


def test_walk_and_equity_bridge() -> None:
    v = _base()
    # PV of the FCFF stream, discounted at WACC with t = 1..5.
    expected_pv = sum(f / 1.10 ** (i + 1) for i, f in enumerate(_FCFF))
    assert v.pv_fcff == pytest.approx(expected_pv)
    # Terminal EV = EBITDA * multiple = 200 * 10 = 2000, discounted at t=5.
    assert v.terminal_metric_value == pytest.approx(200.0)
    assert v.terminal_ev == pytest.approx(2000.0)
    assert v.pv_terminal == pytest.approx(2000.0 / 1.10**5)
    assert v.operating_value == pytest.approx(v.pv_fcff + v.pv_terminal)
    # The net-debt bridge: equity = operating + cash - debt.
    assert v.equity_value == pytest.approx(v.operating_value + 300.0 - 500.0)
    assert v.value_per_share == pytest.approx(v.equity_value / 100.0)
    # Terminal weight (Damodaran's sanity check) is the bulk of value here.
    assert v.pv_terminal_pct == pytest.approx(v.pv_terminal / v.operating_value)
    assert 0.6 < v.pv_terminal_pct < 0.9


def test_implied_price_multiples() -> None:
    v = _base()
    assert v.implied_pe == pytest.approx(v.equity_value / 80.0)
    assert v.implied_ps == pytest.approx(v.equity_value / 1000.0)
    assert v.implied_pfcf == pytest.approx(v.equity_value / 150.0)


def test_net_debt_bridge_moves_value() -> None:
    # Net cash lifts equity vs the no-bridge (EV == equity) case.
    cash_heavy = _base(cash_and_nonop=1000.0, total_debt=0.0)
    debt_heavy = _base(cash_and_nonop=0.0, total_debt=1000.0)
    assert cash_heavy.value_per_share > debt_heavy.value_per_share
    assert cash_heavy.equity_value - debt_heavy.equity_value == pytest.approx(2000.0)


def test_basis_selects_metric() -> None:
    sales = _base(basis="EV/Sales", terminal_multiple=3.0)
    assert sales.terminal_metric_value == pytest.approx(1000.0)  # revenue
    assert sales.terminal_ev == pytest.approx(3000.0)
    ebit = _base(basis="EV/EBIT", terminal_multiple=12.0)
    assert ebit.terminal_metric_value == pytest.approx(180.0)
    fcf = _base(basis="EV/FCF", terminal_multiple=20.0)
    assert fcf.terminal_metric_value == pytest.approx(150.0)


def test_implied_multiple_none_on_nonpositive_metric() -> None:
    v = _base(
        terminal=val.TerminalMetrics(revenue=1000, ebit=180, ebitda=200, fcf=-50, net_income=-10)
    )
    assert v.implied_pe is None  # negative net income
    assert v.implied_pfcf is None  # negative FCF
    assert v.implied_ps is not None


def test_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError, match="empty"):
        _base_args(fcff=[])
    with pytest.raises(ValueError, match="unknown terminal basis"):
        _base(basis="P/E")
    with pytest.raises(ValueError, match="wacc"):
        val.compute_valuation(
            _FCFF,
            _YEARS,
            0.0,
            basis="EV/EBITDA",
            terminal_multiple=10.0,
            terminal=_TERMINAL,
            cash_and_nonop=0.0,
            total_debt=0.0,
            diluted_shares_M=100.0,
        )


def _base_args(fcff: list[float]) -> val.DamodaranValuation:
    return val.compute_valuation(
        fcff,
        [],
        _WACC,
        basis="EV/EBITDA",
        terminal_multiple=10.0,
        terminal=_TERMINAL,
        cash_and_nonop=0.0,
        total_debt=0.0,
        diluted_shares_M=100.0,
    )
