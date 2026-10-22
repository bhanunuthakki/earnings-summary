"""Closed MELI memo selectors reuse native fact/model/scenario verification.

The existing synthetic model fixture isolates only research/inventory coverage.
These tests do not qualify a real company report or a recalculated workbook.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Generator
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import TypeAdapter, ValidationError

from dcf import input_evidence as evidence
from dcf import meli_inputs
from dcf.grade_evidence import load_dcf_verification_evidence
from dcf.meli_model import Assum, mirror
from dcf.readiness import load_valuation_readiness, replay_model_scenarios
from dcf.scenario_acceptance import ScenarioAcceptance, ScenarioReview
from dcf.specialized_price import SpecializedPriceObservation
from execution import build_meli_platform_dcf as builder
from provenance.research_snapshot import ResearchSnapshotRequest
from research.decision_brief import ReviewedMemoClaimV2
from research.memo_model_evidence import (
    MeliMemoModelCommitment,
    MeliMemoModelValue,
    MemoModelCommitment,
    MemoModelCommitmentReference,
    MemoModelEvidenceError,
    MemoModelValue,
    VerifiedMemoModel,
    load_verified_memo_model,
    meli_sensitivity_grid,
    model_value_display,
)
from tests.test_meli_input_evidence import (
    END,
    NOW,
    seal_model_projection_context,
    seed_real_input_facts,
    synthetic_input_values,
    synthetic_model_snapshot,
)
from tests.test_meli_input_evidence import real_inputs as real_inputs
from tests.test_meli_input_evidence import source_context as source_context


def _reference(**changes: object) -> MeliMemoModelValue:
    return MeliMemoModelValue.model_validate(
        {
            "model": "meli_platform_sotp",
            "source": "output",
            "key": "vps",
            "unit": "USD_per_share",
            "displayed_unit": "USD_per_share",
            "display_format": "number2",
            "displayed_value": "1.00",
            **changes,
        }
    )


def test_meli_units_selectors_and_context_tag_are_closed() -> None:
    payload = _reference().model_dump()
    for update in (
        {"unit": "CHF_m"},
        {"unit": "BRL_m"},
        {"model": "onon_economic_fcff"},
        {"key": "scenarios.bear.model_output.vps"},
        {"key": "__class__"},
        {"source": "reverse"},
        {"source": "sensitivity", "wacc_delta": "0.02"},
        {"source": "scenario", "scenario": None},
        {"period": 1},
    ):
        with pytest.raises(ValidationError):
            MeliMemoModelValue.model_validate(payload | update)
    without_tag = dict(payload)
    without_tag.pop("model")
    with pytest.raises(ValidationError):
        MeliMemoModelValue.model_validate(without_tag)


def test_meli_display_checks_units_scale_and_rounding_without_currency_conversion() -> None:
    reference = _reference(
        source="receipt_calculation",
        key="comm_rev0",
        unit="USD_m",
        displayed_unit="USD_bn",
        displayed_value="16.29",
    )
    assert (
        model_value_display(
            reference, calculations={"comm_rev0": 16294.0}, assumptions={}, output={}
        )
        == "16.29"
    )
    for change in (
        {"unit": "shares_m"},
        {"displayed_unit": "shares_m"},
        {"displayed_value": "16.30"},
    ):
        with pytest.raises(MemoModelEvidenceError):
            model_value_display(
                reference.model_copy(update=change),
                calculations={"comm_rev0": 16294.0},
                assumptions={},
                output={},
            )


@pytest.mark.parametrize("value", [None, True, float("nan"), float("inf"), "12.0"])
def test_meli_nonnumeric_output_never_becomes_a_fact(value: object) -> None:
    with pytest.raises(MemoModelEvidenceError):
        model_value_display(_reference(), calculations={}, assumptions={}, output={"vps": value})


def test_exact_projection_period_is_required_without_inventing_calendar_year() -> None:
    reference = _reference(
        source="scenario_row",
        scenario="base",
        key="op_rev",
        period=11,
        unit="USD_m",
        displayed_unit="USD_m",
    )
    output: dict[str, object] = {
        "scenarios": {
            "base": {
                "model_output": {
                    "rows": [{"t": 10, "op_rev": 1.0}],
                }
            }
        }
    }
    with pytest.raises(MemoModelEvidenceError, match="period_unavailable"):
        model_value_display(reference, calculations={}, assumptions={}, output=output)
    duplicate: dict[str, object] = {
        "scenarios": {
            "base": {
                "model_output": {
                    "rows": [{"t": 11, "op_rev": 1.0}, {"t": 11, "op_rev": 2.0}],
                }
            }
        }
    }
    with pytest.raises(MemoModelEvidenceError, match="duplicate"):
        model_value_display(reference, calculations={}, assumptions={}, output=duplicate)
    with pytest.raises(ValidationError):
        _reference(source="scenario_row", scenario="base", key="op_rev", period=101)


def test_closed_sensitivity_grid_matches_existing_numerical_engine() -> None:
    assumptions = Assum(derive_capm=0)
    assumptions.credit_terminal_roe = mirror(assumptions).credit_terminal_roe * (
        1 + assumptions.credit_g_term
    )
    numeric = meli_inputs.effective_numeric_inputs(asdict(assumptions))
    grid = meli_sensitivity_grid(numeric)
    for dw in ("-0.01", "0", "0.01"):
        for dk in ("-0.01", "0", "0.01"):
            result = mirror(
                replace(
                    assumptions,
                    wacc=assumptions.wacc + float(dw),
                    credit_ke=assumptions.credit_ke + float(dk),
                )
            )
            assert grid[dw][dk] == {
                "wacc": assumptions.wacc + float(dw),
                "credit_ke": assumptions.credit_ke + float(dk),
                "vps": result.vps,
            }
    reference = _reference(
        source="sensitivity",
        wacc_delta="0.01",
        credit_ke_delta="-0.01",
        displayed_value=format(grid["0.01"]["-0.01"]["vps"], ".2f"),
    )
    assert (
        model_value_display(
            reference, calculations={}, assumptions={}, output={"sensitivity": grid}
        )
        == reference.displayed_value
    )
    with pytest.raises(MemoModelEvidenceError, match="economics_invalid"):
        meli_sensitivity_grid(numeric | {"credit_ke": numeric["credit_g_term"] + 0.005})


def test_model_variant_is_retained_by_memo_claim_parser_and_display_boundary() -> None:
    reference = _reference()
    claim = ReviewedMemoClaimV2(
        block_id="a" * 64,
        passage="The model value is $1.00.",
        kind="calculation",
        rationale="Synthetic exact model selector for integration.",
        model_values=(reference,),
    )
    assert isinstance(
        ReviewedMemoClaimV2.model_validate_json(claim.model_dump_json()).model_values[0],
        MeliMemoModelValue,
    )
    with pytest.raises(MemoModelEvidenceError, match="model_mismatch"):
        VerifiedMemoModel({}, {}, {"vps": 1.0}).display(reference)
    legacy = MemoModelValue(
        source="output",
        key="vps",
        unit="USD_per_share",
        displayed_unit="USD_per_share",
        display_format="number2",
        displayed_value="1.00",
    )
    assert "model" not in legacy.model_dump()
    assert "sensitivity_sha256" not in MemoModelCommitment.model_fields


@pytest.fixture
def diagnostic_inputs(
    source_context: evidence.SourceReadContext,
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[sqlite3.Connection, evidence.ModelInputRequest], None, None]:
    """Use real source admission and replay; mock only corpus/inventory coverage."""
    conn = sqlite3.connect(migrated_db(tmp_path / "meli-diagnostics.db"))
    numeric = synthetic_input_values() | {"financing_other_expenses_reserve": 2.0}
    request = evidence.ModelInputRequest(
        recipe=meli_inputs.FINANCING_RECIPE,
        ticker="MELI",
        research_snapshot_id="snapshot",
        financial_period_end=END,
        facts={},
        assumptions={
            key: evidence.AssumptionBasis(
                value=numeric[key], attribution="analyst", rationale="Synthetic diagnostic case"
            )
            for key in meli_inputs.FINANCING_ASSUMPTION_KEYS
        },
        recipe_context={
            "schema_version": "meli_financing_bridge.v1",
            "event_key": "synthetic-notes-event",
            "contract_period_end": "2026-09-09",
            "closing_period_end": "2026-09-14",
            "diagnostics": "meli-report-driver-checks/v1",
        },
    )
    references = seed_real_input_facts(
        conn,
        source_path=source_context.content_roots[0] / "fixture.json",
        requirements=meli_inputs.requirements_for_request(request, as_of=NOW),
        amounts_override={
            "financing_issued_principal": 1000.0,
            "financing_underwriting_purchase_fraction": 0.97364,
            # Keep the synthetic residual calibration economically admissible.
            "operating_income": (
                numeric["comm_rev0"] * numeric["comm_margin_near"]
                + numeric["fpay_rev0"] * numeric["fpay_margin_near"]
                + numeric["cb0"] * (0.19 - 0.08)
            ),
        },
    )
    snapshot = synthetic_model_snapshot()
    seal_model_projection_context(conn, snapshot)

    def coverage(
        _conn: sqlite3.Connection, _request: evidence.ModelInputRequest, _cutoff: datetime
    ) -> tuple[ResearchSnapshotRequest, str, tuple[str, ...]]:
        return snapshot, "b" * 64, ("inventory",)

    monkeypatch.setattr(evidence, "verify_source_coverage", coverage)
    request = request.model_copy(update={"facts": references})
    proof = evidence.verify_model_inputs(
        conn,
        request,
        recipe=request.recipe,
        requirements=meli_inputs.requirements_for_request(request, as_of=NOW),
        effective_inputs={key: value.value for key, value in request.assumptions.items()},
        assumption_keys=meli_inputs.FINANCING_ASSUMPTION_KEYS,
        as_of=NOW,
        source_context=source_context,
    )
    actuals, _ = meli_inputs.calculate_actuals(proof)
    numeric.update({key: actuals[key] for key in meli_inputs.REPORTED_DRIVER_KEYS})
    review = evidence.AssumptionReview(
        reviewed_at=NOW,
        reviewer="synthetic analyst",
        effective_inputs_sha256=evidence.canonical_digest(numeric),
        actuals_sha256=evidence.canonical_digest(actuals),
        drivers={
            key: evidence.DriverReview(
                observed=observed,
                forecast=forecast,
                variance=forecast - observed,
                rationale="Synthetic review of the complete diagnostic driver population",
            )
            for key, (observed, forecast) in meli_inputs.review_drivers(actuals, numeric).items()
        },
    )
    try:
        yield conn, request.model_copy(update={"assumption_review": review})
    finally:
        conn.close()


@pytest.fixture
def persisted_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    source_context: evidence.SourceReadContext,
) -> Generator[
    tuple[
        sqlite3.Connection, MeliMemoModelCommitment, dict[str, float], evidence.ModelInputReceipt
    ],
    None,
    None,
]:
    inputs = request.getfixturevalue(
        "diagnostic_inputs" if getattr(request, "param", None) == "diagnostics" else "real_inputs"
    )
    assert isinstance(inputs, tuple)
    inputs = cast(tuple[object, ...], inputs)
    assert len(inputs) == 2
    conn, model_request = inputs
    assert isinstance(conn, sqlite3.Connection)
    assert isinstance(model_request, evidence.ModelInputRequest)
    numeric, receipt = meli_inputs.prepare_meli_inputs(
        conn,
        model_request,
        effective_inputs={key: item.value for key, item in model_request.assumptions.items()},
        as_of=NOW,
        source_context=source_context,
    )
    assumptions = tmp_path / "MELI-assumptions.json"
    assumptions.write_text(json.dumps({"input_evidence": model_request.model_dump(mode="json")}))

    def now(_tz: object) -> datetime:
        return NOW

    monkeypatch.setattr(builder, "datetime", SimpleNamespace(now=now))
    monkeypatch.setattr(builder, "date", SimpleNamespace(today=NOW.date))
    db = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    base, receipt = builder.load_verified_assumptions(
        "MELI", db_path=db, assumptions_path=assumptions, source_context=source_context
    )
    without_quote = getattr(request, "param", None) == "without_quote"
    base = replace(base, price=0 if without_quote else 1684)
    output = meli_inputs.model_output(numeric)
    vectors = {
        "base": numeric,
        "bear": numeric | {"comm_g_near": numeric["comm_g_near"] - 0.03},
        "bull": numeric | {"comm_g_near": numeric["comm_g_near"] + 0.03},
    }
    snapshot = {
        "model": "meli_platform_sotp",
        "effective_model_inputs": numeric,
        "model_output": output,
        "value_per_share": output["vps"],
        "equity_value_m": output["equity_value"],
        "operating_ev_m": output["operating_ev"],
        "credit_equity_value_m": output["credit_equity_value"],
        "reviewed_scenario_inputs": vectors,
    }
    scenario_output = replay_model_scenarios(
        "MELI", numeric, output, snapshot, recipe=receipt.recipe
    )
    scenario_outputs = cast(dict[str, object], scenario_output["scenarios"])
    acceptance = ScenarioAcceptance(
        ticker="MELI",
        recipe=receipt.recipe,
        model="meli_platform_sotp",
        engine_version="meli_platform_sotp_v1",
        reviewer="synthetic independent analyst",
        attribution="analyst",
        reviewed_at=NOW,
        model_input_receipt_sha256=evidence.canonical_digest(receipt.model_dump(mode="json")),
        effective_inputs_sha256=receipt.effective_inputs_sha256,
        actuals_sha256=receipt.actuals_sha256 or "",
        snapshot_member_sha256=receipt.snapshot_member_sha256,
        inventory_snapshot_ids=receipt.inventory_snapshot_ids,
        model_output_sha256=evidence.canonical_digest(scenario_output),
        scenarios={
            name: ScenarioReview(
                output_sha256=evidence.canonical_digest(value),
                accepted=True,
                rationale="Synthetic analyst reviews each exact vector and resulting valuation.",
            )
            for name, value in scenario_outputs.items()
        },
    )
    # Exercise the real producer; manual INSERT fixtures hid a missing output commitment.
    workbook = tmp_path / "synthetic-workbook.xlsx"
    workbook.write_bytes(b"synthetic path/hash fixture; no workbook qualification")
    quote = tmp_path / "synthetic-quote.json"
    quote.write_text('{"price":1684}')
    for synthetic_file in (assumptions, workbook, quote):
        os.utime(synthetic_file, (NOW.timestamp(), NOW.timestamp()))
    monkeypatch.setattr(builder, "REPO", tmp_path)
    monkeypatch.setattr(builder, "DEST", workbook)
    monkeypatch.setenv("DCF_PROMOTE_DEST", str(workbook))
    cases = {
        name: replace(base, comm_g_near=values["comm_g_near"]) for name, values in vectors.items()
    }
    conn.commit()
    assert builder.persist_dcf_run(
        base,
        mirror(base),
        {},
        SpecializedPriceObservation(
            price=0 if without_quote else 1684,
            observed_at=None if without_quote else NOW,
            source_name="unavailable" if without_quote else "synthetic retained close",
            currency="USD",
            source_path=None if without_quote else str(quote),
        ),
        db_path=db,
        input_receipt=receipt,
        assumptions_path=assumptions,
        source_context=source_context,
        cases=cases,
        scenario_acceptance=acceptance,
    )
    grade = load_dcf_verification_evidence(conn, "MELI")
    assert grade.status == "available" and grade.run_id is not None
    assert grade.assumption_snapshot is not None
    assert grade.assumption_snapshot["model_output"] == output
    commitment = MeliMemoModelCommitment(
        run_id=grade.run_id,
        input_sha256=grade.input_sha256 or "",
        receipt_sha256=evidence.canonical_digest(receipt.model_dump(mode="json")),
        research_snapshot_id=receipt.request.research_snapshot_id,
        snapshot_member_sha256=receipt.snapshot_member_sha256,
        effective_inputs_sha256=evidence.canonical_digest(numeric),
        model_output_sha256=evidence.canonical_digest(output),
        scenario_acceptance_sha256=evidence.canonical_digest(acceptance.model_dump(mode="json")),
        sensitivity_sha256=evidence.canonical_digest(meli_sensitivity_grid(numeric)),
    )
    yield conn, commitment, numeric, receipt


@pytest.mark.parametrize("persisted_model", ["without_quote"], indirect=True)
def test_native_meli_memo_without_quote_keeps_verified_model_and_blocks_allocation(
    persisted_model: tuple[
        sqlite3.Connection, MeliMemoModelCommitment, dict[str, float], evidence.ModelInputReceipt
    ],
    source_context: evidence.SourceReadContext,
) -> None:
    conn, commitment, numeric, receipt = persisted_model
    grade = load_dcf_verification_evidence(conn, "MELI")
    assert grade.live_price is None and grade.live_price_at is None
    memo = load_valuation_readiness(
        conn, "MELI", as_of=NOW, purpose="analyst_memo", source_context=source_context
    )
    assert memo.ready, memo.reason_codes
    assert memo.market_status == "missing"
    assert memo.financial_inputs and all(
        item.status == "admitted" for item in memo.financial_inputs
    )
    model = load_verified_memo_model(
        conn,
        commitment,
        primary_snapshot_id="snapshot",
        primary_member_sha256=receipt.snapshot_member_sha256,
        ticker="MELI",
        as_of=NOW,
        source_context=source_context,
    )
    assert model.output["vps"] == meli_inputs.model_output(numeric)["vps"]
    allocation = load_valuation_readiness(
        conn, "MELI", as_of=NOW, purpose="allocation", source_context=source_context
    )
    assert not allocation.ready
    assert allocation.market_status == "missing"
    assert allocation.reason_codes == ("market_price_or_timestamp_missing",)


def test_native_meli_receipt_replay_supports_actuals_cases_forecast_and_sensitivity(
    persisted_model: tuple[
        sqlite3.Connection, MeliMemoModelCommitment, dict[str, float], evidence.ModelInputReceipt
    ],
    source_context: evidence.SourceReadContext,
) -> None:
    conn, commitment, numeric, receipt = persisted_model
    readiness = load_valuation_readiness(
        conn, "MELI", as_of=NOW, purpose="analyst_memo", source_context=source_context
    )
    assert readiness.ready, readiness.reason_codes
    model = load_verified_memo_model(
        conn,
        commitment,
        primary_snapshot_id="snapshot",
        primary_member_sha256=receipt.snapshot_member_sha256,
        ticker="MELI",
        as_of=NOW,
        source_context=source_context,
    )
    actuals, _ = meli_inputs.calculate_actuals(receipt)
    assert model.calculations["comm_rev0"] == actuals["comm_rev0"]
    assert model.calculations["fpay_rev0"] == actuals["revenue_fintech"] - actuals["revenue_credit"]
    assert model.output["vps"] == meli_inputs.model_output(numeric)["vps"]
    result = _reference(displayed_value=format(cast(float, model.output["vps"]), ".2f"))
    assert model.display(result) == result.displayed_value
    bear = cast(dict[str, object], cast(dict[str, object], model.output["scenarios"])["bear"])
    bear_output = cast(dict[str, object], bear["model_output"])
    downside = _reference(
        source="scenario",
        scenario="bear",
        displayed_value=format(cast(float, bear_output["vps"]), ".2f"),
    )
    assert model.display(downside) == downside.displayed_value
    grid = meli_sensitivity_grid(numeric)
    sensitivity = _reference(
        source="sensitivity",
        wacc_delta="0.01",
        credit_ke_delta="0",
        displayed_value=format(grid["0.01"]["0"]["vps"], ".2f"),
    )
    assert model.display(sensitivity) == sensitivity.displayed_value
    legacy = MemoModelValue(
        source="output",
        key="vps",
        unit="USD_per_share",
        displayed_unit="USD_per_share",
        display_format="number2",
        displayed_value=result.displayed_value,
    )
    with pytest.raises(MemoModelEvidenceError, match="model_mismatch"):
        model.display(legacy)
    assert isinstance(
        TypeAdapter(MemoModelCommitmentReference).validate_json(commitment.model_dump_json()),
        MeliMemoModelCommitment,
    )


@pytest.mark.parametrize(
    "damage", ["primary", "sensitivity", "scenario_hash", "model_output", "cases", "owner"]
)
def test_native_meli_rejects_changed_context_commitments_or_unreviewed_cases(
    persisted_model: tuple[
        sqlite3.Connection, MeliMemoModelCommitment, dict[str, float], evidence.ModelInputReceipt
    ],
    source_context: evidence.SourceReadContext,
    damage: str,
) -> None:
    conn, commitment, _numeric, receipt = persisted_model
    if damage in {"sensitivity", "scenario_hash"}:
        field = "sensitivity_sha256" if damage == "sensitivity" else "scenario_acceptance_sha256"
        commitment = commitment.model_copy(update={field: "c" * 64})
    elif damage in {"model_output", "cases", "owner"}:
        row = conn.execute(
            "SELECT assumption_snapshot_json,provenance_json FROM dcf_runs WHERE id=?",
            (commitment.run_id,),
        ).fetchone()
        snapshot, provenance = json.loads(row[0]), json.loads(row[1])
        if damage == "model_output":
            snapshot["model_output"]["vps"] += 1
        elif damage == "cases":
            snapshot.pop("reviewed_scenario_inputs")
        else:
            provenance["scenario_acceptance"]["attribution"] = "owner"
        conn.execute(
            "UPDATE dcf_runs SET assumption_snapshot_json=?,provenance_json=? WHERE id=?",
            (json.dumps(snapshot), json.dumps(provenance), commitment.run_id),
        )
    with pytest.raises(MemoModelEvidenceError):
        load_verified_memo_model(
            conn,
            commitment,
            primary_snapshot_id="wrong" if damage == "primary" else "snapshot",
            primary_member_sha256=receipt.snapshot_member_sha256,
            ticker="MELI",
            as_of=NOW,
            source_context=source_context,
        )


@pytest.mark.parametrize("persisted_model", ["diagnostics"], indirect=True)
def test_native_persisted_diagnostics_reach_memo_reader_packet(
    persisted_model: tuple[
        sqlite3.Connection, MeliMemoModelCommitment, dict[str, float], evidence.ModelInputReceipt
    ],
    source_context: evidence.SourceReadContext,
) -> None:
    conn, commitment, _numeric, receipt = persisted_model
    assert receipt.request.recipe_context is not None
    assert receipt.request.recipe_context["diagnostics"] == "meli-report-driver-checks/v1"
    expected = {item.key: item.value for item in receipt.calculations}
    assert set(meli_inputs.MELI_DIAGNOSTIC_UNITS) <= set(expected)
    model = load_verified_memo_model(
        conn,
        commitment,
        primary_snapshot_id="snapshot",
        primary_member_sha256=receipt.snapshot_member_sha256,
        ticker="MELI",
        as_of=NOW,
        source_context=source_context,
    )
    # This is the native model read-packet population used by the joint brief.
    packet = json.loads(
        json.dumps(
            {
                "model_commitment": commitment.model_dump(mode="json"),
                "model": {
                    "calculations": model.calculations,
                    "assumptions": model.assumptions,
                    "output": model.output,
                },
            }
        )
    )
    assert packet["model"]["calculations"] == expected
    for key, unit in meli_inputs.MELI_DIAGNOSTIC_UNITS.items():
        is_fraction = unit == "fraction"
        displayed = format(expected[key] * (100 if is_fraction else 1), ".2f")
        if is_fraction:
            displayed += "%"
        selector = _reference(
            source="receipt_calculation",
            key=key,
            unit=unit,
            displayed_unit="percent" if is_fraction else unit,
            display_format="percentage2" if is_fraction else "number2",
            displayed_value=displayed,
        )
        assert model.display(selector) == displayed


@pytest.mark.parametrize("persisted_model", ["diagnostics"], indirect=True)
def test_native_persisted_diagnostic_tampering_fails_source_replay(
    persisted_model: tuple[
        sqlite3.Connection, MeliMemoModelCommitment, dict[str, float], evidence.ModelInputReceipt
    ],
    source_context: evidence.SourceReadContext,
) -> None:
    conn, commitment, numeric, receipt = persisted_model
    bad = receipt.model_copy(
        update={
            "calculations": tuple(
                item.model_copy(update={"value": item.value + 1})
                if item.key == "profit_proxy_year0"
                else item
                for item in receipt.calculations
            ),
        }
    )
    with pytest.raises(evidence.InputEvidenceError, match="model_input_receipt_mismatch"):
        meli_inputs.verify_meli_inputs(
            conn,
            bad,
            effective_inputs=numeric,
            as_of=NOW,
            source_context=source_context,
        )
    row = conn.execute(
        "SELECT provenance_json FROM dcf_runs WHERE id=?", (commitment.run_id,)
    ).fetchone()
    provenance = json.loads(row[0])
    provenance["model_input_receipt"] = bad.model_dump(mode="json")
    conn.execute(
        "UPDATE dcf_runs SET provenance_json=? WHERE id=?",
        (json.dumps(provenance), commitment.run_id),
    )
    with pytest.raises(MemoModelEvidenceError):
        load_verified_memo_model(
            conn,
            commitment,
            primary_snapshot_id="snapshot",
            primary_member_sha256=receipt.snapshot_member_sha256,
            ticker="MELI",
            as_of=NOW,
            source_context=source_context,
        )
