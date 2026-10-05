"""Scenario arithmetic and retained-request proof; coverage has its own suites.

The shared financial fixture uses real publications, admission and model replay.
Its source-coverage verifier is isolated, so these are not full memo qualification.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from dcf import cashflow_refresh
from dcf.cashflow_inputs import ASSUMPTION_KEYS, model_output, prepare_cashflow_inputs
from dcf.cashflow_refresh import PreparedCashflowDcfRequest, prepare_cashflow_dcf
from dcf.cashflow_scenarios import (
    AnalystCashflowScenario,
    AnalystCashflowScenarioReview,
    verify_analyst_cashflow_scenarios,
)
from dcf.input_evidence import AssumptionBasis, InputEvidenceError, canonical_digest
from dcf.readiness import load_valuation_readiness
from tests.test_cashflow_input_evidence import CLOCK, seed_cashflow_model_inputs
from tests.test_report_canonical_financials import database as database


def scenario_review(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    request = seed_cashflow_model_inputs(conn, monkeypatch)
    effective, receipt = prepare_cashflow_inputs(
        conn,
        request,
        effective_inputs={key: item.value for key, item in request.assumptions.items()},
        as_of=CLOCK,
    )
    cases: list[AnalystCashflowScenario] = []
    weighted = 0.0
    for name, growth, probability in (
        ("base", 0.04, 0.5),
        ("bear", 0.0, 0.25),
        ("bull", 0.08, 0.25),
    ):
        inputs = {**effective, "growth": growth}
        output = model_output(inputs)
        value = output["vps"]
        assert isinstance(value, (int, float))
        weighted += value * probability
        cases.append(
            AnalystCashflowScenario.model_validate(
                {
                    "name": name,
                    "probability": probability,
                    "probability_rationale": "Synthetic analyst weights describe this scenario distribution.",
                    "effective_inputs": inputs,
                    "assumptions": {
                        key: AssumptionBasis(
                            value=inputs[key],
                            attribution="analyst",
                            rationale="Explicit synthetic scenario assumption rationale.",
                        )
                        for key in ASSUMPTION_KEYS
                    },
                    "replay_output": output,
                }
            )
        )
    review = AnalystCashflowScenarioReview(
        ticker=request.ticker,
        base_model_input_receipt_sha256=canonical_digest(receipt.model_dump(mode="json")),
        base_effective_inputs_sha256=canonical_digest(effective),
        research_snapshot_id=request.research_snapshot_id,
        snapshot_member_sha256=receipt.snapshot_member_sha256,
        reviewer="synthetic-analyst",
        reviewed_at=CLOCK,
        rationale="Exact base, bear and bull scenarios use the admitted reported cash-flow operands.",
        scenarios=tuple(cases),
        weighted_value_per_share=weighted,
    )
    return request, effective, receipt, review


def retain(
    conn: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reviewed: bool = True,
):
    request, effective, _receipt, review = scenario_review(conn, monkeypatch)
    output = model_output(effective)
    value = output["vps"]
    assert isinstance(value, (int, float))
    prepared = PreparedCashflowDcfRequest(
        model_inputs=request,
        effective_inputs=effective,
        as_of=CLOCK,
        valuation_date=CLOCK.date(),
        market_price=float(value),
        market_observed_at=CLOCK,
        market_source="Synthetic dated market observation",
        analyst_scenario_review=review if reviewed else None,
    )
    source = tmp_path / "prepared-scenarios.json"
    source.write_text(prepared.model_dump_json())
    os.utime(source, (CLOCK.timestamp(), CLOCK.timestamp()))
    result = prepare_cashflow_dcf(
        conn,
        prepared,
        request_path=source,
        expected_request_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        repo_root=tmp_path,
        artifact_path=tmp_path / "scenario-calculation.json",
        apply=True,
    )
    assert result.version_created
    return source


def test_reviewed_scenarios_enable_only_analyst_memo_valuation(
    database: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    retain(database, tmp_path, monkeypatch)
    memo = load_valuation_readiness(database, "SYNTH", as_of=CLOCK, purpose="analyst_memo")
    assert memo.ready, memo.reason_codes
    assert memo.purpose == "analyst_memo" and memo.financial_input_completeness == "verified"
    allocation = load_valuation_readiness(database, "SYNTH", as_of=CLOCK)
    assert not allocation.ready and allocation.reason_codes == ("scenario_acceptance_unverified",)


def test_memo_without_review_remains_precisely_degraded(
    database: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    retain(database, tmp_path, monkeypatch, reviewed=False)
    memo = load_valuation_readiness(database, "SYNTH", as_of=CLOCK, purpose="analyst_memo")
    assert not memo.ready and memo.reason_codes == ("analyst_scenario_review_missing",)


def test_fresh_request_after_data_cutoff_uses_actual_capture_and_calculation_clocks(
    database: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, effective, _receipt, review = scenario_review(database, monkeypatch)
    monkeypatch.setattr(cashflow_refresh, "datetime", datetime)
    review = review.model_copy(update={"reviewed_at": datetime.now(UTC)})
    value = model_output(effective)["vps"]
    assert isinstance(value, (int, float))
    prepared = PreparedCashflowDcfRequest(
        model_inputs=request,
        effective_inputs=effective,
        as_of=CLOCK,
        valuation_date=datetime.now(ZoneInfo("America/Los_Angeles")).date(),
        market_price=float(value),
        market_observed_at=datetime.now(UTC),
        market_source="Synthetic current market observation",
        analyst_scenario_review=review,
    )
    source = tmp_path / "fresh-request.json"
    source.write_text(prepared.model_dump_json())
    assert source.stat().st_mtime > CLOCK.timestamp()
    result = prepare_cashflow_dcf(
        database,
        prepared,
        request_path=source,
        expected_request_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        repo_root=tmp_path,
        artifact_path=tmp_path / "fresh-calculation.json",
        apply=True,
    )
    assert CLOCK < review.reviewed_at <= result.source_observed_at <= result.calculated_at
    assert source.stat().st_mtime <= result.source_observed_at.timestamp()
    created = database.execute("SELECT created_at FROM dcf_runs").fetchone()[0]
    assert datetime.fromisoformat(created) == result.calculated_at
    memo = load_valuation_readiness(
        database, "SYNTH", as_of=result.calculated_at, purpose="analyst_memo"
    )
    assert "model_calculation_after_cutoff" not in memo.reason_codes
    assert "analyst_scenario_review_clock_invalid" not in memo.reason_codes
    assert "analyst_scenario_source_commitment_mismatch" not in memo.reason_codes
    assert memo.ready, memo.reason_codes


@pytest.mark.parametrize("clock", ["source", "data", "market", "valuation"])
def test_future_prepared_clocks_fail_before_retention(
    database: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: str
) -> None:
    request, effective, _receipt, review = scenario_review(database, monkeypatch)
    prepared = PreparedCashflowDcfRequest(
        model_inputs=request,
        effective_inputs=effective,
        as_of=CLOCK + timedelta(seconds=1) if clock == "data" else CLOCK,
        valuation_date=(CLOCK + timedelta(days=1)).date() if clock == "valuation" else CLOCK.date(),
        market_price=10,
        market_observed_at=CLOCK + timedelta(seconds=1) if clock == "market" else CLOCK,
        market_source="Synthetic dated market observation",
        analyst_scenario_review=review,
    )
    source = tmp_path / "future-request.json"
    source.write_text(prepared.model_dump_json())
    stamp = CLOCK.timestamp() + (1 if clock == "source" else 0)
    os.utime(source, (stamp, stamp))
    with pytest.raises(InputEvidenceError, match="prepared_cashflow_request_clock_in_future"):
        prepare_cashflow_dcf(
            database,
            prepared,
            request_path=source,
            expected_request_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            repo_root=tmp_path,
            artifact_path=tmp_path / "future-calculation.json",
            apply=True,
        )
    assert database.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == 0
    assert not (tmp_path / "future-calculation.json").exists()


@pytest.mark.parametrize(
    "change", ["source_bytes", "mtime", "source_missing", "snapshot", "model", "stale"]
)
def test_retained_scenario_proof_is_rechecked(
    database: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    source = retain(database, tmp_path, monkeypatch)
    if change == "source_bytes":
        source.write_text(source.read_text() + " ")
    elif change == "mtime":
        os.utime(source, (CLOCK.timestamp() + 1, CLOCK.timestamp() + 1))
    elif change == "source_missing":
        source.unlink()
    elif change == "snapshot":
        raw = json.loads(
            database.execute("SELECT assumption_snapshot_json FROM dcf_runs").fetchone()[0]
        )
        raw["analyst_scenario_review"]["weighted_value_per_share"] += 1
        database.execute("UPDATE dcf_runs SET assumption_snapshot_json=?", (json.dumps(raw),))
    elif change == "model":
        database.execute("UPDATE dcf_runs SET npv=npv+1")
    cutoff = CLOCK + timedelta(days=10) if change == "stale" else CLOCK
    memo = load_valuation_readiness(database, "SYNTH", as_of=cutoff, purpose="analyst_memo")
    assert not memo.ready, memo


@pytest.mark.parametrize(
    "change",
    [
        "weights",
        "population",
        "operand",
        "owner_cashflow",
        "extension",
        "boolean",
        "output",
        "weighted",
        "base",
        "clock",
        "snapshot",
        "attribution",
    ],
)
def test_unreviewed_or_false_scenarios_are_rejected(
    database: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    _request_value, effective, receipt, review = scenario_review(database, monkeypatch)
    raw = review.model_dump(mode="json")
    case = raw["scenarios"][1]
    if change == "weights":
        case["probability"] = 0.3
    elif change == "population":
        case["name"] = "base"
    elif change == "operand":
        case["effective_inputs"]["reported_cash"] += 1
    elif change == "owner_cashflow":
        case["effective_inputs"]["owner_cashflow"] += 1
    elif change == "extension":
        case["effective_inputs"]["invented_driver"] = 1
    elif change == "boolean":
        case["effective_inputs"]["growth"] = True
    elif change == "output":
        case["replay_output"]["equity_value"] += 1
    elif change == "weighted":
        raw["weighted_value_per_share"] += 1
    elif change == "base":
        raw["base_model_input_receipt_sha256"] = "0" * 64
    elif change == "clock":
        raw["reviewed_at"] = (CLOCK - timedelta(seconds=1)).isoformat()
    elif change == "snapshot":
        raw["snapshot_member_sha256"] = "0" * 64
    else:
        case["assumptions"]["growth"]["attribution"] = "owner"
    with pytest.raises(InputEvidenceError):
        verify_analyst_cashflow_scenarios(
            AnalystCashflowScenarioReview.model_validate(raw),
            receipt,
            base_effective_inputs=effective,
            as_of=CLOCK,
            calculated_at=CLOCK,
        )
