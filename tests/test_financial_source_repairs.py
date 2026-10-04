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
from sources.canonical_financial_series import CanonicalFinancialSeriesReader
from tests.test_report_canonical_financials import STAMP, seed_table
from tests.test_report_canonical_financials import database as database


def reviewed_binding(conn: sqlite3.Connection, fact: ReportedSourceFact, metric: str) -> None:
    ontology = MetricOntology(conn)
    prior = ontology.binding_as_known(fact.observation.observation_id, STAMP)
    assert prior is not None and prior.source_component_id is not None
    prior_mapping = ontology.mapping_as_known(prior.source_component_id, STAMP)
    assert prior_mapping is not None and prior_mapping.metric_id is not None
    definition = ontology.metric_definition_as_known(prior_mapping.metric_id, STAMP)
    assert definition is not None
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
        definition.model_copy(
            update={
                "metric_id": metric,
                "metric_definition_revision_id": f"definition:{metric}",
                "idempotency_key": f"definition:{metric}",
                "scope_constraints": {
                    **definition.scope_constraints,
                    "currency": fact.cell.currency,
                },
            }
        )
    )
    mapping = prior_mapping.model_copy(
        update={
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
            knowledge_at=STAMP,
            recorded_at=STAMP,
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
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )
    CanonicalFactResolutionEngine(conn).resolve(
        cell_id,
        STAMP,
        ResolutionPolicy(name="fixture", version="1", config={}),
        recorded_at=STAMP,
    )
    conn.commit()


def test_financial_reader_uses_native_reviewed_binding(database: sqlite3.Connection) -> None:
    facts = seed_table(
        database,
        [("RevenueFromContractWithCustomer", "2025-01-01", "2025-03-31", "Q1", "100", "USD")],
        concept_namespace="http://fasb.org/us-gaap/2025",
    )
    reviewed_binding(database, facts[0], "revenue")
    result = CanonicalFinancialSeriesReader(database, "SYNTH", cutoff=STAMP).read("revenue")
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


def _bind_derived(conn: sqlite3.Connection, fact: object, metric_id: str) -> str:
    from provenance.fact_read_model import FactReadModel
    from provenance.metric_ontology import MappingRevision, SourceTaxonomyComponent
    from provenance.source_fact_repository import DerivedSourceFact

    assert isinstance(fact, DerivedSourceFact)
    ontology = MetricOntology(conn)
    assert fact.cell.taxonomy_version is not None
    bundle = FactReadModel(conn).provenance_bundle(fact.observation.observation_id, cutoff=STAMP)
    assert bundle.derivation is not None
    component_id = f"derived-component:{fact.cell.fact_cell_id[-64:]}"
    ontology.persist_source_component(
        SourceTaxonomyComponent(
            component_id=component_id,
            idempotency_key=component_id,
            component_kind="concept",
            taxonomy_namespace=fact.cell.concept_namespace,
            local_name=fact.cell.concept_name,
            taxonomy_name=fact.cell.taxonomy_name,
            taxonomy_version=fact.cell.taxonomy_version,
            reporting_entity_id=fact.cell.reporting_entity_id,
            is_extension=True,
            standard_label=fact.cell.concept_name,
            definition_text="Reviewed synthetic subtraction",
            evidence_locator={"derivation_seal_id": bundle.derivation.derivation_seal_id},
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )
    mapping_id = f"derived-map:{fact.cell.fact_cell_id[-64:]}"
    ontology.persist_mapping(
        MappingRevision(
            mapping_revision_id=mapping_id,
            idempotency_key=mapping_id,
            revision=1,
            source_component_id=component_id,
            metric_id=metric_id,
            disposition="derived",
            policy_name="reviewed_formula",
            policy_version="1",
            policy_config_sha256="a" * 64,
            method_name="manual_review",
            method_version="1",
            reviewer_identity="synthetic-reviewer",
            constraints={
                "derived_formula": {
                    "formula_id": bundle.derivation.formula_id,
                    "formula_version": bundle.derivation.formula_version,
                    "formula_definition_sha256": bundle.derivation.formula_definition_sha256,
                }
            },
            evidence={"fixture": True},
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )
    cell_id = f"derived-target:{fact.cell.fact_cell_id[-64:]}"
    ontology.persist_canonical_metric_cell(
        CanonicalMetricCell(
            canonical_metric_cell_id=cell_id,
            idempotency_key=cell_id,
            metric_id=metric_id,
            reporting_entity_id=fact.cell.reporting_entity_id,
            period_kind="duration",
            period_start=fact.cell.period_start,
            period_end=fact.cell.period_end,
            unit_family="currency",
            accounting_basis=fact.cell.accounting_basis,
            consolidation_scope=fact.cell.consolidation_scope,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )
    ontology.persist_binding(
        BindingRevision(
            binding_revision_id=f"derived-binding:{fact.cell.fact_cell_id[-64:]}",
            idempotency_key=f"derived-binding:{fact.cell.fact_cell_id[-64:]}",
            revision=1,
            fact_cell_id=fact.cell.fact_cell_id,
            source_observation_id=fact.observation.observation_id,
            source_component_id=component_id,
            mapping_revision_id=mapping_id,
            canonical_metric_cell_id=cell_id,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )
    CanonicalFactResolutionEngine(conn).resolve(
        cell_id,
        STAMP,
        ResolutionPolicy(name="fixture", version="1", config={}),
        recorded_at=STAMP,
    )
    conn.commit()
    return cell_id


def test_ytd_subtraction_publishes_sealed_quarter_and_keeps_original(
    database: sqlite3.Connection,
) -> None:
    from provenance.fact_read_model import FactReadModel
    from provenance.financial_derivations import QuarterDerivationRequest, publish_discrete_quarter

    facts = seed_table(
        database,
        [
            (
                "NetCashProvidedByOperatingActivities",
                "2025-01-01",
                "2025-03-31",
                "Q1",
                "3283",
                "USD",
            ),
            (
                "NetCashProvidedByOperatingActivities",
                "2025-01-01",
                "2025-06-30",
                "Q2",
                "6484",
                "USD",
            ),
        ],
        concept_namespace="http://fasb.org/us-gaap/2025",
    )
    for fact in facts:
        reviewed_binding(database, fact, "operating_cash_flow")
    request = QuarterDerivationRequest(
        cumulative_observation_id=facts[1].observation.observation_id,
        prior_cumulative_observation_id=facts[0].observation.observation_id,
        knowledge_cutoff=STAMP,
        recorded_at=STAMP,
    )
    derived, receipt = publish_discrete_quarter(database, request)
    assert derived.observation.numeric_value == "3201"
    assert (
        derived.cell.period_start is not None
        and derived.cell.period_start.date().isoformat() == "2025-04-01"
    )
    assert derived.cell.fiscal_period == "Q2"
    assert receipt.created_record_ids
    assert publish_discrete_quarter(database, request)[1].created_record_ids == ()
    assert (
        FactReadModel(database)
        .provenance_bundle(facts[1].observation.observation_id, cutoff=STAMP)
        .cell.period_start
        == facts[1].cell.period_start
    )
    _bind_derived(database, derived, "operating_cash_flow")
    reader = CanonicalFinancialSeriesReader(database, "SYNTH", cutoff=STAMP)
    result = reader.read("operating_cash_flow")
    assert result.status == "available", result.reason_code
    assert [item.value for item in result.observations] == [Decimal(3283), Decimal(3201)]
    assert result.observations[1].derivation is not None
    assert result.observations[1].document_version_id is None
    assert reader.project_coordinates("operating_cash_flow").coordinates[1].status == "admitted"


def test_quarter_request_rejects_missing_selected_operand(database: sqlite3.Connection) -> None:
    import pytest

    from provenance.financial_derivations import QuarterDerivationRequest, publish_discrete_quarter

    facts = seed_table(
        database,
        [
            ("operating_cash_flow", "2025-01-01", "2025-03-31", "Q1", "3283", "USD"),
            ("operating_cash_flow", "2025-01-01", "2025-06-30", "Q2", "6484", "USD"),
        ],
        populate=False,
    )
    with pytest.raises(ValueError, match="current reviewed canonical binding"):
        publish_discrete_quarter(
            database,
            QuarterDerivationRequest(
                cumulative_observation_id=facts[1].observation.observation_id,
                prior_cumulative_observation_id=facts[0].observation.observation_id,
                knowledge_cutoff=STAMP,
                recorded_at=STAMP,
            ),
        )


def test_signed_capex_is_preserved_and_positive_outflow_is_rejected(
    database: sqlite3.Connection,
) -> None:

    from provenance.financial_derivations import QuarterDerivationRequest, publish_discrete_quarter

    facts = seed_table(
        database,
        [
            (
                "PaymentsToAcquirePropertyPlantAndEquipment",
                "2025-01-01",
                "2025-03-31",
                "Q1",
                "-121",
                "USD",
            ),
            (
                "PaymentsToAcquirePropertyPlantAndEquipment",
                "2025-01-01",
                "2025-06-30",
                "Q2",
                "-185",
                "USD",
            ),
        ],
        concept_namespace="http://fasb.org/us-gaap/2025",
    )
    for fact in facts:
        reviewed_binding(database, fact, "capital_expenditure")
    request = QuarterDerivationRequest(
        cumulative_observation_id=facts[1].observation.observation_id,
        prior_cumulative_observation_id=facts[0].observation.observation_id,
        knowledge_cutoff=STAMP,
        recorded_at=STAMP,
    )
    derived, _ = publish_discrete_quarter(database, request)
    assert derived.observation.numeric_value == "-64"
    _bind_derived(database, derived, "capital_expenditure")


def test_monetary_scale_is_explicit_and_preserves_source(database: sqlite3.Connection) -> None:
    import pytest

    from provenance.fact_read_model import FactReadModel
    from provenance.financial_derivations import MonetaryScaleRequest, publish_monetary_scale

    facts = seed_table(
        database, [("revenue", "2025-01-01", "2025-03-31", "Q1", "1088", "millions")]
    )
    request = MonetaryScaleRequest(
        source_observation_id=facts[0].observation.observation_id,
        source_scale="millions",
        knowledge_cutoff=STAMP,
        recorded_at=STAMP,
    )
    derived, receipt = publish_monetary_scale(database, request)
    assert derived.observation.numeric_value == "1088000000"
    assert derived.cell.unit_key == "USD"
    assert receipt.derivation_seal_ids
    source = FactReadModel(database).provenance_bundle(
        facts[0].observation.observation_id, cutoff=STAMP
    )
    assert source.cell.unit_key == "millions"
    assert source.observation.decimal_value == Decimal(1088)
    with pytest.raises(ValueError, match="exact admitted source-native unit"):
        publish_monetary_scale(database, request.model_copy(update={"source_scale": "billions"}))


def test_positive_capex_needs_explicit_sign_transform(database: sqlite3.Connection) -> None:
    import pytest

    from provenance.financial_derivations import QuarterDerivationRequest, publish_discrete_quarter

    facts = seed_table(
        database,
        [
            ("capex", "2025-01-01", "2025-03-31", "Q1", "121", "USD"),
            ("capex", "2025-01-01", "2025-06-30", "Q2", "185", "USD"),
        ],
        concept_namespace="http://fasb.org/us-gaap/2025",
    )
    for fact in facts:
        reviewed_binding(database, fact, "capital_expenditure")
    with pytest.raises(ValueError, match="signed outflow"):
        publish_discrete_quarter(
            database,
            QuarterDerivationRequest(
                cumulative_observation_id=facts[1].observation.observation_id,
                prior_cumulative_observation_id=facts[0].observation.observation_id,
                knowledge_cutoff=STAMP,
                recorded_at=STAMP,
            ),
        )


def test_reviewed_kpi_money_binding_and_scale_use_one_reported_observation(
    database: sqlite3.Connection,
) -> None:
    from provenance.financial_derivations import (
        FinancialMetricBindingReview,
        MonetaryScaleRequest,
        bind_reviewed_financial_observation,
        publish_monetary_scale,
    )

    facts = seed_table(
        database,
        [("AdjustedEBITDA", "2025-01-01", "2025-03-31", "Q1", "1088", "millions")],
        concept_namespace="urn:earnings-summary:legacy:kpi",
    )
    original_count = database.execute(
        "SELECT COUNT(*) FROM fact_observations_v2 WHERE observation_kind='reported'"
    ).fetchone()[0]
    scaled, _ = publish_monetary_scale(
        database,
        MonetaryScaleRequest(
            source_observation_id=facts[0].observation.observation_id,
            source_scale="millions",
            knowledge_cutoff=STAMP,
            recorded_at=STAMP,
        ),
    )
    review = FinancialMetricBindingReview(
        observation_id=scaled.observation.observation_id,
        metric_id="financial:adjusted_ebitda",
        canonical_name="adjusted_ebitda",
        definition_text="Reviewed synthetic EBITDA metric retaining the admitted source basis and scope.",
        reviewer_identity="synthetic-reviewer",
        review_evidence={"manifest": "fixture"},
        knowledge_cutoff=STAMP,
        recorded_at=STAMP,
    )
    target = bind_reviewed_financial_observation(database, review)
    assert bind_reviewed_financial_observation(database, review) == target
    CanonicalFactResolutionEngine(database).resolve(
        target, STAMP, ResolutionPolicy(name="fixture", version="2", config={}), recorded_at=STAMP
    )
    result = CanonicalFinancialSeriesReader(database, "SYNTH", cutoff=STAMP).read("adjusted_ebitda")
    assert result.status == "available", result.reason_code
    assert result.observations[0].value == Decimal(1088000000)
    assert result.observations[0].derivation is not None
    assert (
        database.execute(
            "SELECT COUNT(*) FROM fact_observations_v2 WHERE observation_kind='reported'"
        ).fetchone()[0]
        == original_count
    )


def test_financial_candidate_migration_preserves_rows_and_guards(
    database: sqlite3.Connection, monkeypatch: object
) -> None:
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from pytest import MonkeyPatch
    from sqlalchemy import create_engine

    assert isinstance(monkeypatch, MonkeyPatch)
    facts = seed_table(database, [("revenue", "2025-01-01", "2025-03-31", "Q1", "100", "USD")])
    assert facts
    database.commit()
    path = database.execute("PRAGMA database_list").fetchone()[2]
    before = database.execute(
        "SELECT * FROM canonical_fact_candidate_dispositions ORDER BY candidate_disposition_id"
    ).fetchall()
    assert before
    triggers_before = {
        row[0]
        for row in database.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND instr(sql,'canonical_fact_candidate_dispositions')>0"
        )
    }
    migration_path = (
        Path(__file__).parents[1] / "alembic/versions/0053_reviewed_financial_derivations.py"
    )
    spec = importlib.util.spec_from_file_location("financial_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.downgrade()
        migration.upgrade()
    assert (
        database.execute(
            "SELECT * FROM canonical_fact_candidate_dispositions ORDER BY candidate_disposition_id"
        ).fetchall()
        == before
    )
    assert {
        row[0]
        for row in database.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND instr(sql,'canonical_fact_candidate_dispositions')>0"
        )
    } == triggers_before
    assert database.execute("PRAGMA foreign_key_check").fetchall() == []
    assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("mismatch", ["currency", "unit", "year", "year_start", "nonquarter"])
def test_cumulative_transform_rejects_incomparable_operands(
    database: sqlite3.Connection, mismatch: str
) -> None:
    from provenance.financial_derivations import QuarterDerivationRequest, publish_discrete_quarter

    prior_start = "2025-02-01" if mismatch == "year_start" else "2025-01-01"
    end = "2025-08-31" if mismatch == "nonquarter" else "2025-06-30"
    prior_unit = "millions" if mismatch == "unit" else "USD"
    facts = seed_table(
        database,
        [
            ("operating_cash_flow", prior_start, "2025-03-31", "Q1", "3283", prior_unit),
            ("operating_cash_flow", "2025-01-01", end, "Q2", "6484", "USD"),
        ],
        currencies={0: "EUR"} if mismatch == "currency" else None,
        fiscal_years={0: 2024} if mismatch == "year" else None,
    )
    with pytest.raises(ValueError, match=r"incomparable|fiscal label"):
        publish_discrete_quarter(
            database,
            QuarterDerivationRequest(
                cumulative_observation_id=facts[1].observation.observation_id,
                prior_cumulative_observation_id=facts[0].observation.observation_id,
                knowledge_cutoff=STAMP,
                recorded_at=STAMP,
            ),
        )


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


def test_exact_source_admission_is_bounded_replayable_and_scale_ready(
    database: sqlite3.Connection,
) -> None:
    from provenance.financial_derivations import MonetaryScaleRequest, publish_monetary_scale
    from provenance.population_metric_ontology import admit_exact_source_observations

    facts = seed_table(
        database,
        [
            ("AdjustedEBITDA", "2025-01-01", "2025-03-31", "Q1", "1088", "millions"),
            ("SiblingRevenue", "2025-01-01", "2025-03-31", "Q1", "9000", "USD"),
        ],
        concept_namespace="legacy:kpi",
        populate=False,
    )
    request = _exact_source_request(database, facts[:1])
    receipt = admit_exact_source_observations(database, request)
    assert receipt.coverage == "exact_reviewed_observations_only"
    assert receipt.observation_ids == (facts[0].observation.observation_id,)
    assert (
        MetricOntology(database).binding_as_known(facts[1].observation.observation_id, STAMP)
        is None
    )
    assert database.execute("SELECT COUNT(*) FROM ontology_snapshot_headers").fetchone()[0] == 0
    before = database.total_changes
    assert admit_exact_source_observations(database, request) == receipt
    assert database.total_changes == before
    derived, _ = publish_monetary_scale(
        database,
        MonetaryScaleRequest(
            source_observation_id=facts[0].observation.observation_id,
            source_scale="millions",
            knowledge_cutoff=STAMP,
            recorded_at=STAMP,
        ),
    )
    assert derived.observation.numeric_value == "1088000000"
    assert (
        CanonicalFinancialSeriesReader(database, "SYNTH", cutoff=STAMP)
        .read("adjusted_ebitda")
        .status
        == "unavailable"
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


def test_reviewed_financial_currency_cannot_change_or_bypass_sql_guard(
    database: sqlite3.Connection,
) -> None:
    from provenance.financial_derivations import (
        FinancialMetricBindingReview,
        MonetaryScaleRequest,
        bind_reviewed_financial_observation,
        publish_monetary_scale,
    )
    from provenance.population_metric_ontology import admit_exact_source_observations

    facts = seed_table(
        database,
        [
            ("AdjustedEBITDA", "2025-01-01", "2025-03-31", "Q1", "1088", "millions"),
            ("AdjustedEBITDA", "2025-04-01", "2025-06-30", "Q2", "1000", "millions"),
        ],
        concept_namespace="legacy:kpi",
        currencies={1: "EUR"},
        populate=False,
    )
    admit_exact_source_observations(database, _exact_source_request(database, facts))
    scaled = [
        publish_monetary_scale(
            database,
            MonetaryScaleRequest(
                source_observation_id=fact.observation.observation_id,
                source_scale="millions",
                knowledge_cutoff=STAMP,
                recorded_at=STAMP,
            ),
        )[0]
        for fact in facts
    ]
    review = FinancialMetricBindingReview(
        observation_id=scaled[0].observation.observation_id,
        metric_id="adjusted_ebitda",
        canonical_name="adjusted_ebitda",
        definition_text="Reviewed consolidated adjusted EBITDA",
        reviewer_identity="synthetic-reviewer",
        review_evidence={"fixture": True},
        knowledge_cutoff=STAMP,
        recorded_at=STAMP,
    )
    bind_reviewed_financial_observation(database, review)
    binding = MetricOntology(database).binding_as_known(review.observation_id, STAMP)
    assert binding is not None and binding.mapping_revision_id is not None
    with pytest.raises(ValueError, match="explicit revision review required"):
        bind_reviewed_financial_observation(
            database,
            review.model_copy(update={"observation_id": scaled[1].observation.observation_id}),
        )
    # The forged cell matches the EUR period and all coordinates except currency.
    target = "forged-usd-target"
    MetricOntology(database).persist_canonical_metric_cell(
        CanonicalMetricCell(
            canonical_metric_cell_id=target,
            idempotency_key=target,
            metric_id="adjusted_ebitda",
            reporting_entity_id=scaled[1].cell.reporting_entity_id,
            period_kind="duration",
            period_start=scaled[1].cell.period_start,
            period_end=scaled[1].cell.period_end,
            unit_family="currency",
            accounting_basis=scaled[1].cell.accounting_basis,
            consolidation_scope=scaled[1].cell.consolidation_scope,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )
    # Exercise the retained SQL authority with a forged EUR-to-USD coordinate.
    with pytest.raises(sqlite3.IntegrityError, match="exact sealed reviewed"):
        database.execute(
            "INSERT INTO fact_cell_canonical_binding_revisions (binding_revision_id,idempotency_key,fact_cell_id,source_observation_id,revision,canonical_metric_cell_id,mapping_revision_id,source_component_id,binding_status,effective_at,knowledge_at,recorded_at,commitment_json,commitment_sha256) "
            "SELECT 'forged-eur','forged-eur',?,?,1,?,mapping_revision_id,source_component_id,'bound',effective_at,knowledge_at,recorded_at,commitment_json,commitment_sha256 "
            "FROM fact_cell_canonical_binding_revisions WHERE binding_revision_id=?",
            (
                scaled[1].cell.fact_cell_id,
                scaled[1].observation.observation_id,
                target,
                binding.binding_revision_id,
            ),
        )
    assert (
        MetricOntology(database).binding_as_known(scaled[1].observation.observation_id, STAMP)
        is None
    )


def test_derived_revision_preserves_predecessor_and_retired_operand_fails_closed(
    database: sqlite3.Connection,
) -> None:
    from provenance.fact_read_model import FactReadModel
    from provenance.financial_derivations import QuarterDerivationRequest, publish_discrete_quarter

    facts = seed_table(
        database,
        [
            (
                "NetCashProvidedByOperatingActivities",
                "2025-01-01",
                "2025-03-31",
                "Q1",
                "3283",
                "USD",
            ),
            (
                "NetCashProvidedByOperatingActivities",
                "2025-01-01",
                "2025-06-30",
                "Q2",
                "6484",
                "USD",
            ),
        ],
        concept_namespace="http://fasb.org/us-gaap/2025",
    )
    for fact in facts:
        reviewed_binding(database, fact, "operating_cash_flow")
    request = QuarterDerivationRequest(
        cumulative_observation_id=facts[1].observation.observation_id,
        prior_cumulative_observation_id=facts[0].observation.observation_id,
        knowledge_cutoff=STAMP,
        recorded_at=STAMP,
    )
    original, _ = publish_discrete_quarter(database, request)
    target = _bind_derived(database, original, "operating_cash_flow")
    later = STAMP + timedelta(days=1)
    update = request.model_copy(update={"knowledge_cutoff": later, "recorded_at": later})
    with pytest.raises(ValueError, match="expected predecessor required"):
        publish_discrete_quarter(database, update)
    update = update.model_copy(
        update={"expected_prior_derived_observation_id": original.observation.observation_id}
    )
    revised, _ = publish_discrete_quarter(database, update)
    assert revised.observation.revision_kind == "restatement"
    assert revised.observation.supersedes_observation_id == original.observation.observation_id
    assert revised.cell.fact_cell_id == original.cell.fact_cell_id
    assert publish_discrete_quarter(database, update)[1].created_record_ids == ()
    assert FactReadModel(database).provenance_bundle(
        original.observation.observation_id, cutoff=later
    ).observation.decimal_value == Decimal(3201)
    ontology = MetricOntology(database)
    operand = ontology.binding_as_known(facts[0].observation.observation_id, STAMP)
    assert operand is not None
    ontology.persist_binding(
        operand.model_copy(
            update={
                "binding_revision_id": "retired-source-operand",
                "idempotency_key": "retired-source-operand",
                "revision": operand.revision + 1,
                "supersedes_binding_revision_id": operand.binding_revision_id,
                "binding_status": "retired",
                "knowledge_at": later,
                "recorded_at": later,
            }
        )
    )
    reader = CanonicalFinancialSeriesReader(database, "SYNTH", cutoff=later)
    bundle = FactReadModel(database).provenance_bundle(
        original.observation.observation_id, cutoff=later
    )
    assert not reader.derivation_operands_current(bundle)
    rejected = CanonicalFactResolutionEngine(database).resolve(
        target,
        later,
        ResolutionPolicy(name="fixture", version="2", config={}),
        recorded_at=later,
    )
    assert rejected.status != "resolved"
    assert reader.read("operating_cash_flow").status == "unavailable"


def _nested_quarter(conn: sqlite3.Connection) -> tuple[str, str, str, object]:
    from provenance.financial_derivations import (
        FinancialMetricBindingReview,
        MonetaryScaleRequest,
        QuarterDerivationRequest,
        bind_reviewed_financial_observation,
        publish_discrete_quarter,
        publish_monetary_scale,
    )
    from provenance.population_metric_ontology import admit_exact_source_observations

    facts = seed_table(
        conn,
        [
            (
                "NetCashProvidedByOperatingActivities",
                "2025-01-01",
                "2025-03-31",
                "Q1",
                "3283",
                "millions",
            ),
            (
                "NetCashProvidedByOperatingActivities",
                "2025-01-01",
                "2025-06-30",
                "Q2",
                "6484",
                "millions",
            ),
        ],
        concept_namespace="native:issuer-financial",
        populate=False,
    )
    admit_exact_source_observations(conn, _exact_source_request(conn, facts))
    scaled_ids: list[str] = []
    targets: list[str] = []
    for fact in facts:
        scaled, _ = publish_monetary_scale(
            conn,
            MonetaryScaleRequest(
                source_observation_id=fact.observation.observation_id,
                source_scale="millions",
                knowledge_cutoff=STAMP,
                recorded_at=STAMP,
            ),
        )
        scaled_ids.append(scaled.observation.observation_id)
        target = bind_reviewed_financial_observation(
            conn,
            FinancialMetricBindingReview(
                observation_id=scaled.observation.observation_id,
                metric_id="financial:operating_cash_flow",
                canonical_name="operating_cash_flow",
                definition_text="Reviewed consolidated USD operating cash flow",
                reviewer_identity="synthetic-reviewer",
                review_evidence={"fixture": True},
                knowledge_cutoff=STAMP,
                recorded_at=STAMP,
            ),
        )
        targets.append(target)
        CanonicalFactResolutionEngine(conn).resolve(
            target,
            STAMP,
            ResolutionPolicy(name="fixture", version="2", config={}),
            recorded_at=STAMP,
        )
    request = QuarterDerivationRequest(
        cumulative_observation_id=scaled_ids[1],
        prior_cumulative_observation_id=scaled_ids[0],
        knowledge_cutoff=STAMP,
        recorded_at=STAMP,
    )
    quarter, _ = publish_discrete_quarter(conn, request)
    quarter_target = bind_reviewed_financial_observation(
        conn,
        FinancialMetricBindingReview(
            observation_id=quarter.observation.observation_id,
            metric_id="financial:operating_cash_flow",
            canonical_name="operating_cash_flow",
            definition_text="Reviewed consolidated USD operating cash flow",
            reviewer_identity="synthetic-reviewer",
            review_evidence={"fixture": True},
            knowledge_cutoff=STAMP,
            recorded_at=STAMP,
        ),
    )
    CanonicalFactResolutionEngine(conn).resolve(
        quarter_target,
        STAMP,
        ResolutionPolicy(name="fixture", version="2", config={}),
        recorded_at=STAMP,
    )
    return facts[0].observation.observation_id, targets[0], quarter_target, request


def test_shared_resolver_and_publication_reject_nested_retired_raw_operand(
    database: sqlite3.Connection,
) -> None:
    from provenance.financial_derivations import QuarterDerivationRequest, publish_discrete_quarter

    raw_id, scale_target, quarter_target, request = _nested_quarter(database)
    assert isinstance(request, QuarterDerivationRequest)
    engine = CanonicalFactResolutionEngine(database)
    assert engine.as_known(quarter_target, STAMP) is not None
    ontology = MetricOntology(database)
    raw_binding = ontology.binding_as_known(raw_id, STAMP)
    assert raw_binding is not None
    later = STAMP + timedelta(days=1)
    ontology.persist_binding(
        raw_binding.model_copy(
            update={
                "binding_revision_id": "nested-retired-raw",
                "idempotency_key": "nested-retired-raw",
                "revision": raw_binding.revision + 1,
                "supersedes_binding_revision_id": raw_binding.binding_revision_id,
                "binding_status": "retired",
                "knowledge_at": later,
                "recorded_at": later,
            }
        )
    )
    before = database.total_changes
    # No intermediate resolution is refreshed before these shared reads.
    assert engine.as_known(scale_target, later) is None
    assert engine.as_known(quarter_target, later) is None
    with pytest.raises(ValueError, match="lineage is no longer current"):
        publish_discrete_quarter(
            database, request.model_copy(update={"knowledge_cutoff": later, "recorded_at": later})
        )
    assert database.total_changes == before
    result = engine.resolve(
        quarter_target,
        later,
        ResolutionPolicy(name="fixture", version="2", config={}),
        recorded_at=later,
    )
    assert result.status == "unresolved"


@pytest.mark.parametrize("limit", ["depth", "nodes", "cycle"])
def test_shared_derived_lineage_bounds_fail_closed(
    database: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    import provenance.canonical_fact_resolution as owner
    from provenance.fact_read_model import FactReadModel

    _, _, quarter_target, _ = _nested_quarter(database)
    engine = CanonicalFactResolutionEngine(database)
    selected = engine.as_known(quarter_target, STAMP)
    assert selected is not None and selected.selected_observation_id is not None
    if limit == "depth":
        monkeypatch.setattr(owner, "MAX_DERIVATION_DEPTH", 1)
    elif limit == "nodes":
        monkeypatch.setattr(owner, "MAX_DERIVATION_NODES", 2)
    else:
        original = FactReadModel.provenance_bundle
        quarter_id = selected.selected_observation_id

        def cyclic_bundle(self: FactReadModel, observation_id: str, *, cutoff: datetime):
            bundle = original(self, observation_id, cutoff=cutoff)
            if observation_id != quarter_id:
                return bundle
            assert bundle.derivation is not None
            return bundle.model_copy(
                update={
                    "derivation": bundle.derivation.model_copy(
                        update={
                            "input_observation_ids": (quarter_id,),
                            "input_canonical_resolution_revision_ids": (
                                selected.canonical_resolution_revision_id,
                            ),
                        }
                    )
                }
            )

        monkeypatch.setattr(FactReadModel, "provenance_bundle", cyclic_bundle)
    assert not engine.observation_lineage_current(selected.selected_observation_id, STAMP)
    assert engine.as_known(quarter_target, STAMP) is None


@pytest.mark.parametrize(
    "field",
    [
        "observation_id",
        "reviewer_identity",
        "canonical_name",
        "metric_id",
        "definition_text",
        "review_evidence",
        "knowledge_cutoff",
        "recorded_at",
        "expected_prior_binding_revision_id",
        "expected_prior_derived_observation_id",
    ],
)
def test_financial_binding_replay_requires_full_review_commitment(
    database: sqlite3.Connection, field: str
) -> None:
    from provenance.financial_derivations import (
        FinancialMetricBindingReview,
        MonetaryScaleRequest,
        bind_reviewed_financial_observation,
        publish_monetary_scale,
    )
    from provenance.population_metric_ontology import admit_exact_source_observations

    facts = seed_table(
        database,
        [("AdjustedEBITDA", "2025-01-01", "2025-03-31", "Q1", "1088", "millions")],
        concept_namespace="native:kpi",
        populate=False,
    )
    admit_exact_source_observations(database, _exact_source_request(database, facts))
    scaled, _ = publish_monetary_scale(
        database,
        MonetaryScaleRequest(
            source_observation_id=facts[0].observation.observation_id,
            source_scale="millions",
            knowledge_cutoff=STAMP,
            recorded_at=STAMP,
        ),
    )
    review = FinancialMetricBindingReview(
        observation_id=scaled.observation.observation_id,
        metric_id="financial:adjusted_ebitda",
        canonical_name="adjusted_ebitda",
        definition_text="Reviewed USD adjusted EBITDA",
        reviewer_identity="synthetic-reviewer",
        review_evidence={"fixture": True},
        knowledge_cutoff=STAMP,
        recorded_at=STAMP,
    )
    target = bind_reviewed_financial_observation(database, review)
    before = database.total_changes
    assert bind_reviewed_financial_observation(database, review) == target
    assert database.total_changes == before
    altered: object = "changed"
    if field == "observation_id":
        altered = facts[0].observation.observation_id
    elif field in {"knowledge_cutoff", "recorded_at"}:
        altered = STAMP + timedelta(days=1)
    elif field == "review_evidence":
        altered = {"fixture": "changed"}
    with pytest.raises(ValueError, match="binding head changed"):
        bind_reviewed_financial_observation(database, review.model_copy(update={field: altered}))
    assert database.total_changes == before
