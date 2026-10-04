"""Analyst memo scenarios for the bounded operating cash-flow equity recipe.

This evidence certifies arithmetic and attributed scenario assumptions. It
does not grant owner thesis approval, allocation authority, or trade permission.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import AwareDatetime, Field

from dcf.cashflow_inputs import (
    ASSUMPTION_KEYS,
    RECIPE,
    calculate_actuals,
    effective_numeric_inputs,
    model_output,
)
from dcf.input_evidence import (
    AssumptionBasis,
    FrozenModel,
    InputEvidenceError,
    ModelInputReceipt,
    canonical_digest,
)


class AnalystCashflowScenario(FrozenModel):
    name: Literal["base", "bear", "bull"]
    probability: float = Field(gt=0, le=1, strict=True)
    probability_rationale: str = Field(min_length=20)
    effective_inputs: dict[str, object]
    assumptions: dict[str, AssumptionBasis]
    replay_output: dict[str, object]


class AnalystCashflowScenarioReview(FrozenModel):
    schema_version: Literal["analyst_cashflow_scenarios.v1"] = "analyst_cashflow_scenarios.v1"
    recipe: Literal["operating_cashflow_equity.v1"] = RECIPE
    ticker: str
    base_model_input_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_effective_inputs_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    research_snapshot_id: str
    snapshot_member_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer: str = Field(min_length=1)
    reviewed_at: AwareDatetime
    rationale: str = Field(min_length=30)
    scenarios: tuple[AnalystCashflowScenario, ...] = Field(min_length=3, max_length=3)
    weighted_value_per_share: float = Field(gt=0, strict=True)
    owner_approval: Literal[False] = False
    allocation_permission: Literal[False] = False


class AnalystCashflowScenarioSource(FrozenModel):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(gt=0, strict=True)
    observed_at: AwareDatetime
    data_cutoff_at: AwareDatetime
    mtime: float


def verify_analyst_scenario_source(
    source: AnalystCashflowScenarioSource,
    review: AnalystCashflowScenarioReview,
    receipt: ModelInputReceipt,
    *,
    base_effective_inputs: dict[str, float],
    calculated_at: datetime,
) -> None:
    """Recheck the exact retained analyst request, including its capture clock."""
    path = Path(source.path)
    try:
        raw = path.read_bytes() if path.is_absolute() else b""
        stat = path.stat()
        decoded: object = json.loads(raw)
        if not isinstance(decoded, dict):
            raise ValueError("analyst scenario request must be an object")
        request = cast(dict[str, object], decoded)
        clock = request.get("as_of")
        request_clock = datetime.fromisoformat(clock) if isinstance(clock, str) else None
    except (OSError, ValueError):
        raise InputEvidenceError("analyst_scenario_source_unavailable") from None
    if (
        len(raw) != source.byte_size
        or hashlib.sha256(raw).hexdigest() != source.sha256
        or stat.st_mtime != source.mtime
        or source.mtime > source.observed_at.timestamp()
        or not source.data_cutoff_at <= source.observed_at <= calculated_at
        or source.data_cutoff_at != receipt.verified_at
        or request.get("analyst_scenario_review") != review.model_dump(mode="json")
        or request.get("model_inputs") != receipt.request.model_dump(mode="json")
        or request.get("effective_inputs") != base_effective_inputs
        or request_clock != source.data_cutoff_at
    ):
        raise InputEvidenceError("analyst_scenario_source_commitment_mismatch")


def verify_analyst_cashflow_scenarios(
    review: AnalystCashflowScenarioReview,
    receipt: ModelInputReceipt,
    *,
    base_effective_inputs: dict[str, float],
    as_of: datetime,
    calculated_at: datetime,
) -> None:
    """Reconstruct every case from the admitted base facts and reviewed assumptions."""
    if (
        receipt.recipe != RECIPE
        or review.ticker != receipt.request.ticker
        or review.base_model_input_receipt_sha256
        != canonical_digest(receipt.model_dump(mode="json"))
        or review.base_effective_inputs_sha256 != canonical_digest(base_effective_inputs)
        or review.research_snapshot_id != receipt.request.research_snapshot_id
        or review.snapshot_member_sha256 != receipt.snapshot_member_sha256
    ):
        raise InputEvidenceError("analyst_scenario_base_commitment_mismatch")
    if not receipt.verified_at <= review.reviewed_at <= calculated_at <= as_of:
        raise InputEvidenceError("analyst_scenario_review_clock_invalid")
    if tuple(case.name for case in review.scenarios) != ("base", "bear", "bull"):
        raise InputEvidenceError("analyst_scenario_population_mismatch")
    if abs(sum(case.probability for case in review.scenarios) - 1) > 1e-12:
        raise InputEvidenceError("analyst_scenario_probabilities_invalid")
    actuals, _ = calculate_actuals(receipt)
    weighted = 0.0
    prices: list[float] = []
    for case in review.scenarios:
        inputs = effective_numeric_inputs(case.effective_inputs)
        if frozenset(case.assumptions) != ASSUMPTION_KEYS or any(
            item.attribution != "analyst" or item.value != inputs[key]
            for key, item in case.assumptions.items()
        ):
            raise InputEvidenceError("analyst_scenario_assumption_attribution_mismatch")
        if any(
            inputs[key] != actuals[key]
            for key in ("reported_cash", "reported_debt", "reported_shares")
        ) or inputs["owner_cashflow"] != (
            actuals["reported_fcf_after_sbc"]
            + inputs["cashflow_normalization"]
            - inputs["incremental_capex"]
        ):
            raise InputEvidenceError("analyst_scenario_reported_operand_mismatch")
        if case.name == "base" and inputs != base_effective_inputs:
            raise InputEvidenceError("analyst_scenario_base_inputs_mismatch")
        output = model_output(inputs)
        if canonical_digest(output) != canonical_digest(case.replay_output):
            raise InputEvidenceError("analyst_scenario_output_replay_mismatch")
        value = output["vps"]
        if not isinstance(value, (float, int)):
            raise InputEvidenceError("analyst_scenario_output_invalid")
        prices.append(float(value))
        weighted += float(value) * case.probability
    if not prices[1] <= prices[0] <= prices[2]:
        raise InputEvidenceError("analyst_scenario_value_order_invalid")
    if abs(weighted - review.weighted_value_per_share) > max(1e-9, abs(weighted) * 1e-12):
        raise InputEvidenceError("analyst_scenario_weighted_output_mismatch")
