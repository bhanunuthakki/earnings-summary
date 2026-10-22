"""Retained SEC originals enter the native ledger without invented child HTTP."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    LedgerRecord,
    PersistResult,
    SourceObservation,
)
from provenance.sec_native_capture import load_expected_sec_documents
from provenance.sec_package_subject_witness import verify_sec_package_subject_witness
from provenance.sec_submission_import import (
    RetainedSubmissionImportRequest,
    SecSubmissionImportError,
    import_sec_submission_members,
    verify_sec_submission_import,
)
from provenance.source_coverage import (
    CoverageAssessment,
    ExpectedDocument,
    SourceCoverageLedger,
    SourceInventorySnapshot,
)
from provenance.source_inventory_seal import (
    InventoryComponent,
    InventorySeal,
    SourceInventorySealStore,
    component_digest,
)
from sqlite_runtime import register_sqlite_integrity_functions

STAMP = datetime(2026, 10, 19, 6, tzinfo=UTC)
ACCESSION = "0001140361-26-000001"
BASE = f"https://www.sec.gov/Archives/edgar/data/1/{ACCESSION.replace('-', '')}/"
BODY = b"  <html>\r\n<p>Exact original statement.</p>\r\n</html>\n\n"
CONFIG = "c" * 64


def _source(identity: str, raw: bytes, url: str, kind: str) -> SourceObservation:
    return SourceObservation(
        observation_id=identity,
        idempotency_key=identity,
        source_kind=kind,
        source_url=url,
        blob_sha256=hashlib.sha256(raw).hexdigest(),
        source_published_at=None,
        filing_at=None,
        accepted_at=None,
        observed_at=STAMP,
        retrieved_at=STAMP + timedelta(seconds=1),
        retrieval_config_sha256=CONFIG,
        collector_code_version="synthetic-direct-parent-capture@1",
    )


@pytest.fixture
def retained(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    request: pytest.FixtureRequest,
) -> Generator[tuple[sqlite3.Connection, RetainedSubmissionImportRequest], None, None]:
    review_mode = getattr(request, "param", False)
    reviewed = review_mode in (True, "imported_review", "conflicting_review")
    form = "6-K" if reviewed else "10-K"
    kind = "financial_statement" if reviewed else "filing"
    period = datetime(2026, 6, 30, tzinfo=UTC) if reviewed else datetime(2025, 12, 31, tzinfo=UTC)
    heading = "Reviewed interim financial statements for the quarter ended June 30, 2026"
    document_body = f'<html><h1 id="period">{heading}</h1></html>\n'.encode() if reviewed else BODY
    conn = sqlite3.connect(migrated_db(tmp_path / "isolated.db"))
    register_sqlite_integrity_functions(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO issuer_entities VALUES(?,?,?,?)",
        ("issuer-1", "issuer-1", "operating_company", STAMP),
    )
    conn.execute(
        "INSERT INTO source_obligation_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "duty",
            "duty",
            "duty",
            1,
            "issuer-1",
            None,
            "sec_edgar",
            "continuous_disclosure" if reviewed else "operating_company_periodic",
            "required",
            "regulator_inventory",
            STAMP,
            None,
            "deterministic",
            "synthetic",
            "{}",
            STAMP,
            STAMP,
            STAMP,
            None,
        ),
    )
    metadata = b'{"synthetic_inventory":true}'
    metadata_url = "https://data.sec.gov/submissions/CIK0000000001.json"
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_bytes(metadata)
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=hashlib.sha256(metadata).hexdigest(),
            byte_size=len(metadata),
            media_type="application/json",
            storage_uri=metadata_path.as_uri(),
            recorded_at=STAMP,
        )
    )
    ledger.persist(_source("metadata", metadata, metadata_url, "sec_submissions"))
    coverage = SourceCoverageLedger(conn)
    coverage.persist(
        SourceInventorySnapshot(
            snapshot_id="inventory",
            idempotency_key="inventory",
            inventory_key="synthetic:sec",
            revision=1,
            issuer_id="issuer-1",
            ticker="SYNTH",
            source_kind="sec_submissions",
            source_url=metadata_url,
            source_observation_id="metadata",
            outcome="succeeded",
            authoritative=True,
            retrieval_config_sha256=CONFIG,
            collector_code_version="synthetic",
            started_at=STAMP,
            completed_at=STAMP + timedelta(seconds=1),
            recorded_at=STAMP + timedelta(seconds=1),
        )
    )
    component = InventoryComponent(
        component_id="root",
        idempotency_key="root",
        snapshot_id="inventory",
        component_key="root",
        component_kind="primary",
        source_url=metadata_url,
        source_observation_id="metadata",
        outcome="succeeded",
        required=True,
        ordinal=0,
        recorded_at=STAMP,
    )
    components = [component]
    details: dict[str, str] = {}
    if reviewed:
        review = json.dumps(
            {
                "schema_version": "sec_package_subject_review.v1",
                "cik": "0000000001",
                "ticker": "SYNTH",
                "reviewer": "synthetic-reviewer",
                "reviewed_at": STAMP.isoformat(),
                "subjects": [
                    {
                        "accession_number": ACCESSION,
                        "source_url": BASE + "primary.htm",
                        "source_sha256": hashlib.sha256(document_body).hexdigest(),
                        "raw_path": "primary.htm",
                        "document_type": kind,
                        "heading_selector": "h1#period",
                        "heading_text": heading,
                        "period_end": "2026-06-30",
                        "period_end_text": "June 30, 2026",
                        "rationale": "The exact raw heading identifies the financial reporting quarter.",
                    }
                ],
            }
        ).encode()
        review_path = tmp_path / "review.json"
        review_path.write_bytes(review)
        source = _source(
            "review",
            review,
            metadata_url + "#reviewed-package-subjects",
            "sec_package_subject_review",
        ).model_copy(update={"collector_code_version": "sec_package_subject_review.v1"})
        ledger.persist(
            ContentBlob(
                sha256=source.blob_sha256,
                byte_size=len(review),
                media_type="application/json",
                storage_uri=review_path.as_uri(),
                recorded_at=STAMP,
            )
        )
        ledger.persist(source)
        components.append(
            InventoryComponent(
                component_id="subject-review",
                idempotency_key="subject-review",
                snapshot_id="inventory",
                component_key="reviewed-package-subjects",
                component_kind="other",
                source_url=source.source_url,
                source_observation_id=source.observation_id,
                outcome="succeeded",
                required=True,
                ordinal=1,
                recorded_at=STAMP,
            )
        )
        details = {
            "subject_review_sha256": source.blob_sha256,
            "subject_source_sha256": hashlib.sha256(document_body).hexdigest(),
            "subject_reviewer": "synthetic-reviewer",
            "subject_reviewed_at": STAMP.isoformat(),
            "subject_heading_selector": "h1#period",
            "subject_heading_text": heading,
            "subject_period_end_text": "June 30, 2026",
            "subject_rationale": "The exact raw heading identifies the financial reporting quarter.",
        }
    if review_mode == "imported_review":
        details = {"imported_detail_" + key: value for key, value in details.items()}
    elif review_mode == "conflicting_review":
        details["imported_detail_subject_reviewer"] = "different-synthetic-reviewer"
    seals = SourceInventorySealStore(conn)
    for entry in components:
        seals.persist(entry)
    seals.persist(
        InventorySeal(
            snapshot_id="inventory",
            expected_component_count=len(components),
            component_digest_sha256=component_digest(tuple(components)),
            completion_status="complete",
            sealed_at=STAMP + timedelta(seconds=1),
        )
    )
    coverage.persist(
        ExpectedDocument(
            expected_document_id="primary",
            idempotency_key="primary",
            snapshot_id="inventory",
            expected_document_key="issuer-1:original-primary",
            issuer_id="issuer-1",
            ticker="SYNTH",
            source_kind="sec_filing",
            document_type=kind,
            form_type=form,
            accession_number=ACCESSION,
            source_url=BASE + "primary.htm",
            primary_document="primary.htm",
            period_end=period,
            filing_at=datetime(2026, 2, 10, tzinfo=UTC),
            expectation_basis="authoritative",
            recorded_at=STAMP + timedelta(seconds=1),
        )
    )
    if reviewed:
        coverage.persist(
            CoverageAssessment(
                assessment_id="reviewed-available",
                idempotency_key="reviewed-available",
                expected_document_id="primary",
                revision=1,
                coverage_status="available",
                reason_code="reviewed_source_available",
                reason_details=tuple(sorted(details.items())),
                decision_kind="imported",
                policy_name="synthetic_review",
                policy_version="1",
                policy_config_sha256=CONFIG,
                effective_at=STAMP + timedelta(seconds=1),
                knowledge_at=STAMP + timedelta(seconds=1),
                recorded_at=STAMP + timedelta(seconds=1),
                material_dissent=False,
            )
        )
    conn.commit()
    raw = (
        (
            f"<SEC-DOCUMENT>{ACCESSION}.txt\n<SEC-HEADER>header\n"
            "<ACCEPTANCE-DATETIME>20260210163039\n"
            f"ACCESSION NUMBER: {ACCESSION}\nCONFORMED SUBMISSION TYPE: {form}\n"
            "FILER:\n COMPANY DATA:\n  CENTRAL INDEX KEY: 0000000001\n</SEC-HEADER>\n"
            f"<DOCUMENT>\n<TYPE>{form}\n<SEQUENCE>1\n<FILENAME>primary.htm\n<TEXT>\n"
        ).encode()
        + document_body
        + b"</TEXT>\n</DOCUMENT>\n</SEC-DOCUMENT>\n"
    )
    parent_path = tmp_path / "parent.txt"
    parent_path.write_bytes(raw)
    parent = _source("actual-parent", raw, BASE + ACCESSION + ".txt", "sec_submission")
    blob = ContentBlob(
        sha256=parent.blob_sha256,
        byte_size=len(raw),
        media_type="text/plain",
        storage_uri=parent_path.as_uri(),
        recorded_at=parent.retrieved_at,
    )
    candidates, truncated = load_expected_sec_documents(
        conn, inventory_keys=("synthetic:sec",), limit=10
    )
    assert not truncated and len(candidates) == 1
    import_request = RetainedSubmissionImportRequest(
        parent_observation=parent,
        parent_blob=blob,
        cik="1",
        expected_documents=candidates,
        content_roots=(tmp_path,),
        blob_root=tmp_path / "blobs",
        receipt_root=tmp_path / "receipts",
        imported_at=STAMP + timedelta(seconds=2),
    )
    try:
        yield conn, import_request
    finally:
        conn.close()


def test_dry_run_has_no_database_or_file_writes(
    retained: tuple[sqlite3.Connection, RetainedSubmissionImportRequest],
) -> None:
    conn, request = retained
    before = conn.total_changes
    result = import_sec_submission_members(conn, request)
    assert not result.applied and result.records_created == 0
    assert conn.total_changes == before and not conn.in_transaction
    assert not request.blob_root.exists() and not request.receipt_root.exists()
    assert result.receipt.package_completeness == "not_assessed"
    with pytest.raises(SecSubmissionImportError, match="missing"):
        verify_sec_submission_import(conn, request, result.receipt)


def test_native_import_replay_preserves_parent_and_exact_original_bytes(
    retained: tuple[sqlite3.Connection, RetainedSubmissionImportRequest],
) -> None:
    conn, request = retained
    dry = import_sec_submission_members(conn, request)
    applied = import_sec_submission_members(conn, request.model_copy(update={"apply": True}))
    assert applied.receipt == dry.receipt and applied.records_created > 0
    assert applied.receipt.acceptance_datetime_raw == "20260210163039"
    assert applied.receipt.acceptance_clock_basis == "sec_asserted_eastern_wall_clock"
    member = applied.receipt.members[0]
    raw = (request.blob_root / member.span.text_sha256[:2] / member.span.text_sha256).read_bytes()
    assert raw == BODY
    row = conn.execute(
        "SELECT source_kind,source_url,collector_code_version,observed_at,retrieved_at FROM evidence_source_observations WHERE observation_id=?",
        (member.child_observation_id,),
    ).fetchone()
    assert row[:3] == ("sec_filing", BASE + "primary.htm", "sec-submission-member-import@1")
    assert datetime.fromisoformat(row[3]) == request.parent_observation.observed_at
    assert datetime.fromisoformat(row[4]) == request.parent_observation.retrieved_at
    assert conn.execute(
        "SELECT recorded_at FROM evidence_document_versions WHERE document_version_id=?",
        (member.document_version_id,),
    ).fetchone()[0] == str(request.imported_at)
    before = conn.total_changes
    assert verify_sec_submission_import(conn, request, applied.receipt) == applied.receipt
    replay = import_sec_submission_members(conn, request.model_copy(update={"apply": True}))
    assert replay.receipt == applied.receipt and replay.records_created == 0
    assert conn.total_changes == before
    assert load_expected_sec_documents(conn, inventory_keys=("synthetic:sec",), limit=10)[0] == ()


@pytest.mark.parametrize(
    "damage", ["hash", "cik", "filename", "url", "form", "generated", "parent_url", "span"]
)
def test_wrong_parent_or_original_identity_is_rejected_without_partial_rows(
    retained: tuple[sqlite3.Connection, RetainedSubmissionImportRequest],
    damage: str,
) -> None:
    conn, request = retained
    if damage == "hash":
        path = Path(request.parent_blob.storage_uri.removeprefix("file://"))
        path.write_bytes(path.read_bytes() + b"changed")
    elif damage == "cik":
        request = request.model_copy(update={"cik": "2"})
    elif damage == "parent_url":
        request = request.model_copy(
            update={
                "parent_observation": request.parent_observation.model_copy(
                    update={"source_url": BASE + "wrong.txt"}
                )
            }
        )
    elif damage == "span":
        request = request.model_copy(update={"imported_at": STAMP})
    else:
        key, value = {
            "filename": ("primary_document", "missing.htm"),
            "url": ("source_url", BASE + "other.htm"),
            "form": ("form_type", "6-K"),
            "generated": ("document_type", "sec_financial_report"),
        }[damage]
        request = request.model_copy(
            update={
                "expected_documents": (
                    request.expected_documents[0].model_copy(update={key: value}),
                )
            }
        )
    before = conn.total_changes
    with pytest.raises((ValueError, RuntimeError)):
        import_sec_submission_members(conn, request.model_copy(update={"apply": True}))
    assert conn.total_changes == before and not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM evidence_document_versions").fetchone()[0] == 0


def test_idle_connection_and_verified_native_receipt_are_required(
    retained: tuple[sqlite3.Connection, RetainedSubmissionImportRequest],
) -> None:
    conn, request = retained
    conn.execute("BEGIN")
    with pytest.raises(SecSubmissionImportError, match="idle"):
        import_sec_submission_members(conn, request)
    conn.rollback()
    applied = import_sec_submission_members(conn, request.model_copy(update={"apply": True}))
    with pytest.raises(SecSubmissionImportError, match="differs"):
        verify_sec_submission_import(
            conn, request, applied.receipt.model_copy(update={"index_sha256": "f" * 64})
        )
    applied.receipt_path.write_bytes(b"tampered receipt")
    with pytest.raises(SecSubmissionImportError, match="receipt differs"):
        verify_sec_submission_import(conn, request, applied.receipt)


def test_mid_batch_failure_rolls_back_admission_and_reuses_exact_retained_bytes(
    retained: tuple[sqlite3.Connection, RetainedSubmissionImportRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, request = retained
    original = EvidenceLedger.persist

    def fail_document(self: EvidenceLedger, record: LedgerRecord) -> PersistResult:
        if isinstance(record, DocumentVersion):
            raise ValueError("synthetic mid-batch admission failure")
        return original(self, record)

    monkeypatch.setattr(EvidenceLedger, "persist", fail_document)
    with pytest.raises(ValueError, match="mid-batch"):
        import_sec_submission_members(conn, request.model_copy(update={"apply": True}))
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM evidence_document_versions").fetchone()[0] == 0
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM evidence_source_observations WHERE observation_id='actual-parent'"
        ).fetchone()[0]
        == 0
    )
    assert not request.receipt_root.exists()
    monkeypatch.setattr(EvidenceLedger, "persist", original)
    recovered = import_sec_submission_members(conn, request.model_copy(update={"apply": True}))
    assert verify_sec_submission_import(conn, request, recovered.receipt) == recovered.receipt


def test_changed_child_bytes_fail_native_containment_replay(
    retained: tuple[sqlite3.Connection, RetainedSubmissionImportRequest],
) -> None:
    conn, request = retained
    applied = import_sec_submission_members(conn, request.model_copy(update={"apply": True}))
    member = applied.receipt.members[0]
    child = request.blob_root / member.span.text_sha256[:2] / member.span.text_sha256
    child.write_bytes(BODY + b"changed")
    with pytest.raises(SecSubmissionImportError, match="child bytes differ"):
        verify_sec_submission_import(conn, request, applied.receipt)


@pytest.mark.parametrize("retained", [True, "imported_review"], indirect=True)
def test_reviewed_subject_witness_details_survive_original_member_capture(
    retained: tuple[sqlite3.Connection, RetainedSubmissionImportRequest],
) -> None:
    conn, request = retained
    before = json.loads(
        conn.execute(
            "SELECT reason_details_json FROM source_coverage_assessments WHERE assessment_id='reviewed-available'"
        ).fetchone()[0]
    )
    request = request.model_copy(update={"apply": True})
    imported = import_sec_submission_members(conn, request)
    member = imported.receipt.members[0]
    after = json.loads(
        conn.execute(
            "SELECT reason_details_json FROM v_source_coverage_current WHERE expected_document_id='primary'"
        ).fetchone()[0]
    )
    assert {key: value for key, value in after.items() if key in before} == before
    verify_sec_submission_import(conn, request, imported.receipt)
    verify_sec_package_subject_witness(
        conn,
        identity="primary",
        issuer_id="issuer-1",
        document_version_ids=(member.document_version_id,),
        kind="financial_statement",
        period=datetime(2026, 6, 30, tzinfo=UTC).isoformat(" "),
        accession=ACCESSION,
        source_url=BASE + "primary.htm",
        cutoff=request.imported_at,
        observed_through=request.imported_at,
    )


@pytest.mark.parametrize("retained", ["conflicting_review"], indirect=True)
def test_member_import_preserves_conflicting_review_fields_for_native_rejection(
    retained: tuple[sqlite3.Connection, RetainedSubmissionImportRequest],
) -> None:
    conn, request = retained
    imported = import_sec_submission_members(conn, request.model_copy(update={"apply": True}))
    verify_sec_submission_import(conn, request, imported.receipt)
    with pytest.raises(ValueError, match="subject_review_conflict"):
        verify_sec_package_subject_witness(
            conn,
            identity="primary",
            issuer_id="issuer-1",
            document_version_ids=(imported.receipt.members[0].document_version_id,),
            kind="financial_statement",
            period=datetime(2026, 6, 30, tzinfo=UTC).isoformat(" "),
            accession=ACCESSION,
            source_url=BASE + "primary.htm",
            cutoff=request.imported_at,
            observed_through=request.imported_at,
        )
