"""Native source names and reviewed financial calculation admission."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from compute.thesis_evaluator import KpiObservation, evaluate_rule, load_holdings_spec
from models.facts import Unit
from models.kpis import BreachStatus
from provenance.canonical_fact_resolution import CanonicalFactResolutionEngine, ResolutionPolicy
from provenance.metric_ontology import (
    BindingRevision,
    CanonicalMetric,
    CanonicalMetricCell,
    MetricOntology,
)
from provenance.source_fact_repository import ReportedSourceFact
from sources.canonical_financial_series import (
    CanonicalFinancialSeriesReader,
)
from tests.test_report_canonical_financials import STAMP, seed_table
from tests.test_report_canonical_financials import database as database

# A reviewed admission follows the source population and its retained ontology seal.
REVIEW_AT = STAMP + timedelta(seconds=1)


def _verify_ontology_seals(conn: sqlite3.Connection) -> None:
    snapshot_ids = tuple(
        str(row[0])
        for row in conn.execute("SELECT ontology_snapshot_id FROM ontology_snapshot_seals")
    )
    assert snapshot_ids
    for snapshot_id in snapshot_ids:
        MetricOntology(conn).verify_snapshot(snapshot_id)


def reviewed_binding(conn: sqlite3.Connection, fact: ReportedSourceFact, metric: str) -> None:
    ontology = MetricOntology(conn)
    prior = ontology.binding_as_known(fact.observation.observation_id, STAMP)
    assert prior is not None and prior.source_component_id is not None
    prior_mapping = ontology.mapping_as_known(prior.source_component_id, REVIEW_AT)
    assert prior_mapping is not None and prior_mapping.metric_id is not None
    definition = ontology.metric_definition_as_known(prior_mapping.metric_id, REVIEW_AT)
    assert definition is not None
    ontology.persist_metric(
        CanonicalMetric(
            metric_id=metric,
            idempotency_key=metric,
            canonical_name=metric,
            effective_at=STAMP,
            knowledge_at=REVIEW_AT,
            recorded_at=REVIEW_AT,
        )
    )
    ontology.persist_metric_definition(
        definition.model_copy(
            update={
                "metric_id": metric,
                "metric_definition_revision_id": f"definition:{metric}",
                "idempotency_key": f"definition:{metric}",
                "knowledge_at": REVIEW_AT,
                "recorded_at": REVIEW_AT,
                "scope_constraints": {
                    **definition.scope_constraints,
                    "currency": fact.cell.currency,
                },
            }
        )
    )
    mapping = prior_mapping.model_copy(
        update={
            "knowledge_at": REVIEW_AT,
            "recorded_at": REVIEW_AT,
            "mapping_revision_id": f"review:{fact.cell.fact_cell_id}",
            "idempotency_key": f"review:{fact.cell.fact_cell_id}",
            "revision": prior_mapping.revision + 1,
            "supersedes_mapping_revision_id": prior_mapping.mapping_revision_id,
            "metric_id": metric,
            "reviewer_identity": "synthetic-reviewer",
            "policy_name": "reviewed_financial_metric",
            "evidence": {"fixture": True},
            "constraints": {**prior_mapping.constraints, "source_currency": fact.cell.currency},
        }
    )
    ontology.persist_mapping(mapping)
    cell_id = f"target:{fact.cell.fact_cell_id}"
    ontology.persist_canonical_metric_cell(
        CanonicalMetricCell(
            canonical_metric_cell_id=cell_id,
            idempotency_key=cell_id,
            metric_id=metric,
            reporting_entity_id=fact.cell.reporting_entity_id,
            period_kind=fact.cell.period_kind,
            period_start=fact.cell.period_start,
            period_end=fact.cell.period_end,
            unit_family="currency",
            accounting_basis=fact.cell.accounting_basis,
            consolidation_scope=fact.cell.consolidation_scope,
            effective_at=STAMP,
            knowledge_at=REVIEW_AT,
            recorded_at=REVIEW_AT,
        )
    )
    ontology.persist_binding(
        BindingRevision(
            binding_revision_id=f"review-bind:{fact.cell.fact_cell_id}",
            idempotency_key=f"review-bind:{fact.cell.fact_cell_id}",
            fact_cell_id=fact.cell.fact_cell_id,
            source_observation_id=fact.observation.observation_id,
            revision=prior.revision + 1,
            supersedes_binding_revision_id=prior.binding_revision_id,
            canonical_metric_cell_id=cell_id,
            mapping_revision_id=mapping.mapping_revision_id,
            source_component_id=prior.source_component_id,
            effective_at=STAMP,
            knowledge_at=REVIEW_AT,
            recorded_at=REVIEW_AT,
        )
    )
    CanonicalFactResolutionEngine(conn).resolve(
        cell_id,
        REVIEW_AT,
        ResolutionPolicy(name="fixture", version="1", config={}),
        recorded_at=REVIEW_AT,
    )
    _verify_ontology_seals(conn)
    conn.commit()


def test_financial_reader_uses_native_reviewed_binding(database: sqlite3.Connection) -> None:
    facts = seed_table(
        database,
        [("RevenueFromContractWithCustomer", "2025-01-01", "2025-03-31", "Q1", "100", "USD")],
        concept_namespace="http://fasb.org/us-gaap/2025",
    )
    reviewed_binding(database, facts[0], "revenue")
    result = CanonicalFinancialSeriesReader(database, "SYNTH", cutoff=REVIEW_AT).read("revenue")
    assert result.status == "available", result.reason_code
    assert result.observations[0].observation_id == facts[0].observation.observation_id
    assert facts[0].cell.concept_namespace == "http://fasb.org/us-gaap/2025"


def test_unbound_native_name_cannot_supply_financial_series(database: sqlite3.Connection) -> None:
    seed_table(
        database,
        [("revenue", "2025-01-01", "2025-03-31", "Q1", "100", "USD")],
        concept_namespace="http://fasb.org/us-gaap/2025",
        populate=False,
    )
    result = CanonicalFinancialSeriesReader(database, "SYNTH", cutoff=STAMP).read("revenue")
    assert result.status == "unavailable"
    assert result.reason_code == "exact_financial_concept_unavailable"


def test_universal_consecutive_rules_require_quarters(tmp_path: Path) -> None:
    payload = {
        "ticker": "SYNTH",
        "thesis": "Synthetic fixture",
        "break_rules": [
            {
                "rule_id": "revenue_decline",
                "kpi_name": "Revenue YoY",
                "comparator": "lt",
                "threshold": 0,
                "unit": "percent",
                "consecutive_periods": 2,
                "narrative": "Two consecutive quarters",
                "require_adjacent_quarters": True,
            }
        ],
    }
    (tmp_path / "SYNTH.json").write_text(json.dumps(payload))
    rule = load_holdings_spec(tmp_path, "SYNTH").break_rules[0]
    assert rule.require_adjacent_quarters
    annual = [
        KpiObservation(datetime(2025, 12, 31), Decimal(-1), Unit.PERCENT, fiscal_period_type="FY"),
        KpiObservation(datetime(2024, 12, 31), Decimal(-1), Unit.PERCENT, fiscal_period_type="FY"),
    ]
    assert evaluate_rule(rule, annual).status is BreachStatus.UNRESOLVED


def _exact_source_request(conn: sqlite3.Connection, facts: tuple[ReportedSourceFact, ...]):
    from provenance.fact_read_model import FactReadModel
    from provenance.population_metric_ontology import (
        ExactSourceAdmissionRequest,
        ExactSourceObservationReview,
    )

    reviews: list[ExactSourceObservationReview] = []
    for fact in facts:
        bundle = FactReadModel(conn).provenance_bundle(
            fact.observation.observation_id, cutoff=STAMP
        )
        assert bundle.evidence is not None
        seal = conn.execute(
            "SELECT semantic_key_sha256 FROM fact_cell_identity_seals_v2 WHERE fact_cell_id=?",
            (fact.cell.fact_cell_id,),
        ).fetchone()
        reviews.append(
            ExactSourceObservationReview(
                observation_id=fact.observation.observation_id,
                document_version_id=bundle.evidence.document_version_id,
                subject_binding_revision_id=bundle.evidence.subject_binding_revision_id,
                observation_payload_sha256=bundle.observation_payload_sha256,
                source_locator_sha256=bundle.evidence.source_locator_sha256,
                fact_cell_semantic_key_sha256=seal[0],
            )
        )
    return ExactSourceAdmissionRequest(
        observations=tuple(reviews),
        reviewer_identity="synthetic-reviewer",
        review_evidence={"reviewed_fixture": True},
        knowledge_cutoff=STAMP,
        operation_recorded_at=STAMP,
    )


@pytest.mark.parametrize(
    "commitment",
    [
        "observation_payload_sha256",
        "source_locator_sha256",
        "fact_cell_semantic_key_sha256",
        "subject_binding_revision_id",
        "document_version_id",
        "expected_prior_binding_revision_id",
    ],
)
def test_exact_source_admission_rejects_changed_review_atomically(
    database: sqlite3.Connection, commitment: str
) -> None:
    from provenance.population_metric_ontology import admit_exact_source_observations

    facts = seed_table(
        database,
        [
            ("AdjustedEBITDA", "2025-01-01", "2025-03-31", "Q1", "1088", "millions"),
            ("GrossBookings", "2025-01-01", "2025-03-31", "Q1", "46000", "millions"),
        ],
        concept_namespace="legacy:kpi",
        populate=False,
    )
    request = _exact_source_request(database, facts)
    bad = request.observations[1].model_copy(update={commitment: "0" * 64})
    bad_request = request.model_copy(update={"observations": (request.observations[0], bad)})
    before = database.total_changes
    with pytest.raises(ValueError, match=r"commitment changed|binding head changed"):
        admit_exact_source_observations(database, bad_request)
    assert database.total_changes == before
    assert (
        MetricOntology(database).binding_as_known(facts[0].observation.observation_id, STAMP)
        is None
    )


@pytest.mark.parametrize("corruption", ["commitment", "publication_member"])
def test_selected_source_corruption_preserves_precise_admission_failure(
    database: sqlite3.Connection, corruption: str
) -> None:
    from provenance.fact_read_model import FactAdmissionError

    facts = seed_table(database, [("revenue", "2025-01-01", "2025-03-31", "Q1", "100", "USD")])
    binding = MetricOntology(database).binding_as_known(facts[0].observation.observation_id, STAMP)
    assert binding is not None and binding.canonical_metric_cell_id is not None
    if corruption == "commitment":
        database.execute("DROP TRIGGER trg_fact_observation_payload_commitments_v2_append_only")
        database.execute(
            "UPDATE fact_observation_payload_commitments_v2 SET observation_payload_sha256=? WHERE observation_id=?",
            ("0" * 64, facts[0].observation.observation_id),
        )
    else:
        database.execute("DROP TRIGGER trg_source_fact_publication_members_append_only")
        database.execute(
            "UPDATE source_fact_publication_members SET canonical_member_sha256=? WHERE record_kind='fact_observation' AND record_id=?",
            ("0" * 64, facts[0].observation.observation_id),
        )
    with pytest.raises(FactAdmissionError) as failure:
        CanonicalFactResolutionEngine(database).as_known(binding.canonical_metric_cell_id, STAMP)
    assert failure.value.reason_code in {
        "publication_record_commitment_mismatch",
        "publication_member_tampered",
        "publication_member_set_tampered",
    }
    assert failure.value.disposition == "quarantined"
