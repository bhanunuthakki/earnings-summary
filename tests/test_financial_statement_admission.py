"""A migrated source publication reaches the shared reader through review."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import timedelta

import pytest

from provenance.fact_read_model import FactReadModel
from provenance.financial_statement_admission import (
    FinancialStatementAdmissionRequest,
    FinancialStatementContextReview,
    ReviewedFinancialStatementRole,
    admit_reviewed_financial_statements,
)
from provenance.metric_ontology import MetricOntology
from sources.discovery_financials import read_financial_history
from sources.report_financials import read_financial_table
from tests.test_report_canonical_financials import STAMP, seed_table
from tests.test_report_canonical_financials import database as database


def _role(
    conn: sqlite3.Connection, *, multiple_periods: bool = False
) -> ReviewedFinancialStatementRole:
    rows = [("Revenues", "2025-01-01", "2025-12-31", "FY", "1000000", "USD")]
    if multiple_periods:
        rows.append(("Revenues", "2024-01-01", "2024-12-31", "FY", "900000", "USD"))
    facts = seed_table(
        conn,
        rows,
        concept_namespace="https://fasb.org/us-gaap/2026",
        consolidation_scope="other",
    )
    observation_id = facts[0].observation.observation_id
    bundle = FactReadModel(conn).provenance_bundle(observation_id, cutoff=STAMP)
    binding = MetricOntology(conn).binding_as_known(observation_id, STAMP)
    assert binding is not None
    row = conn.execute(
        "SELECT metric_id FROM canonical_metric_cells WHERE canonical_metric_cell_id=?",
        (binding.canonical_metric_cell_id,),
    ).fetchone()
    definition = MetricOntology(conn).metric_definition_as_known(str(row[0]), STAMP)
    assert definition is not None
    # A separate retained section extraction qualifies the statement heading.
    conn.execute(
        "INSERT INTO evidence_extraction_runs SELECT 'context-run','context-run',"
        "document_version_id,input_sha256,'statement-context-section',extractor_config_sha256,"
        "extractor_code_version,output_sha256,started_at,completed_at,outcome "
        "FROM evidence_extraction_runs WHERE extraction_run_id='report-run'"
    )
    locator = '{"path":"/statements/heading"}'
    locator_sha = hashlib.sha256(locator.encode()).hexdigest()
    conn.execute(
        "INSERT INTO evidence_nodes VALUES ('context-node','context-node',1,"
        "'context-run',NULL,NULL,'section','Combined carve-out statements of income',?,?,?)",
        (locator, locator_sha, STAMP),
    )
    doc = conn.execute(
        "SELECT blob_sha256 FROM evidence_document_versions WHERE document_version_id='report-document'"
    ).fetchone()
    assert bundle.cell.period_start is not None
    context = FinancialStatementContextReview(
        document_version_id="report-document",
        document_sha256=str(doc[0]),
        issuer_id="issuer-1",
        reporting_entity_id=bundle.cell.reporting_entity_id,
        evidence_node_id="context-node",
        evidence_locator_sha256=locator_sha,
        source_wording="Combined carve-out statements of income",
        accounting_basis="us_gaap",
        consolidation_scope="other",
        source_scope_label="combined_carve_out",
        period_start=bundle.cell.period_start,
        period_end=bundle.cell.period_end,
        fiscal_year=2025,
        fiscal_period="FY",
        reviewer="synthetic-source-review",
        reviewed_at=STAMP,
        rationale="Synthetic heading establishes the reported combined carve-out scope.",
    )
    return ReviewedFinancialStatementRole(
        observation_id=observation_id,
        observation_payload_sha256=bundle.observation_payload_sha256,
        expected_definition_revision_id=definition.metric_definition_revision_id,
        concept="revenue",
        context=context,
    )


def test_native_qname_needs_exact_review_and_keeps_combined_scope(
    database: sqlite3.Connection,
) -> None:
    role = _role(database)
    original = FactReadModel(database).provenance_bundle(role.observation_id, cutoff=STAMP)
    assert read_financial_table(database, "SYNTH", as_of=STAMP).reason_codes == (
        "no_canonical_financial_cells",
    )
    clock = STAMP + timedelta(seconds=1)
    request = FinancialStatementAdmissionRequest(roles=(role,), recorded_at=clock)
    planned = admit_reviewed_financial_statements(database, request)
    assert planned.mode == "dry_run"
    assert read_financial_table(database, "SYNTH", as_of=clock).cells == ()
    admit_reviewed_financial_statements(database, request.model_copy(update={"apply": True}))
    table = read_financial_table(database, "SYNTH", as_of=clock)
    assert len(table.cells) == 1
    cell = table.cells[0]
    assert cell.available, cell.reason_codes
    assert cell.source_scope_label == "combined_carve_out"
    assert cell.provenance is not None
    assert cell.provenance.cell.consolidation_scope == "other"
    assert cell.display_value == 1
    history = read_financial_history(database, "SYNTH", as_of=clock.date(), concepts=("revenue",))
    assert len(history.references) == 1
    assert history.references[0].source_scope_label == "combined_carve_out"
    assert history.references[0].observation_id == role.observation_id
    assert FactReadModel(database).provenance_bundle(role.observation_id, cutoff=STAMP) == original
    assert read_financial_table(database, "SYNTH", as_of=STAMP).cells == ()


def test_incremental_review_retains_prior_period_admission_and_history(
    database: sqlite3.Connection,
) -> None:
    first = _role(database, multiple_periods=True)
    clock = STAMP + timedelta(seconds=1)
    admit_reviewed_financial_statements(
        database, FinancialStatementAdmissionRequest(roles=(first,), recorded_at=clock, apply=True)
    )
    old_binding = MetricOntology(database).binding_as_known(first.observation_id, clock)
    assert old_binding is not None
    row = database.execute(
        "SELECT observation.observation_id FROM fact_observations_v2 observation JOIN fact_cells_v2 source USING(fact_cell_id) WHERE source.concept_name='Revenues' AND source.period_end LIKE '2024-12-31%'"
    ).fetchone()
    assert row is not None
    second = FactReadModel(database).provenance_bundle(str(row[0]), cutoff=clock)
    metric = str(
        database.execute(
            "SELECT metric_id FROM canonical_metric_cells WHERE canonical_metric_cell_id=?",
            (old_binding.canonical_metric_cell_id,),
        ).fetchone()[0]
    )
    definition = MetricOntology(database).metric_definition_as_known(metric, clock)
    assert definition is not None
    second_role = first.model_copy(
        update={
            "observation_id": second.observation.observation_id,
            "observation_payload_sha256": second.observation_payload_sha256,
            "expected_definition_revision_id": definition.metric_definition_revision_id,
            "context": first.context.model_copy(
                update={
                    "period_start": second.cell.period_start,
                    "period_end": second.cell.period_end,
                    "fiscal_year": 2024,
                }
            ),
        }
    )
    later = clock + timedelta(seconds=1)
    admit_reviewed_financial_statements(
        database,
        FinancialStatementAdmissionRequest(roles=(second_role,), recorded_at=later, apply=True),
    )
    table = read_financial_table(database, "SYNTH", as_of=later)
    assert len(table.cells) == 2 and all(item.available for item in table.cells), [
        item.reason_codes for item in table.cells
    ]
    assert (
        len(
            read_financial_history(
                database, "SYNTH", as_of=later.date(), concepts=("revenue",)
            ).references
        )
        == 2
    )
    rebound = MetricOntology(database).binding_as_known(first.observation_id, later)
    assert (
        rebound is not None
        and rebound.supersedes_binding_revision_id == old_binding.binding_revision_id
    )
    assert MetricOntology(database).binding_as_known(first.observation_id, clock) == old_binding
    assert (
        sum(item.available for item in read_financial_table(database, "SYNTH", as_of=clock).cells)
        == 1
    )


@pytest.mark.parametrize("change", ["hash", "issuer", "wording", "period", "unit", "head", "role"])
def test_review_rejects_wrong_source_coordinates_atomically(
    database: sqlite3.Connection, change: str
) -> None:
    role = _role(database)
    if change == "hash":
        role = role.model_copy(update={"observation_payload_sha256": "0" * 64})
    elif change == "head":
        role = role.model_copy(update={"expected_definition_revision_id": "wrong-head"})
    elif change == "unit":
        role = role.model_copy(update={"concept": "eps_diluted"})
    elif change == "role":
        role = role.model_copy(update={"concept": "operating_cash_flow"})
    else:
        edits = {
            "issuer": {"issuer_id": "unrelated-issuer"},
            "wording": {"source_wording": "Consolidated standalone statements"},
            "period": {"fiscal_year": 2024},
        }
        role = role.model_copy(update={"context": role.context.model_copy(update=edits[change])})
    before = database.execute(
        "SELECT COUNT(*) FROM canonical_metric_definition_revisions"
    ).fetchone()[0]
    with pytest.raises(ValueError):
        admit_reviewed_financial_statements(
            database,
            FinancialStatementAdmissionRequest(
                roles=(role,),
                recorded_at=STAMP + timedelta(seconds=1),
                apply=True,
            ),
        )
    assert (
        database.execute("SELECT COUNT(*) FROM canonical_metric_definition_revisions").fetchone()[0]
        == before
    )
    assert read_financial_table(database, "SYNTH", as_of=STAMP + timedelta(seconds=1)).cells == ()
