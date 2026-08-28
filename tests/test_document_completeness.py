"""Regressions for IR document extraction completeness selection."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

from execution import extract_kpis_from_ir as extract_ir
from models.facts import Currency, FactLocator, FiscalPeriodType, LocatorKind, Unit
from pipeline.document_completeness import document_completeness
from pipeline.issuer_document_coverage import (
    DownstreamAvailability,
    DownstreamAvailabilityStatus,
    ExpectedIssuerFact,
    ExtractorFactPopulationFrame,
    IssuerDocumentCoverageReceipt,
    IssuerFactCoverageResult,
    IssuerFactKind,
)
from pipeline.issuer_fact_manifest import (
    IssuerFactManifest,
    IssuerFactValue,
    IssuerManifestFactKind,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            source_type TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            period_end TEXT,
            source_url TEXT,
            file_path TEXT,
            fetched_at TEXT NOT NULL
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY,
            source_doc_id INTEGER NOT NULL
        );
        CREATE TABLE issuer_fact_coverage_receipts (
            record_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL,
            reconciliation_key TEXT NOT NULL,
            document_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            fact_identity TEXT NOT NULL,
            receipt_json TEXT NOT NULL,
            receipt_sha256 TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO documents VALUES (1, 'MELI', 'ir_doc', 'ir_presentation', ?, ?, ?, ?)",
        (
            "2026-06-30",
            "https://example.test/1",
            "ir_documents/MELI/2026-06-30/1.pdf",
            "2026-08-05T00:00:00+00:00",
        ),
    )
    return conn


def _expected(name: str) -> ExpectedIssuerFact:
    return ExpectedIssuerFact(
        ticker="MELI",
        kind=IssuerFactKind.KPI,
        canonical_name=name,
        period_end=date(2026, 6, 30),
        fiscal_period_type="Q2",
        unit=Unit.ACTUAL,
        currency=Currency.USD,
    )


def _receipt_rows(
    conn: sqlite3.Connection,
    results: list[IssuerFactCoverageResult],
    *,
    bind_manifest: bool = True,
) -> None:
    rejected = {
        result.expected.identity_key: result.rejection_reason or ""
        for result in results
        if result.coverage_status == "rejected"
    }
    frame = ExtractorFactPopulationFrame(
        document_id=1,
        ticker="MELI",
        expected=tuple(result.expected for result in results),
        rejected=rejected,
        extracted_at=datetime(2026, 8, 6, tzinfo=UTC),
    )
    frame_json = frame.model_dump_json(exclude_none=False, by_alias=True)
    frame_sha256 = hashlib.sha256(frame_json.encode()).hexdigest()
    should_bind_manifest = bind_manifest and all(
        result.coverage_status != "missing" for result in results
    )
    manifest: IssuerFactManifest | None = None
    if should_bind_manifest:
        captured = [result.expected for result in results if result.coverage_status == "captured"]
        manifest = IssuerFactManifest(
            ticker="MELI",
            source_doc_id=1,
            source_doc_sha256="a" * 64,
            period_end=date(2026, 6, 30),
            fiscal_period_type=FiscalPeriodType.Q2,
            values=tuple(
                IssuerFactValue(
                    ticker="MELI",
                    kind=IssuerManifestFactKind.KPI,
                    canonical_name=expected.canonical_name,
                    period_end=expected.period_end,
                    fiscal_period_type=FiscalPeriodType(expected.fiscal_period_type),
                    unit=expected.unit,
                    currency=expected.currency,
                    value=Decimal("1"),
                    locator=FactLocator(
                        locator_version=2,
                        kind=LocatorKind.PDF_SLIDE,
                        pdf_page=1,
                    ),
                )
                for expected in captured
            ),
            expected=tuple(result.expected for result in results),
            rejected=rejected,
            extracted_at=datetime(2026, 8, 6, tzinfo=UTC),
        )
    receipt = IssuerDocumentCoverageReceipt(
        document_id=1,
        ticker="MELI",
        source_type="ir_doc",
        doc_type="ir_presentation",
        source_url="https://example.test/1",
        source_fetched_at=datetime(2026, 8, 5, tzinfo=UTC),
        extracted_at=datetime(2026, 8, 6, tzinfo=UTC),
        rejection_frame_json=frame_json if rejected else None,
        rejection_frame_sha256=frame_sha256 if rejected else None,
        application_manifest_json=manifest.canonical_json if manifest is not None else None,
        application_manifest_sha256=manifest.manifest_sha256 if manifest is not None else None,
        results=results,
    )
    envelope = receipt.model_dump(mode="json", exclude={"results"})
    for result in results:
        row_envelope = envelope.copy()
        if result.coverage_status != "rejected":
            row_envelope["rejection_frame_json"] = None
            row_envelope["rejection_frame_sha256"] = None
        payload = json.dumps(
            {"receipt": row_envelope, "result": result.model_dump(mode="json")},
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode()).hexdigest()
        key = f"1|{result.expected.identity_key}|current|none"
        conn.execute(
            "INSERT INTO issuer_fact_coverage_receipts VALUES (?, ?, ?, 1, 'MELI', ?, ?, ?, ?)",
            (
                f"issuer-fact-coverage:{digest}",
                digest,
                key,
                result.expected.identity_key,
                payload,
                digest,
                receipt.extracted_at.isoformat(),
            ),
        )


def _result(
    expected: ExpectedIssuerFact,
    status: Literal["captured", "rejected", "missing"],
) -> IssuerFactCoverageResult:
    return IssuerFactCoverageResult(
        expected=expected,
        coverage_status=status,
        captured_fact_ids=[7] if status == "captured" else [],
        downstream=DownstreamAvailability(status=DownstreamAvailabilityStatus.AVAILABLE),
    )


def test_fact_without_terminal_receipt_is_pending() -> None:
    conn = _conn()
    conn.execute("INSERT INTO kpi_facts VALUES (7, 1)")
    assert document_completeness(conn, 1).status == "pending"
    assert [item["document_id"] for item in extract_ir.list_pending_documents(conn, None)] == [1]


def test_malformed_receipt_envelope_fails_closed_as_pending() -> None:
    conn = _conn()
    payload = json.dumps({"result": None})
    digest = hashlib.sha256(payload.encode()).hexdigest()
    conn.execute(
        "INSERT INTO issuer_fact_coverage_receipts VALUES (?, ?, ?, 1, 'MELI', ?, ?, ?, ?)",
        (
            f"issuer-fact-coverage:{digest}",
            digest,
            "1|malformed|current|none",
            "malformed",
            payload,
            digest,
            "2026-08-06T00:00:00+00:00",
        ),
    )

    result = document_completeness(conn, 1)

    assert result.status == "pending"
    assert result.reason == "invalid_receipt"
    assert [item["document_id"] for item in extract_ir.list_pending_documents(conn, None)] == [1]


def test_partial_receipt_is_pending() -> None:
    conn = _conn()
    first, second = _expected("Revenue"), _expected("Margin")
    _receipt_rows(conn, [_result(first, "captured"), _result(second, "missing")])
    assert document_completeness(conn, 1).status == "pending"
    assert [item["document_id"] for item in extract_ir.list_pending_documents(conn, None)] == [1]


def test_complete_receipt_is_terminal() -> None:
    conn = _conn()
    first, second = _expected("Revenue"), _expected("Margin")
    _receipt_rows(conn, [_result(first, "captured"), _result(second, "captured")])
    assert document_completeness(conn, 1).status == "complete"
    assert extract_ir.list_pending_documents(conn, None) == []


def test_terminal_looking_receipt_without_application_manifest_is_pending() -> None:
    conn = _conn()
    first, second = _expected("Revenue"), _expected("Margin")
    _receipt_rows(
        conn,
        [_result(first, "captured"), _result(second, "captured")],
        bind_manifest=False,
    )

    result = document_completeness(conn, 1)

    assert result.status == "pending"
    assert result.reason == "invalid_receipt"
    assert [item["document_id"] for item in extract_ir.list_pending_documents(conn, None)] == [1]


def test_rejected_expected_fact_is_terminal() -> None:
    conn = _conn()
    captured = _expected("Revenue")
    rejected = _expected("Margin")
    _receipt_rows(
        conn,
        [
            _result(captured, "captured"),
            IssuerFactCoverageResult(
                expected=rejected,
                coverage_status="rejected",
                rejection_reason="not reported by issuer",
                downstream=DownstreamAvailability(status=DownstreamAvailabilityStatus.MISSING),
            ),
        ],
    )
    assert document_completeness(conn, 1).status == "complete"
    assert extract_ir.list_pending_documents(conn, None) == []
