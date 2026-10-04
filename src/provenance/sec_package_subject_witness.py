"""Shared captured-source witness for reviewed native SEC package subjects.

A document-type label does not prove a financial period or reviewed source.
This read-only check binds retained review bytes to the exact captured version.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import cast

from provenance.issuer_registry import evidence_document_relation


class SecPackageSubjectWitnessError(ValueError):
    """Stable failure to reconstruct a reviewed SEC subject witness."""


def _time(raw: object) -> datetime:
    parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def verify_sec_package_subject_witness(
    conn: sqlite3.Connection,
    *,
    identity: str,
    issuer_id: str,
    document_version_ids: tuple[str, ...] | None = None,
    kind: str,
    period: object,
    accession: str | None,
    source_url: object,
    cutoff: datetime,
    observed_through: datetime | None = None,
) -> None:
    observed = cutoff if observed_through is None else observed_through
    expected = conn.execute(
        "SELECT issuer_id,expectation_basis,source_kind,form_type,document_type,"
        "period_end,accession_number,source_url FROM expected_documents WHERE expected_document_id=?",
        (identity,),
    ).fetchone()
    if (
        expected is None
        or str(expected[0]) != issuer_id
        or str(expected[1]) != "authoritative"
        or str(expected[2]) != "sec_filing"
        or str(expected[3]) not in {"6-K", "6-K/A"}
        or str(expected[4]) != kind
        or expected[5] != period
        or expected[6] != accession
        or expected[7] != source_url
    ):
        raise SecPackageSubjectWitnessError("financial_reporting_subject_identity_invalid")
    assessment = conn.execute(
        "SELECT coverage_status,document_version_id,reason_details_json,recorded_at "
        "FROM source_coverage_assessments WHERE expected_document_id=? "
        "AND datetime(knowledge_at)<=datetime(?) AND datetime(recorded_at)<=datetime(?) "
        "ORDER BY revision DESC LIMIT 1",
        (identity, cutoff.isoformat(), observed.isoformat()),
    ).fetchone()
    if (
        assessment is None
        or str(assessment[0]) not in {"captured", "extracted", "indexed"}
        or (document_version_ids is not None and str(assessment[1]) not in document_version_ids)
    ):
        raise SecPackageSubjectWitnessError("financial_reporting_subject_capture_unavailable")
    raw_details = json.loads(str(assessment[2]))
    if not isinstance(raw_details, dict):
        raise SecPackageSubjectWitnessError("financial_reporting_subject_review_invalid")
    details = cast(dict[str, object], raw_details)

    def detail(key: str) -> str:
        direct, imported = details.get(key), details.get("imported_detail_" + key)
        if direct is not None and imported is not None and direct != imported:
            raise SecPackageSubjectWitnessError("financial_reporting_subject_review_conflict")
        value = direct if direct is not None else imported
        if not isinstance(value, str) or not value.strip():
            raise SecPackageSubjectWitnessError("financial_reporting_subject_review_missing")
        return value

    source_sha = detail("subject_source_sha256")
    review_sha = detail("subject_review_sha256")
    if any(
        len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
        for value in (source_sha, review_sha)
    ):
        raise SecPackageSubjectWitnessError("financial_reporting_subject_review_invalid")
    reviewed_at = _time(detail("subject_reviewed_at"))
    if reviewed_at > cutoff or reviewed_at > _time(assessment[3]):
        raise SecPackageSubjectWitnessError("financial_reporting_subject_review_after_capture")
    detail("subject_reviewer")
    detail("subject_heading_selector")
    heading = detail("subject_heading_text")
    if len(detail("subject_rationale")) < 20:
        raise SecPackageSubjectWitnessError("financial_reporting_subject_review_invalid")
    review_blob = conn.execute(
        "SELECT observation.blob_sha256 FROM expected_documents expected "
        "JOIN source_inventory_components component USING(snapshot_id) "
        "JOIN evidence_source_observations observation "
        "ON observation.observation_id=component.source_observation_id "
        "WHERE expected.expected_document_id=? AND component.component_key='reviewed-package-subjects' "
        "AND component.required=1 AND component.outcome='succeeded' "
        "AND observation.source_kind='sec_package_subject_review' "
        "AND observation.collector_code_version='sec_package_subject_review.v1' "
        "AND datetime(observation.retrieved_at)<=datetime(?) "
        "AND datetime(component.recorded_at)<=datetime(?)",
        (identity, observed.isoformat(), observed.isoformat()),
    ).fetchall()
    if len(review_blob) != 1 or str(review_blob[0][0]) != review_sha:
        raise SecPackageSubjectWitnessError("financial_reporting_subject_review_blob_mismatch")
    if kind in {"investor_presentation", "investor_update"}:
        if period is not None:
            raise SecPackageSubjectWitnessError("financial_reporting_subject_period_mismatch")
    else:
        text = detail("subject_period_end_text")
        parsed = datetime.strptime(text.replace(",", ""), "%B %d %Y").date()
        if period is None or parsed != _time(period).date() or text not in heading:
            raise SecPackageSubjectWitnessError("financial_reporting_subject_period_mismatch")
    relation = evidence_document_relation(conn)
    document = conn.execute(
        f"SELECT document.blob_sha256,document.document_type,document.period_end,"
        f"document.accession_number,document.form_type,document.issuer_id,document.recorded_at,"
        f"observation.source_url,observation.blob_sha256 FROM {relation} document "
        "JOIN evidence_source_observations observation USING(observation_id) "
        "WHERE document.document_version_id=?",
        (str(assessment[1]),),
    ).fetchone()
    if (
        document is None
        or str(document[0]) != source_sha
        or str(document[8]) != source_sha
        or str(document[1]) != kind
        or (None if document[2] is None else _time(document[2]).date())
        != (None if period is None else _time(period).date())
        or document[3] != accession
        or str(document[4]) not in {"6-K", "6-K/A"}
        or str(document[5]) != issuer_id
        or _time(document[6]) > observed
        or document[7] != source_url
    ):
        raise SecPackageSubjectWitnessError("financial_reporting_subject_capture_mismatch")
