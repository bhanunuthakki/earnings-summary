"""Current-schema scoped producer regressions using real source owners."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path

import pytest

import provenance.fulltext_backfill as fulltext
from provenance.analysis_scope import AnalysisScopeRequest, build_analysis_scope
from provenance.evidence_ledger import ContentBlob, EvidenceLedger, SourceObservation
from provenance.filing_xbrl_extraction_ledger import FilingXbrlExtractionLedger
from provenance.population_document_processing import (
    DocumentProcessingPopulationRequest,
    populate_document_processing,
)
from provenance.reporting_entity_registry import ReportingEntityRegistry, SourceObligationRevision
from provenance.source_coverage import (
    CoverageAssessment,
    ExpectedDocument,
    SourceCoverageLedger,
    SourceInventorySnapshot,
)
from provenance.source_expectation_lifecycle import (
    ExpectedDocumentLifecycle,
    persist_expected_document_lifecycle,
)
from provenance.source_fact_stream import bind_resolution_snapshot_watermark
from provenance.source_inventory_seal import (
    InventoryComponent,
    InventorySeal,
    SourceInventorySealStore,
    component_digest,
)
from search.canonical_fact_projection import (
    ProjectionGenerationRequest,
    build_canonical_projection_generation,
)
from tests.test_filing_xbrl_extraction_ledger import filing_xbrl_ledger_database
from tests.test_heterogeneous_retrieval import NOW, _seed_resolved_periods, _two_period_output

PERIOD = datetime(2024, 12, 31, tzinfo=UTC)


def test_current_schema_scoped_processing_is_idempotent(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    blob = tmp_path / "data/evidence/blobs/filing.xhtml"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"filing-bytes")
    output = _two_period_output()
    conn = filing_xbrl_ledger_database(
        tmp_path,
        output,
        migrated_db,
        document_ticker="ACME",
        document_period_end=PERIOD,
        document_type="filing",
        blob_path=blob,
    )
    try:
        FilingXbrlExtractionLedger(conn).publish(output)
        _seed_resolved_periods(conn)
        bind_resolution_snapshot_watermark(
            conn, resolution_snapshot_id="resolution:checkpoint", cutoff_at=NOW, recorded_at=NOW
        )
        build_canonical_projection_generation(
            conn,
            ProjectionGenerationRequest(
                generation_id="projection:checkpoint",
                idempotency_key="projection:checkpoint",
                generation_kind="checkpoint",
                resolution_snapshot_id="resolution:checkpoint",
                ontology_snapshot_id="ontology:checkpoint",
                cutoff_at=NOW,
                recorded_at=NOW,
            ),
        )
        ledger = EvidenceLedger(conn)
        raw = b'{"filings": []}'
        inventory_blob = blob.parent / "inventory.json"
        inventory_blob.write_bytes(raw)
        sha = hashlib.sha256(raw).hexdigest()
        ledger.persist(
            ContentBlob(
                sha256=sha,
                byte_size=len(raw),
                media_type="application/json",
                storage_uri=inventory_blob.as_uri(),
                recorded_at=NOW,
            )
        )
        url = "https://data.sec.gov/submissions/CIK0000000001.json"
        ledger.persist(
            SourceObservation(
                observation_id="inventory-observation",
                idempotency_key="inventory-observation",
                source_kind="sec_submissions",
                source_url=url,
                blob_sha256=sha,
                source_published_at=None,
                filing_at=None,
                accepted_at=None,
                observed_at=NOW,
                retrieved_at=NOW,
                retrieval_config_sha256="a" * 64,
                collector_code_version="synthetic@1",
            )
        )
        coverage = SourceCoverageLedger(conn)
        coverage.persist(
            SourceInventorySnapshot(
                snapshot_id="inventory",
                idempotency_key="inventory",
                inventory_key="issuer-1:sec",
                revision=1,
                issuer_id="issuer-1",
                ticker="ACME",
                source_kind="sec_submissions",
                source_url=url,
                source_observation_id="inventory-observation",
                outcome="succeeded",
                authoritative=True,
                retrieval_config_sha256="a" * 64,
                collector_code_version="synthetic@1",
                started_at=NOW,
                completed_at=NOW,
                recorded_at=NOW,
            )
        )
        component = InventoryComponent(
            component_id="component",
            idempotency_key="component",
            snapshot_id="inventory",
            component_key="root",
            component_kind="primary",
            source_url=url,
            source_observation_id="inventory-observation",
            outcome="succeeded",
            required=True,
            ordinal=0,
            recorded_at=NOW,
        )
        seals = SourceInventorySealStore(conn)
        seals.persist(component)
        seals.persist(
            InventorySeal(
                snapshot_id="inventory",
                expected_component_count=1,
                component_digest_sha256=component_digest((component,)),
                completion_status="complete",
                sealed_at=NOW,
            )
        )
        ReportingEntityRegistry(conn).persist(
            SourceObligationRevision(
                obligation_revision_id="periodic:v1",
                idempotency_key="periodic:v1",
                obligation_key="periodic",
                revision=1,
                issuer_id="issuer-1",
                reporting_entity_id="reporting-1",
                authority_kind="sec_edgar",
                document_family="operating_company_periodic",
                obligation_state="required",
                completeness_rule="regulator_inventory",
                active_from=NOW,
                active_to=None,
                decision_kind="deterministic",
                reason_code="synthetic",
                reason_details=(("source", "fixture"),),
                effective_at=NOW,
                knowledge_at=NOW,
                recorded_at=NOW,
            )
        )
        expected_document = ExpectedDocument(
            expected_document_id="expected",
            idempotency_key="expected",
            snapshot_id="inventory",
            expected_document_key="expected",
            issuer_id="issuer-1",
            ticker="ACME",
            source_kind="sec_filing",
            document_type="filing",
            form_type="10-K",
            accession_number="0000000001-26-000001",
            source_url="https://www.sec.gov/Archives/example/filing.xhtml",
            primary_document="filing.xhtml",
            filing_at=NOW,
            period_start=PERIOD - timedelta(days=365),
            period_end=PERIOD,
            expectation_basis="authoritative",
            recorded_at=NOW,
        )
        coverage.persist(expected_document)
        coverage.persist(
            CoverageAssessment(
                assessment_id="captured",
                idempotency_key="captured",
                expected_document_id="expected",
                revision=1,
                coverage_status="captured",
                document_version_id="document-1",
                reason_code="synthetic_capture",
                reason_details=(("source", "fixture"),),
                decision_kind="deterministic",
                policy_name="fixture",
                policy_version="1",
                policy_config_sha256="a" * 64,
                material_dissent=False,
                effective_at=NOW,
                knowledge_at=NOW,
                recorded_at=NOW,
            )
        )
        persist_expected_document_lifecycle(
            conn,
            ExpectedDocumentLifecycle(
                lifecycle_id="lifecycle",
                idempotency_key="lifecycle",
                inventory_key="issuer-1:sec",
                expected_document_key="expected",
                source_inventory_snapshot_id="inventory",
                revision=1,
                status="expected",
                expected_document_id="expected",
                authority_observation_id="inventory-observation",
                reason_code="synthetic",
                reason_details=(("source", "fixture"),),
                effective_at=NOW,
                knowledge_at=NOW,
                recorded_at=NOW,
            ),
        )
        conn.commit()
        scope = build_analysis_scope(
            conn,
            AnalysisScopeRequest(
                purpose="post_earnings_readout",
                issuer_id="issuer-1",
                inventory_key="issuer-1:sec",
                required_period_ends=(date(2024, 12, 31),),
                cutoff_at=NOW,
                observed_through=NOW,
            ),
        )

        class FrozenType(type):
            def __instancecheck__(cls, instance: object) -> bool:
                return isinstance(instance, datetime)

        class FrozenDatetime(datetime, metaclass=FrozenType):
            @classmethod
            def now(cls, tz: tzinfo | None = None) -> datetime:
                return NOW if tz is None else NOW.astimezone(tz)

        monkeypatch.setattr(fulltext, "datetime", FrozenDatetime)
        result = fulltext.backfill_fulltext_evidence(
            conn,
            fulltext.FullTextBackfillRequest(
                repo_root=tmp_path,
                content_roots=(blob.parent,),
                source_lane="evidence_native",
                document_version_id="document-1",
                apply=True,
            ),
        )
        assert result.documents_extracted == 1
        processing = populate_document_processing(
            conn,
            DocumentProcessingPopulationRequest(
                cutoff_at=NOW,
                operation_recorded_at=NOW,
                apply=True,
                analysis_scope=scope,
                phase="all",
            ),
        )
        assert processing.processing_snapshot_count == 1
        binding_id = conn.execute(
            "SELECT binding_id FROM expected_document_obligation_bindings "
            "WHERE expected_document_id='expected'"
        ).fetchone()[0]
        assert (
            binding_id
            == "expected-obligation-binding:" + hashlib.sha256(b"expected\0periodic:v1").hexdigest()
        )
        replay = populate_document_processing(
            conn,
            DocumentProcessingPopulationRequest(
                cutoff_at=NOW,
                operation_recorded_at=NOW,
                apply=True,
                analysis_scope=scope,
                phase="all",
            ),
        )
        assert replay.binding_created_count == 0
        assert replay.processing_snapshot_count == 1
        assert replay.failed_obligation_count == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM expected_document_obligation_bindings").fetchone()[0]
            == 1
        )

        original_binding = tuple(
            conn.execute("SELECT * FROM expected_document_obligation_bindings").fetchone()
        )
        later = NOW + timedelta(days=1)
        later_scope = build_analysis_scope(
            conn, scope.request.model_copy(update={"cutoff_at": later, "observed_through": later})
        )
        later_processing = populate_document_processing(
            conn,
            DocumentProcessingPopulationRequest(
                cutoff_at=later,
                operation_recorded_at=later,
                apply=True,
                analysis_scope=later_scope,
                phase="all",
            ),
        )
        assert later_processing.binding_created_count == 0
        assert later_processing.binding_failure_count == 0
        assert later_processing.processing_snapshot_count == 1
        assert (
            tuple(conn.execute("SELECT * FROM expected_document_obligation_bindings").fetchone())
            == original_binding
        )
        with pytest.raises(ValueError, match="immutable expected_documents"):
            coverage.persist(expected_document.model_copy(update={"form_type": "10-Q"}))

    finally:
        conn.close()
