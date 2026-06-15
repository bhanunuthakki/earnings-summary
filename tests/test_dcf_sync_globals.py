"""refresh_dcf no longer mirrors the global rf/ERP back into per-name blocks (PR6).

risk-free + ERP are global (global_dcf_assumptions, migration 0112), so the
workbook→JSON round-trip must NOT re-pin them into ``data/dcf_assumptions/<T>.json``
— otherwise the one-time strip would be undone on the next refresh. cost_of_debt
+ tax stay per-name (company-specific).
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import refresh_dcf  # noqa: E402

from dcf.redesign import RedesignInputs  # noqa: E402


def _inputs(**over: object) -> RedesignInputs:
    base: dict[str, object] = dict(
        segments=("Seg",),
        base_revenue_by_segment={"Seg": 100.0},
        near_growth_by_segment={"Seg": 0.10},
        terminal_growth_by_segment={"Seg": 0.03},
        near_op_margin=0.20,
        terminal_op_margin=0.25,
        tax_rate=0.24,
        capex_2026_m=10.0,
        terminal_capex_da=1.05,
        da_ratio=0.05,
        consensus_years=2,
        wacc=0.09,
        beta=1.2,
        risk_free_rate=0.043,
        equity_risk_premium=0.045,
        cost_of_debt=0.045,
        terminal_method="Exit multiple",
        terminal_basis="EV/EBITDA",
        exit_multiple=12.0,
        terminal_growth_g=0.025,
        current_price=100.0,
        cash_m=5.0,
        total_debt_m=2.0,
        diluted_shares_m=10.0,
        fx_to_usd=1.0,
    )
    base.update(over)
    return RedesignInputs(**base)  # type: ignore[arg-type]


def test_sync_does_not_pin_global_rf_erp() -> None:
    rd: dict[str, object] = {}
    refresh_dcf._apply_inputs_to_block(rd, _inputs())
    assert "risk_free_rate" not in rd
    assert "equity_risk_premium" not in rd


def test_sync_still_mirrors_per_name_wacc_inputs() -> None:
    rd: dict[str, object] = {}
    refresh_dcf._apply_inputs_to_block(rd, _inputs(beta=1.4, cost_of_debt=0.05, tax_rate=0.21))
    # company-specific WACC inputs + tax still round-trip per name.
    assert rd["beta"] == 1.4
    assert rd["cost_of_debt"] == 0.05
    assert rd["tax_rate"] == 0.21
