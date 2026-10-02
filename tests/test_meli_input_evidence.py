"""MELI recipe integration against real published/ontologized/resolved facts.

Only research-snapshot/source-inventory coverage is isolated in the model tests;
its separate tests exercise inventory logic. This is not live package qualification.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Generator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast

import pytest

from dcf import input_evidence as evidence
from dcf import meli_inputs
from dcf.readiness import load_valuation_readiness
from dcf.specialized_price import SpecializedPriceObservation
from execution import build_meli_platform_dcf as meli
from provenance.canonical_fact_resolution import (
    CanonicalFactResolutionEngine,
    ResolutionPolicy,
    ResolutionSnapshotScope,
)
from provenance.fact_plane_v2 import (
    CanonicalJSONObject,
    ExtractionRunCompletenessSealV2,
    FactCellV2,
    FactDimensionV2,
)
from provenance.fact_read_model import FactReadModel
from provenance.issuer_registry import IssuerRegistry, Security
from provenance.metric_ontology import (
    BindingRevision,
    CanonicalAxis,
    CanonicalDimension,
    CanonicalMember,
    CanonicalMetric,
    CanonicalMetricCell,
    CanonicalMetricDefinitionRevision,
    MappingRevision,
    MetricOntology,
    SourceDimensionMappingRevision,
    SourceObservationTaxonomyAssertion,
    SourceTaxonomyComponent,
)
from provenance.reporting_entity_registry import (
    EvidenceSubjectBindingRevision,
    ReportingEntityRegistry,
    SecurityReportingEntityRevision,
)
from provenance.research_snapshot import ResearchSnapshotRequest
from provenance.source_fact_repository import (
    ReportedSourceFact,
    SourceFactPublication,
    SourceFactRepository,
)
from tests.test_source_fact_repository import STAMP, make_cell, make_report, seed_foundation, sha256

NOW = datetime(2026, 10, 1, 20, microsecond=123456, tzinfo=UTC)
END = date(2026, 6, 30)
SHARED_REVENUE_ROLES = {
    "meli.comm_rev0": "commerce",
    "meli.revenue_total": None,
    "meli.revenue_fintech": "fintech",
    "meli.revenue_credit": "credit",
}
REVENUE_AXIS = "meli-revenue-segment"
COUNTRY_AXIS = "meli-country"
DISTRACTOR_KEY = "comm_rev0_ytd"


def _revenue_dimensions(role: str) -> tuple[CanonicalDimension, ...]:
    member = SHARED_REVENUE_ROLES[role]
    return () if member is None else (CanonicalDimension(axis_id=REVENUE_AXIS, member_id=member),)


def _snapshot() -> ResearchSnapshotRequest:
    return ResearchSnapshotRequest.model_validate(
        {
            "research_snapshot_id": "snapshot",
            "idempotency_key": "snapshot",
            "research_universe": {
                "issuer_id": "issuer-1",
                "reporting_entity_ids": ["reporting-1"],
                "document_version_ids": ["document-1"],
                "source_obligation_revision_ids": ["obligation"],
            },
            "processing_snapshot_ids": ["processing"],
            "corpus_bundles": [
                {"corpus_manifest_id": "manifest", "lexical_index_run_id": "lexical"}
            ],
            "source_fact_publication_ids": ["publication-meli"],
            "ontology_snapshot_id": "ontology",
            "canonical_fact_resolution_snapshot_id": "resolution-snapshot",
            "canonical_fact_projection_run_id": "projection",
            "cutoff_at": NOW,
            "recorded_at": NOW,
        }
    )


def _inputs() -> dict[str, float]:
    assumptions = meli.Assum(
        derive_capm=0,
        credit_cash_allocation=1000,
        operating_cash_reserve=500,
        credit_funding_debt_allocation=8000,
    )
    assumptions.credit_terminal_roe = meli.mirror(assumptions).credit_terminal_roe * (
        1 + assumptions.credit_g_term
    )
    return meli_inputs.effective_numeric_inputs(dataclasses.asdict(assumptions))


def _seed_real_inputs(
    conn: sqlite3.Connection,
    *,
    shared_revenues: bool = False,
    extra_country: bool = False,
    security_scope: bool = False,
) -> dict[str, evidence.FactBinding]:
    """No mocking source publication, ontology, binding, resolver or fact reader."""
    seed_foundation(conn)
    if security_scope:
        IssuerRegistry(conn).persist(
            Security(
                security_id="meli-fixture-security",
                idempotency_key="meli-fixture-security",
                issuer_id="issuer-1",
                security_kind="common_stock",
                created_at=STAMP,
            )
        )
        ReportingEntityRegistry(conn).persist(
            SecurityReportingEntityRevision(
                relationship_revision_id="meli-fixture-security-reporting",
                idempotency_key="meli-fixture-security-reporting",
                relationship_key="meli-fixture-security:reporting-1",
                revision=1,
                security_id="meli-fixture-security",
                reporting_entity_id="reporting-1",
                relationship_kind="reports_through",
                decision_kind="deterministic",
                reason_code="synthetic_fixture",
                reason_details=(("fixture", "security-scope-negative"),),
                effective_at=STAMP,
                knowledge_at=STAMP,
                recorded_at=STAMP,
            )
        )
        ReportingEntityRegistry(conn).persist(
            EvidenceSubjectBindingRevision(
                binding_revision_id="meli-fixture-security-binding",
                idempotency_key="meli-fixture-security-binding",
                recorded_issuer_id="issuer-1",
                revision=2,
                issuer_id="issuer-1",
                reporting_entity_id="reporting-1",
                security_id="meli-fixture-security",
                outcome="selected",
                decision_kind="deterministic",
                reason_code="synthetic_fixture",
                reason_details=(("fixture", "security-scope-negative"),),
                material_dissent=False,
                effective_at=STAMP,
                knowledge_at=STAMP,
                recorded_at=STAMP,
                supersedes_binding_revision_id="binding-1",
            )
        )
    inputs = _inputs()
    amounts = {
        **inputs,
        "revenue_total": inputs["comm_rev0"] + 10000,
        "revenue_fintech": 10000,
        "revenue_credit": 10000 - inputs["fpay_rev0"],
        "operating_income": 2000,
        "depreciation": 500,
        "capex": 700,
        "nimal_actual": 0.19,
        # Actual disclosed Q2-2026 liquidity/debt reconciliation totals, USDm.
        # Identity/admission infrastructure and all other facts remain synthetic.
        "reported_available_cash_and_investments": 6751,
        "reported_total_financial_debt_and_leases": 13176,
        "reported_current_operating_lease_liabilities": 513,
        "reported_noncurrent_operating_lease_liabilities": 2037,
    }
    requirements = meli_inputs.requirements_for(END)
    role_requirements = {req.role: req for req in requirements}
    facts: list[ReportedSourceFact] = []
    for index, req in enumerate(requirements):
        concept = (
            "Revenues"
            if shared_revenues and req.role in SHARED_REVENUE_ROLES
            else req.role.removeprefix("meli.")
        )
        key = req.key
        amount = Decimal(
            str(
                amounts[
                    req.role.removeprefix("meli.")
                    if req.role
                    not in {
                        "meli.diluted_shares",
                        "meli.nimal_after_funding_and_losses",
                    }
                    else req.key
                ]
            )
        )
        if req.period_end is not None:
            amount *= (
                Decimal(".9")
                if key.endswith("_fy")
                else Decimal(".5")
                if key.endswith("_prior_ytd")
                else Decimal(".6")
            )
        amount /= req.scale
        start = (
            None
            if req.period_kind == "instant"
            else datetime.combine(
                req.period_start or date(2026, 1, 1), datetime.min.time(), tzinfo=UTC
            )
        )
        end = datetime.combine(req.period_end or END, datetime.min.time(), tzinfo=UTC)
        member = SHARED_REVENUE_ROLES.get(req.role) if shared_revenues else None
        source_dimensions = (
            (
                FactDimensionV2(
                    dimension_id=f"meli-revenue-dimension-{index}",
                    idempotency_key=f"meli-revenue-dimension-{index}",
                    axis_namespace="urn:fixture:revenue-axis",
                    axis_name="RevenueSegmentAxis",
                    member_kind="explicit",
                    explicit_member_namespace="urn:fixture:revenue-member",
                    explicit_member_name=member.title() + "Member",
                    recorded_at=STAMP,
                ),
            )
            if member is not None
            else ()
        )
        if extra_country and key == DISTRACTOR_KEY:
            source_dimensions = (
                *source_dimensions,
                FactDimensionV2(
                    dimension_id="meli-country-dimension",
                    idempotency_key="meli-country-dimension",
                    axis_namespace="urn:fixture:country-axis",
                    axis_name="CountryAxis",
                    member_kind="explicit",
                    explicit_member_namespace="urn:fixture:country-member",
                    explicit_member_name="BrazilMember",
                    recorded_at=STAMP,
                ),
            )
        cell = FactCellV2.model_validate(
            make_cell(key)
            .model_copy(
                update={
                    "semantic_key_sha256": None,
                    "concept_name": concept,
                    "dimensions": source_dimensions,
                    "scope_security_id": (
                        "meli-fixture-security"
                        if security_scope and key == DISTRACTOR_KEY
                        else None
                    ),
                    "period_start": start,
                    "period_end": end,
                    "period_kind": req.period_kind,
                    "unit_key": req.unit_key,
                    "currency": req.currency,
                    "fiscal_period": None,
                    "accounting_basis": req.accounting_basis or "us_gaap",
                }
            )
            .model_dump(mode="json")
        )
        node = f"meli-node-{index}"
        source_locator: dict[str, object] = {"path": key}
        if key == "reported_available_cash_and_investments":
            source_locator.update(
                {
                    "printed_page": 57,
                    "section": "Net debt",
                    "source_label": "Cash and cash equivalents (1), short-term investments (2) and long-term investments (3)",
                    "displayed_value_usd_millions": "6,751",
                }
            )
        elif key == "reported_total_financial_debt_and_leases":
            source_locator.update(
                {
                    "printed_page": 57,
                    "section": "Net debt",
                    "source_label": "Total debt",
                    "displayed_value_usd_millions": "13,176",
                }
            )
        elif key in {
            "reported_current_operating_lease_liabilities",
            "reported_noncurrent_operating_lease_liabilities",
        }:
            source_locator.update(
                {
                    "printed_page": 57,
                    "section": "Net debt",
                    "source_label": "Current Operating lease liabilities"
                    if key.startswith("reported_current")
                    else "Non-current Operating lease liabilities",
                    "displayed_value_usd_millions": "513"
                    if key.startswith("reported_current")
                    else "2,037",
                }
            )
        locator = json.dumps(source_locator, sort_keys=True, separators=(",", ":"))
        conn.execute(
            "INSERT INTO evidence_nodes VALUES (?,?,1,'run-1',NULL,NULL,'table_cell',?,?,?,?)",
            (node, node, str(amount), locator, sha256(locator), STAMP),
        )
        report = make_report(cell, key, numeric_value=str(amount)).model_copy(
            update={
                "evidence_node_id": node,
                "source_locator": CanonicalJSONObject.model_validate(source_locator),
                "source_locator_sha256": None,
                "subject_binding_revision_id": (
                    "meli-fixture-security-binding"
                    if security_scope and key == DISTRACTOR_KEY
                    else "binding-1"
                ),
            }
        )
        facts.append(ReportedSourceFact(cell=cell, observation=report))
    SourceFactRepository(conn).publish(
        SourceFactPublication(
            publication_id="publication-meli",
            idempotency_key="publication-meli",
            reported_facts=tuple(facts),
            extraction_seals=(
                ExtractionRunCompletenessSealV2(
                    extraction_seal_id="seal-meli",
                    idempotency_key="seal-meli",
                    extraction_run_id="run-1",
                    expected_node_count=len(facts) + 1,
                    completeness_policy_name="synthetic-all-nodes",
                    completeness_policy_version="1",
                    completeness_policy_sha256="a" * 64,
                    knowledge_at=STAMP,
                    recorded_at=STAMP,
                ),
            ),
        )
    )
    ontology, resolver = MetricOntology(conn), CanonicalFactResolutionEngine(conn)
    if shared_revenues:
        ontology.persist_axis(
            CanonicalAxis(
                axis_id=REVENUE_AXIS,
                idempotency_key=REVENUE_AXIS,
                canonical_name="Revenue segment",
                effective_at=STAMP,
                knowledge_at=STAMP,
                recorded_at=STAMP,
            )
        )
        for member in ("commerce", "fintech", "credit"):
            ontology.persist_member(
                CanonicalMember(
                    member_id=member,
                    idempotency_key=f"{REVENUE_AXIS}:{member}",
                    axis_id=REVENUE_AXIS,
                    canonical_name=member.title(),
                    effective_at=STAMP,
                    knowledge_at=STAMP,
                    recorded_at=STAMP,
                )
            )
        dimension_components: tuple[tuple[str, str, Literal["axis", "member"], str | None], ...] = (
            ("RevenueSegmentAxis", "urn:fixture:revenue-axis", "axis", None),
            ("CommerceMember", "urn:fixture:revenue-member", "member", "commerce"),
            ("FintechMember", "urn:fixture:revenue-member", "member", "fintech"),
            ("CreditMember", "urn:fixture:revenue-member", "member", "credit"),
        )
        for local_name, namespace, kind, member_id in dimension_components:
            component_id = f"source-revenue-{local_name}"
            ontology.persist_source_component(
                SourceTaxonomyComponent(
                    component_id=component_id,
                    idempotency_key=component_id,
                    component_kind=kind,
                    taxonomy_namespace=namespace,
                    local_name=local_name,
                    taxonomy_name="US GAAP",
                    taxonomy_version="2026",
                    reporting_entity_id="reporting-1",
                    is_extension=False,
                    evidence_locator={"fixture": True},
                    effective_at=STAMP,
                    knowledge_at=STAMP,
                    recorded_at=STAMP,
                )
            )
            ontology.persist_dimension_mapping(
                SourceDimensionMappingRevision(
                    dimension_mapping_revision_id=f"dimension-mapping:{component_id}",
                    idempotency_key=f"dimension-mapping:{component_id}",
                    source_component_id=component_id,
                    revision=1,
                    disposition="exact",
                    canonical_axis_id=REVENUE_AXIS,
                    canonical_member_id=member_id,
                    policy_name="synthetic-review",
                    policy_version="1",
                    policy_config_sha256="a" * 64,
                    evidence={"fixture": True},
                    reviewer_identity="fixture",
                    effective_at=STAMP,
                    knowledge_at=STAMP,
                    recorded_at=STAMP,
                )
            )
    if extra_country:
        ontology.persist_axis(
            CanonicalAxis(
                axis_id=COUNTRY_AXIS,
                idempotency_key=COUNTRY_AXIS,
                canonical_name="Country",
                effective_at=STAMP,
                knowledge_at=STAMP,
                recorded_at=STAMP,
            )
        )
        ontology.persist_member(
            CanonicalMember(
                member_id="brazil",
                idempotency_key=f"{COUNTRY_AXIS}:brazil",
                axis_id=COUNTRY_AXIS,
                canonical_name="Brazil",
                effective_at=STAMP,
                knowledge_at=STAMP,
                recorded_at=STAMP,
            )
        )
        country_components: tuple[tuple[str, str, Literal["axis", "member"], str | None], ...] = (
            ("CountryAxis", "urn:fixture:country-axis", "axis", None),
            ("BrazilMember", "urn:fixture:country-member", "member", "brazil"),
        )
        for local_name, namespace, kind, member_id in country_components:
            component_id = f"source-country-{local_name}"
            ontology.persist_source_component(
                SourceTaxonomyComponent(
                    component_id=component_id,
                    idempotency_key=component_id,
                    component_kind=kind,
                    taxonomy_namespace=namespace,
                    local_name=local_name,
                    taxonomy_name="US GAAP",
                    taxonomy_version="2026",
                    reporting_entity_id="reporting-1",
                    is_extension=False,
                    evidence_locator={"fixture": True},
                    effective_at=STAMP,
                    knowledge_at=STAMP,
                    recorded_at=STAMP,
                )
            )
            ontology.persist_dimension_mapping(
                SourceDimensionMappingRevision(
                    dimension_mapping_revision_id=f"dimension-mapping:{component_id}",
                    idempotency_key=f"dimension-mapping:{component_id}",
                    source_component_id=component_id,
                    revision=1,
                    disposition="exact",
                    canonical_axis_id=COUNTRY_AXIS,
                    canonical_member_id=member_id,
                    policy_name="synthetic-review",
                    policy_version="1",
                    policy_config_sha256="a" * 64,
                    evidence={"fixture": True},
                    reviewer_identity="fixture",
                    effective_at=STAMP,
                    knowledge_at=STAMP,
                    recorded_at=STAMP,
                )
            )
    registered: set[str] = set()
    refs: dict[str, evidence.FactBinding] = {}
    for req, fact in zip(requirements, facts, strict=True):
        cell, observation = fact.cell, fact.observation
        metric = (
            "meli.shared_revenues"
            if shared_revenues and req.role in SHARED_REVENUE_ROLES
            else req.role
        )
        family = "currency" if req.currency else req.unit_key
        if metric not in registered:
            ontology.persist_metric(
                CanonicalMetric(
                    metric_id=metric,
                    idempotency_key=metric,
                    canonical_name=metric,
                    effective_at=STAMP,
                    knowledge_at=STAMP,
                    recorded_at=STAMP,
                )
            )
            ontology.persist_metric_definition(
                CanonicalMetricDefinitionRevision(
                    metric_definition_revision_id=metric + ":v1",
                    idempotency_key=metric + ":v1",
                    metric_id=metric,
                    revision=1,
                    lifecycle="active",
                    definition_text="Synthetic MELI reported definition",
                    value_kind="numeric",
                    period_kind=req.period_kind,
                    unit_family=family,
                    accounting_basis=cell.accounting_basis,
                    scope_constraints=(
                        {
                            "valuation_role_selectors": {
                                role: {
                                    "canonical_dimensions": [
                                        item.model_dump() for item in _revenue_dimensions(role)
                                    ],
                                    "semantic_constraints": role_requirements[
                                        role
                                    ].definition_constraints,
                                }
                                for role in SHARED_REVENUE_ROLES
                            }
                        }
                        if metric == "meli.shared_revenues"
                        else {"valuation_role": metric, **req.definition_constraints}
                    ),
                    effective_at=STAMP,
                    knowledge_at=STAMP,
                    recorded_at=STAMP,
                )
            )
            qualifier = {
                "accounting_basis": cell.accounting_basis,
                "concept_name": cell.concept_name,
                "concept_namespace": "us-gaap",
                "consolidation_scope": "consolidated",
                "period_kind": req.period_kind,
                "reporting_entity_id": "reporting-1",
                "schema_version": "source-definition-identity/v1",
                "taxonomy_name": "US GAAP",
                "taxonomy_version": "2026",
                "unit_family": family,
                "value_kind": "numeric",
            }
            ontology.persist_source_component(
                SourceTaxonomyComponent(
                    component_id=metric + ":component",
                    idempotency_key=metric + ":component",
                    component_kind="concept",
                    taxonomy_namespace="us-gaap",
                    local_name=cell.concept_name,
                    taxonomy_name="US GAAP",
                    taxonomy_version="2026",
                    definition_qualifier_sha256=evidence.canonical_digest(qualifier),
                    reporting_entity_id="reporting-1",
                    is_extension=False,
                    evidence_locator={"fixture": True},
                    effective_at=STAMP,
                    knowledge_at=STAMP,
                    recorded_at=STAMP,
                )
            )
            ontology.persist_mapping(
                MappingRevision(
                    mapping_revision_id=metric + ":mapping",
                    idempotency_key=metric + ":mapping",
                    source_component_id=metric + ":component",
                    metric_id=metric,
                    revision=1,
                    disposition="exact",
                    policy_name="synthetic-review",
                    policy_version="1",
                    policy_config_sha256="a" * 64,
                    method_name="synthetic-review",
                    method_version="1",
                    constraints={},
                    evidence={"fixture": True},
                    reviewer_identity="fixture",
                    effective_at=STAMP,
                    knowledge_at=STAMP,
                    recorded_at=STAMP,
                )
            )
            registered.add(metric)
        proof = conn.execute(
            """SELECT anchor.extraction_run_id,cell.taxonomy_name,anchor.source_taxonomy_version,
            cell_seal.semantic_key_sha256,anchor.anchor_payload_sha256,payload.observation_payload_sha256,
            run.output_sha256,anchor.raw_entry_sha256,completeness.observation_set_sha256
            FROM fact_reported_observation_anchors_v2 anchor JOIN fact_cells_v2 cell ON cell.fact_cell_id=?
            JOIN fact_cell_identity_seals_v2 cell_seal ON cell_seal.fact_cell_id=cell.fact_cell_id
            JOIN fact_observation_payload_commitments_v2 payload ON payload.observation_id=anchor.observation_id
            JOIN evidence_extraction_runs run ON run.extraction_run_id=anchor.extraction_run_id
            JOIN fact_extraction_run_completeness_seals_v2 completeness ON completeness.extraction_run_id=anchor.extraction_run_id
            WHERE anchor.observation_id=?""",
            (cell.fact_cell_id, observation.observation_id),
        ).fetchone()
        assert proof is not None
        ontology.persist_observation_taxonomy_assertion(
            SourceObservationTaxonomyAssertion(
                observation_id=observation.observation_id,
                idempotency_key="taxonomy:" + req.key,
                extraction_run_id=str(proof[0]),
                taxonomy_name=str(proof[1]),
                taxonomy_version=str(proof[2]),
                fact_cell_semantic_key_sha256=str(proof[3]),
                anchor_payload_sha256=str(proof[4]),
                observation_payload_sha256=str(proof[5]),
                extraction_output_sha256=str(proof[6]),
                raw_entry_sha256=str(proof[7]),
                observation_set_sha256=str(proof[8]),
                knowledge_at=STAMP,
                recorded_at=STAMP,
            )
        )
        canonical_id = "canonical:" + req.key
        ontology.persist_canonical_metric_cell(
            CanonicalMetricCell(
                canonical_metric_cell_id=canonical_id,
                idempotency_key=canonical_id,
                metric_id=metric,
                reporting_entity_id="reporting-1",
                period_kind=req.period_kind,
                period_start=cell.period_start,
                period_end=cell.period_end,
                dimensions=(
                    *(
                        _revenue_dimensions(req.role)
                        if shared_revenues and req.role in SHARED_REVENUE_ROLES
                        else ()
                    ),
                    *(
                        (CanonicalDimension(axis_id=COUNTRY_AXIS, member_id="brazil"),)
                        if extra_country and req.key == DISTRACTOR_KEY
                        else ()
                    ),
                ),
                scope_security_id=cell.scope_security_id,
                unit_family=family,
                accounting_basis=cell.accounting_basis,
                consolidation_scope="consolidated",
                effective_at=STAMP,
                knowledge_at=STAMP,
                recorded_at=STAMP,
            )
        )
        ontology.persist_binding(
            BindingRevision(
                binding_revision_id="binding:" + req.key,
                idempotency_key="binding:" + req.key,
                fact_cell_id=cell.fact_cell_id,
                source_observation_id=observation.observation_id,
                revision=1,
                canonical_metric_cell_id=canonical_id,
                mapping_revision_id=metric + ":mapping",
                source_component_id=metric + ":component",
                effective_at=STAMP,
                knowledge_at=STAMP,
                recorded_at=STAMP,
            )
        )
        resolved = resolver.resolve(
            canonical_id,
            NOW,
            ResolutionPolicy(name="synthetic", version="1", config={}),
            recorded_at=NOW,
        )
        assert resolved.status == "resolved"
        refs[req.key] = evidence.FactBinding(
            canonical_metric_cell_id=canonical_id,
            metric_id=metric,
            metric_definition_revision_id=metric + ":v1",
            canonical_resolution_revision_id=resolved.canonical_resolution_revision_id,
            observation_id=observation.observation_id,
            observation_payload_sha256=FactReadModel(conn)
            .provenance_bundle(observation.observation_id, cutoff=NOW)
            .observation_payload_sha256,
        )
    resolver.seal_snapshot(
        "resolution-snapshot",
        NOW,
        NOW,
        ResolutionSnapshotScope(issuer_id="issuer-1", reporting_entity_ids=("reporting-1",)),
    )
    conn.commit()
    return refs


def _request(
    conn: sqlite3.Connection, refs: dict[str, evidence.FactBinding]
) -> evidence.ModelInputRequest:
    inputs = _inputs()
    request = evidence.ModelInputRequest(
        recipe=meli_inputs.RECIPE,
        ticker="MELI",
        research_snapshot_id="snapshot",
        financial_period_end=END,
        facts=refs,
        assumptions={
            key: evidence.AssumptionBasis(
                value=inputs[key],
                attribution="analyst",
                rationale="Explicit synthetic forecast choice",
            )
            for key in meli_inputs.ASSUMPTION_KEYS
        },
    )
    proof = evidence.verify_model_inputs(
        conn,
        request,
        recipe=meli_inputs.RECIPE,
        requirements=meli_inputs.requirements_for(END),
        effective_inputs={key: inputs[key] for key in meli_inputs.ASSUMPTION_KEYS},
        assumption_keys=meli_inputs.ASSUMPTION_KEYS,
        as_of=NOW,
    )
    actuals, _calculations = meli_inputs.calculate_actuals(proof)
    complete = {**inputs, **{key: actuals[key] for key in meli_inputs.REPORTED_DRIVER_KEYS}}
    review = evidence.AssumptionReview(
        reviewed_at=NOW,
        reviewer="synthetic analyst",
        effective_inputs_sha256=evidence.canonical_digest(complete),
        actuals_sha256=evidence.canonical_digest(actuals),
        drivers={
            key: evidence.DriverReview(
                observed=actual,
                forecast=forecast,
                variance=forecast - actual,
                rationale="Synthetic fixture explicitly accepts this forecast variance",
            )
            for key, (actual, forecast) in meli_inputs.review_drivers(actuals, complete).items()
        },
    )
    return request.model_copy(update={"assumption_review": review})


@pytest.fixture
def real_inputs(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> Generator[tuple[sqlite3.Connection, evidence.ModelInputRequest], None, None]:
    conn = sqlite3.connect(migrated_db(tmp_path / "meli.db"))
    refs = _seed_real_inputs(conn)

    def coverage(
        _conn: sqlite3.Connection, _request: evidence.ModelInputRequest, _cutoff: datetime
    ) -> tuple[ResearchSnapshotRequest, str, tuple[str, ...]]:
        return _snapshot(), "b" * 64, ("inventory",)

    monkeypatch.setattr(evidence, "verify_source_coverage", coverage)
    try:
        yield conn, _request(conn, refs)
    finally:
        conn.close()


@pytest.fixture
def shared_revenue_inputs(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> Generator[tuple[sqlite3.Connection, evidence.ModelInputRequest], None, None]:
    conn = sqlite3.connect(migrated_db(tmp_path / "meli-shared-revenues.db"))
    refs = _seed_real_inputs(conn, shared_revenues=True)

    def coverage(
        _conn: sqlite3.Connection, _request: evidence.ModelInputRequest, _cutoff: datetime
    ) -> tuple[ResearchSnapshotRequest, str, tuple[str, ...]]:
        return _snapshot(), "b" * 64, ("inventory",)

    monkeypatch.setattr(evidence, "verify_source_coverage", coverage)
    try:
        yield conn, _request(conn, refs)
    finally:
        conn.close()


def test_shared_revenues_qname_routes_four_roles_by_sealed_dimensions(
    shared_revenue_inputs: tuple[sqlite3.Connection, evidence.ModelInputRequest],
) -> None:
    conn, request = shared_revenue_inputs
    receipt = evidence.verify_model_inputs(
        conn,
        request,
        recipe=meli_inputs.RECIPE,
        requirements=meli_inputs.requirements_for(END),
        effective_inputs={key: item.value for key, item in request.assumptions.items()},
        assumption_keys=meli_inputs.ASSUMPTION_KEYS,
        as_of=NOW,
    )
    selected = {
        item.key: item
        for item in receipt.inputs
        if item.key.endswith("_ytd")
        and item.key.rsplit("_", 1)[0]
        in {"comm_rev0", "revenue_total", "revenue_fintech", "revenue_credit"}
    }
    assert len(selected) == 4
    assert {item.reference.metric_id for item in selected.values()} == {"meli.shared_revenues"}
    assert {
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT concept_name FROM fact_cells_v2 WHERE fact_cell_id IN "
            "(SELECT fact_cell_id FROM fact_observations_v2 WHERE observation_id IN "
            "(?,?,?,?))",
            tuple(item.reference.observation_id for item in selected.values()),
        )
    } == {"Revenues"}
    assert set(selected) == {
        "comm_rev0_ytd",
        "revenue_total_ytd",
        "revenue_fintech_ytd",
        "revenue_credit_ytd",
    }


@pytest.mark.parametrize("replacement", ["revenue_total_ytd", "revenue_fintech_ytd"])
def test_shared_revenue_selector_rejects_missing_or_wrong_segment(
    shared_revenue_inputs: tuple[sqlite3.Connection, evidence.ModelInputRequest],
    replacement: str,
) -> None:
    conn, request = shared_revenue_inputs
    altered = request.model_copy(
        update={"facts": {**request.facts, "comm_rev0_ytd": request.facts[replacement]}}
    )
    with pytest.raises(evidence.InputEvidenceError, match="input_semantic_admission_failed"):
        meli_inputs.prepare_meli_inputs(conn, altered, effective_inputs=_inputs(), as_of=NOW)


@pytest.mark.parametrize(
    "distraction,reason",
    [
        ("extra_country", "input_semantic_admission_failed:comm_rev0_ytd"),
        ("security_scope", "input_outside_canonical_snapshot:comm_rev0_ytd"),
    ],
)
def test_real_published_distractor_cell_cannot_feed_meli_role(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    distraction: str,
    reason: str,
) -> None:
    conn = sqlite3.connect(migrated_db(tmp_path / f"meli-{distraction}.db"))
    try:
        refs = _seed_real_inputs(
            conn,
            shared_revenues=True,
            extra_country=distraction == "extra_country",
            security_scope=distraction == "security_scope",
        )

        def coverage(
            _conn: sqlite3.Connection,
            _request: evidence.ModelInputRequest,
            _cutoff: datetime,
        ) -> tuple[ResearchSnapshotRequest, str, tuple[str, ...]]:
            return _snapshot(), "b" * 64, ("inventory",)

        monkeypatch.setattr(evidence, "verify_source_coverage", coverage)
        ref = refs[DISTRACTOR_KEY]
        resolution = CanonicalFactResolutionEngine(conn).as_known(ref.canonical_metric_cell_id, NOW)
        assert resolution is not None and resolution.status == "resolved"
        assert resolution.selected_observation_id == ref.observation_id
        source = FactReadModel(conn).provenance_bundle(ref.observation_id, cutoff=NOW).cell
        canonical_scope = conn.execute(
            "SELECT scope_security_id FROM canonical_metric_cells WHERE canonical_metric_cell_id=?",
            (ref.canonical_metric_cell_id,),
        ).fetchone()
        assert canonical_scope is not None
        if distraction == "extra_country":
            assert {item.axis_name for item in source.dimensions} == {
                "RevenueSegmentAxis",
                "CountryAxis",
            }
            canonical_dimensions = conn.execute(
                "SELECT axis_id,member_id FROM canonical_metric_cell_dimensions "
                "WHERE canonical_metric_cell_id=? ORDER BY dimension_ordinal",
                (ref.canonical_metric_cell_id,),
            ).fetchall()
            assert (COUNTRY_AXIS, "brazil") in [tuple(row) for row in canonical_dimensions]
            assert canonical_scope[0] is None
        else:
            assert source.scope_security_id == "meli-fixture-security"
            assert canonical_scope[0] == "meli-fixture-security"
        inputs = _inputs()
        request = evidence.ModelInputRequest(
            recipe=meli_inputs.RECIPE,
            ticker="MELI",
            research_snapshot_id="snapshot",
            financial_period_end=END,
            facts=refs,
            assumptions={
                key: evidence.AssumptionBasis(
                    value=inputs[key],
                    attribution="analyst",
                    rationale="Explicit synthetic forecast choice",
                )
                for key in meli_inputs.ASSUMPTION_KEYS
            },
        )
        with pytest.raises(evidence.InputEvidenceError, match=reason):
            evidence.verify_model_inputs(
                conn,
                request,
                recipe=meli_inputs.RECIPE,
                requirements=meli_inputs.requirements_for(END),
                effective_inputs={key: inputs[key] for key in meli_inputs.ASSUMPTION_KEYS},
                assumption_keys=meli_inputs.ASSUMPTION_KEYS,
                as_of=NOW,
            )
    finally:
        conn.close()


@pytest.mark.parametrize(
    "change",
    [
        "mixed",
        "missing_dimensions",
        "wrong_semantics",
        "extra_country_dimension",
        "duplicate_axis",
        "duplicate_selector",
        "scalar_dimensioned",
        "stale_head",
    ],
)
def test_shared_revenue_revised_definition_fails_closed(
    shared_revenue_inputs: tuple[sqlite3.Connection, evidence.ModelInputRequest],
    change: str,
) -> None:
    conn, request = shared_revenue_inputs
    ontology = MetricOntology(conn)
    old = ontology.metric_definition_as_known("meli.shared_revenues", NOW)
    assert old is not None
    raw_selectors = old.scope_constraints.get("valuation_role_selectors")
    assert isinstance(raw_selectors, dict)
    selectors = cast("dict[str, object]", raw_selectors)
    commerce = [item.model_dump() for item in _revenue_dimensions("meli.comm_rev0")]
    comm_constraints = next(
        item.definition_constraints
        for item in meli_inputs.requirements_for(END)
        if item.key == "comm_rev0_ytd"
    )
    scopes: dict[str, dict[str, object]] = {
        "mixed": {**old.scope_constraints, "valuation_role": "meli.comm_rev0"},
        "missing_dimensions": {
            "valuation_role_selectors": {
                "meli.comm_rev0": {
                    "semantic_constraints": {
                        "fiscal_year_end": "12-31",
                        "reported_population": "actual",
                    }
                }
            }
        },
        "wrong_semantics": {
            "valuation_role_selectors": {
                **selectors,
                "meli.comm_rev0": {
                    "canonical_dimensions": commerce,
                    "semantic_constraints": {
                        **comm_constraints,
                        "revenue_scope": "country",
                    },
                },
            }
        },
        "extra_country_dimension": {
            "valuation_role_selectors": {
                **selectors,
                "meli.comm_rev0": {
                    "canonical_dimensions": [
                        *commerce,
                        {"axis_id": "country", "member_id": "Brazil"},
                    ],
                    "semantic_constraints": comm_constraints,
                },
            }
        },
        "duplicate_axis": {
            "valuation_role_selectors": {
                **selectors,
                "meli.comm_rev0": {
                    "canonical_dimensions": [*commerce, *commerce],
                    "semantic_constraints": comm_constraints,
                },
            }
        },
        "duplicate_selector": {
            "valuation_role_selectors": {
                **selectors,
                "meli.revenue_fintech": {
                    "canonical_dimensions": commerce,
                    "semantic_constraints": next(
                        item.definition_constraints
                        for item in meli_inputs.requirements_for(END)
                        if item.key == "revenue_fintech_ytd"
                    ),
                },
            }
        },
        "scalar_dimensioned": {"valuation_role": "meli.comm_rev0", **comm_constraints},
        "stale_head": old.scope_constraints,
    }
    revised = old.model_copy(
        update={
            "metric_definition_revision_id": old.metric_definition_revision_id + ":v2",
            "idempotency_key": old.idempotency_key + ":v2",
            "revision": 2,
            "supersedes_metric_definition_revision_id": old.metric_definition_revision_id,
            "scope_constraints": scopes[change],
            "effective_at": NOW,
            "knowledge_at": NOW,
            "recorded_at": NOW,
        }
    )
    ontology.persist_metric_definition(revised)
    if change != "stale_head":
        request = request.model_copy(
            update={
                "facts": {
                    key: (
                        ref.model_copy(
                            update={
                                "metric_definition_revision_id": revised.metric_definition_revision_id
                            }
                        )
                        if ref.metric_id == "meli.shared_revenues"
                        else ref
                    )
                    for key, ref in request.facts.items()
                }
            }
        )
    with pytest.raises(evidence.InputEvidenceError, match="input_semantic_admission_failed"):
        meli_inputs.prepare_meli_inputs(conn, request, effective_inputs=_inputs(), as_of=NOW)


def test_shared_revenue_append_only_dimension_and_recipe_cannot_be_bypassed(
    shared_revenue_inputs: tuple[sqlite3.Connection, evidence.ModelInputRequest],
) -> None:
    conn, request = shared_revenue_inputs
    old_recipe = request.model_copy(update={"recipe": "meli-platform-sotp-inputs/v3"})
    with pytest.raises(evidence.InputEvidenceError, match="input_recipe_mismatch"):
        meli_inputs.prepare_meli_inputs(conn, old_recipe, effective_inputs=_inputs(), as_of=NOW)
    cell_id = request.facts["comm_rev0_ytd"].canonical_metric_cell_id
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE canonical_metric_cell_dimensions SET member_id='credit' "
            "WHERE canonical_metric_cell_id=?",
            (cell_id,),
        )


def test_real_reported_bindings_compute_ttm_and_bridge(
    real_inputs: tuple[sqlite3.Connection, evidence.ModelInputRequest],
) -> None:
    conn, request = real_inputs
    inputs, receipt = meli_inputs.prepare_meli_inputs(
        conn, request, effective_inputs=_inputs(), as_of=NOW
    )
    assert len(receipt.inputs) == 28
    assert all(item.observation_kind == "reported" for item in receipt.inputs)
    assert inputs["comm_rev0"] == pytest.approx(_inputs()["comm_rev0"])
    assert inputs["net_cash"] == 6751 - 1000 - 500 - (13176 - 513 - 2037) + 8000
    calculated = {item.key: item.value for item in receipt.calculations}
    assert calculated["operating_lease_liabilities"] == 2550
    assert calculated["financial_debt_pool"] == 10626
    assert {item.key for item in receipt.calculations} >= {"comm_rev0", "net_cash", "fpay_rev0"}
    assert (
        meli_inputs.verify_meli_inputs(
            conn, receipt, effective_inputs=inputs, as_of=NOW
        ).model_output_sha256
        == receipt.model_output_sha256
    )


@pytest.mark.parametrize(
    "change,reason",
    [
        ("missing", "required_input_population_mismatch"),
        ("wrong-role", "input_outside_canonical_snapshot"),
        ("old-review", "assumption_review_clock_or_basis_mismatch"),
        ("no-review", "assumption_review_unverified"),
    ],
)
def test_real_authorities_reject_tainted_population(
    real_inputs: tuple[sqlite3.Connection, evidence.ModelInputRequest], change: str, reason: str
) -> None:
    conn, request = real_inputs
    if change == "missing":
        request = request.model_copy(
            update={
                "facts": {key: ref for key, ref in request.facts.items() if key != "comm_rev0_ytd"}
            }
        )
    elif change == "wrong-role":
        ref = request.facts["comm_rev0_ytd"].model_copy(update={"metric_id": "meli.revenue_total"})
        request = request.model_copy(update={"facts": {**request.facts, "comm_rev0_ytd": ref}})
    elif change == "no-review":
        request = request.model_copy(update={"assumption_review": None})
    else:
        assert request.assumption_review is not None
        request = request.model_copy(
            update={
                "assumption_review": request.assumption_review.model_copy(
                    update={"reviewed_at": STAMP - timedelta(days=1)}
                )
            }
        )
    with pytest.raises(evidence.InputEvidenceError, match=reason):
        meli_inputs.prepare_meli_inputs(conn, request, effective_inputs=_inputs(), as_of=NOW)


def test_seed_only_model_cannot_persist(tmp_path: Path) -> None:
    assumptions = meli.Assum(derive_capm=0)
    with pytest.raises(evidence.InputEvidenceError, match="model_input_receipt_required"):
        meli.persist_dcf_run(
            assumptions, meli.mirror(assumptions), {}, db_path=tmp_path / "absent.db"
        )
    assert not (tmp_path / "absent.db").exists()


def _coverage_fixture(monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE research_snapshot_headers (research_snapshot_id TEXT, request_json TEXT);
        CREATE TABLE search_manifest_source_inventories (manifest_id TEXT, snapshot_id TEXT);
        CREATE TABLE source_inventory_snapshots (snapshot_id TEXT, inventory_key TEXT, revision INTEGER,
            issuer_id TEXT, ticker TEXT, source_kind TEXT, outcome TEXT, authoritative INTEGER,
            completed_at TEXT, recorded_at TEXT);
        CREATE TABLE expected_documents (snapshot_id TEXT, period_end TEXT, form_type TEXT, recorded_at TEXT);
    """)
    conn.execute(
        "INSERT INTO research_snapshot_headers VALUES (?,?)",
        ("snapshot", _snapshot().model_dump_json()),
    )
    conn.execute("INSERT INTO search_manifest_source_inventories VALUES ('manifest','inventory')")
    conn.execute(
        "INSERT INTO source_inventory_snapshots VALUES ('inventory','sec',1,'issuer-1','MELI','sec_submissions','succeeded',1,?,?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO expected_documents VALUES ('inventory','2026-06-30','10-Q',?)",
        (NOW.isoformat(),),
    )

    def verify(_conn: sqlite3.Connection, _snapshot_id: str) -> SimpleNamespace:
        return SimpleNamespace(member_set_sha256="b" * 64)

    monkeypatch.setattr(evidence, "verify_research_snapshot", verify)
    return conn


@pytest.mark.parametrize(
    "change,reason",
    [
        ("none", None),
        ("stale", "source_inventory_stale_or_future"),
        ("new-period", "financial_anchor_not_latest_published_period"),
        ("superseded", "source_inventory_superseded"),
        ("failed", "source_inventory_not_authoritative_complete"),
        ("missing-sec", "current_sec_inventory_missing"),
    ],
)
def test_current_source_coverage_is_independent_of_quote_and_capture(
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    reason: str | None,
) -> None:
    conn = _coverage_fixture(monkeypatch)
    request = evidence.ModelInputRequest(
        recipe=meli_inputs.RECIPE,
        ticker="MELI",
        research_snapshot_id="snapshot",
        financial_period_end=END,
        facts={},
        assumptions={},
    )
    if change == "stale":
        conn.execute(
            "UPDATE source_inventory_snapshots SET completed_at=?",
            ((NOW - timedelta(days=2)).isoformat(),),
        )
    elif change == "new-period":
        conn.execute("UPDATE expected_documents SET period_end='2026-09-30'")
    elif change == "superseded":
        conn.execute(
            "INSERT INTO source_inventory_snapshots SELECT 'new','sec',2,issuer_id,ticker,source_kind,outcome,authoritative,completed_at,recorded_at FROM source_inventory_snapshots"
        )
    elif change == "failed":
        conn.execute("UPDATE source_inventory_snapshots SET outcome='failed'")
    elif change == "missing-sec":
        conn.execute("DELETE FROM search_manifest_source_inventories")
    if reason:
        with pytest.raises(evidence.InputEvidenceError, match=reason):
            evidence.verify_source_coverage(conn, request, NOW)
    else:
        snapshot, _digest, inventories = evidence.verify_source_coverage(conn, request, NOW)
        assert snapshot.research_universe.issuer_id == "issuer-1"
        assert inventories == ("inventory",)


def test_replayed_base_is_not_scenario_acceptance_and_wrong_outputs_are_blocked(
    real_inputs: tuple[sqlite3.Connection, evidence.ModelInputRequest],
) -> None:
    conn, request = real_inputs
    inputs, receipt = meli_inputs.prepare_meli_inputs(
        conn, request, effective_inputs=_inputs(), as_of=NOW
    )
    result = meli_inputs.model_output(inputs)
    snapshot = {
        "model": "meli_platform_sotp",
        "effective_model_inputs": inputs,
        "value_per_share": result["vps"],
        "equity_value_m": result["equity_value"],
        "operating_ev_m": result["operating_ev"],
        "credit_equity_value_m": result["credit_equity_value"],
    }
    conn.execute(
        """INSERT INTO dcf_runs (ticker,valuation_date,horizon_years,revenue_growths_json,fcf_margin,wacc,terminal_growth,
        npv,npv_per_share,created_at,live_price,live_price_at,input_sha256,workbook_sha256,engine_version,inputs_as_of,
        assumption_snapshot_json,provenance_json) VALUES ('MELI','2026-10-01',10,'[]',0,.135,.045,?,?,?,1684,?,?,?,'meli_platform_sotp_v1',?,?,?)""",
        (
            result["equity_value"],
            result["vps"],
            NOW.isoformat(),
            NOW.isoformat(),
            "a" * 64,
            "b" * 64,
            NOW.isoformat(),
            json.dumps(snapshot),
            json.dumps({"model_input_receipt": receipt.model_dump(mode="json")}),
        ),
    )
    good = load_valuation_readiness(conn, "MELI", as_of=NOW)
    assert not good.ready
    assert good.financial_input_completeness == "verified", good.reason_codes
    assert good.latest_reporting_period_status == "verified"
    assert good.assumption_reviewed_at == NOW.isoformat()
    assert good.reason_codes == ("scenario_acceptance_unverified",)
    conn.execute("UPDATE dcf_runs SET npv=npv+1000")
    wrong = load_valuation_readiness(conn, "MELI", as_of=NOW)
    assert not wrong.ready
    assert "persisted_model_output_replay_mismatch" in wrong.reason_codes
    assert wrong.financial_input_completeness == "verified"


def test_real_old_period_cannot_fill_current_ytd_slot(
    real_inputs: tuple[sqlite3.Connection, evidence.ModelInputRequest],
) -> None:
    conn, request = real_inputs
    request = request.model_copy(
        update={"facts": {**request.facts, "comm_rev0_ytd": request.facts["comm_rev0_prior_ytd"]}}
    )
    with pytest.raises(evidence.InputEvidenceError, match="input_coordinate_mismatch"):
        meli_inputs.prepare_meli_inputs(conn, request, effective_inputs=_inputs(), as_of=NOW)


@pytest.mark.parametrize("adopt_revision", [False, True])
def test_current_definition_supersession_invalidates_old_binding_recipe(
    real_inputs: tuple[sqlite3.Connection, evidence.ModelInputRequest],
    adopt_revision: bool,
) -> None:
    conn, request = real_inputs
    ontology = MetricOntology(conn)
    old = ontology.metric_definition_as_known("meli.nimal_after_funding_and_losses", NOW)
    assert old is not None
    ontology.persist_metric_definition(
        old.model_copy(
            update={
                "metric_definition_revision_id": old.metric_definition_revision_id + ":new",
                "idempotency_key": old.idempotency_key + ":new",
                "revision": 2,
                "supersedes_metric_definition_revision_id": old.metric_definition_revision_id,
                "scope_constraints": {**old.scope_constraints, "funding_costs": "not_deducted"},
                "effective_at": NOW,
                "knowledge_at": NOW,
                "recorded_at": NOW,
            }
        )
    )
    if adopt_revision:
        request = request.model_copy(
            update={
                "facts": {
                    **request.facts,
                    "nimal_actual": request.facts["nimal_actual"].model_copy(
                        update={
                            "metric_definition_revision_id": old.metric_definition_revision_id
                            + ":new"
                        }
                    ),
                }
            }
        )
    with pytest.raises(evidence.InputEvidenceError, match="input_semantic_admission_failed"):
        meli_inputs.prepare_meli_inputs(conn, request, effective_inputs=_inputs(), as_of=NOW)


@pytest.mark.parametrize(
    "driver_key",
    ["nimal", "credit_cash_allocation", "operating_cash_reserve", "credit_funding_debt_allocation"],
)
def test_changed_variance_or_actual_basis_requires_new_review(
    real_inputs: tuple[sqlite3.Connection, evidence.ModelInputRequest],
    driver_key: str,
) -> None:
    conn, request = real_inputs
    assert request.assumption_review is not None
    driver = request.assumption_review.drivers[driver_key].model_copy(update={"variance": 0.0})
    request = request.model_copy(
        update={
            "assumption_review": request.assumption_review.model_copy(
                update={"drivers": {**request.assumption_review.drivers, driver_key: driver}}
            )
        }
    )
    with pytest.raises(evidence.InputEvidenceError, match="assumption_review_variance_mismatch"):
        meli_inputs.prepare_meli_inputs(conn, request, effective_inputs=_inputs(), as_of=NOW)


def test_verified_loader_uses_explicit_state_artifact_and_detects_changes(
    real_inputs: tuple[sqlite3.Connection, evidence.ModelInputRequest],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, request = real_inputs
    db = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    artifact = tmp_path / "approved-state" / "MELI_sotp.json"
    artifact.parent.mkdir()
    artifact.write_text(json.dumps({"input_evidence": request.model_dump(mode="json")}))

    def now(_tz: object) -> datetime:
        return NOW

    monkeypatch.setattr(meli, "datetime", SimpleNamespace(now=now))
    assumptions, receipt = meli.load_verified_assumptions(
        "MELI", db_path=db, assumptions_path=artifact
    )
    assert assumptions.comm_rev0 == pytest.approx(_inputs()["comm_rev0"])
    assert assumptions.price == 0
    assert receipt.assumptions_source_path == str(artifact.resolve())
    assert receipt.assumptions_source_sha256 == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert receipt.request.assumption_review == request.assumption_review
    artifact.write_text("{}")
    with pytest.raises(evidence.InputEvidenceError, match="authority_changed_after_review"):
        meli.persist_dcf_run(
            assumptions,
            meli.mirror(assumptions),
            db_path=db,
            input_receipt=receipt,
            assumptions_path=artifact,
        )
    assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == 0


def test_verified_loader_rejects_seed_only_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "MELI_sotp.json"
    artifact.write_text(json.dumps(dataclasses.asdict(meli.Assum())))
    absent_database = tmp_path / "must-not-be-created.db"
    with pytest.raises(evidence.InputEvidenceError, match="request_missing_or_invalid"):
        meli.load_verified_assumptions("MELI", db_path=absent_database, assumptions_path=artifact)
    assert not absent_database.exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"credit_cash_allocation": -1.0},
        {"credit_cash_allocation": 6252.0},  # with reserve exceeds the cash pool
        {"operating_cash_reserve": 6752.0},
        {"credit_funding_debt_allocation": -1.0},
        {"credit_funding_debt_allocation": 10627.0},  # must exclude rent-expensed leases
        {"credit_funding_debt_allocation": 13177.0},
    ],
)
def test_reviewed_allocations_must_fit_reported_pools(
    real_inputs: tuple[sqlite3.Connection, evidence.ModelInputRequest],
    changes: dict[str, float],
) -> None:
    conn, request = real_inputs
    inputs = {**_inputs(), **changes}
    revised = request.model_copy(
        update={
            "assumptions": {
                key: value.model_copy(update={"value": inputs[key]})
                for key, value in request.assumptions.items()
            }
        }
    )
    with pytest.raises(evidence.InputEvidenceError, match="allocation_outside_reported_pools"):
        meli_inputs.prepare_meli_inputs(conn, revised, effective_inputs=inputs, as_of=NOW)


def test_real_builder_persistence_keeps_subsecond_calculation_clock(
    real_inputs: tuple[sqlite3.Connection, evidence.ModelInputRequest],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, request = real_inputs
    db = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    artifact = tmp_path / "reviewed.json"
    artifact.write_text(json.dumps({"input_evidence": request.model_dump(mode="json")}))
    os.utime(artifact, (NOW.timestamp(), NOW.timestamp()))

    def now(_tz: object) -> datetime:
        return NOW

    def today() -> date:
        return NOW.date()

    monkeypatch.setattr(meli, "datetime", SimpleNamespace(now=now))
    monkeypatch.setattr(meli, "date", SimpleNamespace(today=today))
    monkeypatch.setattr(meli, "REPO", tmp_path)
    destination = tmp_path / "model.xlsx"
    monkeypatch.setattr(meli, "DEST", destination)
    assumptions, receipt = meli.load_verified_assumptions(
        "MELI", db_path=db, assumptions_path=artifact
    )
    assumptions.price = 1700.0
    quote = SpecializedPriceObservation(
        price=1700.0, observed_at=NOW, source_name="synthetic_quote"
    )
    model = meli.mirror(assumptions)
    # This test owns timestamp/replay, not production scenario acceptance.
    # Use coherent synthetic shifts; default terminal deltas now fail closed.
    assert meli.redesign_mod is not None
    monkeypatch.setattr(
        meli.redesign_mod,
        "BULL_SEED",
        meli.redesign_mod.ScenarioDeltas(growth_near=0.03, exit_multiple=2),
    )
    monkeypatch.setattr(
        meli.redesign_mod,
        "BEAR_SEED",
        meli.redesign_mod.ScenarioDeltas(growth_near=-0.03, exit_multiple=-2),
    )
    meli.build(assumptions, model, destination, None)
    os.utime(destination, (NOW.timestamp(), NOW.timestamp()))
    assert meli.persist_dcf_run(
        assumptions,
        model,
        price_observation=quote,
        db_path=db,
        input_receipt=receipt,
        assumptions_path=artifact,
    )
    saved = conn.execute("SELECT created_at,npv,npv_per_share FROM dcf_runs").fetchone()
    assert datetime.fromisoformat(saved[0]) == NOW
    assert saved[1] == model.equity_value
    assert saved[2] == model.vps
    readiness = load_valuation_readiness(conn, "MELI", as_of=NOW)
    assert readiness.financial_input_completeness == "verified", readiness.reason_codes
    assert "model_input_receipt_after_calculation" not in readiness.reason_codes
    assert "scenario_acceptance_unverified" in readiness.reason_codes
    # The old second-resolution SQL timestamp genuinely precedes this receipt.
    # Retain the strict consumer ordering instead of adding a time tolerance.
    conn.execute("UPDATE dcf_runs SET created_at=?", (NOW.replace(microsecond=0).isoformat(),))
    truncated = load_valuation_readiness(conn, "MELI", as_of=NOW)
    assert "model_input_receipt_after_calculation" in truncated.reason_codes
