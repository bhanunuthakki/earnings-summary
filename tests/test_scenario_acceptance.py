"""Arithmetic and a narrative alone cannot silently accept a scenario vector."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import cast

import pytest
from pydantic import ValidationError

from dcf.input_evidence import (
    InputEvidenceError,
    ModelInputReceipt,
    ModelInputRequest,
    canonical_digest,
)
from dcf.scenario_acceptance import ScenarioAcceptance, verify_scenario_acceptance

NOW = datetime(2026, 10, 3, 20, tzinfo=UTC)
INPUTS = {"forecast": 0.17}
SCENARIOS: dict[str, dict[str, float]] = {
    "bear": {"vps": 16.0},
    "base": {"vps": 35.0},
    "bull": {"vps": 49.0},
}
OUTPUT: dict[str, object] = {
    "vps": 35.0,
    "scenarios": SCENARIOS,
}


def input_receipt() -> ModelInputReceipt:
    return ModelInputReceipt(
        recipe="onon-cash-rent-sbc-inputs/v1",
        request=ModelInputRequest(
            recipe="onon-cash-rent-sbc-inputs/v1",
            ticker="ONON",
            research_snapshot_id="source-snapshot",
            financial_period_end=date(2026, 6, 30),
            facts={},
            assumptions={},
        ),
        verified_at=NOW - timedelta(minutes=1),
        snapshot_member_sha256="a" * 64,
        effective_inputs_sha256=canonical_digest(INPUTS),
        required_keys=(),
        inputs=(),
        inventory_snapshot_ids=("inventory",),
        actuals_sha256="b" * 64,
        model_output_sha256=canonical_digest(OUTPUT),
    )


def acceptance_payload() -> dict[str, object]:
    receipt = input_receipt()
    return {
        "schema_version": "dcf_scenario_acceptance.v1",
        "ticker": "ONON",
        "recipe": receipt.recipe,
        "model": "onon_economic_fcff",
        "engine_version": "onon_economic_fcff_v1",
        "reviewer": "analyst-review",
        "attribution": "analyst",
        "reviewed_at": NOW,
        "model_input_receipt_sha256": canonical_digest(receipt.model_dump(mode="json")),
        "effective_inputs_sha256": receipt.effective_inputs_sha256,
        "actuals_sha256": receipt.actuals_sha256,
        "snapshot_member_sha256": receipt.snapshot_member_sha256,
        "inventory_snapshot_ids": receipt.inventory_snapshot_ids,
        "model_output_sha256": canonical_digest(OUTPUT),
        "scenarios": {
            name: {
                "output_sha256": canonical_digest(SCENARIOS[name]),
                "accepted": True,
                "rationale": "Reviewed growth, margins, cash rent, reinvestment and terminal dependence.",
            }
            for name in ("bear", "base", "bull")
        },
    }


def verify(payload: dict[str, object]) -> ScenarioAcceptance:
    return verify_scenario_acceptance(
        ScenarioAcceptance.model_validate(payload),
        input_receipt=input_receipt(),
        model="onon_economic_fcff",
        effective_inputs=INPUTS,
        output=OUTPUT,
        engine_version="onon_economic_fcff_v1",
        as_of=NOW,
        calculated_at=NOW,
    )


def test_exact_explicit_analyst_review_is_research_acceptance() -> None:
    assert verify(acceptance_payload()).attribution == "analyst"


@pytest.mark.parametrize(
    "field",
    [
        "model_input_receipt_sha256",
        "effective_inputs_sha256",
        "actuals_sha256",
        "snapshot_member_sha256",
        "model_output_sha256",
        "recipe",
        "model",
        "engine_version",
        "ticker",
    ],
)
def test_changed_bound_authority_or_forecast_requires_new_acceptance(field: str) -> None:
    payload = acceptance_payload()
    payload[field] = (
        "c" * 64 if field.endswith("sha256") else "MELI" if field == "ticker" else "other"
    )
    with pytest.raises(InputEvidenceError, match="scenario_acceptance"):
        verify(payload)


@pytest.mark.parametrize(
    "damage",
    [
        "future",
        "before-input-verification",
        "inventory",
        "scenario-output",
        "rejected",
        "missing-scenario",
    ],
)
def test_review_population_clocks_outputs_and_decision_fail_closed(damage: str) -> None:
    payload = acceptance_payload()
    if damage == "future":
        payload["reviewed_at"] = NOW + timedelta(seconds=1)
    elif damage == "before-input-verification":
        payload["reviewed_at"] = NOW - timedelta(hours=1)
    elif damage == "inventory":
        payload["inventory_snapshot_ids"] = ("different",)
    else:
        scenarios = payload["scenarios"]
        assert isinstance(scenarios, dict)
        if damage == "missing-scenario":
            scenarios.pop("bear")
        else:
            bear = cast(dict[str, object], scenarios["bear"])
            assert isinstance(bear, dict)
            bear["output_sha256" if damage == "scenario-output" else "accepted"] = (
                "d" * 64 if damage == "scenario-output" else False
            )
    with pytest.raises((InputEvidenceError, ValidationError)):
        verify(payload)


def test_analyst_review_cannot_be_relabelled_as_owner_approval() -> None:
    payload = acceptance_payload()
    payload["attribution"] = "owner"
    with pytest.raises(InputEvidenceError, match="owner_scenario_approval_unverified"):
        verify(payload)


def test_changed_actual_receipt_or_output_cannot_reuse_an_old_acceptance() -> None:
    review = ScenarioAcceptance.model_validate(acceptance_payload())
    changed = input_receipt().model_copy(update={"actuals_sha256": "d" * 64})
    with pytest.raises(InputEvidenceError, match="scenario_acceptance"):
        verify_scenario_acceptance(
            review,
            input_receipt=changed,
            model="onon_economic_fcff",
            engine_version="onon_economic_fcff_v1",
            effective_inputs=INPUTS,
            output=OUTPUT,
            as_of=NOW,
            calculated_at=NOW,
        )
    with pytest.raises(InputEvidenceError, match="scenario_acceptance"):
        verify_scenario_acceptance(
            review,
            input_receipt=input_receipt(),
            model="onon_economic_fcff",
            engine_version="onon_economic_fcff_v1",
            effective_inputs={"forecast": 0.20},
            output=OUTPUT,
            as_of=NOW,
            calculated_at=NOW,
        )
