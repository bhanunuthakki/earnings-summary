"""A bounded report profile changes duties, never source truth or full-profile proof."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from provenance.analysis_scope import (
    AnalysisEvidenceScope,
    build_analysis_scope,
    require_analysis_documents,
    resolve_analysis_coverage,
    verify_analysis_scope,
)
from provenance.document_processing_evidence import (
    VerifiedDocumentProcessingEvidence,
    publish_document_processing_evidence,
    verify_document_processing_evidence,
)
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    EvidenceLocator,
    EvidenceNode,
    ExtractionRun,
    SourceObservation,
)
from provenance.fulltext_extractor_identity import STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR
from provenance.population_document_processing import (
    DocumentProcessingPopulationRequest,
    populate_document_processing,
)
from provenance.research_snapshot import (
    DocumentProcessingDisposition,
    DocumentProcessingPolicy,
    DocumentProcessingScope,
    ProcessingEvidenceReference,
    derive_obligations,
    record_disposition,
    seal_disposition,
    seal_processing_snapshot,
    verify_processing_snapshot,
)
from provenance.sec_native_capture import SecNativeCaptureError, load_captured_sec_filing_package
from provenance.source_coverage import CoverageAssessment, SourceCoverageLedger
from provenance.source_coverage_reconcile import (
    ExpectedDocumentLifecycle,
    persist_expected_document_lifecycle,
)
from tests.test_analysis_scope import (
    STAMP,
    K,
    add_expected,
    capture_document,
    scope_db,
    scope_request,
)
from tests.test_analysis_scope_processing import processing_db

PROFILE = "complete_report_evidence/v1"


def test_report_profile_is_explicit_and_legacy_request_bytes_stay_unchanged() -> None:
    legacy = scope_request()
    assert "readiness_profile" not in legacy.model_dump(mode="json")
    assert scope_request(readiness_profile=None).model_dump_json() == legacy.model_dump_json()
    assert scope_request(readiness_profile=PROFILE).readiness_profile == PROFILE
    with pytest.raises(ValidationError):
        scope_request(readiness_profile="skip_checks")


def test_report_capture_defers_children_without_erasing_their_gaps(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, _ = scope_db(tmp_path, migrated_db)
    try:
        add_expected(
            conn, "unfetched-child", "0000000001-26-000001", document_type="sec_financial_report"
        )
        strict = build_analysis_scope(conn, scope_request())
        scoped = build_analysis_scope(conn, scope_request(readiness_profile=PROFILE))
        assert strict.schema_version == "analysis-evidence-scope/v1"
        assert scoped.schema_version == "analysis-evidence-scope/v3"
        assert strict.scope_id != scoped.scope_id
        with pytest.raises(ValueError, match="capture is incomplete"):
            require_analysis_documents(conn, strict, K, K)
        before = conn.total_changes
        assert require_analysis_documents(conn, scoped, K, K) == ("document:expected-10k",)
        child = next(
            c
            for c in resolve_analysis_coverage(conn, scoped, K, K)
            if c.expected_document_id == "unfetched-child"
        )
        assert child.role == "package_dependency"
        assert child.coverage_status == "unassessed" and child.document_version_id is None
        assert conn.total_changes == before
    finally:
        conn.close()


def test_report_capture_does_not_defer_missing_primary_amendments(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, _ = scope_db(tmp_path, migrated_db)
    try:
        add_expected(conn, "missing-amendment", "0000000001-26-000002", form="10-K/A")
        scoped = build_analysis_scope(conn, scope_request(readiness_profile=PROFILE))
        with pytest.raises(ValueError, match="capture is incomplete"):
            require_analysis_documents(conn, scoped, K, K)
    finally:
        conn.close()


def test_report_capture_does_not_defer_original_exhibits(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, _ = scope_db(tmp_path, migrated_db)
    try:
        exhibit = add_expected(
            conn,
            "original-exhibit",
            "0000000001-26-000001",
            document_type="sec_exhibit",
            period=None,
        )
        scope = build_analysis_scope(conn, scope_request(readiness_profile=PROFILE))
        assert (
            next(
                e for e in scope.entries if e.expected_document == exhibit
            ).expected_document.period_end
            is None
        )
        with pytest.raises(ValueError, match="capture is incomplete"):
            require_analysis_documents(conn, scope, K, K)
        capture_document(conn, exhibit)
        assert (
            next(e for e in scope.entries if e.expected_document == exhibit).role
            == "research_document"
        )
        assert require_analysis_documents(conn, scope, K, K) == (
            "document:expected-10k",
            "document:original-exhibit",
        )
        with pytest.raises(ValueError, match="reporting period differs"):
            build_analysis_scope(conn, scope_request())
    finally:
        conn.close()


def test_report_scope_cannot_self_rehash_away_package_inventory(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, _ = scope_db(tmp_path, migrated_db)
    try:
        scope = build_analysis_scope(conn, scope_request(readiness_profile=PROFILE))
        payload = scope.model_dump(mode="json", exclude={"scope_id", "scope_sha256"})
        payload["entries"] = [
            entry.model_dump(mode="json")
            for entry in scope.entries
            if entry.role != "package_dependency"
        ]
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        forged = AnalysisEvidenceScope.model_validate(
            payload | {"scope_sha256": digest, "scope_id": "analysis-scope:" + digest}
        )
        with pytest.raises(ValueError, match="fabricated"):
            verify_analysis_scope(conn, forged)
        with pytest.raises(ValueError, match="fabricated"):
            load_captured_sec_filing_package(
                conn,
                inventory_key=scope_request().inventory_key,
                accession_number="0000000001-26-000001",
                analysis_scope=forged,
            )
    finally:
        conn.close()


def test_report_processing_defers_xbrl_but_still_requires_native_text(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, strict = processing_db(tmp_path, migrated_db)
    try:
        policy = DocumentProcessingPolicy(policy_name="profile-regression", policy_version="1")
        full_scope = DocumentProcessingScope(
            document_version_ids=("document:expected-10k",), analysis_scope=strict
        )
        full = derive_obligations(conn, full_scope, K, policy, recorded_at=K)
        native = next(o for o in full if o.processing_lane == "filing_xbrl")
        assert native.applicability == "applicable"
        scoped = build_analysis_scope(conn, scope_request(readiness_profile=PROFILE))
        bounded_scope = DocumentProcessingScope(
            document_version_ids=full_scope.document_version_ids, analysis_scope=scoped
        )
        bounded = derive_obligations(conn, bounded_scope, K, policy, recorded_at=K)
        assert len(bounded) == len(full) - 1
        assert all(o.processing_lane != "filing_xbrl" for o in bounded)
        assert (
            next(o for o in bounded if o.processing_lane == "html_native_hierarchy").applicability
            == "applicable"
        )
        assert (
            conn.execute(
                "SELECT applicability FROM document_processing_obligation_revisions WHERE processing_obligation_revision_id=?",
                (native.processing_obligation_revision_id,),
            ).fetchone()[0]
            == "applicable"
        )
        with pytest.raises(ValueError, match="terminal seal"):
            seal_processing_snapshot(
                conn,
                processing_snapshot_id="unproven-report",
                idempotency_key="unproven-report",
                scope=bounded_scope,
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


def test_primary_loader_preserves_default_package_gate(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, _ = scope_db(tmp_path, migrated_db)
    try:
        add_expected(
            conn, "unfetched-child", "0000000001-26-000001", document_type="sec_financial_report"
        )
        with pytest.raises(SecNativeCaptureError, match="not completely captured"):
            load_captured_sec_filing_package(
                conn,
                inventory_key=scope_request().inventory_key,
                accession_number="0000000001-26-000001",
            )
        primary = load_captured_sec_filing_package(
            conn,
            inventory_key=scope_request().inventory_key,
            accession_number="0000000001-26-000001",
            analysis_scope=build_analysis_scope(conn, scope_request(readiness_profile=PROFILE)),
        )
        assert len(primary) == 1 and primary[0].document_version_id == "document:expected-10k"
    finally:
        conn.close()


@pytest.mark.parametrize("mismatch", ["inventory", "accession"])
def test_scoped_loader_requires_matching_inventory_and_accession(
    tmp_path: Path, migrated_db: Callable[..., Path], mismatch: str
) -> None:
    conn, _ = scope_db(tmp_path, migrated_db)
    try:
        scope = build_analysis_scope(conn, scope_request(readiness_profile=PROFILE))
        with pytest.raises(SecNativeCaptureError, match=mismatch):
            load_captured_sec_filing_package(
                conn,
                inventory_key="other" if mismatch == "inventory" else scope.request.inventory_key,
                accession_number="0000000001-26-000099"
                if mismatch == "accession"
                else scope.accession_numbers[0],
                analysis_scope=scope,
            )
    finally:
        conn.close()


@pytest.mark.parametrize("profile", [None, PROFILE])
def test_scoped_loader_pins_versions_even_after_current_coverage_changes(
    tmp_path: Path, migrated_db: Callable[..., Path], profile: str | None
) -> None:
    conn, _ = scope_db(tmp_path, migrated_db)
    try:
        scope = build_analysis_scope(conn, scope_request(readiness_profile=profile))
        columns = tuple(DocumentVersion.model_fields)
        row = conn.execute(
            "SELECT "
            + ",".join(columns)
            + " FROM evidence_document_versions WHERE document_version_id=?",
            ("document:expected-10k",),
        ).fetchone()
        original = DocumentVersion.model_validate(dict(zip(columns, row, strict=True)))
        later = K + timedelta(days=1)
        ledger = EvidenceLedger(conn)
        body = b"<html>Later original bytes.</html>"
        digest = hashlib.sha256(body).hexdigest()
        source = tmp_path / "later.html"
        source.write_bytes(body)
        expected = next(
            e.expected_document
            for e in scope.entries
            if e.expected_document.expected_document_id == "expected-10k"
        )
        ledger.persist(
            ContentBlob(
                sha256=digest,
                byte_size=len(body),
                media_type="text/html",
                storage_uri=source.as_uri(),
                recorded_at=later,
            )
        )
        ledger.persist(
            SourceObservation(
                observation_id="later-source",
                idempotency_key="later-source",
                source_kind="sec_filing",
                source_url=expected.source_url or "",
                blob_sha256=digest,
                source_published_at=None,
                accepted_at=None,
                filing_at=expected.filing_at,
                observed_at=later,
                retrieved_at=later,
                retrieval_config_sha256="c" * 64,
                collector_code_version="test@1",
            )
        )
        ledger.persist(
            original.model_copy(
                update={
                    "document_version_id": "later-version",
                    "version_sequence": 2,
                    "replaces_document_version_id": original.document_version_id,
                    "recorded_at": later,
                    "observation_id": "later-source",
                    "blob_sha256": digest,
                }
            )
        )
        SourceCoverageLedger(conn).persist(
            CoverageAssessment(
                assessment_id="later-capture",
                idempotency_key="later-capture",
                expected_document_id="expected-10k",
                revision=2,
                coverage_status="captured",
                document_version_id="later-version",
                reason_code="later_version",
                reason_details=(("scope", "test"),),
                decision_kind="deterministic",
                policy_name="test",
                policy_version="1",
                policy_config_sha256="c" * 64,
                effective_at=later,
                knowledge_at=later,
                recorded_at=later,
                supersedes_assessment_id="captured:expected-10k",
                material_dissent=False,
            )
        )
        assert (
            load_captured_sec_filing_package(
                conn,
                inventory_key=scope.request.inventory_key,
                accession_number=scope.accession_numbers[0],
            )[0].document_version_id
            == "later-version"
        )
        members = load_captured_sec_filing_package(
            conn,
            inventory_key=scope.request.inventory_key,
            accession_number=scope.accession_numbers[0],
            analysis_scope=scope,
        )
        assert members[0].document_version_id == original.document_version_id
    finally:
        conn.close()


@pytest.mark.parametrize("invalid", ["json", "schema", "oversize"])
def test_publication_scope_file_is_bounded_and_validation_does_not_echo_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], invalid: str
) -> None:
    from execution.publish_meli_reported_tables import main

    artifact = tmp_path / "scope.json"
    if invalid == "oversize":
        with artifact.open("wb") as handle:
            handle.truncate(16 * 1024 * 1024 + 1)
    else:
        artifact.write_text(
            "invalid" if invalid == "json" else '{"private-payload": "do-not-echo"}'
        )
    assert (
        main(
            [
                "--db",
                str(tmp_path / "absent.db"),
                "--inventory-key",
                "test",
                "--accession",
                "0000000001-26-000001",
                "--document-version-id",
                "test",
                "--fulltext-run-id",
                "test",
                "--content-root",
                str(tmp_path),
                "--analysis-scope",
                str(artifact),
            ]
        )
        == 3
    )
    output = capsys.readouterr().out
    assert json.loads(output)["outcome"] == "unavailable"
    assert "private-payload" not in output and "do-not-echo" not in output
    assert not (tmp_path / "absent.db").exists()


def test_report_population_does_not_attempt_prior_native_xbrl_obligations(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, strict = processing_db(tmp_path, migrated_db)
    try:
        full_result = populate_document_processing(
            conn,
            DocumentProcessingPopulationRequest(
                cutoff_at=K,
                operation_recorded_at=K,
                phase="obligations",
                analysis_scope=strict,
                apply=True,
            ),
        )
        scoped = build_analysis_scope(conn, scope_request(readiness_profile=PROFILE))
        report_result = populate_document_processing(
            conn,
            DocumentProcessingPopulationRequest(
                cutoff_at=K,
                operation_recorded_at=K,
                phase="obligations",
                analysis_scope=scoped,
                apply=True,
            ),
        )
        assert report_result.expected_obligation_count == full_result.expected_obligation_count - 1
        assert (
            report_result.applicable_obligation_count == full_result.applicable_obligation_count - 1
        )
        assert report_result.processing_snapshot_count == 0
    finally:
        conn.close()


def _publish_native_html(conn: sqlite3.Connection, doc: str) -> VerifiedDocumentProcessingEvidence:
    digest = str(
        conn.execute(
            "SELECT blob_sha256 FROM evidence_document_versions WHERE document_version_id=?", (doc,)
        ).fetchone()[0]
    )
    run_id = "report-html:" + doc
    nodes = (
        EvidenceNode(
            node_id=run_id + ":document",
            evidence_key=run_id + ":document",
            revision=1,
            extraction_run_id=run_id,
            parent_node_id=None,
            supersedes_node_id=None,
            node_kind="document",
            text="Synthetic document",
            locator=EvidenceLocator(source_ref=doc),
            recorded_at=STAMP,
        ),
        EvidenceNode(
            node_id=run_id + ":passage",
            evidence_key=run_id + ":passage",
            revision=1,
            extraction_run_id=run_id,
            parent_node_id=run_id + ":document",
            supersedes_node_id=None,
            node_kind="passage",
            text="Synthetic operating disclosure.",
            locator=EvidenceLocator(source_ref=doc, char_start=0, char_end=31),
            recorded_at=STAMP,
        ),
    )
    payload = json.dumps(
        [n.model_dump(mode="json", exclude_none=True) for n in nodes],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    identity = STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ExtractionRun(
            extraction_run_id=run_id,
            idempotency_key=run_id,
            document_version_id=doc,
            input_sha256=digest,
            extractor_name=identity.name,
            extractor_config_sha256=identity.config_sha256,
            extractor_code_version=identity.code_version,
            output_sha256=hashlib.sha256(payload.encode()).hexdigest(),
            started_at=STAMP,
            completed_at=STAMP,
            outcome="succeeded",
        )
    )
    for node in nodes:
        ledger.persist(node)
    evidence = publish_document_processing_evidence(
        conn,
        document_version_id=doc,
        processing_lane="html_native_hierarchy",
        cutoff_at=K,
        recorded_at=K,
    )
    return verify_document_processing_evidence(
        conn,
        evidence.evidence_seal_id,
        document_version_id=doc,
        processing_lane="html_native_hierarchy",
        cutoff_at=K,
        observed_through=K,
    )


def test_native_report_snapshot_replays_explicit_deferred_duty(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, strict = processing_db(tmp_path, migrated_db)
    try:
        scope = build_analysis_scope(conn, scope_request(readiness_profile=PROFILE))
        doc = "document:expected-10k"
        verified = _publish_native_html(conn, doc)
        policy = DocumentProcessingPolicy(policy_name="bounded-proof", policy_version="1")
        selected = DocumentProcessingScope(document_version_ids=(doc,), analysis_scope=scope)
        obligations = derive_obligations(conn, selected, K, policy, recorded_at=K)
        for index, obligation in enumerate(obligations):
            disposition_id = f"bounded-disposition:{index}"
            evidence_refs = (
                ()
                if obligation.applicability == "not_applicable"
                else (
                    ProcessingEvidenceReference(
                        evidence_table="document_processing_evidence_seals",
                        evidence_id=verified.evidence_seal_id,
                        evidence_commitment_sha256=verified.member_set_sha256,
                        knowledge_at=verified.knowledge_at,
                        recorded_at=verified.recorded_at,
                    ),
                )
            )
            record_disposition(
                conn,
                DocumentProcessingDisposition(
                    processing_disposition_id=disposition_id,
                    idempotency_key=disposition_id,
                    processing_obligation_revision_id=obligation.processing_obligation_revision_id,
                    terminal_status="not_applicable"
                    if obligation.applicability == "not_applicable"
                    else "succeeded",
                    reason_code="synthetic_native_processing_proof",
                    reason_details={},
                    evidence=evidence_refs,
                    knowledge_at=K,
                    recorded_at=K,
                ),
            )
            seal_disposition(conn, disposition_id, sealed_at=K)
        snapshot_id = "bounded-processing-proof"
        seal_processing_snapshot(
            conn,
            processing_snapshot_id=snapshot_id,
            idempotency_key=snapshot_id,
            scope=selected,
            cutoff_at=K,
            policy=policy,
            recorded_at=K,
        )
        receipt = verify_processing_snapshot(conn, str(snapshot_id))
        assert receipt.readiness_profile == PROFILE
        assert receipt.deferred_processing_lanes == ("filing_xbrl",)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM document_processing_snapshot_members WHERE processing_lane='filing_xbrl'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM document_processing_disposition_headers h JOIN document_processing_obligation_revisions o USING(processing_obligation_revision_id) WHERE o.processing_lane='filing_xbrl'"
            ).fetchone()[0]
            == 0
        )
        with pytest.raises(ValueError, match="incomplete"):
            seal_processing_snapshot(
                conn,
                processing_snapshot_id="strict-still-held",
                idempotency_key="strict-still-held",
                scope=DocumentProcessingScope(document_version_ids=(doc,), analysis_scope=strict),
                cutoff_at=K,
                policy=DocumentProcessingPolicy(policy_name="strict-proof", policy_version="1"),
                recorded_at=K,
            )
    finally:
        conn.close()


def test_report_original_exhibit_reaches_native_text_and_processing_population(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, strict = processing_db(tmp_path, migrated_db)
    try:
        primary = next(e.expected_document for e in strict.entries if e.role == "research_document")
        assert primary.source_url is not None
        exhibit = primary.model_copy(
            update={
                "expected_document_id": "original-exhibit",
                "idempotency_key": "original-exhibit",
                "expected_document_key": "original-exhibit",
                "document_type": "sec_exhibit",
                "period_end": None,
                "source_url": primary.source_url.replace("expected-10k", "original-exhibit"),
                "primary_document": "original-exhibit.htm",
                "source_obligation_revision_id": "sec-periodic:v2",
            }
        )
        SourceCoverageLedger(conn).persist(exhibit)
        persist_expected_document_lifecycle(
            conn,
            ExpectedDocumentLifecycle(
                lifecycle_id="lifecycle:original-exhibit",
                idempotency_key="lifecycle:original-exhibit",
                inventory_key=strict.inventory.inventory_key,
                expected_document_key=exhibit.expected_document_key,
                source_inventory_snapshot_id=strict.inventory.snapshot_id,
                revision=1,
                status="expected",
                expected_document_id=exhibit.expected_document_id,
                authority_observation_id="inventory-observation",
                reason_code="synthetic_inventory",
                reason_details=(("source", "synthetic_fixture"),),
                effective_at=STAMP,
                knowledge_at=STAMP,
                recorded_at=STAMP,
            ),
        )
        doc = capture_document(conn, exhibit)
        scope = build_analysis_scope(conn, scope_request(readiness_profile=PROFILE))
        verified = _publish_native_html(conn, doc)
        assert verified.document_version_id == doc
        assert require_analysis_documents(conn, scope, K, K) == ("document:expected-10k", doc)
        result = populate_document_processing(
            conn,
            DocumentProcessingPopulationRequest(
                cutoff_at=K,
                operation_recorded_at=K,
                phase="obligations",
                analysis_scope=scope,
                apply=True,
            ),
        )
        assert result.expected_document_count == 2
        obligations = derive_obligations(
            conn,
            DocumentProcessingScope(
                document_version_ids=("document:expected-10k", doc),
                analysis_scope=scope,
            ),
            K,
            DocumentProcessingPolicy(policy_name="original-exhibit-proof", policy_version="1"),
            recorded_at=K,
        )
        original_lanes = {
            item.processing_lane
            for item in obligations
            if item.document_version_id == doc and item.applicability == "applicable"
        }
        assert original_lanes == {"html_native_hierarchy"}
    finally:
        conn.close()


@pytest.mark.parametrize("document_type", ["sec_financial_report", "sec_supporting_attachment"])
def test_report_profile_keeps_undated_native_dependencies_visible_without_period_inference(
    tmp_path: Path, migrated_db: Callable[..., Path], document_type: str
) -> None:
    conn, _ = scope_db(tmp_path, migrated_db)
    try:
        dependency = add_expected(
            conn,
            "native-dependency",
            "0000000001-26-000001",
            document_type=document_type,
            period=None,
        )
        scope = build_analysis_scope(conn, scope_request(readiness_profile=PROFILE))
        entry = next(e for e in scope.entries if e.expected_document == dependency)
        assert entry.role == "package_dependency"
        assert (
            entry.expected_document.period_start is None
            and entry.expected_document.period_end is None
        )
        assert require_analysis_documents(conn, scope, K, K) == ("document:expected-10k",)
        coverage = next(
            e
            for e in resolve_analysis_coverage(conn, scope, K, K)
            if e.expected_document_id == dependency.expected_document_id
        )
        assert coverage.coverage_status == "unassessed" and coverage.document_version_id is None
        verify_analysis_scope(conn, scope)
        with pytest.raises(ValueError, match="reporting period differs"):
            build_analysis_scope(conn, scope_request())
    finally:
        conn.close()


@pytest.mark.parametrize(
    "document_type", ["sec_exhibit", "sec_financial_report", "sec_supporting_attachment"]
)
@pytest.mark.parametrize("mismatch", ["period", "form"])
def test_report_profile_rejects_explicit_dependency_period_or_form_conflict(
    tmp_path: Path, migrated_db: Callable[..., Path], document_type: str, mismatch: str
) -> None:
    conn, _ = scope_db(tmp_path, migrated_db)
    try:
        add_expected(
            conn,
            "conflicting-dependency",
            "0000000001-26-000001",
            document_type=document_type,
            period=datetime(2024, 12, 31, tzinfo=UTC) if mismatch == "period" else None,
            form="10-Q" if mismatch == "form" else "10-K",
        )
        with pytest.raises(ValueError, match="form or reporting period differs"):
            build_analysis_scope(conn, scope_request(readiness_profile=PROFILE))
    finally:
        conn.close()
