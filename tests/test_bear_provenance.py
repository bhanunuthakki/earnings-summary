"""Per-name bear deltas + provenance (Monthly Red Team Phase 1 guard 3).

Covers the pure plumbing that lets ``micro_thesis/holdings/<T>.json`` override
BEAR_SEED with a thesis-calibrated bear, and that classifies which of
seed/thesis/owner produced the bear deltas actually in use:
  * dcf.redesign.parse_thesis_bear_deltas / thesis_bear_seed / classify_bear_provenance
  * refresh_dcf._redesign_snapshot's "provenance" field in the scenarios.bear block
  * dcf.scenario_reward.parse_scenario_bear_provenance (the consumer side)
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import refresh_dcf  # noqa: E402

from dcf import redesign  # noqa: E402
from dcf.scenario_reward import parse_scenario_bear_provenance  # noqa: E402

_BASE = redesign.RedesignInputs(
    segments=("Cloud",),
    base_revenue_by_segment={"Cloud": 1000.0},
    near_growth_by_segment={"Cloud": 0.10},
    terminal_growth_by_segment={"Cloud": 0.03},
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


# --------------------------------------------------------------------------- #
# parse_thesis_bear_deltas
# --------------------------------------------------------------------------- #


def test_no_holdings_returns_none() -> None:
    assert redesign.parse_thesis_bear_deltas(None) is None
    assert redesign.parse_thesis_bear_deltas({}) is None


def test_holdings_with_no_bear_deltas_key_returns_none() -> None:
    assert redesign.parse_thesis_bear_deltas({"ticker": "NU"}) is None


def test_bear_deltas_block_with_no_recognized_lever_returns_none() -> None:
    assert redesign.parse_thesis_bear_deltas({"bear_deltas": {"note": "just a note"}}) is None


def test_full_bear_deltas_block_maps_all_levers() -> None:
    holdings = {
        "bear_deltas": {
            "growth_delta_pp": -8.0,
            "margin_delta_pp": -3.0,
            "exit_multiple_delta": -6.0,
            "terminal_g_delta_pp": -0.5,
            "note": "NIMAL floor breach",
        }
    }
    deltas = redesign.parse_thesis_bear_deltas(holdings)
    assert deltas is not None
    assert deltas.growth_near == -0.08
    assert deltas.growth_term == -0.08  # applied uniformly near+term
    assert deltas.margin_near == -0.03
    assert deltas.margin_term == -0.03
    assert deltas.exit_multiple == -6.0
    assert deltas.terminal_g == -0.005


def test_partial_bear_deltas_block_falls_back_to_seed_per_lever() -> None:
    holdings = {"bear_deltas": {"growth_delta_pp": -8.0}}
    deltas = redesign.parse_thesis_bear_deltas(holdings)
    assert deltas is not None
    assert deltas.growth_near == -0.08
    # Unset levers fall back to BEAR_SEED, not zero.
    assert deltas.margin_near == redesign.BEAR_SEED.margin_near
    assert deltas.exit_multiple == redesign.BEAR_SEED.exit_multiple
    assert deltas.terminal_g == redesign.BEAR_SEED.terminal_g


def test_bear_deltas_ignores_non_numeric_junk() -> None:
    holdings = {"bear_deltas": {"growth_delta_pp": "not a number"}}
    assert redesign.parse_thesis_bear_deltas(holdings) is None


# --------------------------------------------------------------------------- #
# thesis_bear_seed
# --------------------------------------------------------------------------- #


def test_thesis_bear_seed_falls_back_to_bear_seed() -> None:
    assert redesign.thesis_bear_seed(None) == redesign.BEAR_SEED
    assert redesign.thesis_bear_seed({}) == redesign.BEAR_SEED


def test_thesis_bear_seed_uses_holdings_override() -> None:
    holdings = {"bear_deltas": {"growth_delta_pp": -8.0, "margin_delta_pp": -3.0}}
    seed = redesign.thesis_bear_seed(holdings)
    assert seed.growth_near == -0.08
    assert seed != redesign.BEAR_SEED


# --------------------------------------------------------------------------- #
# classify_bear_provenance
# --------------------------------------------------------------------------- #


def test_classify_seed_when_deltas_match_bear_seed() -> None:
    assert redesign.classify_bear_provenance(redesign.BEAR_SEED, None) == "seed"
    holdings = {"bear_deltas": {"growth_delta_pp": -8.0}}
    # A run that happens to use BEAR_SEED even though holdings names an override
    # (e.g. the override was added after this run) still classifies as seed —
    # the classification reads the deltas actually in use, not the file's intent.
    assert redesign.classify_bear_provenance(redesign.BEAR_SEED, holdings) == "seed"


def test_classify_thesis_when_deltas_match_holdings_override() -> None:
    holdings = {"bear_deltas": {"growth_delta_pp": -8.0, "margin_delta_pp": -3.0}}
    thesis_deltas = redesign.parse_thesis_bear_deltas(holdings)
    assert thesis_deltas is not None
    assert redesign.classify_bear_provenance(thesis_deltas, holdings) == "thesis"


def test_classify_owner_when_deltas_match_neither() -> None:
    hand_edited = dataclasses.replace(redesign.BEAR_SEED, growth_near=-0.15, exit_multiple=-9.0)
    assert redesign.classify_bear_provenance(hand_edited, None) == "owner"
    holdings = {"bear_deltas": {"growth_delta_pp": -8.0}}
    assert redesign.classify_bear_provenance(hand_edited, holdings) == "owner"


# --------------------------------------------------------------------------- #
# refresh_dcf._redesign_snapshot — the "provenance" field wiring
# --------------------------------------------------------------------------- #

_RV = redesign.RedesignValuation(
    value_per_share_usd=55.0,
    value_per_share_reporting=55.0,
    operating_value_usd_m=5000.0,
    equity_value_usd_m=5200.0,
    fcff_stream_m=[100.0] * 10,
    forecast_revenue_m=[1000.0] * 10,
    wacc=0.09,
    terminal_method="Exit multiple",
    terminal_basis="EV/EBITDA",
    exit_multiple=12.0,
    fx_to_usd=1.0,
    diluted_shares_m=100.0,
    cash_m=100.0,
    total_debt_m=200.0,
    current_price=50.0,
)
_SV = redesign.ScenarioValues(base=55.0, bull=70.0, bear=35.0)


def test_redesign_snapshot_bear_provenance_is_seed_by_default() -> None:
    raw = refresh_dcf._redesign_snapshot(  # pyright: ignore[reportPrivateUsage]
        _RV, "wb.xlsx", scenarios=_SV, inp=_BASE, holdings=None
    )
    payload = json.loads(raw)
    assert payload["scenarios"]["bear"]["provenance"] == "seed"


def test_redesign_snapshot_bear_provenance_is_thesis_when_deltas_match_holdings() -> None:
    holdings: dict[str, object] = {
        "bear_deltas": {"growth_delta_pp": -8.0, "margin_delta_pp": -3.0}
    }
    thesis_deltas = redesign.parse_thesis_bear_deltas(holdings)
    assert thesis_deltas is not None
    inp = dataclasses.replace(_BASE, bear_deltas=thesis_deltas)
    raw = refresh_dcf._redesign_snapshot(  # pyright: ignore[reportPrivateUsage]
        _RV, "wb.xlsx", scenarios=_SV, inp=inp, holdings=holdings
    )
    payload = json.loads(raw)
    assert payload["scenarios"]["bear"]["provenance"] == "thesis"


def test_redesign_snapshot_bear_provenance_is_owner_on_hand_edit() -> None:
    hand_edited = dataclasses.replace(redesign.BEAR_SEED, growth_near=-0.20)
    inp = dataclasses.replace(_BASE, bear_deltas=hand_edited)
    raw = refresh_dcf._redesign_snapshot(  # pyright: ignore[reportPrivateUsage]
        _RV, "wb.xlsx", scenarios=_SV, inp=inp, holdings=None
    )
    payload = json.loads(raw)
    assert payload["scenarios"]["bear"]["provenance"] == "owner"


def test_redesign_snapshot_omits_scenarios_when_none() -> None:
    raw = refresh_dcf._redesign_snapshot(_RV, "wb.xlsx")  # pyright: ignore[reportPrivateUsage]
    payload = json.loads(raw)
    assert "scenarios" not in payload


# --------------------------------------------------------------------------- #
# dcf.scenario_reward.parse_scenario_bear_provenance — the consumer side
# --------------------------------------------------------------------------- #


def test_parse_scenario_bear_provenance_reads_the_field() -> None:
    snap = json.dumps(
        {"scenarios": {"bear": {"fair_value_per_share_usd": 35.0, "provenance": "thesis"}}}
    )
    assert parse_scenario_bear_provenance(snap) == "thesis"


def test_parse_scenario_bear_provenance_tolerates_absence() -> None:
    assert parse_scenario_bear_provenance(None) is None
    assert parse_scenario_bear_provenance("") is None
    assert parse_scenario_bear_provenance("not json") is None
    assert parse_scenario_bear_provenance(json.dumps({"scenarios": {}})) is None
    assert (
        parse_scenario_bear_provenance(
            json.dumps({"scenarios": {"bear": {"fair_value_per_share_usd": 35.0}}})
        )
        is None
    )  # no provenance field — pre-Phase-1 snapshot


def test_parse_scenario_bear_provenance_rejects_unknown_value() -> None:
    snap = json.dumps({"scenarios": {"bear": {"provenance": "guess"}}})
    assert parse_scenario_bear_provenance(snap) is None
