"""Public current-schema peer admission and whole memo assessment.

Only valuation readiness is unavailable here: this context representation test
expects those precise holds. No snapshot/admission/source verifier is replaced.
The model replay's positive readiness is covered in test_memo_model_roundtrip.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Generator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

from dcf.input_evidence import SourceReadContext
from provenance.canonical_fact_resolution import (
    CanonicalFactResolutionEngine,
    ResolutionSnapshotScope,
)
from provenance.document_processing_evidence import (
    publish_document_processing_evidence,
    verify_document_processing_evidence,
)
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    SourceObservation,
)
from provenance.fulltext_backfill import FullTextBackfillRequest, backfill_fulltext_evidence
from provenance.issuer_registry import IssuerEntity, IssuerRegistry, LegacyIssuerBindingRevision
from provenance.metric_ontology import MetricOntology, OntologySnapshot
from provenance.reporting_entity_registry import (
    EvidenceSubjectBindingRevision,
    ReportingEntity,
    ReportingEntityRegistry,
    SourceObligationRevision,
)
from provenance.research_snapshot import (
    CorpusProjectionBundle,
    DocumentProcessingDisposition,
    DocumentProcessingPolicy,
    DocumentProcessingScope,
    ProcessingEvidenceReference,
    ResearchSnapshotRequest,
    ResearchUniverse,
    build_research_snapshot,
    derive_obligations,
    record_disposition,
    seal_disposition,
    seal_processing_snapshot,
    verify_research_snapshot,
)
from provenance.source_coverage import (
    CoverageAssessment,
    SourceCoverageLedger,
    SourceInventorySnapshot,
)
from provenance.source_coverage import ExpectedDocument as CoverageDocument
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
from report.artifacts import (
    RenderedReportBody,
    ReportArtifactRef,
    ReportInteractionManifest,
    ReportSectionRef,
    persist_report_artifact,
)
from report.models import CellSource
from research.decision_brief import (
    DecisionBriefReadiness,
    MemoContextReviewV2,
    MemoScopedSupport,
    MemoSupportingContext,
    ReviewedMemoClaimV2,
    assess_decision_brief,
    memo_reader_blocks,
)
from search.canonical_fact_projection import (
    ProjectionGenerationRequest,
    build_canonical_projection_generation,
)
from search.corpus_builder import (
    CorpusBuildRequest,
    build_grounded_search_corpus,
    load_coverage_expected_document_inventory,
)
from sources.report_financials import FinancialEvidenceReference, read_financial_table
from tests.test_memo_model_roundtrip import seed_model_facts
from tests.test_onon_inputs import NOW
from tests.test_source_fact_repository import STAMP
from ui.source_chip import source_chip_html

PeerFixture = tuple[
    sqlite3.Connection,
    Path,
    ReportArtifactRef,
    MemoContextReviewV2,
    SourceReadContext,
    datetime,
    FinancialEvidenceReference,
]


def _identity(conn: sqlite3.Connection, issuer: str, entity: str, ticker: str) -> None:
    registry = IssuerRegistry(conn)
    if issuer != "issuer-1":
        registry.persist(
            IssuerEntity(
                issuer_id=issuer,
                idempotency_key=issuer,
                entity_kind="operating_company",
                created_at=STAMP,
            )
        )
        ReportingEntityRegistry(conn).persist(
            ReportingEntity(
                reporting_entity_id=entity,
                idempotency_key=entity,
                issuer_id=issuer,
                reporting_entity_kind="legal_registrant",
                display_name=ticker,
                created_at=STAMP,
            )
        )
    if issuer != "issuer-1":
        ReportingEntityRegistry(conn).persist(
            EvidenceSubjectBindingRevision(
                binding_revision_id=f"{issuer}-subject",
                idempotency_key=f"{issuer}-subject",
                recorded_issuer_id=issuer,
                revision=1,
                issuer_id=issuer,
                reporting_entity_id=entity,
                outcome="selected",
                decision_kind="deterministic",
                material_dissent=False,
                reason_code="synthetic_fixture",
                reason_details=(("fixture", "exact independent subject"),),
                effective_at=STAMP,
                knowledge_at=STAMP,
                recorded_at=STAMP,
            )
        )
    registry.persist(
        LegacyIssuerBindingRevision(
            binding_revision_id=f"ticker-{ticker}",
            idempotency_key=f"ticker-{ticker}",
            recorded_issuer_id=f"legacy-ticker:{ticker}",
            revision=1,
            issuer_id=issuer,
            outcome="selected",
            decision_kind="deterministic",
            material_dissent=False,
            reason_code="synthetic_fixture",
            reason_details=(("fixture", "independent issuer identity"),),
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )


def _seal_context(
    conn: sqlite3.Connection,
    root: Path,
    *,
    issuer: str,
    entity: str,
    ticker: str,
    document: str,
    observation: str,
    cutoff: datetime,
    publications: tuple[str, ...],
    clock_mode: Literal["cutoff_v1", "publication_created_v2"] = "publication_created_v2",
) -> tuple[str, str]:
    key = ticker.casefold()
    obligation = f"{key}-obligation"
    ReportingEntityRegistry(conn).persist(
        SourceObligationRevision(
            obligation_revision_id=obligation,
            idempotency_key=obligation,
            obligation_key=obligation,
            revision=1,
            issuer_id=issuer,
            reporting_entity_id=entity,
            authority_kind="issuer_publisher",
            document_family="issuer_presentations",
            obligation_state="required",
            completeness_rule="publisher_surface_exhaustion",
            active_from=STAMP,
            active_to=None,
            decision_kind="deterministic",
            reason_code="synthetic_fixture",
            reason_details=(("fixture", "complete finite synthetic publisher surface"),),
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=cutoff,
        )
    )
    inventory = f"{key}-inventory"
    url = str(
        conn.execute(
            "SELECT source_url FROM evidence_source_observations WHERE observation_id=?",
            (observation,),
        ).fetchone()[0]
    )
    ledger = SourceCoverageLedger(conn)
    ledger.persist(
        SourceInventorySnapshot(
            snapshot_id=inventory,
            idempotency_key=inventory,
            inventory_key=inventory,
            revision=1,
            issuer_id=issuer,
            ticker=ticker,
            source_kind="ir_crawl",
            source_url=url,
            source_observation_id=observation,
            outcome="succeeded",
            authoritative=True,
            retrieval_config_sha256="a" * 64,
            collector_code_version="synthetic-exact-one-document",
            started_at=STAMP,
            completed_at=STAMP,
            recorded_at=cutoff,
        )
    )
    ledger.persist(
        CoverageDocument(
            expected_document_id=f"{key}-expected",
            idempotency_key=f"{key}-expected",
            snapshot_id=inventory,
            expected_document_key=f"{key}-presentation",
            issuer_id=issuer,
            ticker=ticker,
            source_kind="ir_document",
            document_type="investor_presentation",
            form_type="presentation",
            source_url=url,
            expectation_basis="authoritative",
            recorded_at=cutoff,
            source_obligation_revision_id=obligation,
        )
    )
    persist_expected_document_lifecycle(
        conn,
        ExpectedDocumentLifecycle(
            lifecycle_id=f"{key}-lifecycle",
            idempotency_key=f"{key}-lifecycle",
            inventory_key=inventory,
            expected_document_key=f"{key}-presentation",
            source_inventory_snapshot_id=inventory,
            revision=1,
            status="expected",
            expected_document_id=f"{key}-expected",
            authority_observation_id=observation,
            reason_code="exact_synthetic_expected",
            reason_details=(("fixture", "finite observed publisher expectation"),),
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=cutoff,
        ),
    )
    ledger.persist(
        CoverageAssessment(
            assessment_id=f"{key}-captured",
            idempotency_key=f"{key}-captured",
            expected_document_id=f"{key}-expected",
            revision=1,
            coverage_status="captured",
            document_version_id=document,
            reason_code="exact_synthetic_capture",
            reason_details=(("fixture", "exact retained document"),),
            decision_kind="deterministic",
            policy_name="synthetic",
            policy_version="1",
            policy_config_sha256="a" * 64,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=cutoff,
            material_dissent=False,
        )
    )
    component = InventoryComponent(
        component_id=f"{key}-component",
        idempotency_key=f"{key}-component",
        snapshot_id=inventory,
        component_key="primary",
        component_kind="primary",
        source_url=url,
        source_observation_id=observation,
        outcome="succeeded",
        required=True,
        ordinal=0,
        recorded_at=cutoff,
    )
    store = SourceInventorySealStore(conn)
    store.persist(component)
    store.persist(
        InventorySeal(
            snapshot_id=inventory,
            expected_component_count=1,
            component_digest_sha256=component_digest((component,)),
            completion_status="complete",
            sealed_at=cutoff,
        )
    )
    conn.commit()
    extracted = backfill_fulltext_evidence(
        conn,
        FullTextBackfillRequest(
            repo_root=root,
            content_roots=(root / "sources",),
            document_version_id=document,
            source_lane="evidence_native",
            apply=True,
            task_id=f"extract-{key}",
        ),
    )
    assert extracted.documents_extracted == 1
    scope = DocumentProcessingScope(document_version_ids=(document,))
    policy = DocumentProcessingPolicy(policy_name="complete-synthetic", policy_version="1")
    for index, item in enumerate(derive_obligations(conn, scope, cutoff, policy)):
        evidence = ()
        if item.applicability == "applicable":
            assert item.processing_lane == "html_native_hierarchy"
            receipt = publish_document_processing_evidence(
                conn,
                document_version_id=document,
                processing_lane=item.processing_lane,
                cutoff_at=cutoff,
                recorded_at=cutoff,
            )
            verified = verify_document_processing_evidence(
                conn,
                receipt.evidence_seal_id,
                document_version_id=document,
                processing_lane=item.processing_lane,
                cutoff_at=cutoff,
                observed_through=cutoff,
            )
            evidence = (
                ProcessingEvidenceReference(
                    evidence_table="document_processing_evidence_seals",
                    evidence_id=receipt.evidence_seal_id,
                    evidence_commitment_sha256=receipt.member_set_sha256,
                    knowledge_at=verified.knowledge_at,
                    recorded_at=verified.recorded_at,
                ),
            )
        disposition = f"{key}-disposition-{index}"
        record_disposition(
            conn,
            DocumentProcessingDisposition(
                processing_disposition_id=disposition,
                idempotency_key=disposition,
                processing_obligation_revision_id=item.processing_obligation_revision_id,
                terminal_status="succeeded" if evidence else "not_applicable",
                reason_code="complete_synthetic_source",
                reason_details={"fixture": key},
                evidence=evidence,
                knowledge_at=cutoff,
                recorded_at=cutoff,
            ),
        )
        seal_disposition(conn, disposition, sealed_at=cutoff)
    processing = f"{key}-processing"
    seal_processing_snapshot(
        conn,
        processing_snapshot_id=processing,
        idempotency_key=processing,
        scope=scope,
        cutoff_at=cutoff,
        policy=policy,
        recorded_at=cutoff,
    )
    conn.commit()
    corpus = build_grounded_search_corpus(
        conn,
        CorpusBuildRequest(
            corpus_key=key,
            revision=1,
            selector_code_version="synthetic-current",
            recorded_at=cutoff,
            knowledge_cutoff=cutoff,
            expected_documents=load_coverage_expected_document_inventory(
                conn, (inventory,), knowledge_cutoff=cutoff, observed_through=cutoff
            )[0].expected_documents,
            source_inventory_snapshot_ids=(inventory,),
            required_extractor_names=("fulltext-evidence-backfill",),
            apply=True,
        ),
    )
    assert corpus.completion_status == "complete"
    ontology = f"{key}-ontology"
    MetricOntology(conn).seal_snapshot(
        OntologySnapshot(
            ontology_snapshot_id=ontology,
            idempotency_key=ontology,
            cutoff_at=cutoff,
            recorded_at=cutoff,
        )
    )
    resolution = f"{key}-resolution"
    CanonicalFactResolutionEngine(conn).seal_snapshot(
        resolution,
        cutoff,
        cutoff,
        ResolutionSnapshotScope(issuer_id=issuer, reporting_entity_ids=(entity,)),
    )
    bind_resolution_snapshot_watermark(
        conn,
        resolution_snapshot_id=resolution,
        cutoff_at=cutoff,
        observed_through=cutoff,
        recorded_at=cutoff,
    )
    projection = f"{key}-projection"
    conn.commit()
    build_canonical_projection_generation(
        conn,
        ProjectionGenerationRequest(
            generation_id=projection,
            idempotency_key=projection,
            generation_kind="checkpoint",
            resolution_snapshot_id=resolution,
            ontology_snapshot_id=ontology,
            cutoff_at=cutoff,
            recorded_at=cutoff,
        ),
    )
    snapshot = f"{key}-snapshot"
    conn.commit()
    build_research_snapshot(
        conn,
        ResearchSnapshotRequest(
            research_snapshot_id=snapshot,
            idempotency_key=snapshot,
            research_universe=ResearchUniverse(
                issuer_id=issuer,
                reporting_entity_ids=(entity,),
                document_version_ids=(document,),
                source_obligation_revision_ids=(obligation,),
            ),
            processing_snapshot_ids=(processing,),
            corpus_bundles=(
                CorpusProjectionBundle(
                    corpus_manifest_id=corpus.manifest_id,
                    lexical_index_run_id=corpus.lexical_index_run_id,
                ),
            ),
            source_fact_publication_ids=publications,
            source_publication_reference_clock=clock_mode,
            ontology_snapshot_id=ontology,
            canonical_fact_resolution_snapshot_id=resolution,
            canonical_fact_projection_run_id=projection,
            cutoff_at=cutoff,
            recorded_at=cutoff,
        ),
    )
    admission = verify_research_snapshot(conn, snapshot)
    return snapshot, admission.member_set_sha256


@pytest.fixture
def peer_memo(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> Generator[PeerFixture, None, None]:
    conn = sqlite3.connect(migrated_db(tmp_path / "peer.db"))
    seed_model_facts(conn, tmp_path / "sources" / "primary.json", presentation=True)
    _identity(conn, "issuer-1", "reporting-1", "ONON")
    _identity(conn, "issuer-peer", "entity-peer", "DECK")
    raw = b"<html><body><p>Management said premium customer demand improved.</p></body></html>"
    source = tmp_path / "sources" / "peer.html"
    source.write_bytes(raw)
    blob = hashlib.sha256(raw).hexdigest()
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=blob,
            byte_size=len(raw),
            media_type="text/html",
            storage_uri=source.as_uri(),
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="peer-source",
            idempotency_key="peer-source",
            source_kind="issuer",
            source_url="https://deck.test/presentation",
            blob_sha256=blob,
            source_published_at=STAMP,
            filing_at=None,
            accepted_at=None,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256="a" * 64,
            collector_code_version="synthetic-current",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id="peer-document",
            document_key="DECK:presentation",
            version_sequence=1,
            observation_id="peer-source",
            blob_sha256=blob,
            issuer_id="issuer-peer",
            ticker="DECK",
            document_type="investor_presentation",
            form_type="presentation",
            language="en",
            recorded_at=STAMP,
        )
    )
    # Extraction records are made by the owning backfill at the current clock.
    cutoff = NOW + timedelta(minutes=1)
    primary, _primary_sha = _seal_context(
        conn,
        tmp_path,
        issuer="issuer-1",
        entity="reporting-1",
        ticker="ONON",
        document="document-1",
        observation="source-1",
        cutoff=cutoff,
        publications=("publication-onon",),
    )
    peer, peer_sha = _seal_context(
        conn,
        tmp_path,
        issuer="issuer-peer",
        entity="entity-peer",
        ticker="DECK",
        document="peer-document",
        observation="peer-source",
        cutoff=cutoff,
        publications=(),
    )
    cell = read_financial_table(conn, "ONON", as_of=cutoff).cells[0]
    assert cell.provenance is not None
    assert cell.canonical_resolution_revision_id is not None
    assert cell.metric_definition_revision_id is not None
    reference = FinancialEvidenceReference(
        ticker="ONON",
        concept=cell.concept,
        canonical_metric_cell_id=cell.canonical_metric_cell_id,
        observation_id=cell.provenance.observation.observation_id,
        canonical_resolution_revision_id=cell.canonical_resolution_revision_id,
        metric_definition_revision_id=cell.metric_definition_revision_id,
        as_of=cutoff,
    )
    chip = source_chip_html(
        CellSource(source="sec_official", canonical_reference=reference), link_only=True
    )
    section_ids = ("company", "synthesis", "financials", "bear", "valuation", "sources")
    markup = (
        f'<main data-report-body="v1"><section id="company" data-tab="company"><p>Analyst inference: comparison.{chip}</p><p>Management said premium customer demand improved.</p></section>'
        + "".join(
            f'<section id="{name}" data-tab="{name}"><p>Analyst inference: explicit synthetic {name} context.</p></section>'
            for name in section_ids[1:]
        )
        + "</main>"
    )
    body = RenderedReportBody.from_html(
        ticker="ONON",
        report_date=date(2026, 10, 7),
        body_html=markup,
        sections=tuple(
            ReportSectionRef(section_id=name, label=name.title(), group_id=name)
            for name in section_ids
        ),
        interaction_manifest=ReportInteractionManifest(),
    )
    standalone = tmp_path / "standalone.html"
    standalone.write_text(markup)
    artifact = persist_report_artifact(
        repo_root=tmp_path,
        body=body,
        standalone_path=standalone,
        generated_at=cutoff,
        coverage_role="evaluation",
        title="Synthetic peer evidence",
    )
    node = conn.execute(
        "SELECT node_id FROM evidence_nodes WHERE extraction_run_id IN (SELECT extraction_run_id FROM evidence_extraction_runs WHERE document_version_id='peer-document') AND node_kind='passage'"
    ).fetchone()[0]
    assert artifact.body_path is not None
    blocks = memo_reader_blocks((tmp_path / artifact.body_path).read_text())
    assert artifact.body_sha256 is not None
    review = MemoContextReviewV2(
        artifact_id=artifact.artifact_id,
        body_sha256=artifact.body_sha256,
        research_snapshot_id=primary,
        supporting_contexts=(
            MemoSupportingContext(
                context_id="peer",
                issuer_id="issuer-peer",
                ticker="DECK",
                research_snapshot_id=peer,
                member_set_sha256=peer_sha,
            ),
        ),
        claims=tuple(
            ReviewedMemoClaimV2(
                block_id=block.block_id,
                passage=block.text,
                kind="management_claim" if index == 1 else "analyst_inference",
                rationale="Exact independently admitted peer source wording."
                if index == 1
                else "Explicit synthetic analyst inference.",
                supporting_evidence=(
                    MemoScopedSupport(context_id="peer", evidence_node_ids=(node,)),
                )
                if index == 1
                else (),
            )
            for index, block in enumerate(blocks)
        ),
        reviewed_section_ids=section_ids,
        reviewer="synthetic analyst",
        reviewed_at=cutoff,
        rationale="Whole body exact primary and peer evidence review.",
    )
    conn.commit()
    try:
        yield (
            conn,
            tmp_path,
            artifact,
            review,
            SourceReadContext(content_roots=(source.parent,)),
            cutoff,
            reference,
        )
    finally:
        conn.close()


def _assessment(
    fixture: PeerFixture, review: MemoContextReviewV2 | None = None, cutoff: datetime | None = None
) -> DecisionBriefReadiness:
    conn, root, artifact, original, context, clock, _reference = fixture
    return assess_decision_brief(
        conn,
        repo_root=root,
        artifact=artifact,
        context_review=review or original,
        source_context=context,
        as_of=cutoff or clock,
    )


def test_public_admitted_peer_whole_assessment(peer_memo: PeerFixture) -> None:
    receipt = _assessment(peer_memo)
    assert receipt.financial_reference_count == 1, receipt.reason_codes
    assert receipt.reconstructed_document_count == 2
    assert set(receipt.reason_codes) == {
        "valuation_dcf_missing",
        "memo_model_input_receipt_missing",
    }, receipt.reason_codes


@pytest.mark.parametrize(
    "mutation",
    [
        "issuer",
        "ticker",
        "member",
        "cutoff",
        "foreign-node",
        "financial-reference",
        "primary-split",
    ],
)
def test_changed_peer_context_or_support_refuses(peer_memo: PeerFixture, mutation: str) -> None:
    baseline = _assessment(peer_memo)
    assert baseline.reconstructed_document_count == 2, baseline.reason_codes
    _conn, _root, _artifact, review, _context, clock, reference = peer_memo
    peer = review.supporting_contexts[0]
    if mutation in {"issuer", "ticker", "member", "primary-split"}:
        update = {
            "issuer": "issuer_id",
            "ticker": "ticker",
            "member": "member_set_sha256",
            "primary-split": "issuer_id",
        }[mutation]
        value = {
            "issuer": "invented-issuer",
            "ticker": "ONON",
            "member": "f" * 64,
            "primary-split": "issuer-1",
        }[mutation]
        review = review.model_copy(
            update={"supporting_contexts": (peer.model_copy(update={update: value}),)}
        )
    elif mutation == "cutoff":
        clock -= timedelta(seconds=1)
    else:
        claim = review.claims[1]
        support = claim.supporting_evidence[0]
        support = (
            support.model_copy(update={"evidence_node_ids": ("onon-node-0",)})
            if mutation == "foreign-node"
            else support.model_copy(update={"financial_references": (reference,)})
        )
        review = review.model_copy(
            update={
                "claims": (
                    review.claims[0],
                    claim.model_copy(update={"supporting_evidence": (support,)}),
                    *review.claims[2:],
                )
            }
        )
    receipt = _assessment(peer_memo, review, clock)
    expected = {
        "issuer": "memo_supporting_context_identity_or_commitment_mismatch",
        "ticker": "memo_supporting_primary_issuer_forbidden",
        "member": "memo_supporting_context_identity_or_commitment_mismatch",
        "cutoff": "memo_research_snapshot_after_cutoff",
        "foreign-node": "memo_claim_outside_exact_processing_snapshot",
        "financial-reference": "memo_financial_evidence_outside_exact_snapshot",
        "primary-split": "memo_supporting_primary_issuer_forbidden",
    }[mutation]
    assert expected in receipt.reason_codes, receipt.reason_codes
    assert "memo_claim_population_incomplete" not in receipt.reason_codes


def test_assessment_rejects_two_alias_contexts_for_one_admitted_issuer(
    peer_memo: PeerFixture,
) -> None:
    _conn, _root, _artifact, review, _context, _clock, _reference = peer_memo
    peer = review.supporting_contexts[0]
    duplicate = peer.model_copy(update={"context_id": "second", "ticker": "DECK.A"})
    # model_copy represents an untrusted caller that skipped the JSON parser.
    changed = review.model_copy(update={"supporting_contexts": (peer, duplicate)})
    receipt = _assessment(peer_memo, changed)
    assert "memo_supporting_issuer_population_invalid" in receipt.reason_codes


def test_legacy_public_seal_replays_without_new_serialized_field(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "legacy.db")) as conn:
        seed_model_facts(conn, tmp_path / "sources" / "primary.json", presentation=True)
        _identity(conn, "issuer-1", "reporting-1", "ONON")
        snapshot, sha = _seal_context(
            conn,
            tmp_path,
            issuer="issuer-1",
            entity="reporting-1",
            ticker="ONON",
            document="document-1",
            observation="source-1",
            cutoff=NOW,
            publications=("publication-onon",),
            clock_mode="cutoff_v1",
        )
        raw = str(
            conn.execute(
                "SELECT request_json FROM research_snapshot_headers WHERE research_snapshot_id=?",
                (snapshot,),
            ).fetchone()[0]
        )
        assert "source_publication_reference_clock" not in raw
        request = ResearchSnapshotRequest.model_validate_json(raw)
        assert verify_research_snapshot(conn, snapshot).member_set_sha256 == sha
        assert build_research_snapshot(conn, request).member_set_sha256 == sha
        assert (
            str(
                conn.execute(
                    "SELECT request_json FROM research_snapshot_headers WHERE research_snapshot_id=?",
                    (snapshot,),
                ).fetchone()[0]
            )
            == raw
        )


def test_v2_public_seal_uses_actual_earlier_publication_and_refuses_mode_change(
    peer_memo: PeerFixture,
) -> None:
    from provenance.source_fact_publication import (
        PublicationVerificationError,
        verify_source_fact_publication,
    )

    conn, _root, _artifact, review, _context, clock, _reference = peer_memo
    snapshot = review.research_snapshot_id
    request = ResearchSnapshotRequest.model_validate_json(
        str(
            conn.execute(
                "SELECT request_json FROM research_snapshot_headers WHERE research_snapshot_id=?",
                (snapshot,),
            ).fetchone()[0]
        )
    )
    assert request.source_publication_reference_clock == "publication_created_v2"
    publication = verify_source_fact_publication(
        conn, publication_id="publication-onon", cutoff=clock, observed_through=clock
    )
    assert publication.created_at < request.cutoff_at
    row = conn.execute(
        "SELECT reference_knowledge_at,reference_recorded_at FROM research_snapshot_members WHERE research_snapshot_id=? AND requested_lane='source_fact_publication:publication-onon'",
        (snapshot,),
    ).fetchone()
    assert row is not None
    assert datetime.fromisoformat(str(row[0])).replace(tzinfo=UTC) == publication.created_at
    assert datetime.fromisoformat(str(row[1])).replace(tzinfo=UTC) == max(
        publication.recorded_at, publication.sealed_at
    )
    with pytest.raises(ValueError):
        build_research_snapshot(
            conn, request.model_copy(update={"source_publication_reference_clock": "cutoff_v1"})
        )
    assert verify_research_snapshot(conn, snapshot).research_snapshot_id == snapshot
    # The exact existing graph-clock verifier still rejects a future publication.
    with pytest.raises(PublicationVerificationError):
        verify_source_fact_publication(
            conn,
            publication_id="publication-onon",
            cutoff=publication.created_at - timedelta(seconds=1),
            observed_through=clock,
        )
