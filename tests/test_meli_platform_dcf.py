"""Tests for the MELI sum-of-the-parts platform DCF
(``execution/build_meli_platform_dcf.py``): the value-of-record mirror — the SOTP
identity, the convex growth fade, the credit-book capital charge — plus the
``refresh_dcf`` routing that dispatches MELI to this builder.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import build_meli_platform_dcf as meli  # noqa: E402
import refresh_dcf  # noqa: E402

_BASE = meli.Assum(derive_capm=0)  # explicit scalar discount rates (no DB read)


# --------------------------------------------------------------------------- #
# Value-of-record mirror
# --------------------------------------------------------------------------- #
def test_mirror_is_deterministic() -> None:
    assert meli.mirror(_BASE).vps == meli.mirror(_BASE).vps


def test_sotp_identity_equity_is_sum_of_parts() -> None:
    """Equity value = operating EV + credit-book equity + net non-operating cash."""
    m = meli.mirror(_BASE)
    assert m.equity_value == pytest.approx(m.operating_ev + m.credit_equity_value + _BASE.net_cash)
    assert m.vps == pytest.approx(m.equity_value / _BASE.shares)


def test_operating_ev_decomposes_into_pv_stream_plus_terminal() -> None:
    m = meli.mirror(_BASE)
    assert m.operating_ev == pytest.approx(m.pv_op_fcff + m.pv_op_terminal)
    assert m.credit_equity_value == pytest.approx(m.pv_credit_fcfe + m.pv_credit_terminal)


def test_operating_revenue_is_commerce_plus_fintech_payments() -> None:
    m = meli.mirror(_BASE)
    for r in m.rows:
        assert r.op_rev == pytest.approx(r.comm_rev + r.fpay_rev)


def test_year_one_revenue_reconciles_to_base_plus_near_growth() -> None:
    m = meli.mirror(_BASE)
    y1 = m.rows[0]
    assert y1.comm_rev == pytest.approx(_BASE.comm_rev0 * (1 + _BASE.comm_g_near))
    assert y1.fpay_rev == pytest.approx(_BASE.fpay_rev0 * (1 + _BASE.fpay_g_near))


# --------------------------------------------------------------------------- #
# Convex growth fade
# --------------------------------------------------------------------------- #
def test_fade_hits_near_at_year_one_and_terminal_at_year_n() -> None:
    n = _BASE.years
    assert meli._fade(0.30, 0.05, 1, n) == pytest.approx(0.30)
    assert meli._fade(0.30, 0.05, n, n) == pytest.approx(0.05)


def test_fade_is_convex_front_loaded() -> None:
    """A convex fade sheds more growth in the first half than a straight line —
    the year-by-year rate sits BELOW the linear interpolation through the middle."""
    n = _BASE.years
    near, term = 0.30, 0.05
    for t in range(2, n):
        linear = meli._interp(near, term, t, n)
        assert meli._fade(near, term, t, n) < linear


# --------------------------------------------------------------------------- #
# Driver sensitivities
# --------------------------------------------------------------------------- #
def test_higher_commerce_growth_raises_value() -> None:
    faster = dataclasses.replace(_BASE, comm_g_near=_BASE.comm_g_near + 0.05)
    assert meli.mirror(faster).vps > meli.mirror(_BASE).vps


def test_higher_nimal_raises_credit_value() -> None:
    richer = dataclasses.replace(_BASE, nimal_term=_BASE.nimal_term + 0.03)
    base = meli.mirror(_BASE)
    assert meli.mirror(richer).credit_equity_value > base.credit_equity_value


def test_higher_capital_ratio_lowers_near_term_credit_fcfe() -> None:
    """A heavier capital charge ties up more equity as the book grows, cutting the
    distributable credit FCFE — the capital intensity an FCFF model can't see."""
    heavier = dataclasses.replace(_BASE, cap_ratio=_BASE.cap_ratio + 0.05)
    assert meli.mirror(heavier).rows[0].credit_fcfe < meli.mirror(_BASE).rows[0].credit_fcfe


def test_higher_wacc_lowers_operating_ev() -> None:
    pricier = dataclasses.replace(_BASE, wacc=_BASE.wacc + 0.02)
    assert meli.mirror(pricier).operating_ev < meli.mirror(_BASE).operating_ev


def test_credit_terminal_roe_bounds_terminal_value() -> None:
    """A lower sustainable terminal ROE means more earnings must be retained to
    fund growth, so the credit terminal value falls."""
    lower_roe = dataclasses.replace(_BASE, credit_terminal_roe=0.18)
    assert meli.mirror(lower_roe).credit_terminal < meli.mirror(_BASE).credit_terminal


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
def test_refresh_routes_meli_to_sotp_builder(tmp_path: Path) -> None:
    """A holdings ``valuation_model`` override of 'meli_platform_sotp' resolves to
    the new archetype, so refresh dispatches to the MELI SOTP builder."""
    hp = tmp_path / "micro_thesis" / "holdings" / "MELI.json"
    hp.parent.mkdir(parents=True)
    hp.write_text(
        json.dumps({"ticker": "MELI", "valuation_model": "meli_platform_sotp"}),
        encoding="utf-8",
    )
    model, _suggestion = refresh_dcf._valuation_model(tmp_path, "MELI")
    assert model == "meli_platform_sotp"
