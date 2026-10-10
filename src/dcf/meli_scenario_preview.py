"""Pure explicit-vector MELI calculations; no current-source or acceptance authority."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import AwareDatetime, Field, StrictFloat

from dcf.input_evidence import FrozenModel, ModelInputReceipt, canonical_digest
from dcf.meli_inputs import (
    ASSUMPTION_KEYS,
    RECIPE,
    REPORTED_DRIVER_KEYS,
    calculate_actuals,
    effective_numeric_inputs,
    model_output,
)
from dcf.meli_model import validate_credit_terminal

Role = Literal["base", "bull", "normal_bear", "severe_stress"]
ROLES = ("base", "bull", "normal_bear", "severe_stress")
PROBABILITY_ROLES = frozenset({"base", "bull", "normal_bear"})
BRIDGE_KEYS = frozenset(
    {"credit_cash_allocation", "operating_cash_reserve", "credit_funding_debt_allocation"}
)
SIMPLEX_ABS_TOLERANCE = 1e-9
# A computational ceiling, not a selected forecast or holding period.
MAX_PREVIEW_FORECAST_YEARS = 100
ENGINE_VERSION: Literal["meli_platform_sotp_v1"] = "meli_platform_sotp_v1"


class ExplicitCase(FrozenModel):
    role: Role
    membership: Literal["probability_leg", "stress_only"]
    vector: dict[str, StrictFloat]
    rationale: str = Field(min_length=10, max_length=4000)
    reference_ids: tuple[str, ...] = Field(min_length=1, max_length=32)


class ExplicitPriors(FrozenModel):
    weights: dict[Role, StrictFloat]
    rationale: str = Field(min_length=10, max_length=4000)
    reference_ids: tuple[str, ...] = Field(min_length=1, max_length=32)


class DeclaredQuote(FrozenModel):
    price_per_share_usd: StrictFloat = Field(gt=0)
    observed_at: AwareDatetime
    observation_identity: str = Field(min_length=1, max_length=256)


class MeliScenarioRequest(FrozenModel):
    receipt: ModelInputReceipt | None = None
    cases: tuple[ExplicitCase, ...] = Field(default=(), max_length=4)
    priors: ExplicitPriors | None = None
    quote: DeclaredQuote | None = None


class CaseCalculation(FrozenModel):
    role: Role
    membership: Literal["probability_leg", "stress_only"]
    effective_inputs_sha256: str
    model_output_sha256: str
    value_per_share_usd: float
    # Model-owned derived outputs; never issuer-reported observations.
    output: dict[str, object]


class MeliScenarioPreview(FrozenModel):
    schema_version: Literal["meli-explicit-scenario-preview/v1"] = (
        "meli-explicit-scenario-preview/v1"
    )
    state: Literal["incomplete", "calculated_unaccepted"]
    calculation_reasons: tuple[str, ...]
    candidate_sha256: str | None
    request: MeliScenarioRequest
    engine_version: Literal["meli_platform_sotp_v1"] = ENGINE_VERSION
    currency: Literal["USD"] = "USD"
    input_units: Literal["USD_m_and_million_shares"] = "USD_m_and_million_shares"
    cases: tuple[CaseCalculation, ...] = ()
    probability_weighted_present_value_per_share_usd: float | None = None
    present_valuation_upside: float | None = None
    scenario_acceptance: Literal["unverified"] = "unverified"
    prior_acceptance: Literal["unverified"] = "unverified"
    model_ready: Literal[False] = False
    allocation_eligible: Literal[False] = False
    forward_return: None = None
    unavailable_reasons: tuple[str, ...] = (
        "scenario_acceptance_unverified",
        "prior_acceptance_unverified",
        "current_source_reverification_not_performed",
        "forward_return_context_unavailable",
    )


def preview_meli_scenarios(request: MeliScenarioRequest) -> MeliScenarioPreview:
    """Replay supplied prior input commitments and explicit numerical vectors.

    No SQLite, source reads, assumptions loader, seeds, workbook or publication.
    This cannot reverify a current input receipt or close any readiness gate.
    Base, bull and normal bear are probability roles. Severe stress is either
    an explicitly weighted probability role or a separate stress diagnostic.
    The bridge stays fixed to the reviewed base package; a changed allocation
    needs a separate reconciled assumption/input package, never a forged receipt.
    """

    def result(
        reasons: tuple[str, ...],
        cases: tuple[CaseCalculation, ...] = (),
        weighted: float | None = None,
        upside: float | None = None,
    ) -> MeliScenarioPreview:
        # Roles own economic ordering; supplied tuple order has no semantics.
        payload = request.model_dump(mode="json", exclude={"receipt", "cases"})
        payload["cases"] = [
            case.model_dump(mode="json")
            for role in ROLES
            for case in request.cases
            if case.role == role
        ]
        if request.receipt is not None:
            # Exclude only fresh physical-read clocks. Preserve financial and
            # review clocks, source commitments, policy and quote observation.
            basis = request.receipt.model_dump(mode="json", exclude={"raw_documents"})
            basis["raw_documents"] = [
                document.model_dump(mode="json", exclude={"verified_at"})
                for document in request.receipt.raw_documents
            ]
            payload["receipt"] = basis
        else:
            payload["receipt"] = None
        digest = (
            canonical_digest(
                {"request": payload, "cases": [c.model_dump(mode="json") for c in cases]}
            )
            if cases
            else None
        )
        return MeliScenarioPreview(
            state="incomplete" if reasons else "calculated_unaccepted",
            calculation_reasons=reasons,
            candidate_sha256=digest,
            request=request,
            cases=cases,
            probability_weighted_present_value_per_share_usd=weighted,
            present_valuation_upside=upside,
        )

    if request.receipt is None:
        return result(("base_input_package_missing",))
    receipt = request.receipt
    if receipt.recipe != RECIPE or receipt.model_output_sha256 is None:
        return result(("base_input_package_incomplete",))
    if (
        receipt.schema_version != "dcf_model_inputs.v3"
        or receipt.source_integrity != "present_bytes_verified"
    ):
        return result(("base_input_package_legacy_source_unverified",))
    if len(request.cases) != 4 or {c.role for c in request.cases} != set(ROLES):
        return result(("explicit_case_population_missing_or_duplicate",))
    by_role = {c.role: c for c in request.cases}
    numeric: dict[str, dict[str, float]] = {}
    outputs: list[CaseCalculation] = []
    try:
        for role in ROLES:
            case = by_role[role]
            if len(case.rationale.strip()) < 10 or any(not v.strip() for v in case.reference_ids):
                return result(("case_rationale_or_reference_missing:" + role,))
            if frozenset(case.vector) != ASSUMPTION_KEYS | REPORTED_DRIVER_KEYS:
                return result(("explicit_case_vector_population_invalid:" + role,))
            values = effective_numeric_inputs(case.vector)
            if (
                not 2 <= values["years"] <= MAX_PREVIEW_FORECAST_YEARS
                or values["years"] != int(values["years"])
                or values["wacc"] <= 0
            ):
                return result(("case_horizon_or_discount_invalid:" + role,))
            validate_credit_terminal(values)
            numeric[role] = values
        base = numeric["base"]
        actuals, _calculations = calculate_actuals(receipt)
        if (
            canonical_digest(base) != receipt.effective_inputs_sha256
            or canonical_digest(actuals) != receipt.actuals_sha256
            or any(base[key] != actuals[key] for key in REPORTED_DRIVER_KEYS)
            or canonical_digest(model_output(base)) != receipt.model_output_sha256
        ):
            return result(("base_input_commitment_replay_mismatch",))
        for role in ROLES:
            values = numeric[role]
            if values["years"] != base["years"]:
                return result(("common_forecast_sequence_unavailable:" + role,))
            if any(values[key] != base[key] for key in REPORTED_DRIVER_KEYS | BRIDGE_KEYS):
                return result(("case_reported_or_bridge_change_requires_input_package:" + role,))
            output = model_output(values)
            digest = canonical_digest(output)  # Refuses nonfinite nested outputs.
            value = output["vps"]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                return result(("case_output_unavailable:" + role,))
            outputs.append(
                CaseCalculation(
                    role=by_role[role].role,
                    membership=by_role[role].membership,
                    effective_inputs_sha256=canonical_digest(values),
                    model_output_sha256=digest,
                    value_per_share_usd=value,
                    output=output,
                )
            )
    except (ValueError, KeyError, OverflowError, ZeroDivisionError):
        return result(("case_or_base_economic_replay_invalid",))
    calculated = tuple(outputs)
    probability_roles = frozenset(
        c.role for c in request.cases if c.membership == "probability_leg"
    )
    if not PROBABILITY_ROLES.issubset(probability_roles):
        return result(("scenario_membership_contract_unavailable",), calculated)
    if request.priors is None:
        return result(("explicit_priors_missing",), calculated)
    if len(request.priors.rationale.strip()) < 10 or any(
        not v.strip() for v in request.priors.reference_ids
    ):
        return result(("prior_rationale_or_reference_missing",), calculated)
    weights = request.priors.weights
    if frozenset(weights) != probability_roles or any(
        v < 0 or not math.isfinite(v) for v in weights.values()
    ):
        return result(("explicit_prior_population_or_value_invalid",), calculated)
    try:
        total = math.fsum(weights.values())
        if total <= 0 or not math.isclose(total, 1.0, rel_tol=0, abs_tol=SIMPLEX_ABS_TOLERANCE):
            return result(("scenario_probability_mass_unavailable",), calculated)
        weighted = math.fsum(
            weights[c.role] * c.value_per_share_usd
            for c in calculated
            if c.role in probability_roles
        )
        if not math.isfinite(weighted):
            return result(("weighted_present_value_nonfinite",), calculated)
        if request.quote is None:
            return result(("declared_quote_missing",), calculated, weighted)
        upside = weighted / request.quote.price_per_share_usd - 1.0
        if not math.isfinite(upside):
            return result(("present_valuation_upside_nonfinite",), calculated)
    except (OverflowError, ValueError, ZeroDivisionError):
        return result(("scenario_probability_calculation_unavailable",), calculated)
    return result((), calculated, weighted, upside)
