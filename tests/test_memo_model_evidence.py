"""Closed model selectors cannot certify arbitrary JSON or unavailable roots."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from research.memo_model_evidence import (
    MemoModelCommitment,
    MemoModelEvidenceError,
    MemoModelValue,
    load_verified_memo_model,
    model_value_display,
)
from tests.test_onon_inputs import NOW


def test_forecast_requires_exact_year_unit_and_rounding() -> None:
    value = MemoModelValue(
        source="scenario_row",
        key="revenue_chf_m",
        scenario="base",
        year=2027,
        unit="CHF_m",
        displayed_unit="CHF_bn",
        display_format="number2",
        displayed_value="3.46",
    )
    output: dict[str, object] = {
        "scenarios": {"base": {"rows": [{"year": 2027.0, "revenue_chf_m": 3456.0}]}}
    }
    assert model_value_display(value, calculations={}, assumptions={}, output=output) == "3.46"
    for update in ({"year": 2028}, {"unit": "USD_m"}, {"displayed_value": "3.45"}):
        with pytest.raises(MemoModelEvidenceError):
            model_value_display(
                value.model_copy(update=update), calculations={}, assumptions={}, output=output
            )


@pytest.mark.parametrize(
    "key", ["__class__", "scenarios.base.rows[0]", "native_annual_valuation", "unknown"]
)
def test_no_arbitrary_paths(key: str) -> None:
    with pytest.raises(ValidationError):
        MemoModelValue(
            source="output",
            key=key,
            unit="USD_per_share",
            displayed_unit="USD_per_share",
            display_format="number2",
            displayed_value="34.47",
        )


@pytest.mark.parametrize("raw", [None, True, float("nan"), float("inf"), "0.2"])
def test_unsolved_or_nonnumeric_reverse_is_unavailable(raw: object) -> None:
    value = MemoModelValue(
        source="reverse",
        key="required_margin_shift",
        unit="fraction",
        displayed_unit="percent",
        display_format="number1",
        displayed_value="2.0",
    )
    with pytest.raises(MemoModelEvidenceError):
        model_value_display(
            value, calculations={}, assumptions={}, output={"reverse_dcf": {value.key: raw}}
        )


def test_receipt_value_keeps_negative_expense_and_distinct_cash_interest() -> None:
    value = MemoModelValue(
        source="receipt_calculation",
        key="lease_interest_expense",
        unit="CHF_m",
        displayed_unit="CHF_m",
        display_format="number1",
        displayed_value="-22.7",
    )
    assert (
        model_value_display(value, calculations={value.key: -22.7}, assumptions={}, output={})
        == "-22.7"
    )
    with pytest.raises(ValidationError):
        MemoModelValue(
            source="receipt_calculation",
            key="cash_interest_paid",
            unit="CHF_m",
            displayed_unit="CHF_m",
            display_format="number1",
            displayed_value="22.7",
        )


def test_percentage_display_binds_marker_and_scale() -> None:
    value = MemoModelValue(
        source="scenario",
        key="gordon_terminal_weight",
        scenario="base",
        unit="fraction",
        displayed_unit="percent",
        display_format="percentage1",
        displayed_value="74.7%",
    )
    output: dict[str, object] = {"scenarios": {"base": {"gordon_terminal_weight": 0.747}}}
    assert model_value_display(value, calculations={}, assumptions={}, output=output) == "74.7%"
    with pytest.raises(MemoModelEvidenceError):
        model_value_display(
            value.model_copy(update={"displayed_value": "0.7%"}),
            calculations={},
            assumptions={},
            output=output,
        )


def test_duplicate_forecast_year_never_selects_first_matching_row() -> None:
    value = MemoModelValue(
        source="scenario_row",
        key="year",
        year=2027,
        scenario="bear",
        unit="year",
        displayed_unit="year",
        display_format="number0",
        displayed_value="2027",
    )
    with pytest.raises(MemoModelEvidenceError, match="duplicate"):
        model_value_display(
            value,
            calculations={},
            assumptions={},
            output={"scenarios": {"bear": {"rows": [{"year": 2027}, {"year": 2027}]}}},
        )


def test_model_reference_cannot_certify_missing_run_or_foreign_primary(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    path = tmp_path / "explicit-temporary.db"
    migrated_db(path)
    commitment = MemoModelCommitment(
        run_id=1,
        input_sha256="a" * 64,
        receipt_sha256="b" * 64,
        research_snapshot_id="onon",
        snapshot_member_sha256="c" * 64,
        effective_inputs_sha256="d" * 64,
        model_output_sha256="e" * 64,
        scenario_acceptance_sha256="f" * 64,
    )
    with sqlite3.connect(path) as conn:
        for ticker, snapshot_id, member_sha in (
            ("DECK", "onon", "c" * 64),
            ("ONON", "peer", "c" * 64),
            ("ONON", "onon", "d" * 64),
        ):
            with pytest.raises(MemoModelEvidenceError, match="primary_context_mismatch"):
                load_verified_memo_model(
                    conn,
                    commitment,
                    primary_snapshot_id=snapshot_id,
                    primary_member_sha256=member_sha,
                    ticker=ticker,
                    as_of=NOW,
                    source_context=None,
                )
        with pytest.raises(MemoModelEvidenceError, match="readiness_unverified"):
            load_verified_memo_model(
                conn,
                commitment,
                primary_snapshot_id="onon",
                primary_member_sha256="c" * 64,
                ticker="ONON",
                as_of=NOW,
                source_context=None,
            )


def test_sensitivity_range_and_stress_are_closed_existing_selections() -> None:
    refs = (
        MemoModelValue(
            source="sensitivity",
            key="vps",
            discount_rate="0.105",
            terminal_growth="0.03",
            unit="USD_per_share",
            displayed_unit="USD_per_share",
            display_format="number2",
            displayed_value="34.48",
        ),
        MemoModelValue(
            source="stress",
            key="rent_plus_100bp",
            unit="USD_per_share",
            displayed_unit="USD_per_share",
            display_format="number2",
            displayed_value="32.19",
        ),
        MemoModelValue(
            source="bridge",
            key="ev_to_fy26_adj_ebitda_range",
            range_endpoint=1,
            unit="multiple",
            displayed_unit="multiple",
            display_format="number2",
            displayed_value="11.82",
        ),
    )
    output: dict[str, object] = {
        "gordon_sensitivity": {"0.105": {"0.03": 34.48}},
        "one_factor_stresses": {"rent_plus_100bp": 32.19},
        "bridge": {"ev_to_fy26_adj_ebitda_range": [11.23, 11.82]},
    }
    for ref in refs:
        assert (
            model_value_display(ref, calculations={}, assumptions={}, output=output)
            == ref.displayed_value
        )
    with pytest.raises(ValidationError):
        MemoModelValue.model_validate({**refs[0].model_dump(), "discount_rate": 0.11})
    with pytest.raises(ValidationError):
        MemoModelValue.model_validate({**refs[2].model_dump(), "range_endpoint": 2})
