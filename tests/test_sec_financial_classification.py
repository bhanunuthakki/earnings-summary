"""Review classification cannot substitute SEC event dates for fiscal periods."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from filings.sec_financial_classification import (
    SecPackageSubjectManifest,
    classify_expected_package_subjects,
    load_package_subject_reviews,
)
from provenance.population_document_processing import classify_reporting_document
from provenance.source_coverage_reconcile import ExpectedDocumentImport, ExplicitAbsence

URL = "https://www.sec.gov/Archives/edgar/data/1001/000000100126000001/results.htm"
AT = datetime(2026, 8, 12, tzinfo=UTC)
HEADING = "Condensed consolidated financial statements for the six months ended June 30, 2026"


def _review(tmp_path: Path) -> Path:
    body = f'<html><body><h1 id="period">{HEADING}</h1></body></html>'.encode()
    (tmp_path / "results.htm").write_bytes(body)
    path = tmp_path / "review.json"
    path.write_text(
        json.dumps(
            {
                "cik": "0000001001",
                "ticker": "ACME",
                "reviewer": "analyst",
                "reviewed_at": AT.isoformat(),
                "subjects": [
                    {
                        "accession_number": "0000001001-26-000001",
                        "source_url": URL,
                        "source_sha256": hashlib.sha256(body).hexdigest(),
                        "raw_path": "results.htm",
                        "document_type": "financial_statement",
                        "heading_selector": "h1#period",
                        "heading_text": HEADING,
                        "period_end": "2026-06-30",
                        "period_end_text": "June 30, 2026",
                        "rationale": "The heading identifies interim financial statements and their fiscal period.",
                    }
                ],
            }
        )
    )
    return path


def _expected() -> ExpectedDocumentImport:
    return ExpectedDocumentImport(
        expected_document_key="issuer:acme:0000001001-26-000001:attachment:results",
        source_kind="sec_filing",
        document_type="sec_exhibit",
        form_type="6-K",
        accession_number="0000001001-26-000001",
        source_url=URL,
        primary_document="results.htm",
        filing_at=AT,
        expectation_basis="authoritative",
        absence=ExplicitAbsence(
            coverage_status="available",
            reason_code="sec_authority_package_inventory",
            reason_details=(("attachment_id", "results"),),
        ),
    )


def test_review_creates_native_financial_subject_with_source_fiscal_period(tmp_path: Path) -> None:
    review, digest = load_package_subject_reviews(
        _review(tmp_path), cik="0000001001", ticker="ACME", as_of=AT
    )
    before = _expected()
    (after,) = classify_expected_package_subjects((before,), review, manifest_sha256=digest)
    assert before.document_type == "sec_exhibit"
    assert before.period_end is None
    assert after.source_kind == "sec_filing"
    assert after.document_type == "financial_statement"
    assert after.period_end == datetime(2026, 6, 30, tzinfo=UTC)
    assert after.filing_at == AT
    assert after.absence is not None
    assert dict(after.absence.reason_details)["subject_review_sha256"] == digest
    assert classify_reporting_document(
        source_kind=after.source_kind, document_type=after.document_type, form_type=after.form_type
    ) == ("governed_reporting", "continuous_disclosure", "reviewed_sec_package_subject")


@pytest.mark.parametrize("failure", ["bytes", "heading", "period", "issuer", "future"])
def test_review_rejects_changed_or_misattributed_witness(tmp_path: Path, failure: str) -> None:
    path = _review(tmp_path)
    payload = json.loads(path.read_text())
    subject = payload["subjects"][0]
    if failure == "bytes":
        (tmp_path / "results.htm").write_text("changed financial statement")
    elif failure == "heading":
        subject["heading_text"] = "Wrong heading for June 30, 2026 fiscal period"
    elif failure == "period":
        subject["period_end"] = "2026-08-12"
    elif failure == "issuer":
        payload["cik"] = "0000001002"
    else:
        payload["reviewed_at"] = "2026-08-13T00:00:00Z"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        load_package_subject_reviews(path, cik="0000001001", ticker="ACME", as_of=AT)


def test_review_requires_exact_authoritative_package_member(tmp_path: Path) -> None:
    review, digest = load_package_subject_reviews(
        _review(tmp_path), cik="0000001001", ticker="ACME", as_of=AT
    )
    with pytest.raises(ValueError, match="not_in_authoritative_inventory"):
        classify_expected_package_subjects((), review, manifest_sha256=digest)
    with pytest.raises(ValueError, match="not_authoritative_six_k_member"):
        classify_expected_package_subjects(
            (_expected().model_copy(update={"form_type": "8-K"}),), review, manifest_sha256=digest
        )


def test_investor_presentation_cannot_assert_a_financial_period(tmp_path: Path) -> None:
    payload = json.loads(_review(tmp_path).read_text())
    payload["subjects"][0]["document_type"] = "investor_presentation"
    with pytest.raises(ValueError, match="presentation_cannot_assert_financial_period"):
        SecPackageSubjectManifest.model_validate(payload)


def test_inline_xbrl_source_requires_exact_escaped_namespace_selector(tmp_path: Path) -> None:
    path = _review(tmp_path)
    body = f'<html><body><ix:continuation><h1 id="period">{HEADING}</h1></ix:continuation></body></html>'.encode()
    (tmp_path / "results.htm").write_bytes(body)
    payload = json.loads(path.read_text())
    subject = payload["subjects"][0]
    subject["source_sha256"] = hashlib.sha256(body).hexdigest()
    subject["heading_selector"] = r"ix\:continuation > h1#period"
    path.write_text(json.dumps(payload))
    manifest, _ = load_package_subject_reviews(path, cik="0000001001", ticker="ACME", as_of=AT)
    assert len(manifest.subjects) == 1
    subject["heading_selector"] = "ix:continuation > h1#period"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        load_package_subject_reviews(path, cik="0000001001", ticker="ACME", as_of=AT)
