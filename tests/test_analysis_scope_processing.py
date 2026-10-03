"""Declared analysis evidence cannot waive required processing or replace another purpose."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from provenance.analysis_scope import AnalysisEvidenceScope, build_analysis_scope
from provenance.population_document_processing import (
    DocumentProcessingPopulationRequest,
    populate_document_processing,
)
from provenance.population_research_snapshots import (
    ResearchSnapshotPopulationRequest,
    assemble_research_snapshot_request,
    populate_research_snapshots,
)
from provenance.reporting_entity_registry import (
    EvidenceSubjectBindingRevision,
    ReportingEntity,
    ReportingEntityRegistry,
    SourceObligationRevision,
)
from provenance.research_snapshot import (
    DocumentProcessingPolicy,
    DocumentProcessingScope,
    ResearchUniverse,
    derive_obligations,
    seal_processing_snapshot,
)
from provenance.source_expectation_lifecycle import (
    ExpectedDocumentLifecycle,
    persist_expected_document_lifecycle,
)
from tests.test_analysis_scope import STAMP, K, capture_document, scope_db


def processing_db(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> tuple[sqlite3.Connection, AnalysisEvidenceScope]:
    conn, scope = scope_db(tmp_path, migrated_db)
    registry = ReportingEntityRegistry(conn)
    registry.persist(
        ReportingEntity(
            reporting_entity_id="reporting-acme",
            idempotency_key="reporting-acme",
            issuer_id="issuer-acme",
            reporting_entity_kind="legal_registrant",
            display_name="Synthetic company",
            created_at=STAMP,
        )
    )
    registry.persist(
        EvidenceSubjectBindingRevision(
            binding_revision_id="binding-acme",
            idempotency_key="binding-acme",
            recorded_issuer_id="issuer-acme",
            revision=1,
            issuer_id="issuer-acme",
            reporting_entity_id="reporting-acme",
            outcome="selected",
            decision_kind="deterministic",
            reason_code="synthetic_binding",
            reason_details=(("source", "synthetic_fixture"),),
            material_dissent=False,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )
    registry.persist(
        SourceObligationRevision(
            obligation_revision_id="sec-periodic:v2",
            idempotency_key="sec-periodic:v2",
            obligation_key="sec-periodic",
            revision=2,
            issuer_id="issuer-acme",
            reporting_entity_id="reporting-acme",
            authority_kind="sec_edgar",
            document_family="operating_company_periodic",
            obligation_state="required",
            completeness_rule="regulator_inventory",
            active_from=STAMP,
            active_to=None,
            decision_kind="deterministic",
            reason_code="synthetic_binding",
            reason_details=(("source", "synthetic_fixture"),),
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
            supersedes_obligation_revision_id="sec-periodic:v1",
        )
    )
    for entry in scope.entries:
        expected = entry.expected_document
        persist_expected_document_lifecycle(
            conn,
            ExpectedDocumentLifecycle(
                lifecycle_id="lifecycle:" + expected.expected_document_id,
                idempotency_key="lifecycle:" + expected.expected_document_id,
                inventory_key=scope.inventory.inventory_key,
                expected_document_key=expected.expected_document_key,
                source_inventory_snapshot_id=scope.inventory.snapshot_id,
                revision=1,
                status="expected",
                expected_document_id=expected.expected_document_id,
                authority_observation_id="inventory-observation",
                reason_code="synthetic_inventory",
                reason_details=(("source", "synthetic_fixture"),),
                effective_at=STAMP,
                knowledge_at=STAMP,
                recorded_at=STAMP,
            ),
        )
    conn.commit()
    return conn, scope


def test_analysis_processing_ignores_outside_capture_gaps(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, scope = processing_db(tmp_path, migrated_db)
    try:
        result = populate_document_processing(
            conn,
            DocumentProcessingPopulationRequest(
                cutoff_at=K,
                operation_recorded_at=K,
                phase="obligations",
                apply=True,
                analysis_scope=scope,
            ),
        )
        assert result.expected_document_count == 1
        assert result.missing_document_count == result.unresolved_document_count == 0
        assert result.processing_snapshot_count == 0
        assert conn.execute("SELECT COUNT(*) FROM expected_documents").fetchone()[0] == 3
        whole_archive = populate_document_processing(
            conn,
            DocumentProcessingPopulationRequest(
                cutoff_at=K,
                operation_recorded_at=K,
                phase="obligations",
            ),
        )
        assert whole_archive.missing_document_count == 1
        assert whole_archive.expected_document_count == 2
    finally:
        conn.close()


def test_analysis_processing_still_requires_xbrl(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, scope = processing_db(tmp_path, migrated_db)
    try:
        policy = DocumentProcessingPolicy(policy_name="test-analysis", policy_version="1")
        selected = DocumentProcessingScope(
            document_version_ids=("document:expected-10k",), analysis_scope=scope
        )
        obligations = derive_obligations(conn, selected, K, policy, recorded_at=K)
        assert any(
            item.processing_lane == "filing_xbrl" and item.applicability == "applicable"
            for item in obligations
        )
        with pytest.raises(ValueError, match="terminal seal"):
            seal_processing_snapshot(
                conn,
                processing_snapshot_id="analysis-processing:test",
                idempotency_key="analysis-processing:test",
                scope=selected,
                cutoff_at=K,
                policy=policy,
                recorded_at=K,
            )
        assert (
            conn.execute("SELECT COUNT(*) FROM document_processing_snapshot_headers").fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_analysis_cannot_expand_processing_with_issuer_selection(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, scope = processing_db(tmp_path, migrated_db)
    try:
        with pytest.raises(ValueError, match="exact document versions"):
            DocumentProcessingScope(issuer_ids=("issuer-acme",), analysis_scope=scope)
        with pytest.raises(ValueError, match="declared issuer"):
            ResearchSnapshotPopulationRequest(
                cutoff_at=K,
                operation_recorded_at=K,
                issuer_ids=("another-issuer",),
                analysis_scope=scope,
            )
        with pytest.raises(ValueError, match="issuer differs"):
            assemble_research_snapshot_request(conn, "another-issuer", K, analysis_scope=scope)
    finally:
        conn.close()


def test_legacy_scope_serialization_is_unchanged() -> None:
    assert DocumentProcessingScope(document_version_ids=("document",)).model_dump(mode="json") == {
        "issuer_ids": [],
        "document_version_ids": ["document"],
    }
    assert ResearchUniverse(
        issuer_id="issuer",
        reporting_entity_ids=("entity",),
        document_version_ids=("document",),
        source_obligation_revision_ids=("obligation",),
    ).model_dump(mode="json") == {
        "issuer_id": "issuer",
        "reporting_entity_ids": ["entity"],
        "document_version_ids": ["document"],
        "source_obligation_revision_ids": ["obligation"],
    }


def test_blocked_research_plans_keep_distinct_purpose_identities(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, scope = processing_db(tmp_path, migrated_db)
    try:
        other = build_analysis_scope(
            conn, scope.request.model_copy(update={"purpose": "earnings-summary"})
        )
        first = populate_research_snapshots(
            conn,
            ResearchSnapshotPopulationRequest(
                cutoff_at=K,
                operation_recorded_at=K,
                analysis_scope=scope,
            ),
        )
        second = populate_research_snapshots(
            conn,
            ResearchSnapshotPopulationRequest(
                cutoff_at=K,
                operation_recorded_at=K,
                analysis_scope=other,
            ),
        )
        assert first.blocked_issuer_count == second.blocked_issuer_count == 1
        assert first.statuses[0].blockers == second.statuses[0].blockers
        assert first.input_commitment_sha256 != second.input_commitment_sha256
        assert first.plan_commitment_sha256 != second.plan_commitment_sha256
    finally:
        conn.close()


def test_outside_capture_does_not_invalidate_selected_processing_plan(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, scope = processing_db(tmp_path, migrated_db)
    try:
        request = DocumentProcessingPopulationRequest(
            cutoff_at=K,
            operation_recorded_at=K,
            phase="obligations",
            analysis_scope=scope,
        )
        first = populate_document_processing(conn, request)
        outside = next(
            entry.expected_document for entry in scope.entries if entry.role == "outside_scope"
        )
        capture_document(conn, outside)
        second = populate_document_processing(conn, request)
        assert first.input_commitment_sha256 == second.input_commitment_sha256
        assert first.selection_commitment_sha256 == second.selection_commitment_sha256
        assert first.plan_commitment_sha256 == second.plan_commitment_sha256
    finally:
        conn.close()
