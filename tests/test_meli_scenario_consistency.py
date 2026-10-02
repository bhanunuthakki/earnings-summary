"""Terminal math gates reject inconsistent scenarios without choosing new assumptions."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from dcf import redesign
from execution import build_meli_platform_dcf as meli


def consistent_base() -> meli.Assum:
    # Synthetic algebra fixture, not an economically accepted forecast.
    return meli.Assum(derive_capm=0, credit_terminal_roe=0.4992)


@pytest.mark.parametrize(
    "deltas",
    [
        redesign.ScenarioDeltas(),
        redesign.BULL_SEED,
        redesign.BEAR_SEED,
        redesign.ScenarioDeltas(
            growth_near=-0.075,
            growth_term=-0.075,
            margin_near=-0.05,
            margin_term=-0.05,
            exit_multiple=-4,
            terminal_g=-0.015,
        ),
    ],
    ids=["historical-base", "historical-bull", "historical-bear", "historical-thesis-bear"],
)
def test_historical_inconsistent_vectors_are_rejected(deltas: redesign.ScenarioDeltas) -> None:
    base = meli.Assum(derive_capm=0)
    before = dataclasses.asdict(base)
    with pytest.raises(ValueError, match=r"terminal_credit_(roe_capital|growth)_inconsistent"):
        meli.scenario_assumptions(base, deltas)
    assert dataclasses.asdict(base) == before


def test_consistent_base_does_not_make_shifted_scenario_consistent() -> None:
    with pytest.raises(ValueError, match="terminal_credit_growth_inconsistent"):
        meli.scenario_assumptions(consistent_base(), redesign.BULL_SEED)
    with pytest.raises(ValueError, match="terminal_credit_roe_capital_inconsistent"):
        meli.scenario_assumptions(consistent_base(), redesign.ScenarioDeltas(margin_term=-0.01))


def test_consistent_synthetic_terminal_earns_and_retains_exact_required_capital() -> None:
    base = consistent_base()
    # Keep ROE unchanged while moving both terminal growth definitions together.
    changed_spread = (base.nimal_term - base.credit_opex_ratio) * 1.04 / 1.045
    deltas = redesign.ScenarioDeltas(
        growth_term=0.01,
        terminal_g=0.01,
        margin_term=changed_spread - (base.nimal_term - base.credit_opex_ratio),
    )
    scenario = meli.scenario_assumptions(base, deltas)
    assert scenario.credit_terminal_roe == base.credit_terminal_roe
    assert scenario.credit_g_term == pytest.approx(0.09)
    assert scenario.cbg_term == pytest.approx(0.09)
    result = meli.mirror(scenario)
    last = result.rows[-1]
    next_book = last.cb * (1 + scenario.credit_g_term)
    next_ni = (
        (last.cb + next_book)
        / 2
        * (scenario.nimal_term - scenario.credit_opex_ratio)
        * (1 - scenario.tax)
    )
    required_retention = scenario.cap_ratio * (next_book - last.cb)
    gordon_retention = next_ni * scenario.credit_g_term / scenario.credit_terminal_roe
    assert gordon_retention == pytest.approx(required_retention)
    assert result.credit_terminal == pytest.approx(
        (next_ni - required_retention) / (scenario.credit_ke - scenario.credit_g_term)
    )


@pytest.mark.parametrize("growth", [0.20, float("nan"), float("inf")])
def test_invalid_gordon_inputs_are_not_clamped_into_accepted_scenarios(growth: float) -> None:
    with pytest.raises(ValueError, match="model_terminal_or_capital_inputs_invalid"):
        meli.scenario_assumptions(consistent_base(), redesign.ScenarioDeltas(terminal_g=growth))


def test_invalid_derived_scenarios_cannot_replace_workbook_or_emit_block(tmp_path: Path) -> None:
    base = consistent_base()
    model = meli.mirror(base)
    destination = tmp_path / "existing.xlsx"
    destination.write_bytes(b"preserve existing workbook")
    with pytest.raises(ValueError, match="terminal_credit_growth_inconsistent"):
        meli.scenarios_block(base, model, None)
    with pytest.raises(ValueError, match="terminal_credit_growth_inconsistent"):
        meli.build(base, model, destination, None)
    assert destination.read_bytes() == b"preserve existing workbook"
