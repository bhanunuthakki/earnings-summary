"""Real snapshot boundaries for reported inputs; no model qualification."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dcf.input_evidence import InputEvidenceError
from dcf.meli_input_preview import preview_meli_inputs
from provenance.analysis_scope import AnalysisScopeRequest, build_analysis_scope
from provenance.meli_role_admission import (
    ReviewedRoleAdmission,
    apply_reviewed_meli_role_admission,
    plan_meli_role_admission,
)
from provenance.metric_ontology import MetricOntology, OntologySnapshot
from provenance.population_canonical_resolution import (
    CanonicalResolutionPopulationRequest,
    populate_canonical_resolution,
)
from provenance.population_document_processing import (
    DocumentProcessingPopulationRequest,
    populate_document_processing,
)
from provenance.population_research_snapshots import assemble_research_snapshot_request
from provenance.research_snapshot import build_research_snapshot, verify_research_snapshot
from search.corpus_builder import (
    CorpusBuildRequest,
    build_grounded_search_corpus,
    load_analysis_expected_document_inventory,
)
from tests.test_meli_role_admission import PERIOD, synthetic_current_population


def _new_snapshot(conn: sqlite3.Connection, cutoff: datetime) -> str:
    """Seal the real downstream owners after the definition review."""
    MetricOntology(conn).seal_snapshot(
        OntologySnapshot(
            ontology_snapshot_id="ontology:new-role-boundary",
            idempotency_key="ontology:new-role-boundary",
            cutoff_at=cutoff,
            recorded_at=cutoff,
        )
    )
    assert (
        populate_canonical_resolution(
            conn,
            CanonicalResolutionPopulationRequest(
                cutoff_at=cutoff, operation_recorded_at=cutoff, apply=True
            ),
        ).state
        == "complete"
    )
    scope = build_analysis_scope(
        conn,
        AnalysisScopeRequest(
            purpose="post_earnings_readout",
            issuer_id="issuer-1",
            inventory_key="issuer-1:sec",
            required_period_ends=(PERIOD,),
            cutoff_at=cutoff,
            observed_through=cutoff,
        ),
    )
    assert (
        populate_document_processing(
            conn,
            DocumentProcessingPopulationRequest(
                cutoff_at=cutoff, operation_recorded_at=cutoff, apply=True, analysis_scope=scope
            ),
        ).processing_snapshot_count
        == 1
    )
    inventory, inventory_ids = load_analysis_expected_document_inventory(
        conn, scope, cutoff_at=cutoff, observed_through=cutoff
    )
    conn.commit()
    build_grounded_search_corpus(
        conn,
        CorpusBuildRequest(
            corpus_key=scope.scope_id,
            revision=1,
            selector_code_version="synthetic-role-boundary@1",
            recorded_at=cutoff,
            knowledge_cutoff=cutoff,
            expected_documents=inventory.expected_documents,
            source_inventory_snapshot_ids=inventory_ids,
            analysis_scope=scope,
            apply=True,
        ),
    )
    request = assemble_research_snapshot_request(
        conn, "issuer-1", cutoff, analysis_scope=scope, projection_mode="lexical_only"
    )
    admission = build_research_snapshot(conn, request)
    assert admission.admitted
    assert verify_research_snapshot(conn, request.research_snapshot_id) == admission
    return request.research_snapshot_id


def test_later_role_definition_requires_a_new_ontology_snapshot(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, request = synthetic_current_population(tmp_path, migrated_db)
    try:
        conn.execute("BEGIN")
        plan = plan_meli_role_admission(conn, request)
        assert plan.state == "planned", plan.blockers
        review = ReviewedRoleAdmission(
            plan=plan,
            plan_sha256=plan.commitment_sha256,
            reviewer="synthetic-boundary-reviewer",
            reviewed_at=datetime.now(UTC),
            decision="approved",
        )
        receipt = apply_reviewed_meli_role_admission(conn, review, as_of=datetime.now(UTC))
        assert receipt.inserted_revisions == 14 and receipt.model_ready is False
        conn.commit()
        # A later read clock must not admit new definitions into an older seal.
        later = datetime.now(UTC)
        changes = conn.total_changes
        with pytest.raises(InputEvidenceError, match="input_definition_outside_ontology_snapshot:"):
            preview_meli_inputs(
                conn,
                research_snapshot_id=request.research_snapshot_id,
                financial_period_end=PERIOD,
                as_of=later,
            )
        assert conn.total_changes == changes
        verify_research_snapshot(conn, request.research_snapshot_id)
        new_snapshot_id = _new_snapshot(conn, datetime.now(UTC))
        preview = preview_meli_inputs(
            conn,
            research_snapshot_id=new_snapshot_id,
            financial_period_end=PERIOD,
            as_of=datetime.now(UTC),
        )
        assert preview.state == "reported_inputs_verified_not_model_ready"
        assert preview.model_ready is False and len(preview.facts) == 28
    finally:
        conn.close()
