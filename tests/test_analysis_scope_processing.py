"""Declared analysis evidence cannot waive required processing or replace another purpose."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from provenance.analysis_scope import AnalysisEvidenceScope, build_analysis_scope
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    SourceObservation,
)
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
from provenance.source_coverage import (
    CoverageAssessment,
    SourceCoverageLedger,
    SourceInventorySnapshot,
)
from provenance.source_expectation_lifecycle import (
    ExpectedDocumentLifecycle,
    persist_expected_document_lifecycle,
)
from tests.test_analysis_scope import CONFIG_SHA, STAMP, K, capture_document, scope_db


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


@pytest.mark.parametrize("successor", ["missing", "captured", "lifecycle"])
@pytest.mark.parametrize("clock", ["knowledge", "recorded"])
def test_scoped_processing_retains_prior_same_second_selection(
    tmp_path: Path, migrated_db: Callable[..., Path], successor: str, clock: str
) -> None:
    conn, original_scope = processing_db(tmp_path, migrated_db)
    try:
        cutoff = K.replace(microsecond=100_000)
        later = K.replace(microsecond=900_000)
        observed = K.replace(microsecond=950_000) if clock == "knowledge" else cutoff
        scope = build_analysis_scope(
            conn,
            original_scope.request.model_copy(
                update={"cutoff_at": cutoff, "observed_through": observed}
            ),
        )
        request = DocumentProcessingPopulationRequest(
            cutoff_at=cutoff,
            operation_recorded_at=observed,
            phase="obligations",
            analysis_scope=scope,
        )
        prior = populate_document_processing(conn, request)
        expected = next(
            entry.expected_document for entry in scope.entries if entry.role == "research_document"
        )
        knowledge = later if clock == "knowledge" else K.replace(microsecond=50_000)
        if successor == "lifecycle":
            # Each lifecycle revision belongs to a distinct inventory capture.
            # An unsealed newer inventory invalidates a current-inventory scope.
            SourceCoverageLedger(conn).persist(
                SourceInventorySnapshot(
                    snapshot_id="later-inventory",
                    idempotency_key="later-inventory",
                    inventory_key=scope.inventory.inventory_key,
                    revision=2,
                    issuer_id=expected.issuer_id,
                    ticker="ACME",
                    source_kind="sec_submissions",
                    source_url="https://data.sec.gov/submissions/CIK0000000001.json",
                    source_observation_id="inventory-observation",
                    outcome="succeeded",
                    authoritative=True,
                    retrieval_config_sha256=CONFIG_SHA,
                    collector_code_version="test@1",
                    started_at=STAMP,
                    completed_at=STAMP,
                    recorded_at=later,
                    supersedes_snapshot_id=scope.inventory.snapshot_id,
                ),
            )
            persist_expected_document_lifecycle(
                conn,
                ExpectedDocumentLifecycle(
                    lifecycle_id="later-withdrawal",
                    idempotency_key="later-withdrawal",
                    inventory_key=scope.inventory.inventory_key,
                    expected_document_key=expected.expected_document_key,
                    source_inventory_snapshot_id="later-inventory",
                    revision=2,
                    status="withdrawn_by_authority",
                    authority_observation_id="inventory-observation",
                    reason_code="synthetic_withdrawal",
                    reason_details=(("source", "synthetic_fixture"),),
                    effective_at=STAMP,
                    knowledge_at=knowledge,
                    recorded_at=later,
                    supersedes_lifecycle_id="lifecycle:" + expected.expected_document_id,
                ),
            )
        else:
            document_id = None
            if successor == "captured":
                document_id = "later-document:" + expected.expected_document_id
                source_bytes = b"synthetic changed filing"
                blob_sha = hashlib.sha256(source_bytes).hexdigest()
                EvidenceLedger(conn).persist(
                    ContentBlob(
                        sha256=blob_sha,
                        byte_size=len(source_bytes),
                        media_type="text/html",
                        storage_uri="file:///synthetic/" + blob_sha,
                        recorded_at=later,
                    ),
                )
                observation_id = "later-observation:" + expected.expected_document_id
                EvidenceLedger(conn).persist(
                    SourceObservation(
                        observation_id=observation_id,
                        idempotency_key=observation_id,
                        source_kind="sec_filing",
                        source_url=expected.source_url or "",
                        source_published_at=None,
                        filing_at=expected.filing_at,
                        accepted_at=None,
                        blob_sha256=blob_sha,
                        observed_at=later,
                        retrieved_at=later,
                        retrieval_config_sha256=CONFIG_SHA,
                        collector_code_version="test@1",
                    ),
                )
                EvidenceLedger(conn).persist(
                    DocumentVersion(
                        document_version_id=document_id,
                        document_key=expected.expected_document_key,
                        version_sequence=2,
                        observation_id=observation_id,
                        blob_sha256=blob_sha,
                        issuer_id=expected.issuer_id,
                        ticker="ACME",
                        document_type=expected.document_type,
                        form_type=expected.form_type or "",
                        accession_number=expected.accession_number,
                        period_start=expected.period_start,
                        period_end=expected.period_end,
                        language="en",
                        recorded_at=later,
                    ),
                )
            SourceCoverageLedger(conn).persist(
                CoverageAssessment(
                    assessment_id="later-coverage",
                    idempotency_key="later-coverage",
                    expected_document_id=expected.expected_document_id,
                    revision=2,
                    coverage_status="captured" if successor == "captured" else "not_discovered",
                    document_version_id=document_id,
                    reason_code="synthetic_successor",
                    reason_details=(("source", "synthetic_fixture"),),
                    decision_kind="deterministic",
                    policy_name="test",
                    policy_version="1",
                    policy_config_sha256=CONFIG_SHA,
                    effective_at=STAMP,
                    knowledge_at=knowledge,
                    recorded_at=later,
                    supersedes_assessment_id="captured:" + expected.expected_document_id,
                    material_dissent=False,
                ),
            )
        conn.commit()
        changes = conn.total_changes
        if successor == "lifecycle":
            # This valid successor crosses the stronger inventory guard before
            # document selection. Keep that refusal in both dry-run and apply.
            with pytest.raises(ValueError, match="current complete authoritative"):
                populate_document_processing(conn, request)
            with pytest.raises(ValueError, match="current complete authoritative"):
                populate_document_processing(
                    conn,
                    request.model_copy(
                        update={
                            "apply": True,
                            "input_commitment_sha256": prior.input_commitment_sha256,
                            "plan_commitment_sha256": prior.plan_commitment_sha256,
                        }
                    ),
                )
            assert conn.total_changes == changes
            return
        current = populate_document_processing(conn, request)
        assert current.expected_document_count == prior.expected_document_count == 1
        assert current.missing_document_count == current.unresolved_document_count == 0
        assert current.excluded_document_count == 0
        assert current.selection_commitment_sha256 == prior.selection_commitment_sha256
        # Raw-history commitments still invalidate a prior apply after an append.
        assert current.input_commitment_sha256 != prior.input_commitment_sha256
        with pytest.raises(ValueError, match="input commitment changed"):
            populate_document_processing(
                conn,
                request.model_copy(
                    update={
                        "apply": True,
                        "input_commitment_sha256": prior.input_commitment_sha256,
                        "plan_commitment_sha256": prior.plan_commitment_sha256,
                    }
                ),
            )
        assert conn.total_changes == changes
    finally:
        conn.close()
