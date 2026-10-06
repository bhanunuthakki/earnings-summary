"""Current-schema reporting/capture parity. No full research admission is claimed."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

import pytest

import provenance.population_document_processing as document_population
import provenance.research_snapshot as research_snapshot
import tests.test_grounded_ask_retrieval as ask_fixtures
from provenance.evidence_ledger import DocumentVersion, EvidenceLedger
from provenance.reporting_document_scope import ReportingDocumentDecision
from provenance.reporting_entity_registry import ReportingEntityRegistry, SourceObligationRevision
from provenance.research_snapshot import (
    CorpusProjectionBundle,
    DocumentProcessingDisposition,
    DocumentProcessingPolicy,
    DocumentProcessingScope,
    ResearchSnapshotRequest,
    ResearchUniverse,
    derive_obligations,
    record_disposition,
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
from provenance.source_inventory_seal import (
    InventoryComponent,
    InventorySeal,
    SourceInventorySealStore,
    component_digest,
)
from search.corpus_builder import (
    CorpusBuildRequest,
    CoverageExpectedDocumentInventory,
    build_grounded_search_corpus,
    load_coverage_expected_document_inventory,
)
from tests.test_grounded_ask_retrieval import STAMP, A, C

KEY = "sec-cik:0000000001:submissions"
ISSUER = "sec-cik:0000000001"


class Package(Protocol):
    def __call__(
        self,
        *,
        support_captured: bool = True,
        missing_lifecycle: bool = False,
        subject: bool = False,
    ) -> tuple[sqlite3.Connection, str]: ...


class UniverseVerifier(Protocol):
    def __call__(
        self,
        conn: sqlite3.Connection,
        request: ResearchSnapshotRequest,
        *,
        verify_fact_subjects: bool,
    ) -> None: ...


_document_scope: Callable[
    [sqlite3.Connection, datetime, datetime],
    tuple[tuple[ReportingDocumentDecision, ...], dict[str, tuple[str, ...]], int],
] = getattr(document_population, "_document_scope")
_verify_research_universe: UniverseVerifier = getattr(
    research_snapshot, "_verify_research_universe"
)
_conn: Callable[[Path, Callable[..., Path]], sqlite3.Connection] = getattr(ask_fixtures, "_conn")
_seed_complete_corpus: Callable[[sqlite3.Connection], str] = getattr(
    ask_fixtures, "_seed_complete_corpus"
)


@pytest.fixture
def package(tmp_path: Path, migrated_db: Callable[..., Path]) -> Iterator[Package]:
    conn = _conn(tmp_path, migrated_db)
    _seed_complete_corpus(conn)

    def seed(
        *, support_captured: bool = True, missing_lifecycle: bool = False, subject: bool = False
    ) -> tuple[sqlite3.Connection, str]:
        coverage = SourceCoverageLedger(conn)
        coverage.persist(
            SourceInventorySnapshot(
                snapshot_id="package",
                idempotency_key="package",
                inventory_key=KEY,
                revision=2,
                issuer_id=ISSUER,
                ticker="ACME",
                source_kind="sec_submissions",
                source_url="https://sec.test/submissions",
                source_observation_id="obs",
                outcome="succeeded",
                authoritative=True,
                retrieval_config_sha256=C,
                collector_code_version="synthetic-package",
                supersedes_snapshot_id=str(
                    conn.execute(
                        "SELECT snapshot_id FROM source_inventory_snapshots WHERE inventory_key=? AND revision=1",
                        (KEY,),
                    ).fetchone()[0]
                ),
                started_at=STAMP,
                completed_at=STAMP,
                recorded_at=STAMP,
            )
        )
        kinds = [
            ("primary", "filing", "10-Q", "doc"),
            ("financial", "sec_financial_report", "10-Q", "financial"),
            ("attachment", "sec_supporting_attachment", "10-Q", "attachment"),
        ]
        if subject:
            ReportingEntityRegistry(conn).persist(
                SourceObligationRevision(
                    obligation_revision_id="current-duty",
                    idempotency_key="current-duty",
                    obligation_key="current-duty",
                    revision=1,
                    issuer_id=ISSUER,
                    reporting_entity_id="entity",
                    authority_kind="sec_edgar",
                    document_family="continuous_disclosure",
                    obligation_state="required",
                    completeness_rule="regulator_inventory",
                    active_from=STAMP,
                    active_to=None,
                    decision_kind="manual",
                    effective_at=STAMP,
                    knowledge_at=STAMP,
                    recorded_at=STAMP,
                    reason_code="synthetic_test_duty",
                    reason_details=(("scope", "test"),),
                )
            )
            kinds.append(("subject", "financial_statement", "6-K", "subject"))
        for name, kind, form, document_id in kinds:
            document_key = (
                "missing-lifecycle"
                if name == "primary" and missing_lifecycle
                else "sec-cik:0000000001:accession"
                if name == "primary"
                else name
            )
            captured = name == "primary" or support_captured or name == "subject"
            if captured and name != "primary":
                EvidenceLedger(conn).persist(
                    DocumentVersion(
                        document_version_id=document_id,
                        document_key=document_key,
                        version_sequence=1,
                        observation_id="obs",
                        blob_sha256=A,
                        issuer_id=ISSUER,
                        ticker="ACME",
                        document_type=kind,
                        form_type=form,
                        accession_number="0000000001-26-000001",
                        language="en",
                        recorded_at=STAMP,
                    )
                )
            coverage.persist(
                ExpectedDocument(
                    expected_document_id=name,
                    idempotency_key=name,
                    snapshot_id="package",
                    expected_document_key=document_key,
                    issuer_id=ISSUER,
                    ticker="ACME",
                    source_kind="sec_filing",
                    document_type=kind,
                    form_type=form,
                    accession_number="0000000001-26-000001",
                    expectation_basis="authoritative",
                    recorded_at=STAMP,
                )
            )
            coverage.persist(
                CoverageAssessment(
                    assessment_id=name,
                    idempotency_key=name,
                    expected_document_id=name,
                    revision=1,
                    coverage_status="captured" if captured else "available",
                    document_version_id=document_id if captured else None,
                    reason_code="synthetic_capture" if captured else "synthetic_uncaptured",
                    reason_details=(("scope", "test"),),
                    decision_kind="manual",
                    policy_name="test",
                    policy_version="1",
                    policy_config_sha256=C,
                    effective_at=STAMP,
                    knowledge_at=STAMP,
                    recorded_at=STAMP,
                    material_dissent=False,
                )
            )
            if not (missing_lifecycle and name == "primary"):
                prior = conn.execute(
                    "SELECT lifecycle_id,revision FROM expected_document_lifecycle_revisions "
                    "WHERE inventory_key=? AND expected_document_key=? ORDER BY revision DESC LIMIT 1",
                    (KEY, document_key),
                ).fetchone()
                persist_expected_document_lifecycle(
                    conn,
                    ExpectedDocumentLifecycle(
                        lifecycle_id=name + "-lifecycle",
                        idempotency_key=name + "-lifecycle",
                        inventory_key=KEY,
                        expected_document_key=document_key,
                        source_inventory_snapshot_id="package",
                        revision=1 if prior is None else int(prior[1]) + 1,
                        status="expected",
                        expected_document_id=name,
                        authority_observation_id="obs",
                        reason_code="synthetic_current",
                        reason_details=(("scope", "test"),),
                        effective_at=STAMP,
                        knowledge_at=STAMP,
                        recorded_at=STAMP,
                        supersedes_lifecycle_id=None if prior is None else str(prior[0]),
                    ),
                )
        component = InventoryComponent(
            component_id="package-component",
            idempotency_key="package-component",
            snapshot_id="package",
            component_key="primary",
            component_kind="primary",
            source_url="https://sec.test/submissions",
            source_observation_id="obs",
            outcome="succeeded",
            required=True,
            ordinal=0,
            recorded_at=STAMP,
        )
        store = SourceInventorySealStore(conn)
        store.persist(component)
        store.persist(
            InventorySeal(
                snapshot_id="package",
                expected_component_count=1,
                component_digest_sha256=component_digest((component,)),
                completion_status="complete",
                sealed_at=STAMP,
            )
        )
        conn.commit()
        return conn, "package"

    yield seed
    conn.close()


def projection(
    conn: sqlite3.Connection, *, cutoff: datetime = STAMP, observed: datetime = STAMP
) -> tuple[CoverageExpectedDocumentInventory, tuple[str, ...]]:
    return load_coverage_expected_document_inventory(
        conn,
        (KEY,),
        knowledge_cutoff=cutoff,
        observed_through=observed,
    )


def request(conn: sqlite3.Connection, *, revision: int = 1) -> CorpusBuildRequest:
    inventory, snapshots = projection(conn)
    return CorpusBuildRequest(
        corpus_key="package",
        revision=revision,
        selector_code_version="test",
        recorded_at=STAMP,
        knowledge_cutoff=STAMP,
        expected_documents=inventory.expected_documents,
        source_inventory_snapshot_ids=snapshots,
        required_extractor_names=("parser",),
        apply=True,
    )


def test_captured_package_has_exact_reporting_set_and_acquisition_links(package: Package) -> None:
    conn, snapshot = package()
    inventory, snapshots = projection(conn)
    decisions, groups, _ = _document_scope(conn, STAMP, STAMP)
    assert [item.document_version_id for item in inventory.expected_documents] == ["doc"]
    assert groups[ISSUER] == ("doc",)
    assert len(inventory.reporting_decisions) == 3
    assert {
        item.reason_code
        for item in decisions
        if item.expected_document_id in {"financial", "attachment"}
    } == {"sec_xbrl_report_attachment", "sec_supporting_artifact"}
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM expected_documents WHERE snapshot_id=?", (snapshot,)
        ).fetchone()[0]
        == 3
    )
    result = build_grounded_search_corpus(conn, request(conn))
    assert result.completion_status == "complete"
    assert snapshots == (snapshot,)
    assert (
        conn.execute(
            "SELECT snapshot_id FROM search_manifest_source_inventories WHERE manifest_id=?",
            (result.manifest_id,),
        ).fetchone()[0]
        == snapshot
    )

    # Exercise the actual universe/duty verifier with a deliberately unsealed processing
    # header/member. This proves set closure only; it cannot establish processing admission.
    obligations = derive_obligations(
        conn,
        DocumentProcessingScope(document_version_ids=("doc",)),
        STAMP,
        DocumentProcessingPolicy(policy_name="test", policy_version="1"),
    )
    obligation = obligations[0]
    record_disposition(
        conn,
        DocumentProcessingDisposition(
            processing_disposition_id="failed-processing",
            idempotency_key="failed-processing",
            processing_obligation_revision_id=obligation.processing_obligation_revision_id,
            terminal_status="failed",
            reason_code="synthetic_incomplete_processing",
            reason_details={},
            knowledge_at=STAMP,
            recorded_at=STAMP,
        ),
    )
    digest = hashlib.sha256(b"{}").hexdigest()
    conn.execute(
        "INSERT INTO document_processing_snapshot_headers VALUES (?,?,?,?,?,?,?,?)",
        ("processing", "processing", "{}", digest, "{}", digest, STAMP, STAMP),
    )
    conn.execute(
        "INSERT INTO document_processing_snapshot_members VALUES (?,?,?,?,?,?,?,?)",
        (
            "processing",
            0,
            obligation.processing_obligation_revision_id,
            "failed-processing",
            obligation.processing_lane,
            "doc",
            "{}",
            digest,
        ),
    )
    _verify_research_universe(
        conn,
        ResearchSnapshotRequest(
            research_snapshot_id="research",
            idempotency_key="research",
            research_universe=ResearchUniverse(
                issuer_id=ISSUER,
                reporting_entity_ids=("entity",),
                document_version_ids=("doc",),
                source_obligation_revision_ids=("duty",),
            ),
            processing_snapshot_ids=("processing",),
            corpus_bundles=(
                CorpusProjectionBundle(
                    corpus_manifest_id=result.manifest_id,
                    lexical_index_run_id=result.lexical_index_run_id,
                ),
            ),
            source_fact_publication_ids=(),
            ontology_snapshot_id="ontology",
            canonical_fact_resolution_snapshot_id="resolution",
            canonical_fact_projection_run_id="projection",
            cutoff_at=STAMP,
            recorded_at=STAMP,
        ),
        verify_fact_subjects=False,
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM document_processing_snapshot_seals").fetchone()[0] == 0
    )


def test_uncaptured_support_prevents_complete_corpus(package: Package) -> None:
    conn, _ = package(support_captured=False)
    inventory, _ = projection(conn)
    assert [
        (item.expected_document_key, item.membership_status)
        for item in inventory.expected_documents
    ] == [
        ("attachment", "missing"),
        ("financial", "missing"),
        ("sec-cik:0000000001:accession", "included"),
    ]
    result = build_grounded_search_corpus(conn, request(conn))
    assert result.completion_status == "incomplete"
    assert result.expected_document_count == 3 and result.included_document_count == 1
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM expected_document_obligation_bindings WHERE expected_document_id IN ('financial','attachment')"
        ).fetchone()[0]
        == 2
    )


def test_six_k_without_witness_remains_quarantined(package: Package) -> None:
    conn, _ = package(subject=True)
    inventory, _ = projection(conn)
    assert any(
        item.reason_code == "financial_reporting_subject_review_missing"
        and item.outcome == "unresolved"
        for item in inventory.reporting_decisions
    )
    assert [
        (item.expected_document_key, item.membership_status)
        for item in inventory.expected_documents
    ] == [("sec-cik:0000000001:accession", "included"), ("subject", "quarantined")]
    assert build_grounded_search_corpus(conn, request(conn)).completion_status == "incomplete"


def test_missing_lifecycle_is_visible_and_incomplete(package: Package) -> None:
    conn, _ = package(missing_lifecycle=True)
    inventory, _ = projection(conn)
    decision = next(
        item for item in inventory.reporting_decisions if item.expected_document_id == "primary"
    )
    assert decision.reason_code == "expected_document_lifecycle_missing"
    assert decision.outcome == "unresolved"
    assert inventory.expected_documents[0].membership_status == "quarantined"
    assert build_grounded_search_corpus(conn, request(conn)).completion_status == "incomplete"


@pytest.mark.parametrize("clock", ["knowledge", "recorded"])
def test_future_lifecycle_respects_each_clock(package: Package, clock: str) -> None:
    conn, _ = package()
    late = STAMP + timedelta(days=1)
    SourceCoverageLedger(conn).persist(
        SourceInventorySnapshot(
            snapshot_id="withdrawal-inventory",
            idempotency_key="withdrawal-inventory",
            inventory_key=KEY,
            revision=3,
            issuer_id=ISSUER,
            ticker="ACME",
            source_kind="sec_submissions",
            source_url="https://sec.test/submissions",
            source_observation_id="obs",
            outcome="succeeded",
            authoritative=True,
            retrieval_config_sha256=C,
            collector_code_version="synthetic-withdrawal",
            started_at=late,
            completed_at=late,
            recorded_at=late,
            supersedes_snapshot_id="package",
        )
    )
    persist_expected_document_lifecycle(
        conn,
        ExpectedDocumentLifecycle(
            lifecycle_id="withdrawal",
            idempotency_key="withdrawal",
            inventory_key=KEY,
            expected_document_key="sec-cik:0000000001:accession",
            source_inventory_snapshot_id="withdrawal-inventory",
            revision=3,
            status="withdrawn_by_authority",
            authority_observation_id="obs",
            reason_code="synthetic_withdrawal",
            reason_details=(("scope", "test"),),
            effective_at=STAMP,
            knowledge_at=late if clock == "knowledge" else STAMP,
            recorded_at=late,
            supersedes_lifecycle_id="primary-lifecycle",
        ),
    )
    _, old_groups, _ = _document_scope(conn, STAMP, late if clock == "knowledge" else STAMP)
    assert old_groups[ISSUER] == ("doc",)
    decisions, groups, incomplete = _document_scope(conn, late, late)
    assert not groups
    assert (
        next(item for item in decisions if item.expected_document_id == "primary").reason_code
        == "expected_document_not_current"
    )
    assert incomplete == 1  # The new acquisition inventory was not sealed.
    with pytest.raises(ValueError, match="source inventory is absent"):
        projection(conn, cutoff=late, observed=late)


@pytest.mark.parametrize("clock", ["knowledge", "recorded"])
def test_future_quarantine_respects_each_clock(package: Package, clock: str) -> None:
    conn, _ = package()
    late = STAMP + timedelta(days=1)
    SourceCoverageLedger(conn).persist(
        CoverageAssessment(
            assessment_id="quarantine",
            idempotency_key="quarantine",
            expected_document_id="primary",
            revision=2,
            coverage_status="quarantined",
            reason_code="synthetic_quarantine",
            reason_details=(("scope", "test"),),
            decision_kind="manual",
            policy_name="test",
            policy_version="1",
            policy_config_sha256=C,
            effective_at=STAMP,
            knowledge_at=late if clock == "knowledge" else STAMP,
            recorded_at=late,
            supersedes_assessment_id="primary",
            material_dissent=False,
        )
    )
    old, _ = projection(conn, observed=late if clock == "knowledge" else STAMP)
    assert [item.document_version_id for item in old.expected_documents] == ["doc"]
    new, _ = projection(conn, cutoff=late, observed=late)
    assert new.expected_documents[0].membership_status == "quarantined"


def test_builder_rejects_manual_projection_reduction(package: Package) -> None:
    conn, _ = package(support_captured=False)
    full = request(conn)
    reduced = full.model_copy(
        update={
            "expected_documents": tuple(
                item for item in full.expected_documents if item.membership_status == "included"
            )
        }
    )
    with pytest.raises(ValueError, match="exact governed reporting projection"):
        build_grounded_search_corpus(conn, reduced)
