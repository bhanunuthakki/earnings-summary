from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from dcf import input_evidence as evidence
from dcf import readiness, redesign, redesign_inputs
from dcf.grade_evidence import DcfEvidenceChecks, DcfGradeEvidence
from dcf.input_evidence import (
    AssumptionBasis,
    AssumptionReview,
    DriverReview,
    FactBinding,
    InputEvidenceError,
    ModelInputReceipt,
    ModelInputRequest,
    RawDocumentWitness,
    SourceReadContext,
    VerifiedInput,
    canonical_digest,
)
from dcf.input_recipes import (
    model_engine,
    model_output,
    prepare_model_inputs,
    verify_model_input_receipt,
)
from dcf.scenario_acceptance import ScenarioAcceptance, ScenarioReview
from provenance.canonical_fact_resolution import (
    CanonicalFactResolutionEngine,
    ResolutionPolicy,
    ResolutionSnapshotScope,
)
from provenance.fact_read_model import FactReadModel
from provenance.financial_statement_admission import (
    FinancialStatementAdmissionRequest,
    ReviewedFinancialStatementRole,
    admit_reviewed_financial_statements,
)
from provenance.metric_ontology import MetricOntology
from provenance.research_snapshot import ResearchSnapshotRequest
from tests.test_meli_input_evidence import (
    NOW as MELI_NOW,
)
from tests.test_meli_input_evidence import (
    seal_model_projection_context,
    seed_real_input_facts,
    synthetic_model_snapshot,
)

CLOCK = datetime(2026, 11, 12, tzinfo=UTC)
BASE = redesign.RedesignInputs(
    segments=("Total company",),
    base_revenue_by_segment={"Total company": 1000.0},
    near_growth_by_segment={"Total company": 0.1},
    terminal_growth_by_segment={"Total company": 0.03},
    near_op_margin=0.2,
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


def test_generic_engine_replay_matches_existing_model() -> None:
    inputs = redesign_inputs.serialize_flat_inputs(BASE)
    assert redesign_inputs.reconstruct_inputs(inputs, segments=BASE.segments) == BASE
    assert model_engine(redesign_inputs.RECIPE) == "redesign_fcff"
    output = model_output(redesign_inputs.RECIPE, inputs)
    expected = redesign.value(BASE)
    assert output["vps"] == expected.value_per_share_usd
    assert output["equity_value"] == expected.equity_value_usd_m
    assert output["operating_ev"] == expected.operating_value_usd_m
    assert output["scenarios"] == dataclasses.asdict(redesign.scenario_values(BASE))


def _context() -> dict[str, object]:
    return {
        "segments": ["Total company"],
        "fiscal_periods": [{"start": "2025-01-01", "end": "2025-12-31"}],
        "balance_date": "2026-06-30",
        "shares_period": {"start": "2026-04-01", "end": "2026-06-30"},
        "currency": "USD",
        "accounting_basis": "us_gaap",
        "consolidation_scope": "consolidated",
        "source_scope_label": "consolidated",
        "monetary_units": {
            **{f"{concept}_0": "currency" for concept in redesign_inputs.FLOW_CONCEPTS},
            "reported_cash_m": "currency",
            "reported_total_debt_m": "currency",
            "reported_finance_lease_liability_m": "currency",
        },
        "capex_sign": "positive_outflow",
        "cash_scope": "cash_and_equivalents",
        "cash_basis": "reported_aggregate",
        "debt_scope": "interest_bearing_debt_only",
        "debt_basis": "reported_aggregate",
        "finance_lease_basis": "reported_aggregate",
    }


def synthetic_redesign_proof(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bridge_context: dict[str, object] | None = None,
    reported_bridge_values: Mapping[str, float] | None = None,
    model_inputs: redesign.RedesignInputs = BASE,
) -> tuple[ModelInputRequest, dict[str, float]]:
    effective = redesign_inputs.serialize_flat_inputs(model_inputs)
    context = _context()
    context["balance_date"] = "2025-12-31"
    context["shares_period"] = {"start": "2025-10-01", "end": "2025-12-31"}
    values = {
        "revenue_0": 1000.0,
        "operating_income_0": 200.0,
        "depreciation_and_amortization_0": 50.0,
        "stock_based_compensation_0": 20.0,
        "capital_expenditure_0": 60.0,
        "reported_cash_m": 100.0,
        "reported_total_debt_m": 200.0,
        "reported_finance_lease_liability_m": 0.0,
        "diluted_shares_m": 100.0,
    }
    if bridge_context is not None:
        context.update(bridge_context)
    if reported_bridge_values is not None:
        values = {key: value for key, value in values.items() if not key.startswith("reported_")}
        values.update(reported_bridge_values)
        context["monetary_units"] = {key: "currency" for key in values if key != "diluted_shares_m"}
    bindings = {
        key: FactBinding(
            canonical_metric_cell_id=key,
            metric_id=key,
            metric_definition_revision_id=key,
            canonical_resolution_revision_id=key,
            observation_id=key,
            observation_payload_sha256="a" * 64,
        )
        for key in values
    }
    assumptions = {
        key: AssumptionBasis(
            value=value, attribution="owner", rationale="Saved owner assumption file."
        )
        for key, value in effective.items()
        if key not in redesign_inputs.reported_keys(effective)
    }
    request = ModelInputRequest(
        recipe=redesign_inputs.RECIPE,
        ticker="SYNTH",
        research_snapshot_id="synthetic",
        financial_period_end=date(2025, 12, 31),
        facts=bindings,
        assumptions=assumptions,
        recipe_context=context,
    )

    def verify(
        _conn: sqlite3.Connection, req: ModelInputRequest, **_kwargs: object
    ) -> ModelInputReceipt:
        items = tuple(
            VerifiedInput(
                key=reqt.key,
                value=values[reqt.key],
                reference=bindings[reqt.key],
                period_start=reqt.period_start,
                period_end=reqt.period_end or date(2025, 12, 31),
                document_version_id="synthetic-document",
                reporting_entity_id="synthetic",
                unit_key=reqt.unit_key,
                currency=reqt.currency,
                observation_kind="reported",
                knowledge_at=CLOCK,
                recorded_at=CLOCK,
            )
            for reqt in redesign_inputs.requirements_for(req)
        )
        return ModelInputReceipt(
            schema_version="dcf_model_inputs.v3",
            source_integrity="present_bytes_verified",
            recipe=req.recipe,
            request=req,
            verified_at=CLOCK,
            snapshot_member_sha256="a" * 64,
            effective_inputs_sha256=canonical_digest(effective),
            required_keys=tuple(sorted(values)),
            inputs=items,
            inventory_snapshot_ids=("synthetic-inventory",),
            raw_documents=(
                RawDocumentWitness(
                    document_version_id="synthetic-document",
                    blob_sha256="b" * 64,
                    byte_size=1,
                    verified_at=CLOCK,
                    storage_uri_sha256="c" * 64,
                ),
            ),
        )

    monkeypatch.setattr(redesign_inputs, "verify_model_inputs", verify)
    actuals, _ = redesign_inputs.calculate_actuals(verify(sqlite3.connect(":memory:"), request))
    review = AssumptionReview(
        reviewed_at=CLOCK,
        reviewer="owner",
        effective_inputs_sha256=canonical_digest(effective),
        actuals_sha256=canonical_digest(actuals),
        drivers={
            key: DriverReview(
                observed=observed,
                forecast=forecast,
                variance=forecast - observed,
                rationale="Compare saved forecast to reported synthetic anchor.",
            )
            for key, (observed, forecast) in redesign_inputs.review_drivers(
                actuals, effective
            ).items()
        },
    )
    return request.model_copy(update={"assumption_review": review}), effective


def test_receipt_seals_actuals_model_and_scenarios(monkeypatch: pytest.MonkeyPatch) -> None:
    request, inputs = synthetic_redesign_proof(monkeypatch)
    with sqlite3.connect(":memory:") as conn:
        complete, receipt = prepare_model_inputs(
            conn, request, effective_inputs=inputs, as_of=CLOCK
        )
        assert complete == inputs
        assert redesign_inputs.effective_inputs_from_receipt(receipt) == complete
        assert receipt.actuals_sha256
        assert receipt.model_output_sha256 == canonical_digest(model_output(request.recipe, inputs))
        verify_model_input_receipt(conn, receipt, effective_inputs=inputs, as_of=CLOCK)
        changed = {**inputs, "cash_m": inputs["cash_m"] + 1}
        with pytest.raises(InputEvidenceError, match="reported_or_calculated_input_mismatch"):
            verify_model_input_receipt(conn, receipt, effective_inputs=changed, as_of=CLOCK)
        with pytest.raises(InputEvidenceError, match="model_input_receipt_mismatch"):
            verify_model_input_receipt(
                conn,
                receipt.model_copy(update={"model_output_sha256": "0" * 64}),
                effective_inputs=inputs,
                as_of=CLOCK,
            )


def test_noncontiguous_or_partial_fiscal_population_fails() -> None:
    context = _context()
    context["fiscal_periods"] = [{"start": "2025-04-01", "end": "2025-12-31"}]
    with pytest.raises(ValueError, match="full fiscal year"):
        redesign_inputs.RedesignRecipeContext.model_validate(context)
    context["fiscal_periods"] = [
        {"start": "2025-01-01", "end": "2025-06-30"},
        {"start": "2025-07-02", "end": "2025-12-31"},
    ]
    with pytest.raises(ValueError, match="contiguous"):
        redesign_inputs.RedesignRecipeContext.model_validate(context)


def test_optional_expense_display_fields_are_not_required() -> None:
    request = ModelInputRequest(
        recipe=redesign_inputs.RECIPE,
        ticker="SYNTH",
        research_snapshot_id="synthetic",
        financial_period_end=date(2026, 6, 30),
        facts={},
        assumptions={},
        recipe_context=_context(),
    )
    keys = {item.key for item in redesign_inputs.requirements_for(request)}
    assert "revenue_0" in keys
    assert not any("research_and_development" in key or "cost_of_revenue" in key for key in keys)


def test_flat_vector_rejects_unknown_fields_and_bad_allocations() -> None:
    inputs = redesign_inputs.serialize_flat_inputs(BASE)
    with pytest.raises(InputEvidenceError):
        redesign_inputs.effective_numeric_inputs({**inputs, "unreviewed_extra": 1.0})
    with pytest.raises(InputEvidenceError, match="allocation"):
        redesign_inputs.effective_numeric_inputs({**inputs, "segment_0_allocation": 0.5})


@pytest.mark.parametrize("monetary_unit", ["currency", "millions", "billions"])
def test_real_retained_source_recipe_and_changed_source_bytes(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    monetary_unit: str,
) -> None:
    context = _context()
    context["cash_scope"] = "cash_and_short_term_investments"
    context["monetary_units"] = {
        **{f"{concept}_0": monetary_unit for concept in redesign_inputs.FLOW_CONCEPTS},
        "reported_cash_and_short_term_investments_m": monetary_unit,
        "reported_total_debt_m": monetary_unit,
        "reported_finance_lease_liability_m": monetary_unit,
    }
    context["balance_date"] = "2025-12-31"
    context["shares_period"] = {"start": "2025-10-01", "end": "2025-12-31"}
    source_context = SourceReadContext.for_sec_state_root(tmp_path / "sources")
    source_path = source_context.content_roots[0] / "source.json"
    conn = sqlite3.connect(migrated_db(tmp_path / "redesign.db"))
    conn.execute("PRAGMA foreign_keys=ON")
    effective = redesign_inputs.serialize_flat_inputs(
        dataclasses.replace(BASE, cash_m=130.0, total_debt_m=185.0)
    )
    request = ModelInputRequest(
        recipe=redesign_inputs.RECIPE,
        ticker="SYNTH",
        research_snapshot_id="snapshot",
        financial_period_end=date(2025, 12, 31),
        facts={},
        assumptions={
            key: AssumptionBasis(
                value=value,
                attribution="owner",
                rationale="Retained current owner assumption file.",
            )
            for key, value in effective.items()
            if key not in redesign_inputs.reported_keys(effective)
        },
        recipe_context=context,
    )
    requirements = redesign_inputs.requirements_for(request)
    amounts = {
        "financial_statement.revenue": 1000.0,
        "financial_statement.operating_income": 200.0,
        "financial_statement.depreciation_and_amortization": 50.0,
        "financial_statement.stock_based_compensation": 20.0,
        "financial_statement.capital_expenditure": 60.0,
        "financial_statement.cash_and_short_term_investments": 130.0,
        "financial_statement.total_financial_debt": 200.0,
        "financial_statement.finance_lease_liability": 15.0,
        "financial_statement.weighted_avg_shares_diluted": 100.0,
    }
    refs = seed_real_input_facts(
        conn,
        source_path=source_path,
        requirements=requirements,
        fiscal_periods={req.key: "FY" if req.annual else "Q4" for req in requirements},
        amounts_override={**amounts, **{req.key: amounts[req.role] for req in requirements}},
    )
    read_at = MELI_NOW + timedelta(seconds=1)
    ontology = MetricOntology(conn)
    reader = FactReadModel(conn)
    roles: list[ReviewedFinancialStatementRole] = []
    for req in requirements:
        bundle = reader.provenance_bundle(refs[req.key].observation_id, cutoff=read_at)
        assert bundle.evidence is not None
        node_id = bundle.evidence.evidence_node_id
        row = conn.execute(
            "SELECT text,locator_sha256 FROM evidence_nodes WHERE node_id=?",
            (node_id,),
        ).fetchone()
        doc_sha = conn.execute(
            "SELECT blob_sha256 FROM evidence_document_versions WHERE document_version_id='document-1'"
        ).fetchone()[0]
        roles.append(
            ReviewedFinancialStatementRole.model_validate(
                {
                    "observation_id": refs[req.key].observation_id,
                    "observation_payload_sha256": refs[req.key].observation_payload_sha256,
                    "expected_definition_revision_id": refs[req.key].metric_definition_revision_id,
                    "concept": req.role.split(".", 1)[1],
                    "concept_review": {
                        "concept_namespace": bundle.cell.concept_namespace,
                        "concept_name": bundle.cell.concept_name,
                        "canonical_concept": req.role.split(".", 1)[1],
                        "evidence_node_id": node_id,
                        "evidence_locator_sha256": str(row[1]),
                        "source_wording": str(row[0]),
                        "reviewer": "synthetic-reviewer",
                        "reviewed_at": read_at,
                        "rationale": "Explicit source-bound synthetic economic role review.",
                    },
                    "context": {
                        "document_version_id": "document-1",
                        "document_sha256": doc_sha,
                        "issuer_id": "issuer-1",
                        "reporting_entity_id": "reporting-1",
                        "evidence_node_id": node_id,
                        "evidence_locator_sha256": str(row[1]),
                        "source_wording": str(row[0]),
                        "accounting_basis": "us_gaap",
                        "consolidation_scope": "consolidated",
                        "source_scope_label": "consolidated",
                        "period_start": bundle.cell.period_start,
                        "period_end": bundle.cell.period_end,
                        "fiscal_year": 2025,
                        "fiscal_period": "FY" if req.annual else "Q4",
                        "reviewer": "synthetic-reviewer",
                        "reviewed_at": read_at,
                        "rationale": "Synthetic exact reported period, accounting basis, and consolidated source scope.",
                    },
                }
            )
        )
    admit_reviewed_financial_statements(
        conn,
        FinancialStatementAdmissionRequest(roles=tuple(roles), recorded_at=read_at, apply=True),
    )
    read_at += timedelta(seconds=1)
    resolver = CanonicalFactResolutionEngine(conn)
    reviewed_refs = {}
    for key, ref in refs.items():
        binding = ontology.binding_as_known(ref.observation_id, read_at)
        definition = ontology.metric_definition_as_known(ref.metric_id, read_at)
        assert binding is not None and definition is not None
        resolved = resolver.resolve(
            ref.canonical_metric_cell_id,
            read_at,
            ResolutionPolicy(name="redesign-reviewed", version="1", config={}),
            recorded_at=read_at,
        )
        reviewed_refs[key] = ref.model_copy(
            update={
                "metric_definition_revision_id": definition.metric_definition_revision_id,
                "canonical_resolution_revision_id": resolved.canonical_resolution_revision_id,
            }
        )
    resolver.seal_snapshot(
        "redesign-reviewed",
        read_at,
        read_at,
        ResolutionSnapshotScope(issuer_id="issuer-1", reporting_entity_ids=("reporting-1",)),
    )
    snapshot = synthetic_model_snapshot().model_copy(
        update={
            "ontology_snapshot_id": "redesign-ontology",
            "canonical_fact_resolution_snapshot_id": "redesign-reviewed",
            "canonical_fact_projection_run_id": "redesign-projection",
            "cutoff_at": read_at,
            "recorded_at": read_at,
        }
    )
    seal_model_projection_context(conn, snapshot)

    def coverage(
        _conn: sqlite3.Connection, _request: ModelInputRequest, _cutoff: datetime
    ) -> tuple[ResearchSnapshotRequest, str, tuple[str, ...]]:
        return snapshot, "a" * 64, ("synthetic-inventory",)

    monkeypatch.setattr(evidence, "verify_source_coverage", coverage)
    request = request.model_copy(update={"facts": reviewed_refs})
    proof = evidence.verify_model_inputs(
        conn,
        request,
        recipe=request.recipe,
        requirements=requirements,
        effective_inputs={key: item.value for key, item in request.assumptions.items()},
        assumption_keys=frozenset(request.assumptions),
        as_of=read_at,
        source_context=source_context,
    )
    actuals, _ = redesign_inputs.calculate_actuals(proof)
    review = AssumptionReview(
        reviewed_at=read_at,
        reviewer="synthetic-reviewer",
        effective_inputs_sha256=canonical_digest(effective),
        actuals_sha256=canonical_digest(actuals),
        drivers={
            key: DriverReview(
                observed=observed,
                forecast=forecast,
                variance=forecast - observed,
                rationale="Review forecast against exact retained synthetic financial anchors.",
            )
            for key, (observed, forecast) in redesign_inputs.review_drivers(
                actuals, effective
            ).items()
        },
    )
    request = request.model_copy(update={"assumption_review": review})
    complete, receipt = prepare_model_inputs(
        conn, request, effective_inputs=effective, as_of=read_at, source_context=source_context
    )
    assert complete == effective
    assert all(
        item.native_value is not None and item.scale_factor is not None for item in receipt.inputs
    )
    verify_model_input_receipt(
        conn, receipt, effective_inputs=effective, as_of=read_at, source_context=source_context
    )
    output = model_output(request.recipe, effective)
    scenarios = output["scenarios"]
    assert isinstance(scenarios, dict)
    scenarios = cast(dict[str, object], scenarios)
    acceptance = ScenarioAcceptance(
        ticker=request.ticker,
        recipe=request.recipe,
        model=redesign_inputs.ENGINE,
        engine_version="redesign_fcff_v1",
        reviewer="synthetic-analyst",
        attribution="analyst",
        reviewed_at=read_at,
        model_input_receipt_sha256=canonical_digest(receipt.model_dump(mode="json")),
        effective_inputs_sha256=canonical_digest(effective),
        actuals_sha256=receipt.actuals_sha256 or "",
        snapshot_member_sha256=receipt.snapshot_member_sha256,
        inventory_snapshot_ids=receipt.inventory_snapshot_ids,
        model_output_sha256=canonical_digest(output),
        scenarios={
            name: ScenarioReview(
                output_sha256=canonical_digest(value),
                accepted=True,
                rationale="Explicit synthetic analyst review of each complete scenario output.",
            )
            for name, value in scenarios.items()
        },
    )
    vps, equity = output["vps"], output["equity_value"]
    assert isinstance(vps, (int, float)) and isinstance(equity, (int, float))
    operating_ev = output["operating_ev"]
    assert isinstance(operating_ev, (int, float))
    rich_scenarios = {
        name: {
            "fair_value_per_share_usd": value,
            **(
                {
                    "deltas": dataclasses.asdict(
                        BASE.bull_deltas if name == "bull" else BASE.bear_deltas
                    ),
                    "provenance": "existing analyst scenario review",
                }
                if name != "base"
                else {}
            ),
        }
        for name, value in scenarios.items()
    }
    saved_snapshot = {
        "model": redesign_inputs.ENGINE,
        "effective_model_inputs": effective,
        "model_output": output,
        "value_per_share": vps,
        "equity_value_m": equity,
        "operating_ev_m": output["operating_ev"],
        "scenarios": rich_scenarios,
    }
    saved = DcfGradeEvidence(
        status="available",
        ticker="SYNTH",
        engine_version="redesign_fcff_v1",
        created_at=read_at.isoformat(),
        valuation_date=read_at.date().isoformat(),
        live_price=50.0,
        live_price_at=read_at.isoformat(),
        npv=operating_ev,
        npv_per_share=vps,
        assumption_snapshot=saved_snapshot,
        provenance={
            "model_input_receipt": receipt.model_dump(mode="json"),
            "scenario_acceptance": acceptance.model_dump(mode="json"),
        },
        checks=DcfEvidenceChecks(
            input_hash_valid=True,
            workbook_hash_valid=True,
            snapshot_status="valid",
            provenance_status="valid",
            source_count=1,
            scenario_receipt_present=True,
            reverse_receipt_present=False,
            primary_fact_overlay_status="verified",
            equity_bridge_status="verified",
            country_risk_authority=None,
            market_price_consistent=True,
        ),
    )
    holder = [saved]

    def stored(
        _conn: sqlite3.Connection, _ticker: str, *, run_id: int | None = None
    ) -> DcfGradeEvidence:
        return holder[0]

    monkeypatch.setattr(readiness, "load_dcf_verification_evidence", stored)
    ready = readiness.load_valuation_readiness(
        conn, "SYNTH", as_of=read_at, source_context=source_context
    )
    assert ready.ready, ready.reason_codes
    assert saved_snapshot["scenarios"] == rich_scenarios
    holder[0] = saved.model_copy(
        update={"assumption_snapshot": {**saved_snapshot, "scenarios": scenarios}}
    )
    direct_ready = readiness.load_valuation_readiness(
        conn, "SYNTH", as_of=read_at, source_context=source_context
    )
    assert direct_ready.ready, direct_ready.reason_codes
    holder[0] = saved.model_copy(
        update={
            "assumption_snapshot": {
                **saved_snapshot,
                "scenarios": {**rich_scenarios, "base": {"fair_value_per_share_usd": vps + 1}},
            }
        }
    )
    wrong_scenario = readiness.load_valuation_readiness(
        conn, "SYNTH", as_of=read_at, source_context=source_context
    )
    assert (
        not wrong_scenario.ready
        and "persisted_scenario_output_replay_mismatch" in wrong_scenario.reason_codes
    )
    holder[0] = saved.model_copy(
        update={
            "assumption_snapshot": {**saved_snapshot, "model_output": {**output, "vps": vps + 1}}
        }
    )
    invalid = readiness.load_valuation_readiness(
        conn, "SYNTH", as_of=read_at, source_context=source_context
    )
    assert not invalid.ready and "persisted_model_output_replay_mismatch" in invalid.reason_codes
    holder[0] = saved.model_copy(update={"live_price": 51.0})
    wrong_market = readiness.load_valuation_readiness(
        conn, "SYNTH", as_of=read_at, source_context=source_context
    )
    assert not wrong_market.ready and "reviewed_market_price_mismatch" in wrong_market.reason_codes
    holder[0] = saved

    def missing_coverage(
        _conn: sqlite3.Connection, _request: ModelInputRequest, _cutoff: datetime
    ) -> tuple[ResearchSnapshotRequest, str, tuple[str, ...]]:
        raise InputEvidenceError("model_input_source_coverage_incomplete")

    monkeypatch.setattr(evidence, "verify_source_coverage", missing_coverage)
    degraded = readiness.load_valuation_readiness(
        conn, "SYNTH", as_of=read_at, source_context=source_context
    )
    assert not degraded.ready and "model_input_source_coverage_incomplete" in degraded.reason_codes
    monkeypatch.setattr(evidence, "verify_source_coverage", coverage)
    source_path.write_bytes(b"changed synthetic raw bytes")
    with pytest.raises(
        (InputEvidenceError, ValueError), match=r"source|fact|artifact|digest|payload|blob"
    ):
        verify_model_input_receipt(
            conn, receipt, effective_inputs=effective, as_of=read_at, source_context=source_context
        )
    conn.close()


@pytest.mark.parametrize("change", ["missing", "extra", "nonnumeric", "mixed", "economic"])
def test_generic_scenario_projection_rejects_invalid_population(change: str) -> None:
    effective = redesign_inputs.serialize_flat_inputs(BASE)
    replay = model_output(redesign_inputs.RECIPE, effective)
    raw = replay["scenarios"]
    assert isinstance(raw, dict)
    expected = cast(dict[str, object], raw)
    scenarios: dict[str, object] = {
        name: {"fair_value_per_share_usd": value, "provenance": "retained analyst review"}
        for name, value in expected.items()
    }
    if change == "missing":
        del scenarios["bull"]
    elif change == "extra":
        scenarios["extra"] = {"fair_value_per_share_usd": 1.0}
    elif change == "nonnumeric":
        scenarios["base"] = {"fair_value_per_share_usd": True}
    elif change == "mixed":
        scenarios["base"] = expected["base"]
    else:
        scenarios["base"] = {"fair_value_per_share_usd": -1.0}
    with pytest.raises(InputEvidenceError, match="persisted_scenario_output_replay_mismatch"):
        readiness.replay_model_scenarios(
            "SYNTH",
            effective,
            replay,
            {"model_output": replay, "scenarios": scenarios},
            recipe=redesign_inputs.RECIPE,
        )


@pytest.mark.parametrize("cash_scope", ["cash_and_equivalents", "cash_and_short_term_investments"])
@pytest.mark.parametrize("debt_scope", ["interest_bearing_debt_only", "debt_and_lease_obligations"])
@pytest.mark.parametrize("component_liabilities", [False, True])
def test_generic_equity_bridge_scopes_replay_exact_reported_operands(
    monkeypatch: pytest.MonkeyPatch, cash_scope: str, debt_scope: str, component_liabilities: bool
) -> None:
    context: dict[str, object] = {
        "cash_scope": cash_scope,
        "cash_basis": "reported_aggregate",
        "debt_scope": debt_scope,
        "debt_basis": "reported_aggregate",
        "finance_lease_basis": (
            "complete_component_sum" if component_liabilities else "reported_aggregate"
        )
        if debt_scope == "interest_bearing_debt_only"
        else None,
        "operating_lease_basis": (
            "complete_component_sum" if component_liabilities else "reported_aggregate"
        )
        if debt_scope == "debt_and_lease_obligations"
        else None,
    }
    target_cash = 100.0 if cash_scope == "cash_and_equivalents" else 130.0
    values = (
        {"reported_cash_m": target_cash}
        if cash_scope == "cash_and_equivalents"
        else {"reported_cash_and_short_term_investments_m": target_cash}
    )
    values["reported_total_debt_m"] = 200.0
    if debt_scope == "interest_bearing_debt_only":
        values.update(
            {
                "reported_finance_lease_liability_current_m": 5.0,
                "reported_finance_lease_liability_non_current_m": 10.0,
            }
            if component_liabilities
            else {"reported_finance_lease_liability_m": 15.0}
        )
        target_debt = 185.0
    else:
        values.update(
            {
                "reported_operating_lease_liability_current_m": 10.0,
                "reported_operating_lease_liability_non_current_m": 20.0,
            }
            if component_liabilities
            else {"reported_operating_lease_liability_m": 30.0}
        )
        target_debt = 230.0
    request, effective = synthetic_redesign_proof(
        monkeypatch,
        bridge_context=context,
        reported_bridge_values=values,
        model_inputs=dataclasses.replace(BASE, cash_m=target_cash, total_debt_m=target_debt),
    )
    with sqlite3.connect(":memory:") as conn:
        complete, receipt = prepare_model_inputs(
            conn, request, effective_inputs=effective, as_of=CLOCK
        )
        assert complete["cash_m"] == target_cash and complete["total_debt_m"] == target_debt
        assert {"cash_m", "total_debt_m"} <= {item.key for item in receipt.calculations}
        verify_model_input_receipt(conn, receipt, effective_inputs=complete, as_of=CLOCK)
        missing = tuple(
            item
            for item in receipt.inputs
            if item.key != next(key for key in values if "lease" in key)
        )
        with pytest.raises(InputEvidenceError, match="verified_input_population_mismatch"):
            redesign_inputs.calculate_actuals(receipt.model_copy(update={"inputs": missing}))


@pytest.mark.parametrize(
    "change",
    [
        "missing_finance_basis",
        "missing_operating_basis",
        "cash_component_scope",
        "cash_and_investment_components",
        "debt_component_scope",
        "extra_unused_lease",
    ],
)
def test_generic_bridge_context_rejects_unknown_or_unreviewed_scope(change: str) -> None:
    context = _context()
    if change == "missing_finance_basis":
        del context["finance_lease_basis"]
    elif change == "missing_operating_basis":
        context["debt_scope"] = "debt_and_lease_obligations"
        context["finance_lease_basis"] = None
    elif change in {"cash_component_scope", "cash_and_investment_components"}:
        context["cash_basis"] = "complete_component_sum"
        if change == "cash_and_investment_components":
            context["cash_scope"] = "cash_and_short_term_investments"
    elif change == "debt_component_scope":
        context["debt_basis"] = "complete_component_sum"
    else:
        context["operating_lease_basis"] = "reported_aggregate"
    with pytest.raises(ValueError):
        redesign_inputs.RedesignRecipeContext.model_validate(context)


@pytest.mark.parametrize("component_set", ["cash", "debt"])
def test_component_population_cannot_claim_reported_bridge_perimeter(component_set: str) -> None:
    context = _context()
    units = dict(redesign_inputs.RedesignRecipeContext.model_validate(context).monetary_units)
    if component_set == "cash":
        context.update(
            cash_scope="cash_and_short_term_investments", cash_basis="complete_component_sum"
        )
        units["reported_short_term_investments_m"] = "currency"
    else:
        context["debt_basis"] = "complete_component_sum"
        del units["reported_total_debt_m"]
        units.update(reported_short_term_debt_m="currency", reported_long_term_debt_m="currency")
    context["monetary_units"] = units
    with pytest.raises(ValueError, match="Input should be 'reported_aggregate'"):
        redesign_inputs.RedesignRecipeContext.model_validate(context)
