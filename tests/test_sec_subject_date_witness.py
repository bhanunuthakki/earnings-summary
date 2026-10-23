"""Exact IFRS date wording remains source evidence in reviewed SEC subjects."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from filings.sec_financial_classification import (
    SecPackageSubjectManifest,
    classify_expected_package_subjects,
    load_package_subject_reviews,
)
from provenance.source_coverage_reconcile import ExpectedDocumentImport, ExplicitAbsence

AT = datetime(2026, 8, 12, tzinfo=UTC)
URL = "https://www.sec.gov/Archives/edgar/data/1001/000000100126000001/results.htm"


def _review(tmp_path: Path) -> Path:
    heading = "Condensed consolidated financial statements for the six months ended June 30, 2026"
    body = f'<html><body><h1 id="period">{heading}</h1></body></html>'.encode()
    (tmp_path / "results.htm").write_bytes(body)
    payload = {
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
                "heading_text": heading,
                "period_end": "2026-06-30",
                "period_end_text": "June 30, 2026",
                "rationale": "The heading identifies interim financial statements and their fiscal period.",
            }
        ],
    }
    path = tmp_path / "review.json"
    path.write_text(json.dumps(payload))
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


def _dated_review(tmp_path: Path, period_text: str, period_end: str) -> Path:
    path = _review(tmp_path)
    payload = json.loads(path.read_text())
    subject = payload["subjects"][0]
    heading = "Managerial profit and loss reconciliation for the period ended " + period_text
    body = f'<html><body><h1 id="period">{heading}</h1></body></html>'.encode()
    (tmp_path / "results.htm").write_bytes(body)
    subject.update(
        document_type="supplement",
        source_sha256=hashlib.sha256(body).hexdigest(),
        heading_text=heading,
        period_end=period_end,
        period_end_text=period_text,
    )
    path.write_text(json.dumps(payload))
    return path


@pytest.mark.parametrize(
    "period_text,period_end",
    [("31 March 2026", "2026-03-31"), ("30 June 2026", "2026-06-30")],
)
def test_day_first_date_admits_only_its_exact_reported_period(
    tmp_path: Path, period_text: str, period_end: str
) -> None:
    path = _dated_review(tmp_path, period_text, period_end)
    manifest, digest = load_package_subject_reviews(path, cik="0000001001", ticker="ACME", as_of=AT)
    subject = manifest.subjects[0]
    assert subject.period_end == date.fromisoformat(period_end)
    assert subject.period_end_text == period_text
    assert subject.heading_text.endswith(period_text)
    assert subject.model_dump(mode="json")["period_end_text"] == period_text
    (expected,) = classify_expected_package_subjects(
        (_expected(),), manifest, manifest_sha256=digest
    )
    assert expected.period_end is not None and expected.period_end.date() == subject.period_end
    assert expected.absence is not None
    assert dict(expected.absence.reason_details)["subject_period_end_text"] == period_text


@pytest.mark.parametrize("period_text", ["31 March 2026", "30 June 2026", "June 30, 2026"])
def test_wrong_iso_date_cannot_override_literal_period_text(
    tmp_path: Path, period_text: str
) -> None:
    path = _dated_review(tmp_path, period_text, "2026-02-28")
    with pytest.raises(ValueError, match="period_witness_mismatch"):
        SecPackageSubjectManifest.model_validate_json(path.read_text())


@pytest.mark.parametrize("period_text", ["30/06/2026", "06/30/2026", "30 Jun 2026", "31 June 2026"])
def test_numeric_abbreviated_and_impossible_dates_remain_rejected(
    tmp_path: Path, period_text: str
) -> None:
    path = _dated_review(tmp_path, period_text, "2026-06-30")
    with pytest.raises(ValueError, match="period_text_invalid"):
        SecPackageSubjectManifest.model_validate_json(path.read_text())


def test_original_month_first_subject_serialization_remains_unchanged(tmp_path: Path) -> None:
    payload = json.loads(_review(tmp_path).read_text())
    expected = {"schema_version": "sec_package_subject_review.v1", **payload}
    expected["reviewed_at"] = "2026-08-12T00:00:00Z"
    before = SecPackageSubjectManifest.model_validate(payload).model_dump_json()
    assert before == json.dumps(expected, separators=(",", ":"))
    assert SecPackageSubjectManifest.model_validate_json(before).model_dump_json() == before
    assert '"period_end_text":"June 30, 2026"' in before
    assert payload["subjects"][0]["heading_text"] in before
