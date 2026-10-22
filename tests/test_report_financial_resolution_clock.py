"""Later physical resolution materialization does not widen admitted fact time."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from provenance.fact_plane_v2 import CanonicalJSONObject, ExtractionRunCompletenessSealV2
from provenance.population_canonical_resolution import (
    CanonicalResolutionPopulationRequest,
    populate_canonical_resolution,
)
from provenance.population_metric_ontology import (
    MetricOntologyPopulationRequest,
    populate_metric_ontology,
)
from provenance.research_snapshot import ResearchSnapshotRequest, ResearchUniverse
from provenance.source_fact_repository import (
    ReportedSourceFact,
    SourceFactPublication,
    SourceFactRepository,
)
from research.decision_brief import MemoEvidenceError, verify_memo_snapshot_reference
from research.nu_earnings_evidence import NuEarningsEvidenceError, verify_nu_definition_successor
from sources.report_financials import (
    FinancialEvidenceReference,
    FinancialTableCell,
    read_financial_evidence,
    read_financial_table,
)
from tests import test_source_fact_repository as foundation
from tests.test_report_canonical_financials import STAMP, seed_table
from tests.test_report_canonical_financials import database as database


def _native_materialization(
    conn: sqlite3.Connection,
) -> tuple[ReportedSourceFact, FinancialEvidenceReference, ResearchSnapshotRequest]:
    original = seed_table(
        conn, [("revenue", "2025-01-01", "2025-03-31", "Q1", "100000000", "USD")], populate=False
    )[0]
    ontology = populate_metric_ontology(
        conn,
        MetricOntologyPopulationRequest(
            knowledge_cutoff=STAMP, operation_recorded_at=STAMP, apply=True
        ),
    )
    observed = STAMP + timedelta(minutes=2)
    populate_canonical_resolution(
        conn,
        CanonicalResolutionPopulationRequest(
            cutoff_at=STAMP, operation_recorded_at=observed, apply=True
        ),
    )
    conn.commit()
    cell = read_financial_table(
        conn, "SYNTH", as_of=STAMP, resolution_observed_through=observed
    ).cells[0]
    assert cell.available and cell.provenance is not None
    assert cell.canonical_resolution_revision_id is not None
    assert cell.metric_definition_revision_id is not None
    ref = FinancialEvidenceReference(
        ticker="SYNTH",
        concept="revenue",
        canonical_metric_cell_id=cell.canonical_metric_cell_id,
        observation_id=cell.provenance.observation.observation_id,
        canonical_resolution_revision_id=cell.canonical_resolution_revision_id,
        metric_definition_revision_id=cell.metric_definition_revision_id,
        as_of=STAMP,
        resolution_observed_through=observed,
    )
    resolution = conn.execute(
        "SELECT resolution_snapshot_id FROM canonical_fact_resolution_snapshot_members WHERE canonical_resolution_revision_id=?",
        (cell.canonical_resolution_revision_id,),
    ).fetchone()[0]
    # The reference gate consumes real native ontology/resolution members here.
    # This small request is not a claim of full ResearchSnapshot qualification.
    snapshot = ResearchSnapshotRequest.model_construct(
        cutoff_at=STAMP,
        recorded_at=observed,
        canonical_fact_resolution_snapshot_id=resolution,
        ontology_snapshot_id=ontology.snapshot_id,
        research_universe=ResearchUniverse.model_construct(
            document_version_ids=("report-document",)
        ),
    )
    return original, ref, snapshot


def test_native_resolution_after_k_requires_exact_physical_o(database: sqlite3.Connection):
    _, ref, snapshot = _native_materialization(database)
    assert not read_financial_table(database, "SYNTH", as_of=STAMP).cells[0].available
    assert read_financial_evidence(database, ref) is not None
    assert (
        read_financial_evidence(
            database, ref.model_copy(update={"resolution_observed_through": None})
        )
        is None
    )
    verify_memo_snapshot_reference(database, ref, snapshot, "SYNTH", snapshot.recorded_at)
    with pytest.raises(MemoEvidenceError, match="observation_clock_mismatch"):
        verify_memo_snapshot_reference(
            database,
            ref.model_copy(
                update={"resolution_observed_through": snapshot.recorded_at + timedelta(seconds=1)}
            ),
            snapshot,
            "SYNTH",
            snapshot.recorded_at + timedelta(seconds=2),
        )
    with pytest.raises(MemoEvidenceError, match="outside_exact_snapshot"):
        verify_memo_snapshot_reference(
            database,
            ref.model_copy(update={"canonical_resolution_revision_id": "other-resolution"}),
            snapshot,
            "SYNTH",
            snapshot.recorded_at,
        )


def test_later_actual_restatement_stays_outside_k_with_later_resolution_o(
    database: sqlite3.Connection,
):
    original, ref, snapshot = _native_materialization(database)
    later = STAMP + timedelta(minutes=1)
    locator = CanonicalJSONObject.model_validate({"path": "/restated/revenue"})
    locator_json = locator.model_dump_json()

    def sha(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    database.execute(
        "INSERT INTO evidence_extraction_runs SELECT 'late-run','late-run','report-document',input_sha256,extractor_name,extractor_config_sha256,'late-v2',output_sha256,?,? ,outcome FROM evidence_extraction_runs WHERE extraction_run_id='run-1'",
        (later, later),
    )
    database.execute(
        "INSERT INTO evidence_nodes VALUES ('late-node','late-node',1,'late-run',NULL,NULL,'table_cell','110000000',?,?,?)",
        (locator_json, sha(locator_json), later),
    )
    database.commit()
    observation = foundation.make_report(
        original.cell, "late", numeric_value="110000000", at=later
    ).model_copy(
        update={
            "revision_kind": "restatement",
            "supersedes_observation_id": original.observation.observation_id,
            "document_version_id": "report-document",
            "evidence_node_id": "late-node",
            "source_locator": locator,
            "source_locator_sha256": sha(locator_json),
        }
    )
    SourceFactRepository(database).publish(
        SourceFactPublication(
            publication_id="late-publication",
            idempotency_key="late-publication",
            reported_facts=(ReportedSourceFact(cell=original.cell, observation=observation),),
            extraction_seals=(
                ExtractionRunCompletenessSealV2(
                    extraction_seal_id="late-seal",
                    idempotency_key="late-seal",
                    extraction_run_id="late-run",
                    expected_node_count=1,
                    completeness_policy_name="all-run-nodes",
                    completeness_policy_version="v1",
                    completeness_policy_sha256=sha("report-completeness"),
                    knowledge_at=later,
                    recorded_at=later,
                ),
            ),
        )
    )
    populate_metric_ontology(
        database,
        MetricOntologyPopulationRequest(
            knowledge_cutoff=later, operation_recorded_at=snapshot.recorded_at, apply=True
        ),
    )
    populate_canonical_resolution(
        database,
        CanonicalResolutionPopulationRequest(
            cutoff_at=later,
            operation_recorded_at=snapshot.recorded_at + timedelta(seconds=1),
            apply=True,
        ),
    )
    database.commit()
    before = read_financial_evidence(database, ref)
    assert isinstance(before, FinancialTableCell) and before.provenance is not None
    assert before.provenance.observation.observation_id == original.observation.observation_id
    assert before.display_value == 100
    after = read_financial_table(
        database,
        "SYNTH",
        as_of=later,
        resolution_observed_through=snapshot.recorded_at + timedelta(seconds=1),
    ).cells[0]
    assert after.available and after.display_value == 110
    assert after.provenance is not None
    assert after.canonical_resolution_revision_id is not None
    assert after.metric_definition_revision_id is not None
    current_ref = ref.model_copy(
        update={
            "as_of": later,
            "resolution_observed_through": snapshot.recorded_at + timedelta(seconds=1),
            "observation_id": after.provenance.observation.observation_id,
            "canonical_resolution_revision_id": after.canonical_resolution_revision_id,
            "metric_definition_revision_id": after.metric_definition_revision_id,
        }
    )
    with pytest.raises(NuEarningsEvidenceError, match="financial_identity_changed"):
        verify_nu_definition_successor(database, ref, current_ref)
    verify_memo_snapshot_reference(database, ref, snapshot, "SYNTH", snapshot.recorded_at)


def test_optional_resolution_clock_preserves_legacy_json_and_rejects_invalid_clocks():
    legacy = {
        "ticker": "SYNTH",
        "concept": "revenue",
        "canonical_metric_cell_id": "cell",
        "observation_id": "obs",
        "canonical_resolution_revision_id": "resolution",
        "metric_definition_revision_id": "definition",
        "as_of": STAMP.isoformat(),
    }
    old = FinancialEvidenceReference.model_validate_json(json.dumps(legacy))
    assert "resolution_observed_through" not in old.model_dump()
    assert (
        old.model_dump_json()
        == old.model_copy(update={"resolution_observed_through": None}).model_dump_json()
    )
    for observed in [
        STAMP - timedelta(seconds=1),
        datetime.now(UTC) + timedelta(minutes=1),
        datetime(2026, 1, 1),
    ]:
        with pytest.raises(ValidationError):
            FinancialEvidenceReference.model_validate_json(
                json.dumps(legacy | {"resolution_observed_through": observed.isoformat()})
            )


def test_native_pre_post_resolution_refresh_retains_exact_original_observation(
    database: sqlite3.Connection,
):
    _, previous, pre = _native_materialization(database)
    knowledge = STAMP + timedelta(minutes=3)
    observed = STAMP + timedelta(minutes=4)
    ontology = populate_metric_ontology(
        database,
        MetricOntologyPopulationRequest(
            knowledge_cutoff=knowledge, operation_recorded_at=observed, apply=True
        ),
    )
    populate_canonical_resolution(
        database,
        CanonicalResolutionPopulationRequest(
            cutoff_at=knowledge, operation_recorded_at=observed, apply=True
        ),
    )
    database.commit()
    cell = read_financial_table(
        database, "SYNTH", as_of=knowledge, resolution_observed_through=observed
    ).cells[0]
    assert cell.available and cell.provenance is not None
    assert cell.canonical_resolution_revision_id is not None
    assert cell.canonical_resolution_revision_id != previous.canonical_resolution_revision_id
    current = previous.model_copy(
        update={
            "as_of": knowledge,
            "resolution_observed_through": observed,
            "canonical_resolution_revision_id": cell.canonical_resolution_revision_id,
        }
    )
    resolution = database.execute(
        "SELECT resolution_snapshot_id FROM canonical_fact_resolution_snapshot_members WHERE canonical_resolution_revision_id=?",
        (cell.canonical_resolution_revision_id,),
    ).fetchone()[0]
    post = pre.model_copy(
        update={
            "cutoff_at": knowledge,
            "recorded_at": observed,
            "ontology_snapshot_id": ontology.snapshot_id,
            "canonical_fact_resolution_snapshot_id": resolution,
        }
    )
    verify_memo_snapshot_reference(database, previous, pre, "SYNTH", observed)
    verify_memo_snapshot_reference(database, current, post, "SYNTH", observed)
    verify_nu_definition_successor(database, previous, current)
    with pytest.raises(NuEarningsEvidenceError, match="resolution_refresh_unverified"):
        verify_nu_definition_successor(
            database,
            previous,
            current.model_copy(update={"canonical_resolution_revision_id": "unsealed-revision"}),
        )
