"""Unit tests for the equity-side bank credit model (execution/build_bank_dcf.py).

Mirror-level math only — no FMP, no DB, no workbook. Two invariants:
  * the value formula must NOT double-count terminal book equity (the terminal is
    the *continuing residual income*, not the full justified-P/B equity price);
  * the FX / ADR translation scales the per-share output linearly, and collapses
    to a no-op for a USD reporter with no ADR (NU's defaults).
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import build_bank_dcf as bank  # noqa: E402


def _actuals(**kw: float) -> bank.Actuals:
    base: dict[str, float] = dict(
        book=10000.0,
        ea=22700.0,
        nii=3859.0,
        fees=2000.0,
        opex=1500.0,
        credit_cost=1000.0,
        pretax=3359.0,
        tax_rate=0.25,
        ni=2519.0,
        equity=4000.0,
        equity_prior=3500.0,
        shares=1000.0,
        price=10.0,
    )
    base.update(kw)
    return bank.Actuals(**base)


def test_value_equals_textbook_residual_income() -> None:
    """value == book0 + PV(excess) + PV(continuing RI). The terminal must be the
    continuing residual income (ROE_term-Ke)*Eq_T/(Ke-g)."""
    a = _actuals()
    s = bank.Assum()
    m = bank.mirror(a, s)
    ke, g, n = s.ke, s.g_terminal, s.years
    eq_t = m.rows[-1].equity
    df_n = 1 / (1 + ke) ** n
    cont_ri = (m.roe_term - ke) * eq_t / (ke - g)
    expected = a.equity + m.pv_excess + cont_ri * df_n
    assert abs(m.value - expected) < 1e-6


def test_terminal_is_not_the_double_counted_price() -> None:
    """The buggy full-price terminal Eq_T*(ROE_term-g)/(Ke-g) exceeds the correct
    value by EXACTLY PV(terminal book equity) = Eq_T/(1+Ke)^n. This locks the fix:
    if someone reverts to the justified-P/B price, this gap assertion fails."""
    a = _actuals()
    s = bank.Assum()
    m = bank.mirror(a, s)
    ke, g, n = s.ke, s.g_terminal, s.years
    eq_t = m.rows[-1].equity
    df_n = 1 / (1 + ke) ** n
    buggy_value = a.equity + m.pv_excess + eq_t * (m.roe_term - g) / (ke - g) * df_n
    assert abs((buggy_value - m.value) - eq_t * df_n) < 1e-6


def test_pv_tv_matches_continuing_residual_income_formula() -> None:
    a = _actuals()
    s = bank.Assum()
    m = bank.mirror(a, s)
    ke, g, n = s.ke, s.g_terminal, s.years
    eq_t = m.rows[-1].equity
    df_n = 1 / (1 + ke) ** n
    assert abs(m.pv_tv - (m.roe_term - ke) * eq_t / (ke - g) * df_n) < 1e-6


def test_fx_and_adr_scale_per_share_linearly() -> None:
    a = _actuals()
    base = bank.mirror(a, bank.Assum())
    scaled = bank.mirror(a, bank.Assum(fx_to_usd=0.012, adr_ratio=3.0))
    # reporting value/share is currency-agnostic (unchanged); only vps_usd scales
    assert abs(scaled.vps - base.vps) < 1e-9
    assert abs(scaled.vps_usd - base.vps * 3.0 * 0.012) < 1e-9


def test_usd_reporter_defaults_leave_vps_usd_equal_to_reporting() -> None:
    m = bank.mirror(_actuals(), bank.Assum())  # fx=1, adr=1
    assert abs(m.vps_usd - m.vps) < 1e-12


def test_nu_preserving_defaults() -> None:
    """Defaults keep NU's behaviour: 17% NIM EA derivation, USD, 1:1 ADR."""
    s = bank.Assum()
    assert s.nim_y0 == 0.17
    assert s.fx_to_usd == 1.0
    assert s.adr_ratio == 1.0
