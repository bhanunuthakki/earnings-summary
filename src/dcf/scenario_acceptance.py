"""Explicit, input-bound analyst scenario review; no trade or owner authority.

The verifier consumes a review after the model inputs and outputs were replayed.
It does not synthesize an acceptance from successful arithmetic or mint approval.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Literal, cast

from pydantic import AwareDatetime, Field

from dcf.input_evidence import FrozenModel, InputEvidenceError, ModelInputReceipt, canonical_digest

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ScenarioReview(FrozenModel):
    output_sha256: Sha256
    accepted: bool
    rationale: str = Field(min_length=20)


class ScenarioAcceptance(FrozenModel):
    schema_version: Literal["dcf_scenario_acceptance.v1"] = "dcf_scenario_acceptance.v1"
    ticker: Literal["MELI", "ONON"]
    recipe: str = Field(min_length=1)
    model: str = Field(min_length=1)
    engine_version: str = Field(min_length=1)
    reviewer: str = Field(min_length=1)
    attribution: Literal["analyst", "owner"]
    reviewed_at: AwareDatetime
    model_input_receipt_sha256: Sha256
    effective_inputs_sha256: Sha256
    actuals_sha256: Sha256
    snapshot_member_sha256: Sha256
    inventory_snapshot_ids: tuple[str, ...]
    model_output_sha256: Sha256
    scenarios: dict[str, ScenarioReview]


def verify_scenario_acceptance(
    review: ScenarioAcceptance,
    *,
    input_receipt: ModelInputReceipt,
    model: str,
    engine_version: str,
    effective_inputs: Mapping[str, float],
    output: Mapping[str, object],
    as_of: datetime,
    calculated_at: datetime,
) -> ScenarioAcceptance:
    """Verify the exact review after upstream receipt and model reconstruction.

    Only analyst research acceptance is currently executable. Owner attribution
    needs a separate verified approval authority; a caller-provided label cannot
    establish personal approval or authorize an account action.
    """
    if as_of.tzinfo is None or calculated_at.tzinfo is None:
        raise InputEvidenceError("scenario_acceptance_timezone_required")
    if review.attribution == "owner":
        raise InputEvidenceError("owner_scenario_approval_unverified")
    if (
        review.reviewed_at > as_of
        or review.reviewed_at < input_receipt.verified_at
        or calculated_at > as_of
        or calculated_at < input_receipt.verified_at
    ):
        raise InputEvidenceError("scenario_acceptance_clock_invalid")
    if (
        review.ticker != input_receipt.request.ticker
        or review.recipe != input_receipt.recipe
        or review.model != model
        or review.engine_version != engine_version
        or review.model_input_receipt_sha256
        != canonical_digest(input_receipt.model_dump(mode="json"))
        or review.effective_inputs_sha256 != canonical_digest(dict(effective_inputs))
        or review.effective_inputs_sha256 != input_receipt.effective_inputs_sha256
        or review.actuals_sha256 != input_receipt.actuals_sha256
        or review.snapshot_member_sha256 != input_receipt.snapshot_member_sha256
        or review.inventory_snapshot_ids != input_receipt.inventory_snapshot_ids
        or not review.inventory_snapshot_ids
        or review.model_output_sha256 != canonical_digest(dict(output))
    ):
        raise InputEvidenceError("scenario_acceptance_commitment_mismatch")
    raw_scenarios = output.get("scenarios")
    if not isinstance(raw_scenarios, dict):
        raise InputEvidenceError("scenario_acceptance_replay_missing")
    scenarios = cast(dict[str, object], raw_scenarios)
    expected = {"bear", "base", "bull"}
    if set(review.scenarios) != expected or set(scenarios) != expected:
        raise InputEvidenceError("scenario_acceptance_population_mismatch")
    for name, decision in review.scenarios.items():
        if not decision.accepted or decision.output_sha256 != canonical_digest(scenarios[name]):
            raise InputEvidenceError(f"scenario_acceptance_not_verified:{name}")
    return review
