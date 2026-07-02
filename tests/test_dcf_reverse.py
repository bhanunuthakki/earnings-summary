"""Reverse-DCF solver: does the market-implied assumption reprice to the price?

The correctness contract is a round-trip: an inversion's implied lever value,
fed back through the forward ``value()`` engine, must reproduce the target price
(the exit-multiple lever is checked directly; the growth lever is checked via
CAGR monotonicity, sharing the same bisection engine). Plus the honest-n/a
boundary: an unreachable price returns ``None`` with a directional note, never a
clamped bound.
"""

from __future__ import annotations

from dataclasses import replace

from dcf import redesign, reverse

_BASE = redesign.RedesignInputs(
    segments=("Cloud", "Devices"),
    base_revenue_by_segment={"Cloud": 600.0, "Devices": 400.0},
    near_growth_by_segment={"Cloud": 0.10, "Devices": 0.05},
    terminal_growth_by_segment={"Cloud": 0.03, "Devices": 0.02},
    near_op_margin=0.20,
    terminal_op_margin=0.25,
    tax_rate=0.24,
    capex_2026_m=60.0,
    terminal_capex_da=1.05,
    da_ratio=0.05,
    consensus_years=5,
    wacc=0.09,
    beta=1.2,
    risk_free_rate=0.043,
    equity_risk_premium=0.045,
    cost_of_debt=0.045,
    terminal_method="Exit multiple",
    terminal_basis="EV/EBITDA",
    exit_multiple=12.0,
    terminal_growth_g=0.03,
    current_price=50.0,
    cash_m=100.0,
    total_debt_m=200.0,
    diluted_shares_m=100.0,
    fx_to_usd=1.0,
)


def _base_value() -> float:
    return redesign.value(_BASE).value_per_share_usd


# --------------------------------------------------------------------------- #
# 1. Round-trip: the implied exit multiple actually reprices to the target
# --------------------------------------------------------------------------- #
def test_implied_exit_multiple_reprices_to_price() -> None:
    base_val = _base_value()
    target = base_val * 1.4  # market values the name 40% above the base case
    pi = reverse.solve_priced_in(_BASE, price=target)
    assert pi is not None
    inv = pi.terminal
    assert inv.lever == "exit_multiple"
    assert inv.implied_value is not None
    # Feeding the implied multiple back through the forward engine reproduces price.
    repriced = redesign.value(replace(_BASE, exit_multiple=inv.implied_value)).value_per_share_usd
    assert abs(repriced - target) <= 1e-3 * target
    # And a higher-than-base price implies a higher-than-base multiple.
    assert inv.implied_value > _BASE.exit_multiple
    assert inv.gap is not None and inv.gap > 0


def test_lower_price_implies_lower_multiple_and_growth() -> None:
    base_val = _base_value()
    target = base_val * 0.7
    pi = reverse.solve_priced_in(_BASE, price=target)
    assert pi is not None
    assert pi.terminal.implied_value is not None
    assert pi.terminal.implied_value < _BASE.exit_multiple
    assert pi.growth.implied_value is not None
    # Base CAGR is the analyst's; a cheaper price implies a lower CAGR.
    assert pi.growth.implied_value < pi.growth.base_value


# --------------------------------------------------------------------------- #
# 2. Self-consistency: pricing AT the base fair value implies ~the base levers
# --------------------------------------------------------------------------- #
def test_price_at_base_value_implies_base_levers() -> None:
    base_val = _base_value()
    pi = reverse.solve_priced_in(_BASE, price=base_val)
    assert pi is not None
    assert pi.terminal.implied_value is not None
    assert abs(pi.terminal.implied_value - _BASE.exit_multiple) <= 0.05
    assert pi.growth.implied_value is not None
    assert pi.growth.base_value is not None
    assert abs(pi.growth.implied_value - pi.growth.base_value) <= 0.005  # within 0.5pt


# --------------------------------------------------------------------------- #
# 3. Implied 5y revenue CAGR is a real, ordered rate
# --------------------------------------------------------------------------- #
def test_implied_cagr_is_monotone_in_price() -> None:
    base_val = _base_value()
    lo = reverse.solve_priced_in(_BASE, price=base_val * 0.8)
    hi = reverse.solve_priced_in(_BASE, price=base_val * 1.25)
    assert lo is not None and hi is not None
    assert lo.growth.implied_value is not None and hi.growth.implied_value is not None
    # A richer price implies a faster growth path.
    assert hi.growth.implied_value > lo.growth.implied_value
    assert lo.growth.unit == "pct"


# --------------------------------------------------------------------------- #
# 4. Honest n/a when the price is unreachable inside model bounds
# --------------------------------------------------------------------------- #
def test_absurdly_high_price_returns_unsolved_with_direction() -> None:
    base_val = _base_value()
    pi = reverse.solve_priced_in(_BASE, price=base_val * 500.0)
    assert pi is not None
    # Neither lever can reach a 500x price inside its bounds.
    assert pi.terminal.implied_value is None
    assert "bounds" in pi.terminal.note or "ceiling" in pi.terminal.note
    assert pi.growth.implied_value is None
    assert "ceiling" in pi.growth.note
    assert pi.growth.gap is None
    assert not pi.growth.solved


def test_absurdly_low_price_implies_growth_below_floor() -> None:
    base_val = _base_value()
    pi = reverse.solve_priced_in(_BASE, price=base_val * 0.001)
    assert pi is not None
    assert pi.growth.implied_value is None
    assert "floor" in pi.growth.note


# --------------------------------------------------------------------------- #
# 5. Perpetuity terminal → implied terminal growth g (round-trips to price)
# --------------------------------------------------------------------------- #
def test_perpetuity_inverts_terminal_growth() -> None:
    perp = replace(_BASE, terminal_method="Perpetuity")
    base_val = redesign.value(perp).value_per_share_usd
    target = base_val * 1.1
    pi = reverse.solve_priced_in(perp, price=target)
    assert pi is not None
    inv = pi.terminal
    assert inv.lever == "terminal_growth_g"
    assert inv.unit == "pct"
    assert inv.implied_value is not None
    repriced = redesign.value(
        replace(perp, terminal_growth_g=inv.implied_value)
    ).value_per_share_usd
    assert abs(repriced - target) <= 1e-3 * target
    assert inv.implied_value < perp.wacc  # perpetuity requires g < WACC


# --------------------------------------------------------------------------- #
# 6. Honest-None guards (mirrors scenario_reward)
# --------------------------------------------------------------------------- #
def test_none_when_no_usable_price() -> None:
    assert reverse.solve_priced_in(_BASE, price=0.0) is None
    assert reverse.solve_priced_in(_BASE, price=-5.0) is None
    assert reverse.solve_priced_in(replace(_BASE, current_price=0.0)) is None


def test_defaults_to_workbook_price() -> None:
    pi = reverse.solve_priced_in(_BASE)  # uses _BASE.current_price = 50.0
    assert pi is not None
    assert pi.price == 50.0
    assert pi.base_value_per_share > 0
