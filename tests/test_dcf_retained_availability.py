"""Retained native statement availability and bounded DCF input requirements."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from dcf.primary_fact_overlay import Statement
from provenance import financial_statement_admission
from provenance.fact_plane_v2 import (
    CanonicalJSONObject,
    ExtractionRunCompletenessSealV2,
    FactCellV2,
)
from provenance.fact_read_model import FactReadModel
from provenance.financial_statement_admission import (
    FinancialStatementAdmissionRequest,
    FinancialStatementConcept,
    FinancialStatementConceptReview,
    FinancialStatementContextReview,
    ReviewedFinancialStatementRole,
    admit_reviewed_financial_statements,
)
from provenance.metric_ontology import MetricOntology
from provenance.population_metric_ontology import (
    admit_exact_reported_observations,
)
from provenance.reporting_entity_registry import (
    EvidenceSubjectBindingRevision,
    ReportingEntityRegistry,
)
from provenance.source_fact_repository import (
    ReportedSourceFact,
    SourceFactPublication,
    SourceFactRepository,
)
from sources.dcf_statements import DcfStatementInputs, read_dcf_statements
from tests import test_source_fact_repository as foundation
from tests.fixtures.dcf_statements import STAMP, seed_dcf_statements
from tests.test_dcf_canonical_statements import admitted as admitted
from tests.test_dcf_canonical_statements import statement_database as statement_database


@pytest.fixture(scope="module", params=["currency", "millions"])
def reviewed_statement_database(
    request: pytest.FixtureRequest,
    admitted: DcfStatementInputs,
    migrated_db: Callable[..., Path],
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    path = migrated_db(tmp_path_factory.mktemp("reviewed_dcf") / "statements.sqlite")
    statements: dict[Statement, list[dict[str, object]]] = {
        statement: [
            {**row, "fiscalYear": year}
            for year in (2024, 2025)
            for row in admitted.builder_records(statement)
        ]
        for statement in ("income", "balance", "cash_flow")
    }

    def native(cell: FactCellV2) -> FactCellV2:
        return FactCellV2.model_validate(
            {
                **cell.model_dump(),
                "semantic_key_sha256": None,
                "concept_namespace": "urn:synthetic:reported:financial",
                "concept_name": f"Reported {cell.concept_name}",
                "unit_key": "millions"
                if request.param == "millions" and cell.currency
                else cell.unit_key,
            }
        )

    with sqlite3.connect(path) as conn:
        seed_dcf_statements(
            conn,
            "NATIVE",
            statements,
            currency="USD",
            transform_cell=native,
            legacy_document_link=False,
            recorded_issuer_id="legacy-ticker:NATIVE",
        )
        conn.execute(
            "INSERT INTO evidence_extraction_runs SELECT 'review-context','review-context',"
            "document_version_id,input_sha256,'statement-context-review',extractor_config_sha256,"
            "extractor_code_version,output_sha256,started_at,completed_at,outcome "
            "FROM evidence_extraction_runs WHERE extraction_run_id='dcf-run-income-2024-Q1'"
        )
        ontology, reader = MetricOntology(conn), FactReadModel(conn)
        document = conn.execute(
            "SELECT blob_sha256 FROM evidence_document_versions WHERE document_version_id='dcf-document'"
        ).fetchone()
        assert document is not None
        roles: list[ReviewedFinancialStatementRole] = []
        for observation_id, label in conn.execute(
            "SELECT observation.observation_id,source.concept_name FROM fact_observations_v2 "
            "observation JOIN fact_cells_v2 source USING(fact_cell_id) "
            "WHERE source.concept_namespace='urn:synthetic:reported:financial'"
        ).fetchall():
            bundle = reader.provenance_bundle(str(observation_id), cutoff=STAMP)
            binding = ontology.binding_as_known(str(observation_id), STAMP)
            assert binding is not None
            metric = conn.execute(
                "SELECT metric_id FROM canonical_metric_cells WHERE canonical_metric_cell_id=?",
                (binding.canonical_metric_cell_id,),
            ).fetchone()
            definition = ontology.metric_definition_as_known(str(metric[0]), STAMP)
            assert definition is not None
            concept = TypeAdapter[FinancialStatementConcept](
                FinancialStatementConcept
            ).validate_python(str(label).removeprefix("Reported "))
            node = f"context-{observation_id}"
            wording = f"Synthetic consolidated statement reports {label}."
            locator = json.dumps({"path": f"/statements/{node}"})
            locator_sha = hashlib.sha256(locator.encode()).hexdigest()
            conn.execute(
                "INSERT INTO evidence_nodes VALUES (?,?,1,'review-context',NULL,NULL,"
                "'section',?,?,?,?)",
                (node, node, wording, locator, locator_sha, STAMP),
            )
            assert bundle.evidence is not None
            context = FinancialStatementContextReview(
                document_version_id="dcf-document",
                document_sha256=str(document[0]),
                issuer_id="issuer-1",
                reporting_entity_id=bundle.cell.reporting_entity_id,
                evidence_node_id=node,
                evidence_locator_sha256=locator_sha,
                source_wording=wording,
                accounting_basis=bundle.cell.accounting_basis,
                consolidation_scope="consolidated",
                source_scope_label="consolidated",
                period_start=bundle.cell.period_start,
                period_end=bundle.cell.period_end,
                fiscal_year=bundle.cell.fiscal_year or 2025,
                fiscal_period=bundle.cell.fiscal_period or "FY",
                reviewer="synthetic-statement-review",
                reviewed_at=STAMP,
                rationale="Synthetic source review preserves the exact reported financial role.",
            )
            roles.append(
                ReviewedFinancialStatementRole(
                    observation_id=str(observation_id),
                    observation_payload_sha256=bundle.observation_payload_sha256,
                    expected_definition_revision_id=definition.metric_definition_revision_id,
                    concept=concept,
                    context=context,
                    concept_review=FinancialStatementConceptReview(
                        concept_namespace=bundle.cell.concept_namespace,
                        concept_name=bundle.cell.concept_name,
                        canonical_concept=concept,
                        evidence_node_id=node,
                        evidence_locator_sha256=locator_sha,
                        source_wording=wording,
                        reviewer=context.reviewer,
                        reviewed_at=STAMP,
                        rationale=context.rationale,
                    ),
                )
            )
        admit_reviewed_financial_statements(
            conn,
            FinancialStatementAdmissionRequest(
                roles=tuple(roles), recorded_at=STAMP + timedelta(seconds=1), apply=True
            ),
        )
        conn.commit()
    return path


def test_native_statement_roles_require_review_and_keep_exact_input_lineage(
    reviewed_statement_database: Path,
) -> None:
    with sqlite3.connect(reviewed_statement_database) as conn:
        before_review = read_dcf_statements(conn, "NATIVE", as_of=STAMP)
        assert before_review.cells == ()
        reviewed = read_dcf_statements(conn, "NATIVE", as_of=STAMP + timedelta(seconds=1))
    reviewed.require_complete_actuals()
    assert all(cell.available for cell in reviewed.cells)
    assert all(cell.source_document_id is None for cell in reviewed.cells)
    assert all(cell.source_type == "sec_filing" for cell in reviewed.cells)
    lineage = TypeAdapter[list[dict[str, object]]](list[dict[str, object]]).validate_python(
        reviewed.bridge_lineage("income")["applied"]
    )
    assert all(row["source_document_version_id"] == "dcf-document" for row in lineage)
    assert all(
        cell.provenance
        and cell.provenance.cell.concept_namespace == "urn:synthetic:reported:financial"
        and cell.provenance.evidence
        and cell.provenance.evidence.recorded_issuer_id == "legacy-ticker:NATIVE"
        for cell in reviewed.cells
    )
    assert all(row["capitalExpenditure"] == 0 for row in reviewed.builder_records("cash_flow"))


def test_native_statement_window_preserves_complete_fiscal_year_and_rejects_missing_history(
    reviewed_statement_database: Path,
) -> None:
    with sqlite3.connect(reviewed_statement_database) as conn:
        selected = read_dcf_statements(
            conn, "NATIVE", as_of=STAMP + timedelta(seconds=1), fiscal_year_start=2025
        )
        missing = read_dcf_statements(
            conn, "NATIVE", as_of=STAMP + timedelta(seconds=1), fiscal_year_start=2026
        )
    selected.require_complete_actuals()
    assert selected.fiscal_year_start == 2025
    assert {row["fiscalYear"] for row in selected.builder_records("income")} == {2025}
    with pytest.raises(ValueError, match="no admitted history"):
        missing.require_complete_actuals()


def test_native_context_verification_failure_prevents_dcf_use(
    reviewed_statement_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(
        conn: sqlite3.Connection, context: FinancialStatementContextReview, *, cutoff: datetime
    ) -> None:
        raise ValueError("financial statement context lacks exact source evidence")

    monkeypatch.setattr(
        financial_statement_admission, "verify_financial_statement_context", unavailable
    )
    with sqlite3.connect(reviewed_statement_database) as conn:
        changed = read_dcf_statements(conn, "NATIVE", as_of=STAMP + timedelta(seconds=1))
    assert changed.cells
    assert all(
        "reviewed_financial_statement_context_unavailable" in cell.reason_codes
        for cell in changed.cells
    )
    with pytest.raises(ValueError, match="unavailable"):
        changed.require_complete_actuals()


def test_native_subject_dissent_does_not_inherit_prior_admission(
    reviewed_statement_database: Path,
) -> None:
    with sqlite3.connect(reviewed_statement_database) as conn:
        clock = STAMP + timedelta(seconds=2)
        try:
            ReportingEntityRegistry(conn).persist(
                EvidenceSubjectBindingRevision(
                    binding_revision_id="dcf-subject-dissent",
                    idempotency_key="dcf-subject-dissent",
                    recorded_issuer_id="legacy-ticker:NATIVE",
                    revision=2,
                    supersedes_binding_revision_id="dcf-recorded-subject",
                    issuer_id="issuer-1",
                    reporting_entity_id="reporting-1",
                    outcome="selected",
                    decision_kind="deterministic",
                    material_dissent=True,
                    reason_code="synthetic_material_dissent",
                    reason_details=(("fixture", "source-subject-conflict"),),
                    effective_at=clock,
                    knowledge_at=clock,
                    recorded_at=clock,
                )
            )
            changed = read_dcf_statements(conn, "NATIVE", as_of=clock)
            assert changed.cells and not any(cell.available for cell in changed.cells)
            with pytest.raises(ValueError, match="unavailable"):
                changed.require_complete_actuals()
        finally:
            conn.rollback()


def test_native_values_keep_source_units_and_explicit_scale(
    reviewed_statement_database: Path,
) -> None:
    with sqlite3.connect(reviewed_statement_database) as conn:
        result = read_dcf_statements(conn, "NATIVE", as_of=STAMP + timedelta(seconds=1))
    revenue = next(item for item in result.cells if item.concept == "revenue")
    assert revenue.provenance is not None
    raw = revenue.provenance.observation.decimal_value
    factor = 1000000 if revenue.provenance.observation.unit_key == "millions" else 1
    assert raw is not None and revenue.numeric_value == raw * factor
    records = result.builder_records("income")
    assert records[0]["revenue"] == 1000 * factor
    assert result.bridge_lineage("income")["status"] == "ok"


def test_alias_source_labels_do_not_duplicate_resolved_canonical_inputs(
    reviewed_statement_database: Path,
) -> None:
    alias_at = STAMP + timedelta(seconds=2)
    cutoff = STAMP + timedelta(seconds=3)
    with sqlite3.connect(reviewed_statement_database) as conn:
        conn.row_factory = sqlite3.Row
        initial = read_dcf_statements(conn, "NATIVE", as_of=cutoff)
        revenue = next(item for item in initial.cells if item.concept == "revenue")
        assert revenue.provenance is not None
        cell = FactCellV2.model_validate(
            {
                **revenue.provenance.cell.model_dump(),
                "fact_cell_id": "alias-revenue-cell",
                "idempotency_key": "alias-revenue-cell",
                "semantic_key_sha256": None,
                "concept_name": "Equivalent reported revenue",
                "effective_at": alias_at,
                "knowledge_at": alias_at,
                "recorded_at": alias_at,
            }
        )
        node, run = "alias-revenue-node", "alias-revenue-run"
        locator = {"path": "/alias-revenue/value"}
        encoded = json.dumps(locator, sort_keys=True, separators=(",", ":"))
        conn.execute(
            "INSERT INTO evidence_extraction_runs SELECT ?,?,document_version_id,input_sha256,"
            "'alias-revenue',extractor_config_sha256,extractor_code_version,output_sha256,"
            "started_at,completed_at,outcome FROM evidence_extraction_runs WHERE extraction_run_id='review-context'",
            (run, run),
        )
        conn.execute(
            "INSERT INTO evidence_nodes VALUES (?,?,1,?,NULL,NULL,'table_cell','1000',?,?,?)",
            (node, node, run, encoded, hashlib.sha256(encoded.encode()).hexdigest(), STAMP),
        )
        observation = foundation.make_report(cell, "alias-revenue", at=alias_at).model_copy(
            update={
                "evidence_node_id": node,
                "document_version_id": "dcf-document",
                "subject_binding_revision_id": "dcf-recorded-subject",
                "source_locator": CanonicalJSONObject.model_validate(locator),
                "source_locator_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
            }
        )
        SourceFactRepository(conn).publish(
            SourceFactPublication(
                publication_id="alias-revenue-publication",
                idempotency_key="alias-revenue-publication",
                reported_facts=(ReportedSourceFact(cell=cell, observation=observation),),
                extraction_seals=(
                    ExtractionRunCompletenessSealV2(
                        extraction_seal_id="alias-revenue-seal",
                        idempotency_key="alias-revenue-seal",
                        extraction_run_id=run,
                        expected_node_count=1,
                        completeness_policy_name="all-run-nodes",
                        completeness_policy_version="v1",
                        completeness_policy_sha256=hashlib.sha256(
                            b"alias-completeness"
                        ).hexdigest(),
                        knowledge_at=alias_at,
                        recorded_at=alias_at,
                    ),
                ),
            )
        )
        admit_exact_reported_observations(
            conn,
            (observation.observation_id,),
            knowledge_cutoff=cutoff,
            operation_recorded_at=cutoff,
            apply=True,
        )
        ontology = MetricOntology(conn)
        alias_binding = ontology.binding_as_known(observation.observation_id, cutoff)
        assert alias_binding is not None and alias_binding.source_component_id is not None
        mapping = ontology.mapping_as_known(alias_binding.source_component_id, cutoff)
        assert mapping is not None
        ontology.persist_mapping(
            mapping.model_copy(
                update={
                    "mapping_revision_id": "alias-revenue-mapping",
                    "idempotency_key": "alias-revenue-mapping",
                    "metric_id": revenue.metric_id,
                    "revision": mapping.revision + 1,
                    "supersedes_mapping_revision_id": mapping.mapping_revision_id,
                    "effective_at": alias_at,
                    "knowledge_at": alias_at,
                    "recorded_at": alias_at,
                }
            )
        )
        ontology.persist_binding(
            alias_binding.model_copy(
                update={
                    "binding_revision_id": "alias-revenue-binding",
                    "idempotency_key": "alias-revenue-binding",
                    "canonical_metric_cell_id": revenue.canonical_metric_cell_id,
                    "mapping_revision_id": "alias-revenue-mapping",
                    "revision": alias_binding.revision + 1,
                    "supersedes_binding_revision_id": alias_binding.binding_revision_id,
                    "effective_at": alias_at,
                    "knowledge_at": alias_at,
                    "recorded_at": alias_at,
                }
            )
        )
        available = read_dcf_statements(conn, "NATIVE", as_of=cutoff)
        assert len(available.cells) == len(initial.cells)
        available.require_complete_actuals()
        conn.rollback()
