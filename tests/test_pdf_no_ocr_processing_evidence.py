"""Native-sufficient PDFs prove OCR is unnecessary without a fictitious engine."""

from __future__ import annotations

import json
import sqlite3

import pytest

from provenance.document_processing_evidence import (
    DocumentProcessingEvidenceIntegrityError,
    DocumentProcessingEvidenceMissingError,
    publish_document_processing_evidence,
    verify_document_processing_evidence,
)
from tests.test_native_pdf_transcript import T0, T1, T2, T3
from tests.test_native_pdf_transcript import native_source as native_source


def _publish(conn: sqlite3.Connection):
    return publish_document_processing_evidence(
        conn,
        document_version_id="native-call",
        processing_lane="pdf_ocr",
        cutoff_at=T3,
        recorded_at=T3,
    )


def _corruptible(conn: sqlite3.Connection, table: str) -> None:
    assert table in {"ocr_preflight_pages", "ocr_document_assessments"}
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)
    ).fetchall():
        conn.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')


def test_native_sufficient_pdf_closes_no_ocr_required_with_exact_native_proof(
    native_source: sqlite3.Connection,
) -> None:
    conn = native_source
    receipt = _publish(conn)
    assert receipt.extraction_run_id == "pdf-run" and receipt.member_count == 2
    assert not conn.execute(
        "SELECT 1 FROM evidence_extraction_runs WHERE extractor_name='governed-pdf-ocr'"
    ).fetchall()
    scope, assessment_id = conn.execute(
        "SELECT native_scope_json,assessment_id FROM document_processing_evidence_headers "
        "WHERE evidence_seal_id=?",
        (receipt.evidence_seal_id,),
    ).fetchone()
    decision = json.loads(scope)["ocr_decision"]
    assert decision["outcome"] == "no_ocr_required" and decision["ocr_performed"] is False
    assert len(decision["preflight"]) == 2 and assessment_id == "pdf-assessment"
    verified = verify_document_processing_evidence(
        conn,
        receipt.evidence_seal_id,
        document_version_id="native-call",
        processing_lane="pdf_ocr",
        cutoff_at=T3,
    )
    assert verified.member_set_sha256 == receipt.member_set_sha256
    assert _publish(conn).exact_replay


def test_required_ocr_without_engine_still_fails(native_source: sqlite3.Connection) -> None:
    conn = native_source
    _corruptible(conn, "ocr_document_assessments")
    conn.execute("UPDATE ocr_document_assessments SET outcome='ocr_required'")
    before = conn.execute("SELECT COUNT(*) FROM document_processing_evidence_headers").fetchone()[0]
    with pytest.raises(
        DocumentProcessingEvidenceMissingError, match="native_extraction_run_missing"
    ):
        _publish(conn)
    assert (
        conn.execute("SELECT COUNT(*) FROM document_processing_evidence_headers").fetchone()[0]
        == before
    )


@pytest.mark.parametrize(
    "tamper", ["requires_ocr", "missing_page", "changed_text_hash", "wrong_input"]
)
def test_no_ocr_proof_rejects_changed_or_incomplete_native_preflight(
    native_source: sqlite3.Connection,
    tamper: str,
) -> None:
    conn = native_source
    receipt = _publish(conn)
    _corruptible(conn, "ocr_preflight_pages")
    _corruptible(conn, "ocr_document_assessments")
    if tamper == "requires_ocr":
        conn.execute("UPDATE ocr_preflight_pages SET requires_ocr=1 WHERE page_number=2")
    elif tamper == "missing_page":
        conn.execute("DELETE FROM ocr_preflight_pages WHERE page_number=2")
    elif tamper == "changed_text_hash":
        conn.execute(
            "UPDATE ocr_preflight_pages SET native_text_sha256=? WHERE page_number=2", ("a" * 64,)
        )
    else:
        conn.execute("UPDATE ocr_document_assessments SET input_sha256=?", ("a" * 64,))
    with pytest.raises(
        (DocumentProcessingEvidenceMissingError, DocumentProcessingEvidenceIntegrityError)
    ):
        verify_document_processing_evidence(
            conn,
            receipt.evidence_seal_id,
            document_version_id="native-call",
            processing_lane="pdf_ocr",
            cutoff_at=T3,
        )


def test_stored_no_ocr_and_pdf_text_proofs_replay_pinned_assessment(
    native_source: sqlite3.Connection,
) -> None:
    conn = native_source
    receipt = _publish(conn)
    conn.execute(
        "INSERT INTO ocr_document_assessments SELECT 'later-native-assessment',"
        "'later-native-assessment',document_version_id,input_sha256,detector_name,"
        "?,detector_code_version,native_output_sha256,page_count,"
        "outcome,reason_code,? FROM ocr_document_assessments WHERE assessment_id='pdf-assessment'",
        ("f" * 64, T2),
    )
    assert (
        verify_document_processing_evidence(
            conn,
            receipt.evidence_seal_id,
            document_version_id="native-call",
            processing_lane="pdf_ocr",
            cutoff_at=T3,
        ).member_set_sha256
        == receipt.member_set_sha256
    )
    pdf_text_id = conn.execute(
        "SELECT evidence_seal_id FROM document_processing_evidence_headers "
        "WHERE processing_lane='pdf_text'"
    ).fetchone()[0]
    assert (
        verify_document_processing_evidence(
            conn,
            pdf_text_id,
            document_version_id="native-call",
            processing_lane="pdf_text",
            cutoff_at=T1,
            observed_through=T3,
        ).member_count
        == 2
    )
    assert (
        conn.execute(
            "SELECT assessed_at FROM ocr_document_assessments WHERE assessment_id='pdf-assessment'"
        )
        .fetchone()[0]
        .startswith(str(T0.date()))
    )


def test_a_new_required_assessment_cannot_authorize_native_no_ocr_admission(
    native_source: sqlite3.Connection,
) -> None:
    conn = native_source
    conn.execute(
        "INSERT INTO ocr_document_assessments SELECT 'required-assessment',"
        "'required-assessment',document_version_id,input_sha256,detector_name,"
        "?,detector_code_version,native_output_sha256,page_count,"
        "'ocr_required',reason_code,? FROM ocr_document_assessments WHERE assessment_id='pdf-assessment'",
        ("f" * 64, T2),
    )
    with pytest.raises(
        DocumentProcessingEvidenceMissingError, match="native_extraction_run_missing"
    ):
        _publish(conn)
