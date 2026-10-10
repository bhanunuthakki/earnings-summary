"""Pure synthetic scenario replay using current admitted input fixtures.

Research/source inventory coverage is mocked by the current fixture. Its fact
publication, ontology, resolution, raw bytes and model verifier remain real.
This proves computation boundaries, not source coverage or financial acceptance.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from pydantic import ValidationError

from dcf import meli_inputs
from dcf.input_evidence import ModelInputReceipt, ModelInputRequest, SourceReadContext
from dcf.meli_scenario_preview import (
    DeclaredQuote,
    ExplicitCase,
    ExplicitPriors,
    MeliScenarioRequest,
    Role,
    preview_meli_scenarios,
)
from tests import test_meli_input_evidence as fixtures

real_inputs = fixtures.real_inputs
source_context = fixtures.source_context


@pytest.fixture
def package(
    real_inputs: tuple[sqlite3.Connection, ModelInputRequest],
    source_context: SourceReadContext,
) -> tuple[ModelInputReceipt, dict[str, float]]:
    conn, request = real_inputs
    conn.execute("PRAGMA query_only=ON")
    before = conn.total_changes
    values, receipt = meli_inputs.prepare_meli_inputs(
        conn,
        request,
        effective_inputs={key: basis.value for key, basis in request.assumptions.items()},
        as_of=fixtures.NOW,
        source_context=source_context,
    )
    assert conn.total_changes == before
    assert receipt.schema_version == "dcf_model_inputs.v3"
    assert receipt.source_integrity == "present_bytes_verified" and receipt.raw_documents
    assert receipt.model_output_sha256 is not None
    return receipt, values


@pytest.fixture
def scenario_request(package: tuple[ModelInputReceipt, dict[str, float]]) -> MeliScenarioRequest:
    receipt, inputs = package
    # Explicit test vectors and priors are not a recommended economic package.
    memberships: tuple[tuple[Role, Literal["probability_leg", "stress_only"]], ...] = (
        ("base", "probability_leg"),
        ("bull", "probability_leg"),
        ("normal_bear", "probability_leg"),
        ("severe_stress", "stress_only"),
    )
    cases = tuple(
        ExplicitCase(
            role=role,
            membership=membership,
            vector=dict(inputs),
            rationale="Synthetic explicit vector; no owner acceptance",
            reference_ids=("synthetic-review",),
        )
        for role, membership in memberships
    )
    return MeliScenarioRequest(
        receipt=receipt,
        cases=cases,
        priors=ExplicitPriors(
            weights={"base": 0.5, "bull": 0.3, "normal_bear": 0.2},
            rationale="Synthetic probabilities; no owner acceptance",
            reference_ids=("synthetic-priors",),
        ),
        quote=DeclaredQuote(
            price_per_share_usd=100.0,
            observed_at=datetime(2026, 10, 4, tzinfo=UTC),
            observation_identity="synthetic-quote",
        ),
    )


def test_public_base_replay_is_unaccepted(scenario_request: MeliScenarioRequest) -> None:
    result = preview_meli_scenarios(scenario_request)
    assert result.state == "calculated_unaccepted", result.calculation_reasons
    assert len(result.cases) == 4
    assert scenario_request.receipt is not None
    assert result.cases[0].model_output_sha256 == scenario_request.receipt.model_output_sha256
    assert result.probability_weighted_present_value_per_share_usd == pytest.approx(
        result.cases[0].value_per_share_usd
    )
    assert result.present_valuation_upside == pytest.approx(
        result.cases[0].value_per_share_usd / 100 - 1
    )
    assert result.model_ready is False and result.allocation_eligible is False
    assert result.forward_return is None
    assert result.scenario_acceptance == result.prior_acceptance == "unverified"
    assert "current_source_reverification_not_performed" in result.unavailable_reasons
    assert result == preview_meli_scenarios(scenario_request)


def test_roles_order_semantics_and_clock_commitments(scenario_request: MeliScenarioRequest) -> None:
    result = preview_meli_scenarios(scenario_request)
    reordered = scenario_request.model_copy(
        update={"cases": tuple(reversed(scenario_request.cases))}
    )
    assert preview_meli_scenarios(reordered).candidate_sha256 == result.candidate_sha256
    assert scenario_request.receipt is not None and scenario_request.quote is not None
    receipt = scenario_request.receipt.model_copy(
        update={
            "raw_documents": tuple(
                d.model_copy(update={"verified_at": d.verified_at + timedelta(seconds=1)})
                for d in scenario_request.receipt.raw_documents
            ),
        }
    )
    fresh = preview_meli_scenarios(scenario_request.model_copy(update={"receipt": receipt}))
    assert fresh.candidate_sha256 == result.candidate_sha256
    assert fresh.request.receipt == receipt
    quote = scenario_request.quote.model_copy(
        update={"observed_at": scenario_request.quote.observed_at + timedelta(seconds=1)}
    )
    assert (
        preview_meli_scenarios(
            scenario_request.model_copy(update={"quote": quote})
        ).candidate_sha256
        != result.candidate_sha256
    )


def test_financial_receipt_cutoff_changes_candidate_identity(
    scenario_request: MeliScenarioRequest,
) -> None:
    assert scenario_request.receipt is not None
    original = preview_meli_scenarios(scenario_request)
    changed_receipt = scenario_request.receipt.model_copy(
        update={"verified_at": scenario_request.receipt.verified_at + timedelta(seconds=1)}
    )
    changed = preview_meli_scenarios(
        scenario_request.model_copy(update={"receipt": changed_receipt})
    )
    assert original.state == changed.state == "calculated_unaccepted"
    assert original.cases == changed.cases
    assert original.candidate_sha256 != changed.candidate_sha256
    assert changed.model_ready is False and changed.allocation_eligible is False
    assert changed.forward_return is None


def test_missing_and_duplicate_cases(scenario_request: MeliScenarioRequest) -> None:
    assert preview_meli_scenarios(MeliScenarioRequest()).calculation_reasons == (
        "base_input_package_missing",
    )
    for cases in (
        scenario_request.cases[:-1],
        (*scenario_request.cases[:-1], scenario_request.cases[0]),
    ):
        result = preview_meli_scenarios(scenario_request.model_copy(update={"cases": cases}))
        assert result.calculation_reasons == ("explicit_case_population_missing_or_duplicate",)
        assert not result.cases and result.present_valuation_upside is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True])
def test_nonfinite_or_boolean_vectors_refused(
    scenario_request: MeliScenarioRequest, value: float | bool
) -> None:
    vector = dict(scenario_request.cases[0].vector)
    vector["wacc"] = value
    with pytest.raises(ValidationError):
        ExplicitCase(
            role="base",
            membership="probability_leg",
            vector=vector,
            rationale="Synthetic invalid boundary",
            reference_ids=("synthetic",),
        )


def test_no_probability_renormalization(scenario_request: MeliScenarioRequest) -> None:
    assert scenario_request.priors is not None
    prior = scenario_request.priors.model_copy(
        update={"weights": {"base": 0.2, "bull": 0.2, "normal_bear": 0.2}}
    )
    result = preview_meli_scenarios(scenario_request.model_copy(update={"priors": prior}))
    assert result.calculation_reasons == ("scenario_probability_mass_unavailable",)
    assert (
        len(result.cases) == 4 and result.probability_weighted_present_value_per_share_usd is None
    )
    assert result.present_valuation_upside is None
    assert preview_meli_scenarios(
        scenario_request.model_copy(update={"priors": None})
    ).calculation_reasons == ("explicit_priors_missing",)


def test_explicit_four_leg_membership_and_exact_prior_population(
    scenario_request: MeliScenarioRequest,
) -> None:
    stress = scenario_request.cases[3].model_copy(update={"membership": "probability_leg"})
    four = scenario_request.model_copy(update={"cases": (*scenario_request.cases[:3], stress)})
    missing = preview_meli_scenarios(four)
    assert missing.calculation_reasons == ("explicit_prior_population_or_value_invalid",)
    assert missing.present_valuation_upside is None
    assert scenario_request.priors is not None
    prior = scenario_request.priors.model_copy(
        update={"weights": {"base": 0.4, "bull": 0.3, "normal_bear": 0.2, "severe_stress": 0.1}}
    )
    # A supplied stress weight cannot be silently ignored when stress is diagnostic.
    extra = preview_meli_scenarios(scenario_request.model_copy(update={"priors": prior}))
    assert extra.calculation_reasons == ("explicit_prior_population_or_value_invalid",)
    result = preview_meli_scenarios(four.model_copy(update={"priors": prior}))
    assert result.state == "calculated_unaccepted", result.calculation_reasons
    assert len(result.cases) == 4
    assert result.probability_weighted_present_value_per_share_usd == pytest.approx(
        sum(prior.weights[case.role] * case.value_per_share_usd for case in result.cases)
    )
    assert result.model_ready is False and result.scenario_acceptance == "unverified"
    bear = scenario_request.cases[2].model_copy(update={"membership": "stress_only"})
    missing_bear = preview_meli_scenarios(
        scenario_request.model_copy(
            update={"cases": (*scenario_request.cases[:2], bear, scenario_request.cases[3])}
        )
    )
    assert missing_bear.calculation_reasons == ("scenario_membership_contract_unavailable",)


@pytest.mark.parametrize(
    "key",
    [
        "shares",
        "credit_cash_allocation",
        "operating_cash_reserve",
        "credit_funding_debt_allocation",
    ],
)
def test_changed_reported_or_bridge_needs_input_package(
    scenario_request: MeliScenarioRequest, key: str
) -> None:
    vector = dict(scenario_request.cases[1].vector)
    vector[key] += 1.0
    bull = scenario_request.cases[1].model_copy(update={"vector": vector})
    result = preview_meli_scenarios(
        scenario_request.model_copy(
            update={"cases": (scenario_request.cases[0], bull, *scenario_request.cases[2:])}
        )
    )
    assert result.calculation_reasons == (
        "case_reported_or_bridge_change_requires_input_package:bull",
    )
    assert result.present_valuation_upside is None


def test_missing_vector_and_base_commitment_mismatch(scenario_request: MeliScenarioRequest) -> None:
    vector = dict(scenario_request.cases[0].vector)
    vector.pop("shares")
    base = scenario_request.cases[0].model_copy(update={"vector": vector})
    assert preview_meli_scenarios(
        scenario_request.model_copy(update={"cases": (base, *scenario_request.cases[1:])})
    ).calculation_reasons == ("explicit_case_vector_population_invalid:base",)
    vector = dict(scenario_request.cases[0].vector)
    vector["wacc"] += 0.01
    base = scenario_request.cases[0].model_copy(update={"vector": vector})
    assert preview_meli_scenarios(
        scenario_request.model_copy(update={"cases": (base, *scenario_request.cases[1:])})
    ).calculation_reasons == ("base_input_commitment_replay_mismatch",)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True])
def test_nonfinite_or_boolean_priors_refused(value: float | bool) -> None:
    with pytest.raises(ValidationError):
        ExplicitPriors(
            weights={"base": value, "bull": 0.3, "normal_bear": 0.2},
            rationale="Synthetic invalid prior boundary",
            reference_ids=("synthetic",),
        )


def test_legacy_receipt_and_missing_quote_are_unavailable(
    scenario_request: MeliScenarioRequest,
) -> None:
    assert scenario_request.receipt is not None
    legacy = scenario_request.receipt.model_copy(update={"schema_version": "dcf_model_inputs.v2"})
    refusal = preview_meli_scenarios(scenario_request.model_copy(update={"receipt": legacy}))
    assert refusal.calculation_reasons == ("base_input_package_legacy_source_unverified",)
    assert not refusal.cases
    missing_quote = preview_meli_scenarios(scenario_request.model_copy(update={"quote": None}))
    assert missing_quote.calculation_reasons == ("declared_quote_missing",)
    assert missing_quote.probability_weighted_present_value_per_share_usd is not None
    assert missing_quote.present_valuation_upside is None
    assert missing_quote.model_ready is False


def test_distinct_explicit_cases_weight_only_probability_members(
    scenario_request: MeliScenarioRequest,
) -> None:
    cases = [scenario_request.cases[0]]
    for case, delta in zip(scenario_request.cases[1:], (0.03, -0.03, -0.15), strict=True):
        vector = dict(case.vector)
        vector["comm_g_near"] += delta
        cases.append(case.model_copy(update={"vector": vector}))
    result = preview_meli_scenarios(scenario_request.model_copy(update={"cases": tuple(cases)}))
    assert result.state == "calculated_unaccepted", result.calculation_reasons
    base, bull, bear, stress = result.cases
    assert len({case.value_per_share_usd for case in result.cases}) == 4
    assert result.probability_weighted_present_value_per_share_usd == pytest.approx(
        0.5 * base.value_per_share_usd
        + 0.3 * bull.value_per_share_usd
        + 0.2 * bear.value_per_share_usd
    )
    assert stress.role == "severe_stress" and stress.membership == "stress_only"
    assert bear.role == "normal_bear" and bear.membership == "probability_leg"
    assert result.scenario_acceptance == "unverified" and result.model_ready is False


def test_common_forecast_sequence_required(scenario_request: MeliScenarioRequest) -> None:
    vector = dict(scenario_request.cases[1].vector)
    vector["years"] += 1.0
    bull = scenario_request.cases[1].model_copy(update={"vector": vector})
    result = preview_meli_scenarios(
        scenario_request.model_copy(
            update={"cases": (scenario_request.cases[0], bull, *scenario_request.cases[2:])}
        )
    )
    assert result.calculation_reasons == ("common_forecast_sequence_unavailable:bull",)
    assert result.present_valuation_upside is None
