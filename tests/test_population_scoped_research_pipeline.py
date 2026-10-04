"""Current-schema scoped producer regressions using real source owners."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta, tzinfo
from decimal import Decimal
from pathlib import Path

import pytest

import provenance.fulltext_backfill as fulltext
from ask.sealed_retrieval import load_verified_trace_evidence
from provenance.analysis_scope import AnalysisScopeRequest, build_analysis_scope
from provenance.canonical_fact_resolution import CanonicalFactResolutionEngine, ResolutionPolicy
from provenance.evidence_ledger import ContentBlob, EvidenceLedger, SourceObservation
from provenance.filing_xbrl_extraction_ledger import FilingXbrlExtractionLedger
from provenance.filing_xbrl_fact_adapter import FilingXbrlNormalizedOutput, NormalizedFilingXbrlFact
from provenance.metric_ontology import (
    BindingRevision,
    CanonicalMetric,
    CanonicalMetricCell,
    CanonicalMetricDefinitionRevision,
    MappingRevision,
    MetricOntology,
    OntologySnapshot,
    SourceObservationTaxonomyAssertion,
    SourceTaxonomyComponent,
)
from provenance.population_document_processing import (
    DocumentProcessingPopulationRequest,
    populate_document_processing,
)
from provenance.population_research_snapshots import assemble_research_snapshot_request
from provenance.reporting_entity_registry import ReportingEntityRegistry, SourceObligationRevision
from provenance.research_snapshot import (
    ResearchSnapshotRequest,
    build_research_snapshot,
    verify_research_snapshot,
)
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
    CanonicalFactProjectionError,
    ProjectionGenerationRequest,
    build_canonical_projection_generation,
    canonical_json,
    load_canonical_fact_entry,
)
from search.corpus_builder import (
    CorpusBuildRequest,
    build_grounded_search_corpus,
    load_analysis_expected_document_inventory,
)
from search.heterogeneous_retrieval import (
    HeterogeneousRetrievalReceipt,
    HeterogeneousRetrievalRequest,
    NarrativeBundle,
    audit_research_snapshot_for_retrieval,
    retrieve_heterogeneous,
    verify_heterogeneous_retrieval_trace,
)
from tests.test_canonical_fact_resolution import NOW, SCOPE
from tests.test_filing_xbrl_extraction_ledger import (
    filing_xbrl_entry,
    filing_xbrl_ledger_database,
    filing_xbrl_output,
)

PERIOD = datetime(2024, 12, 31, tzinfo=UTC)


def two_annual_period_output() -> FilingXbrlNormalizedOutput:
    """Two explicit annual facts; these are never quarterly comparison data."""
    entries = tuple(
        NormalizedFilingXbrlFact.model_validate(
            {
                **filing_xbrl_entry(ordinal, numeric_value=value).model_dump(),
                "period_start": datetime(year, 1, 1, tzinfo=UTC),
                "period_end": datetime(year, 12, 31, tzinfo=UTC),
                "fiscal_year": year,
                "fiscal_period": "FY",
                "effective_at": datetime(year, 12, 31, tzinfo=UTC),
            }
        )
        for ordinal, year, value in ((0, 2023, Decimal("100")), (1, 2024, Decimal("120")))
    )
    return filing_xbrl_output(entries)


def _annual_component() -> SourceTaxonomyComponent:
    name = "Revenue"
    qualifier = {
        "accounting_basis": "us_gaap",
        "concept_name": name,
        "concept_namespace": "https://fasb.org/us-gaap/2026",
        "consolidation_scope": "consolidated",
        "period_kind": "duration",
        "reporting_entity_id": "reporting-1",
        "unit_family": "currency",
        "value_kind": "numeric",
        "schema_version": "source-definition-identity/v1",
        "taxonomy_name": "US GAAP",
        "taxonomy_version": "2026",
    }
    definition_qualifier_sha256 = hashlib.sha256(canonical_json(qualifier).encode()).hexdigest()
    return SourceTaxonomyComponent(
        component_id=f"component:{name}",
        idempotency_key=f"component:{name}",
        component_kind="concept",
        taxonomy_namespace="https://fasb.org/us-gaap/2026",
        local_name=name,
        taxonomy_name="US GAAP",
        taxonomy_version="2026",
        is_extension=False,
        data_type="monetaryItemType",
        period_type="duration",
        balance="credit",
        is_abstract=False,
        standard_label=name,
        definition_text=name,
        references=(),
        definition_qualifier_sha256=definition_qualifier_sha256,
        reporting_entity_id="reporting-1",
        evidence_locator={"source": "test"},
        effective_at=NOW,
        knowledge_at=NOW,
        recorded_at=NOW,
    )


def _annual_mapping(component: SourceTaxonomyComponent) -> MappingRevision:
    return MappingRevision(
        mapping_revision_id=f"mapping:{component.local_name}",
        idempotency_key=f"mapping:{component.local_name}",
        source_component_id=component.component_id,
        metric_id="revenue",
        revision=1,
        disposition="equivalent",
        policy_name="test",
        policy_version="v1",
        policy_config_sha256="a" * 64,
        method_name="review",
        method_version="v1",
        constraints={},
        evidence={"test": True},
        reviewer_identity="reviewer@example.test",
        effective_at=NOW,
        knowledge_at=NOW,
        recorded_at=NOW,
    )


def _persist_annual_taxonomy_assertion(
    conn: sqlite3.Connection,
    observation_id: str,
    fact_cell_id: str,
    *,
    idempotency_key: str,
) -> None:
    proof = conn.execute(
        "SELECT anchor.extraction_run_id,cell.taxonomy_name,"
        "anchor.source_taxonomy_version,cell_seal.semantic_key_sha256,"
        "anchor.anchor_payload_sha256,payload.observation_payload_sha256,"
        "run.output_sha256,anchor.raw_entry_sha256,"
        "completeness.observation_set_sha256 "
        "FROM fact_reported_observation_anchors_v2 anchor "
        "JOIN fact_cells_v2 cell ON cell.fact_cell_id=? "
        "JOIN fact_cell_identity_seals_v2 cell_seal "
        "ON cell_seal.fact_cell_id=cell.fact_cell_id "
        "JOIN fact_observation_payload_commitments_v2 payload "
        "ON payload.observation_id=anchor.observation_id "
        "JOIN evidence_extraction_runs run "
        "ON run.extraction_run_id=anchor.extraction_run_id "
        "JOIN fact_extraction_run_completeness_seals_v2 completeness "
        "ON completeness.extraction_run_id=anchor.extraction_run_id "
        "WHERE anchor.observation_id=?",
        (fact_cell_id, observation_id),
    ).fetchone()
    assert proof is not None
    MetricOntology(conn).persist_observation_taxonomy_assertion(
        SourceObservationTaxonomyAssertion(
            observation_id=observation_id,
            idempotency_key=idempotency_key,
            extraction_run_id=str(proof[0]),
            taxonomy_name=str(proof[1]),
            taxonomy_version=str(proof[2]),
            fact_cell_semantic_key_sha256=str(proof[3]),
            anchor_payload_sha256=str(proof[4]),
            observation_payload_sha256=str(proof[5]),
            extraction_output_sha256=str(proof[6]),
            raw_entry_sha256=str(proof[7]),
            observation_set_sha256=str(proof[8]),
            knowledge_at=NOW,
            recorded_at=NOW,
        )
    )


def seed_resolved_annual_periods(
    conn: sqlite3.Connection,
) -> tuple[dict[int, BindingRevision], tuple[str, ...]]:
    ontology = MetricOntology(conn)
    ontology.persist_metric(
        CanonicalMetric(
            metric_id="revenue",
            idempotency_key="metric:revenue",
            canonical_name="Revenue",
            effective_at=NOW,
            knowledge_at=NOW,
            recorded_at=NOW,
        )
    )
    ontology.persist_metric_definition(
        CanonicalMetricDefinitionRevision(
            metric_definition_revision_id="metric:revenue:v1",
            idempotency_key="metric:revenue:v1",
            metric_id="revenue",
            revision=1,
            lifecycle="active",
            definition_text="Revenue recognized from customer contracts.",
            aliases=("sales", "top line"),
            value_kind="numeric",
            period_kind="duration",
            unit_family="currency",
            accounting_basis="us_gaap",
            scope_constraints={},
            effective_at=NOW,
            knowledge_at=NOW,
            recorded_at=NOW,
        )
    )
    rows = conn.execute(
        "SELECT cell.fact_cell_id,cell.concept_name,cell.period_start,"
        "cell.period_end,observation.observation_id "
        "FROM fact_cells_v2 cell JOIN fact_observations_v2 observation "
        "ON observation.fact_cell_id=cell.fact_cell_id "
        "JOIN filing_xbrl_extraction_dispositions disposition "
        "ON disposition.observation_id=observation.observation_id "
        "WHERE disposition.disposition='published' ORDER BY cell.period_end"
    ).fetchall()
    assert len(rows) == 2
    component = _annual_component()
    mapping = _annual_mapping(component)
    ontology.persist_source_component(component)
    ontology.persist_mapping(mapping)
    bindings: dict[int, BindingRevision] = {}
    canonical_cells: list[str] = []
    for row in rows:
        year = datetime.fromisoformat(str(row[3])).year
        canonical_cell_id = f"canonical:revenue:{year}"
        _persist_annual_taxonomy_assertion(
            conn,
            str(row[4]),
            str(row[0]),
            idempotency_key=f"taxonomy:{year}",
        )
        ontology.persist_canonical_metric_cell(
            CanonicalMetricCell(
                canonical_metric_cell_id=canonical_cell_id,
                idempotency_key=canonical_cell_id,
                metric_id="revenue",
                reporting_entity_id="reporting-1",
                period_kind="duration",
                period_start=datetime.fromisoformat(str(row[2])),
                period_end=datetime.fromisoformat(str(row[3])),
                unit_family="currency",
                accounting_basis="us_gaap",
                consolidation_scope="consolidated",
                effective_at=NOW,
                knowledge_at=NOW,
                recorded_at=NOW,
            )
        )
        binding = BindingRevision(
            binding_revision_id=f"binding:revenue:{year}:v1",
            idempotency_key=f"binding:revenue:{year}:v1",
            fact_cell_id=str(row[0]),
            source_observation_id=str(row[4]),
            revision=1,
            canonical_metric_cell_id=canonical_cell_id,
            mapping_revision_id=mapping.mapping_revision_id,
            source_component_id=component.component_id,
            effective_at=NOW,
            knowledge_at=NOW,
            recorded_at=NOW,
        )
        ontology.persist_binding(binding)
        bindings[year] = binding
        canonical_cells.append(canonical_cell_id)
    resolver = CanonicalFactResolutionEngine(conn)
    for canonical_cell_id in canonical_cells:
        result = resolver.resolve(
            canonical_cell_id,
            NOW,
            ResolutionPolicy(name="deterministic", version="v1", config={}),
            recorded_at=NOW,
        )
        assert result.status == "resolved"
    ontology.seal_snapshot(
        OntologySnapshot(
            ontology_snapshot_id="ontology:checkpoint",
            idempotency_key="ontology:checkpoint",
            cutoff_at=NOW,
            recorded_at=NOW,
        )
    )
    resolver.seal_snapshot("resolution:checkpoint", NOW, NOW, SCOPE)
    return bindings, tuple(canonical_cells)


@pytest.fixture
def scoped_research_pipeline(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[sqlite3.Connection, ResearchSnapshotRequest]]:
    blob = tmp_path / "data/evidence/blobs/filing.xhtml"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"filing-bytes")
    output = two_annual_period_output()
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
        seed_resolved_annual_periods(conn)
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
        inventory, snapshots = load_analysis_expected_document_inventory(
            conn, scope, cutoff_at=NOW, observed_through=NOW
        )
        build_grounded_search_corpus(
            conn,
            CorpusBuildRequest(
                corpus_key=scope.scope_id,
                revision=1,
                selector_code_version="synthetic@1",
                recorded_at=NOW,
                knowledge_cutoff=NOW,
                expected_documents=inventory.expected_documents,
                source_inventory_snapshot_ids=snapshots,
                analysis_scope=scope,
                apply=True,
            ),
        )
        request = assemble_research_snapshot_request(
            conn, "issuer-1", NOW, analysis_scope=scope, projection_mode="lexical_only"
        )
        assert request.research_universe.analysis_scope == scope
        assert request.canonical_fact_resolution_snapshot_id == "resolution:checkpoint"
        admission = build_research_snapshot(conn, request)
        assert admission.admitted
        assert verify_research_snapshot(conn, request.research_snapshot_id) == admission
        assert build_research_snapshot(conn, request) == admission
        header = conn.execute(
            "SELECT request_json,request_sha256 FROM research_snapshot_headers "
            "WHERE research_snapshot_id=?",
            (request.research_snapshot_id,),
        ).fetchone()
        assert json.loads(header[0])["research_universe"]["analysis_scope"] == scope.model_dump(
            mode="json"
        )
        assert hashlib.sha256(header[0].encode()).hexdigest() == header[1]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE research_snapshot_headers SET request_json='{}' WHERE research_snapshot_id=?",
                (request.research_snapshot_id,),
            )
        assert verify_research_snapshot(conn, request.research_snapshot_id) == admission
        changed_scope = build_analysis_scope(
            conn, scope.request.model_copy(update={"purpose": "thesis_review"})
        )
        changed_request = request.model_copy(
            update={
                "research_universe": request.research_universe.model_copy(
                    update={"analysis_scope": changed_scope}
                )
            }
        )
        with pytest.raises(ValueError, match="scope"):
            build_research_snapshot(conn, changed_request)
        assert verify_research_snapshot(conn, request.research_snapshot_id) == admission
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

        conn.commit()
        yield conn, request
    finally:
        conn.close()


def test_current_schema_scoped_processing_and_autoassembly(
    scoped_research_pipeline: tuple[sqlite3.Connection, ResearchSnapshotRequest],
) -> None:
    conn, request = scoped_research_pipeline
    assert verify_research_snapshot(conn, request.research_snapshot_id).admitted


@pytest.fixture
def scoped_retrieval_trace(
    scoped_research_pipeline: tuple[sqlite3.Connection, ResearchSnapshotRequest],
) -> tuple[sqlite3.Connection, HeterogeneousRetrievalReceipt]:
    conn, request = scoped_research_pipeline
    audit_research_snapshot_for_retrieval(conn, request.research_snapshot_id, audited_at=NOW)
    bundle = request.corpus_bundles[0]
    assert bundle.lexical_index_run_id is not None
    receipt = retrieve_heterogeneous(
        conn,
        HeterogeneousRetrievalRequest(
            trace_id="trace:current",
            idempotency_key="trace:current",
            research_snapshot_id=request.research_snapshot_id,
            fact_generation_id=request.canonical_fact_projection_run_id,
            narrative_bundles=(
                NarrativeBundle(
                    corpus_manifest_id=bundle.corpus_manifest_id,
                    lexical_index_run_id=bundle.lexical_index_run_id,
                ),
            ),
            query_text="Revenue 2024",
            cutoff_at=NOW,
            recorded_at=NOW,
        ),
    )
    assert receipt.result_count > 0
    conn.commit()
    return conn, receipt


def test_current_schema_scoped_snapshot_yields_verified_trace(
    scoped_retrieval_trace: tuple[sqlite3.Connection, HeterogeneousRetrievalReceipt],
) -> None:
    conn, receipt = scoped_retrieval_trace
    assert verify_heterogeneous_retrieval_trace(conn, receipt.trace_id) == receipt
    items = load_verified_trace_evidence(conn, receipt.trace_id)
    assert len(items) == 1
    assert items[0].kind == "fact"
    assert items[0].period == "2024-12-31"
    assert items[0].value == "120 USD"


@pytest.mark.parametrize("mismatch", ("generation", "commitment", "cell_sql", "generation_sql"))
def test_current_schema_fact_reader_rejects_wrong_exact_binding(
    scoped_retrieval_trace: tuple[sqlite3.Connection, HeterogeneousRetrievalReceipt],
    mismatch: str,
) -> None:
    conn, receipt = scoped_retrieval_trace
    conn.row_factory = sqlite3.Row
    candidate = conn.execute(
        "SELECT candidate_id,source_commitment_sha256 FROM heterogeneous_retrieval_trace_candidates "
        "WHERE trace_id=? AND candidate_kind='fact'",
        (receipt.trace_id,),
    ).fetchone()
    assert candidate is not None
    generation_id = "projection:checkpoint"
    cell_id = str(candidate["candidate_id"])
    if mismatch == "generation":
        generation_id = "absent-generation"
    elif mismatch == "generation_sql":
        generation_id = "projection:checkpoint' OR 1=1 --"
    elif mismatch == "cell_sql":
        cell_id += "' OR 1=1 --"
    with pytest.raises(CanonicalFactProjectionError):
        load_canonical_fact_entry(
            conn,
            generation_id=generation_id,
            canonical_metric_cell_id=cell_id,
            entry_sha256="0" * 64
            if mismatch == "commitment"
            else str(candidate["source_commitment_sha256"]),
        )
    assert load_verified_trace_evidence(conn, receipt.trace_id)[0].value == "120 USD"


@pytest.fixture
def inherited_delta(
    scoped_research_pipeline: tuple[sqlite3.Connection, ResearchSnapshotRequest],
) -> tuple[sqlite3.Connection, ResearchSnapshotRequest]:
    conn, request = scoped_research_pipeline
    delta = build_canonical_projection_generation(
        conn,
        ProjectionGenerationRequest(
            generation_id="projection:unchanged-delta",
            idempotency_key="projection:unchanged-delta",
            generation_kind="delta",
            parent_generation_id=request.canonical_fact_projection_run_id,
            resolution_snapshot_id=request.canonical_fact_resolution_snapshot_id,
            ontology_snapshot_id=request.ontology_snapshot_id,
            cutoff_at=NOW,
            recorded_at=NOW,
        ),
    )
    assert delta.change_count == 0
    assert delta.effective_entry_count == 2
    delta_request = request.model_copy(
        update={
            "research_snapshot_id": "snapshot:unchanged-delta",
            "idempotency_key": "snapshot:unchanged-delta",
            "canonical_fact_projection_run_id": delta.generation_id,
        }
    )
    assert build_research_snapshot(conn, delta_request).admitted
    return conn, delta_request


def test_current_schema_delta_trace_reads_inherited_fact(
    inherited_delta: tuple[sqlite3.Connection, ResearchSnapshotRequest],
) -> None:
    conn, request = inherited_delta
    audit_research_snapshot_for_retrieval(conn, request.research_snapshot_id, audited_at=NOW)
    bundle = request.corpus_bundles[0]
    assert bundle.lexical_index_run_id is not None
    receipt = retrieve_heterogeneous(
        conn,
        HeterogeneousRetrievalRequest(
            trace_id="trace:delta",
            idempotency_key="trace:delta",
            research_snapshot_id=request.research_snapshot_id,
            fact_generation_id=request.canonical_fact_projection_run_id,
            narrative_bundles=(
                NarrativeBundle(
                    corpus_manifest_id=bundle.corpus_manifest_id,
                    lexical_index_run_id=bundle.lexical_index_run_id,
                ),
            ),
            query_text="Revenue 2024",
            cutoff_at=NOW,
            recorded_at=NOW,
        ),
    )
    (item,) = load_verified_trace_evidence(conn, receipt.trace_id)
    entry = load_canonical_fact_entry(
        conn,
        generation_id=request.canonical_fact_projection_run_id,
        canonical_metric_cell_id=item.candidate_id,
        entry_sha256=item.source_commitment_sha256,
    )
    assert entry["generation_id"] == "projection:checkpoint"
    assert item.value == "120 USD"
    with pytest.raises(CanonicalFactProjectionError, match="projection_parent_scope_mismatch"):
        build_canonical_projection_generation(
            conn,
            ProjectionGenerationRequest(
                generation_id="projection:missing-parent",
                idempotency_key="projection:missing-parent",
                generation_kind="delta",
                parent_generation_id="projection:absent",
                resolution_snapshot_id=request.canonical_fact_resolution_snapshot_id,
                ontology_snapshot_id=request.ontology_snapshot_id,
                cutoff_at=NOW,
                recorded_at=NOW,
            ),
        )
    with pytest.raises(CanonicalFactProjectionError, match="missing_or_changed"):
        load_canonical_fact_entry(
            conn,
            generation_id=request.canonical_fact_projection_run_id,
            canonical_metric_cell_id=item.candidate_id,
            entry_sha256="0" * 64,
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "DELETE FROM canonical_fact_projection_entries WHERE generation_id='projection:checkpoint'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE canonical_fact_projection_entries SET entry_sha256=? WHERE generation_id='projection:checkpoint'",
            ("0" * 64,),
        )
    assert load_verified_trace_evidence(conn, receipt.trace_id) == (item,)


def test_current_schema_delta_tombstone_does_not_recover_ancestor_fact(
    inherited_delta: tuple[sqlite3.Connection, ResearchSnapshotRequest],
) -> None:
    conn, request = inherited_delta
    source = conn.execute(
        "SELECT selected_observation_id,entry_sha256 FROM canonical_fact_projection_entries "
        "WHERE generation_id='projection:checkpoint' AND canonical_metric_cell_id='canonical:revenue:2023'"
    ).fetchone()
    ontology = MetricOntology(conn)
    old = ontology.binding_as_known(str(source[0]), NOW)
    assert old is not None
    later = NOW + timedelta(days=2)
    ontology.persist_binding(
        old.model_copy(
            update={
                "binding_revision_id": "binding:revenue:2023:retired",
                "idempotency_key": "binding:revenue:2023:retired",
                "revision": 2,
                "supersedes_binding_revision_id": old.binding_revision_id,
                "binding_status": "retired",
                "reason_code": "synthetic_retirement",
                "reason_details": {"test": True},
                "effective_at": later,
                "knowledge_at": later,
                "recorded_at": later,
            }
        )
    )
    resolver = CanonicalFactResolutionEngine(conn)
    for year in (2023, 2024):
        resolver.resolve(
            f"canonical:revenue:{year}",
            later,
            ResolutionPolicy(name="deterministic", version="v1", config={}),
            recorded_at=later,
        )
    ontology.seal_snapshot(
        OntologySnapshot(
            ontology_snapshot_id="ontology:retired",
            idempotency_key="ontology:retired",
            cutoff_at=later,
            recorded_at=later,
        )
    )
    resolver.seal_snapshot("resolution:retired", later, later, SCOPE)
    bind_resolution_snapshot_watermark(
        conn, resolution_snapshot_id="resolution:retired", cutoff_at=later, recorded_at=later
    )
    generation = build_canonical_projection_generation(
        conn,
        ProjectionGenerationRequest(
            generation_id="projection:retired",
            idempotency_key="projection:retired",
            generation_kind="delta",
            parent_generation_id=request.canonical_fact_projection_run_id,
            resolution_snapshot_id="resolution:retired",
            ontology_snapshot_id="ontology:retired",
            cutoff_at=later,
            recorded_at=later,
        ),
    )
    assert generation.tombstone_count == 1
    with pytest.raises(CanonicalFactProjectionError, match="missing_or_changed"):
        load_canonical_fact_entry(
            conn,
            generation_id=generation.generation_id,
            canonical_metric_cell_id="canonical:revenue:2023",
            entry_sha256=str(source[1]),
        )
