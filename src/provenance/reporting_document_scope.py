"""One read-only, dual-clock investor-reporting projection for all readers.

Acquisition inventories retain every expected package artifact. Reporting scope
excludes only explicit policy dispositions; unresolved expectations stay visible.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from filings.sec_submissions_inventory import SEC_REGISTRATION_FINANCIAL_FORMS
from provenance.sec_package_subject_witness import (
    SecPackageSubjectWitnessError,
    verify_sec_package_subject_witness,
)

REPORTING_DOCUMENT_SELECTION_POLICY = "document-processing-terminal-at-k-observed-through-o.v2"


class ReportingDocumentDecision(BaseModel):
    """One explicit keep, safe exclusion, or unresolved classification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_document_id: str
    issuer_id: str
    outcome: Literal["governed_reporting", "excluded_supporting", "unresolved"]
    reason_code: str
    document_family: str | None = None
    coverage_status: str
    document_version_id: str | None = None
    reporting_entity_id: str | None = None


def reporting_document_scope(
    conn: sqlite3.Connection,
    cutoff: datetime,
    observed_through: datetime,
    *,
    inventory_snapshot_ids: tuple[str, ...] | None = None,
) -> tuple[
    tuple[ReportingDocumentDecision, ...],
    dict[str, tuple[str, ...]],
    int,
]:
    if inventory_snapshot_ids is not None and not inventory_snapshot_ids:
        raise ValueError("reporting projection requires at least one inventory snapshot")
    snapshot_ids_json = (
        json.dumps(inventory_snapshot_ids) if inventory_snapshot_ids is not None else None
    )
    cursor = conn.cursor()
    cursor.row_factory = sqlite3.Row
    rows = cursor.execute(
        "SELECT expected.expected_document_id,expected.issuer_id,"
        "expected.source_kind,expected.document_type,expected.form_type,"
        "coverage.document_version_id,"
        "COALESCE(coverage.coverage_status,'unassessed'),"
        "canonical.reporting_entity_id,lifecycle.status,"
        "lifecycle.expected_document_id,expected.period_end,expected.accession_number,expected.source_url "
        "FROM expected_documents expected "
        "JOIN source_inventory_snapshots inventory "
        "ON inventory.snapshot_id=expected.snapshot_id "
        "LEFT JOIN expected_document_lifecycle_revisions lifecycle "
        "ON lifecycle.inventory_key=inventory.inventory_key "
        "AND lifecycle.expected_document_key=expected.expected_document_key "
        "AND datetime(lifecycle.knowledge_at)<=datetime(?) "
        "AND datetime(lifecycle.recorded_at)<=datetime(?) "
        "AND NOT EXISTS (SELECT 1 FROM expected_document_lifecycle_revisions newer_lifecycle "
        "WHERE newer_lifecycle.inventory_key=lifecycle.inventory_key "
        "AND newer_lifecycle.expected_document_key=lifecycle.expected_document_key "
        "AND newer_lifecycle.revision>lifecycle.revision "
        "AND datetime(newer_lifecycle.knowledge_at)<=datetime(?) "
        "AND datetime(newer_lifecycle.recorded_at)<=datetime(?)) "
        "LEFT JOIN source_coverage_assessments coverage "
        "ON coverage.expected_document_id=expected.expected_document_id "
        "AND datetime(coverage.knowledge_at)<=datetime(?) "
        "AND datetime(coverage.recorded_at)<=datetime(?) "
        "AND NOT EXISTS (SELECT 1 FROM source_coverage_assessments newer "
        "WHERE newer.expected_document_id=coverage.expected_document_id "
        "AND newer.revision>coverage.revision "
        "AND datetime(newer.knowledge_at)<=datetime(?) "
        "AND datetime(newer.recorded_at)<=datetime(?)) "
        "LEFT JOIN v_evidence_document_versions_canonical canonical "
        "ON canonical.document_version_id=coverage.document_version_id "
        "WHERE datetime(expected.recorded_at)<=datetime(?) "
        "AND (? IS NULL OR expected.snapshot_id IN (SELECT value FROM json_each(?))) "
        "ORDER BY expected.issuer_id,expected.expected_document_key",
        (
            _db_time(cutoff),
            _db_time(observed_through),
            _db_time(cutoff),
            _db_time(observed_through),
            _db_time(cutoff),
            _db_time(observed_through),
            _db_time(cutoff),
            _db_time(observed_through),
            _db_time(observed_through),
            snapshot_ids_json,
            snapshot_ids_json,
        ),
    ).fetchall()
    grouped: dict[str, list[str]] = {}
    decisions: list[ReportingDocumentDecision] = []
    for row in rows:
        lifecycle_status = None if row[8] is None else str(row[8])
        lifecycle_expected_id = None if row[9] is None else str(row[9])
        if lifecycle_status is None:
            outcome: Literal["governed_reporting", "excluded_supporting", "unresolved"] = (
                "unresolved"
            )
            family = None
            reason = "expected_document_lifecycle_missing"
        elif lifecycle_status != "expected" or lifecycle_expected_id != str(row[0]):
            outcome = "excluded_supporting"
            family = None
            reason = "expected_document_not_current"
        else:
            outcome, family, reason = classify_reporting_document(
                source_kind=str(row["source_kind"]),
                document_type=str(row["document_type"]),
                form_type=None if row["form_type"] is None else str(row["form_type"]),
            )
        if reason == "reviewed_sec_package_subject":
            try:
                verify_sec_package_subject_witness(
                    conn,
                    identity=str(row[0]),
                    issuer_id=str(row[1]),
                    kind=str(row["document_type"]),
                    period=row[10],
                    accession=None if row[11] is None else str(row[11]),
                    source_url=row[12],
                    cutoff=cutoff,
                    observed_through=observed_through,
                )
            except (SecPackageSubjectWitnessError, ValueError, sqlite3.Error) as exc:
                outcome, family = "unresolved", None
                reason = (
                    str(exc)
                    if isinstance(exc, SecPackageSubjectWitnessError)
                    else "financial_reporting_subject_witness_invalid"
                )
        coverage_status = str(row[6])
        document_version_id = None if row[5] is None else str(row[5])
        reporting_entity_id = None if row[7] is None else str(row[7])
        decision = ReportingDocumentDecision(
            expected_document_id=str(row[0]),
            issuer_id=str(row[1]),
            outcome=outcome,
            reason_code=reason,
            document_family=family,
            coverage_status=coverage_status,
            document_version_id=document_version_id,
            reporting_entity_id=reporting_entity_id,
        )
        decisions.append(decision)
        if outcome != "governed_reporting":
            continue
        if coverage_status not in {"captured", "extracted", "indexed"}:
            continue
        if document_version_id is None:
            raise ValueError("positive source coverage has no document version")
        if reporting_entity_id is None:
            raise ValueError("governed reporting document lacks a canonical reporting entity")
        grouped.setdefault(str(row[1]), []).append(document_version_id)
    incomplete_inventory_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM source_inventory_snapshots inventory "
            "WHERE datetime(inventory.recorded_at)<=datetime(?) "
            "AND NOT EXISTS (SELECT 1 FROM source_inventory_snapshots newer "
            "WHERE newer.inventory_key=inventory.inventory_key "
            "AND newer.revision>inventory.revision "
            "AND datetime(newer.recorded_at)<=datetime(?)) "
            "AND NOT EXISTS (SELECT 1 FROM v_source_inventory_sealed_complete complete "
            "WHERE complete.snapshot_id=inventory.snapshot_id) "
            "AND (? IS NULL OR inventory.snapshot_id IN (SELECT value FROM json_each(?)))",
            (
                _db_time(observed_through),
                _db_time(observed_through),
                snapshot_ids_json,
                snapshot_ids_json,
            ),
        ).fetchone()[0]
    )
    return (
        tuple(decisions),
        {
            issuer_id: tuple(sorted(set(document_ids)))
            for issuer_id, document_ids in sorted(grouped.items())
        },
        incomplete_inventory_count,
    )


def classify_reporting_document(
    *,
    source_kind: str,
    document_type: str,
    form_type: str | None,
) -> tuple[
    Literal["governed_reporting", "excluded_supporting", "unresolved"],
    str | None,
    str,
]:
    """Classify the governed investor-reporting surface without deleting inventory."""

    source = source_kind.strip().lower()
    document = document_type.strip().lower()
    form = (form_type or "").strip().upper()
    if source == "sec_filing":
        if document in {
            "financial_statement",
            "supplement",
            "earnings_release",
            "investor_presentation",
            "investor_update",
        }:
            if form in {"6-K", "6-K/A"}:
                return (
                    "governed_reporting",
                    "continuous_disclosure",
                    "reviewed_sec_package_subject",
                )
            return "unresolved", None, "reviewed_sec_subject_form_mismatch"
        if document == "sec_financial_report":
            return "excluded_supporting", None, "sec_xbrl_report_attachment"
        if document != "filing":
            return "excluded_supporting", None, "sec_supporting_artifact"
        if form in {
            "10-K",
            "10-K/A",
            "10-Q",
            "10-Q/A",
            "20-F",
            "20-F/A",
            "40-F",
            "40-F/A",
        }:
            return (
                "governed_reporting",
                "operating_company_periodic",
                "governed_periodic_filing",
            )
        if form in {"6-K", "6-K/A", "8-K", "8-K/A"}:
            return (
                "governed_reporting",
                "continuous_disclosure",
                "governed_current_report",
            )
        if form in SEC_REGISTRATION_FINANCIAL_FORMS:
            return (
                "governed_reporting",
                "issuer_financial_statements",
                "governed_registration_financial_package",
            )
        return "excluded_supporting", None, "sec_form_outside_reporting_policy"
    if source == "earnings_call":
        if document in {
            "earnings_call",
            "earnings_call_transcript",
            "earnings_transcript",
            "transcript",
        }:
            return (
                "governed_reporting",
                "issuer_earnings_materials",
                "governed_earnings_call_transcript",
            )
        return "unresolved", None, "unclassified_earnings_call_artifact"
    if source != "ir_document":
        return "unresolved", None, "unknown_expected_document_source_kind"
    ir_families = {
        "annual_report": "issuer_financial_statements",
        "financial_statement": "issuer_financial_statements",
        "supplement": "issuer_financial_statements",
        "earnings_material": "issuer_earnings_materials",
        "earnings_release": "issuer_earnings_materials",
        "press_release": "issuer_earnings_materials",
        "earnings_transcript": "issuer_earnings_materials",
        "transcript": "issuer_earnings_materials",
        "investor_presentation": "issuer_presentations",
        "presentation": "issuer_presentations",
        "investor_update": "issuer_presentations",
    }
    family = ir_families.get(document)
    if family is None:
        return "unresolved", None, "unclassified_ir_reporting_document"
    return "governed_reporting", family, "governed_ir_reporting_document"


def _db_time(value: datetime) -> str:
    utc = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return utc.isoformat()
