"""Opt-in integration evidence from retained ONON bytes in a disposable database.

The private source template is not a CI fixture. This test performs no network
or production access and grants no semantic role or owner approval.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from dcf.input_evidence import AssumptionBasis, FactBinding, ModelInputRequest
from dcf.onon_inputs import ASSUMPTION_KEYS, RECIPE, prepare_onon_inputs, requirements_for
from provenance.canonical_fact_resolution import CanonicalFactResolutionEngine
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
from provenance.fact_read_model import FactReadModel
from provenance.fulltext_backfill import FullTextBackfillRequest, backfill_fulltext_evidence
from provenance.issuer_registry import (
    IdentifierAssertion,
    IdentifierResolution,
    IssuerRegistry,
    identifier_candidate_digest,
)
from provenance.metric_ontology import MetricOntology
from provenance.population_metric_ontology import (
    ExactSourceAdmissionRequest,
    ExactSourceObservationReview,
    admit_exact_source_observations,
)
from provenance.reporting_entity_registry import ReportingEntityRegistry, SourceObligationRevision
from provenance.research_snapshot import (
    DocumentProcessingDisposition,
    DocumentProcessingPolicy,
    DocumentProcessingScope,
    ProcessingEvidenceReference,
    derive_obligations,
    record_disposition,
    seal_disposition,
    seal_processing_snapshot,
)
from provenance.reviewed_sec_financial_tables import (
    ReviewedSecFinancialFact,
    ReviewedSecFinancialRequest,
    ReviewedSecFinancialSelector,
    SecSourceKind,
    bind_reviewed_sec_financial_selector,
    load_reviewed_sec_html_evidence,
    publish_reviewed_sec_financial_tables,
)
from sqlite_runtime import register_sqlite_integrity_functions
from tests.test_onon_model import memo_inputs
from tests.test_source_fact_repository import seed_foundation

TEMPLATE = Path(os.environ.get("ONON_RETAINED_SOURCE_TEMPLATE", ""))
pytestmark = pytest.mark.skipif(
    not os.environ.get("ONON_RETAINED_SOURCE_TEMPLATE") or not TEMPLATE.is_file(),
    reason="Explicit retained ONON integration artifact required",
)


def test_retained_publication_exact_admission_and_recipe_boundary(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    payload = json.loads(TEMPLATE.read_bytes())
    sources = cast(dict[str, dict[str, str]], payload["sources"])
    entries = cast(list[dict[str, object]], payload["facts"])
    assert len(entries) == 45
    conn = sqlite3.connect(migrated_db(tmp_path / "onon-retained-integration.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    register_sqlite_integrity_functions(conn)
    try:
        seed_foundation(conn)  # Explicit disposable issuer/reporting-entity fixture IDs.
        captured = datetime.now(UTC)
        ledger = EvidenceLedger(conn)
        for filename, source in sources.items():
            path = Path(source["raw_source_path"])
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            assert digest == source["source_doc_sha256"]
            ledger.persist(
                ContentBlob(
                    sha256=digest,
                    byte_size=len(raw),
                    media_type="text/html",
                    storage_uri=path.as_uri(),
                    recorded_at=captured,
                )
            )
            ledger.persist(
                SourceObservation(
                    observation_id="retained-source:" + filename,
                    idempotency_key="retained-source:" + filename,
                    source_kind="sec_filing",
                    source_url=source["source_url"],
                    blob_sha256=digest,
                    source_published_at=None,
                    filing_at=None,
                    accepted_at=None,
                    observed_at=captured,
                    retrieved_at=captured,
                    retrieval_config_sha256="a" * 64,
                    collector_code_version="retained-local-integration.v1",
                )
            )
            ledger.persist(
                DocumentVersion(
                    document_version_id="retained-doc:" + filename,
                    document_key="retained-doc:" + filename,
                    version_sequence=1,
                    observation_id="retained-source:" + filename,
                    blob_sha256=digest,
                    issuer_id="issuer-1",
                    ticker="ONON",
                    document_type="filing",
                    form_type="20-F" if source["source_kind"] == "sec_20f" else "6-K",
                    accession_number=source["accession_number"],
                    period_end=datetime(2025, 12, 31, tzinfo=UTC)
                    if source["source_kind"] == "sec_20f"
                    else datetime(2026, 6, 30, tzinfo=UTC),
                    language="en",
                    recorded_at=captured,
                )
            )
        registry = IssuerRegistry(conn)
        assertion = IdentifierAssertion(
            assertion_id="retained-on-cik",
            idempotency_key="retained-on-cik",
            issuer_id="issuer-1",
            identifier_type="sec_cik",
            identifier_value="1858985",
            normalized_value="0001858985",
            authority="sec_registry",
            source_observation_id="retained-source:annual_2025.html",
            effective_at=captured,
            knowledge_at=captured,
            recorded_at=captured,
        )
        registry.persist(assertion)
        registry.persist(
            IdentifierResolution(
                resolution_id="retained-on-cik-resolution",
                idempotency_key="retained-on-cik-resolution",
                resolution_key=assertion.resolution_key,
                revision=1,
                outcome="selected",
                selected_assertion_id=assertion.assertion_id,
                candidate_digest_sha256=identifier_candidate_digest((assertion,)),
                policy_name="disposable-retained-integration",
                policy_version="1",
                policy_config_sha256="a" * 64,
                reason_code="test_fixture",
                reason_details=(("fixture", "disposable integration authority"),),
                material_dissent=False,
                effective_at=captured,
                knowledge_at=captured,
                recorded_at=captured,
            )
        )
        conn.commit()
        facts: list[ReviewedSecFinancialFact] = []
        roots = tuple({Path(source["raw_source_path"]).parent for source in sources.values()})
        for filename, source in sources.items():
            document = "retained-doc:" + filename
            backfill_fulltext_evidence(
                conn,
                FullTextBackfillRequest(
                    repo_root=tmp_path,
                    content_roots=roots,
                    source_lane="evidence_native",
                    document_version_id=document,
                    apply=True,
                ),
            )
            run = str(
                conn.execute(
                    "SELECT extraction_run_id FROM evidence_extraction_runs WHERE document_version_id=?",
                    (document,),
                ).fetchone()[0]
            )
            witness = load_reviewed_sec_html_evidence(
                conn,
                document_version_id=document,
                fulltext_run_id=run,
                source_kind=cast(SecSourceKind, source["source_kind"]),
                accession_number=source["accession_number"],
                sec_cik="0001858985",
                source_doc_sha256=source["source_doc_sha256"],
                content_roots=roots,
                knowledge_cutoff=datetime.now(UTC),
            )
            for entry in entries:
                if entry["source_file"] == filename:
                    facts.append(
                        bind_reviewed_sec_financial_selector(
                            witness, ReviewedSecFinancialSelector.model_validate(entry["selector"])
                        )
                    )
            print("CAPTURE", filename, source["source_doc_sha256"], "nodes", len(witness.nodes))
        now = datetime.now(UTC)
        draft = ReviewedSecFinancialRequest.model_construct(
            ticker="ONON",
            reviewed_by="disposable-integration-fixture",
            reviewed_at=now,
            review_evidence="Private template replay in disposable test DB; no investment or owner approval",
            expected_fact_keys=tuple(payload["expected_fact_keys"]),
            facts=tuple(facts),
            rejections=(),
            content_roots=roots,
            recorded_at=now,
            review_sha256="0" * 64,
            apply=False,
        )
        review = ReviewedSecFinancialRequest.model_validate(
            {
                **draft.model_dump(mode="json"),
                "review_sha256": hashlib.sha256(draft.canonical_review_json.encode()).hexdigest(),
            }
        )
        dry = publish_reviewed_sec_financial_tables(conn, review)
        assert dry.captured_count == 45
        applied = publish_reviewed_sec_financial_tables(
            conn,
            review.model_copy(update={"apply": True, "expected_plan_sha256": dry.plan_sha256}),
        )
        assert len(applied.observation_ids) == 45
        assert publish_reviewed_sec_financial_tables(
            conn,
            review.model_copy(update={"apply": True, "expected_plan_sha256": dry.plan_sha256}),
        ).exact_replay
        print(
            "FULL45_PUBLICATION",
            applied.publication_id,
            "count",
            applied.captured_count,
            "plan",
            dry.plan_sha256,
        )
        reader = FactReadModel(conn)
        reviews: list[ExactSourceObservationReview] = []
        for _key, observation_id in applied.observation_ids:
            bundle = reader.provenance_bundle(observation_id, cutoff=now)
            assert bundle.evidence is not None
            seal = conn.execute(
                "SELECT semantic_key_sha256 FROM fact_cell_identity_seals_v2 WHERE fact_cell_id=?",
                (bundle.cell.fact_cell_id,),
            ).fetchone()
            reviews.append(
                ExactSourceObservationReview(
                    observation_id=observation_id,
                    document_version_id=bundle.evidence.document_version_id,
                    subject_binding_revision_id=bundle.evidence.subject_binding_revision_id,
                    observation_payload_sha256=bundle.observation_payload_sha256,
                    source_locator_sha256=bundle.evidence.source_locator_sha256,
                    fact_cell_semantic_key_sha256=str(seal[0]),
                )
            )
        admitted = admit_exact_source_observations(
            conn,
            ExactSourceAdmissionRequest(
                observations=tuple(reviews),
                reviewer_identity="disposable-integration-fixture",
                review_evidence={
                    "scope": "exact private retained observations only; no valuation roles approved"
                },
                knowledge_cutoff=now,
                operation_recorded_at=now,
            ),
        )
        conn.commit()
        assert len(admitted.observation_ids) == 45
        ontology = MetricOntology(conn)
        resolver = CanonicalFactResolutionEngine(conn)
        bindings: dict[str, FactBinding] = {}
        for (key, observation_id), canonical in zip(
            applied.observation_ids, admitted.canonical_metric_cell_ids, strict=True
        ):
            row = conn.execute(
                "SELECT metric_id FROM canonical_metric_cells WHERE canonical_metric_cell_id=?",
                (canonical,),
            ).fetchone()
            metric_id = str(row[0])
            definition = ontology.metric_definition_as_known(metric_id, now)
            resolution = resolver.as_known(canonical, now)
            assert definition is not None and resolution is not None
            assert (
                resolution.status == "resolved"
                and resolution.selected_observation_id == observation_id
            )
            assert "valuation_role" not in definition.scope_constraints
            bindings[key] = FactBinding(
                canonical_metric_cell_id=canonical,
                metric_id=metric_id,
                metric_definition_revision_id=definition.metric_definition_revision_id,
                canonical_resolution_revision_id=resolution.canonical_resolution_revision_id,
                observation_id=observation_id,
                observation_payload_sha256=reader.provenance_bundle(
                    observation_id, cutoff=now
                ).observation_payload_sha256,
            )
        request = ModelInputRequest(
            recipe=RECIPE,
            ticker="ONON",
            research_snapshot_id="unavailable-integration-research-snapshot",
            financial_period_end=datetime(2026, 6, 30).date(),
            facts=bindings,
            assumptions={
                key: AssumptionBasis(
                    value=value,
                    attribution="analyst",
                    rationale="Disposable integration forecast fixture; not owner approval",
                    source_reference="test:fixture",
                    source_as_of=datetime(2026, 10, 3).date(),
                    recorded_at=now,
                )
                for key, value in memo_inputs().items()
                if key in ASSUMPTION_KEYS
            },
        )
        with pytest.raises(ValueError, match="Research Snapshot is not fully sealed") as blocked:
            prepare_onon_inputs(conn, request, effective_inputs=memo_inputs(), as_of=now)
        print("PREPARE_BLOCKED", type(blocked.value).__name__, str(blocked.value))
        print(
            "ROLE_GAP",
            len(bindings),
            "exact admitted source definitions lack ONON valuation roles/constraints",
        )
        assert set(bindings) == {req.key for req in requirements_for(request.financial_period_end)}
        _prove_processing_boundary(conn, now, tuple("retained-doc:" + key for key in sources))
    finally:
        conn.close()


def _prove_processing_boundary(
    conn: sqlite3.Connection, now: datetime, documents: tuple[str, ...]
) -> None:
    registry = ReportingEntityRegistry(conn)
    for family in ("operating_company_periodic", "continuous_disclosure"):
        registry.persist(
            SourceObligationRevision(
                obligation_revision_id="synthetic-retained-obligation:" + family,
                idempotency_key="synthetic-retained-obligation:" + family,
                obligation_key="synthetic-retained-obligation:" + family,
                revision=1,
                issuer_id="issuer-1",
                reporting_entity_id="reporting-1",
                authority_kind="sec_edgar",
                document_family=family,
                obligation_state="required",
                completeness_rule="regulator_inventory",
                active_from=now,
                active_to=None,
                decision_kind="deterministic",
                reason_code="synthetic_test_context",
                reason_details=(
                    (
                        "scope",
                        "Explicit synthetic disposable obligations; no live completeness claim",
                    ),
                ),
                effective_at=now,
                knowledge_at=now,
                recorded_at=now,
            )
        )
    scope = DocumentProcessingScope(document_version_ids=documents)
    policy = DocumentProcessingPolicy(
        policy_name="synthetic-retained-integration", policy_version="1"
    )
    obligations = derive_obligations(conn, scope, now, policy)
    applicable = tuple(item for item in obligations if item.applicability == "applicable")
    assert {(item.document_version_id, item.processing_lane) for item in applicable} == {
        *((doc, "html_native_hierarchy") for doc in documents),
        ("retained-doc:annual_2025.html", "filing_xbrl"),
    }
    print(
        "PROCESSING_APPLICABLE",
        [(item.document_version_id, item.processing_lane) for item in applicable],
    )
    references: dict[str, ProcessingEvidenceReference] = {}
    for document in documents:
        receipt = publish_document_processing_evidence(
            conn,
            document_version_id=document,
            processing_lane="html_native_hierarchy",
            cutoff_at=now,
            recorded_at=now,
        )
        verified = verify_document_processing_evidence(
            conn,
            receipt.evidence_seal_id,
            document_version_id=document,
            processing_lane="html_native_hierarchy",
            cutoff_at=now,
        )
        print("HTML_PROCESSING_SEALED", document, verified.member_count, verified.member_set_sha256)
        references[document] = ProcessingEvidenceReference(
            evidence_table="document_processing_evidence_seals",
            evidence_id=verified.evidence_seal_id,
            evidence_commitment_sha256=verified.member_set_sha256,
            knowledge_at=verified.knowledge_at,
            recorded_at=verified.recorded_at,
        )
    for ordinal, obligation in enumerate(obligations):
        if obligation.applicability == "applicable" and obligation.processing_lane == "filing_xbrl":
            # The closed reviewed numeric population cannot impersonate a complete
            # XBRL extraction. No succeeded or not-applicable disposition is minted.
            continue
        identity = "synthetic-retained-disposition:" + str(ordinal)
        is_html = obligation.applicability == "applicable"
        record_disposition(
            conn,
            DocumentProcessingDisposition(
                processing_disposition_id=identity,
                idempotency_key=identity,
                processing_obligation_revision_id=obligation.processing_obligation_revision_id,
                terminal_status="succeeded" if is_html else "not_applicable",
                reason_code="exact_native_html_seal" if is_html else "typed_policy_not_applicable",
                reason_details={"scope": "synthetic disposable context"},
                evidence=(references[obligation.document_version_id],) if is_html else (),
                knowledge_at=now,
                recorded_at=now,
            ),
        )
        seal_disposition(conn, identity, sealed_at=now)
    assert (
        conn.execute("SELECT count(*) FROM filing_xbrl_extraction_disposition_seals").fetchone()[0]
        == 0
    )
    with pytest.raises(
        ValueError, match="every processing lane requires exactly one terminal seal"
    ) as blocked:
        seal_processing_snapshot(
            conn,
            processing_snapshot_id="synthetic-retained-processing",
            idempotency_key="synthetic-retained-processing",
            scope=scope,
            cutoff_at=now,
            policy=policy,
            recorded_at=now,
        )
    print("PROCESSING_SNAPSHOT_BLOCKED", str(blocked.value))
