"""Primary-fact overlays for the generic FCFF builder."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from dcf.primary_fact_overlay import overlay_quarterly_records
from execution.refresh_dcf import build_dcf_provenance, primary_fact_overlay_from_builder


def _db() -> sqlite3.Connection:
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
            fetched_at TEXT NOT NULL,
            source_url TEXT,
            source_quality_tier TEXT NOT NULL
        );
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            period_end TEXT NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            line_item TEXT NOT NULL,
            value NUMERIC NOT NULL,
            currency TEXT,
            unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL,
            extracted_by TEXT,
            locator TEXT
        );
        """
    )
    conn.execute(
        """
        INSERT INTO documents
            (id, ticker, source_type, doc_type, period_end, fetched_at, source_url, source_quality_tier)
        VALUES (1, 'TEST', 'sec', 'sec_companyfacts', '2026-06-30',
                '2026-08-20T12:00:00+00:00', 'https://www.sec.gov/example', 'sec_official')
        """
    )
    return conn


def _income_row() -> dict[str, object]:
    return {
        "date": "2026-06-30",
        "fiscalYear": "2026",
        "period": "Q2",
        "reportedCurrency": "USD",
        "revenue": 1_000_000,
        "totalDebt": 900_000,
    }


def test_exact_primary_fact_replaces_only_mapped_fmp_field_and_records_lineage() -> None:
    conn = _db()
    conn.execute(
        """
        INSERT INTO financial_facts
            (id, ticker, period_end, fiscal_period_type, line_item, value, currency, unit,
             source_doc_id, extracted_by, locator)
        VALUES (10, 'TEST', '2026-06-30', 'Q2', 'revenue', 1200000, 'USD', 'actual', 1,
                'sec_xbrl', '{"accession_number":"0000000000-26-000001"}')
        """
    )

    result = overlay_quarterly_records(
        conn, ticker="TEST", statement="income", records=[_income_row()]
    )

    assert result.records[0]["revenue"] == 1_200_000
    assert result.records[0]["totalDebt"] == 900_000  # debt is never synthesized by this overlay
    assert len(result.applied) == 1
    assert result.applied[0].fmp_field == "revenue"
    assert result.applied[0].source_doc_id == 1
    assert result.applied[0].as_of == "2026-08-20T12:00:00+00:00"
    assert result.conflicts[0].reason == "value_conflict"


def test_currency_or_period_mismatch_never_replaces_fmp_value() -> None:
    conn = _db()
    conn.execute(
        """
        INSERT INTO financial_facts
            (id, ticker, period_end, fiscal_period_type, line_item, value, currency, unit,
             source_doc_id, extracted_by, locator)
        VALUES (11, 'TEST', '2026-06-30', 'Q2', 'revenue', 999, 'EUR', 'actual', 1,
                'sec_xbrl', NULL)
        """
    )

    result = overlay_quarterly_records(
        conn, ticker="TEST", statement="income", records=[_income_row()]
    )

    assert result.records[0]["revenue"] == 1_000_000
    assert result.applied == ()
    assert result.rejected[0].reason == "currency_mismatch"


def test_cross_issuer_primary_document_never_overlays_the_requested_ticker() -> None:
    conn = _db()
    conn.execute("UPDATE documents SET ticker='OTHER' WHERE id=1")
    conn.execute(
        """
        INSERT INTO financial_facts
            (id, ticker, period_end, fiscal_period_type, line_item, value, currency, unit,
             source_doc_id, extracted_by, locator)
        VALUES (13, 'TEST', '2026-06-30', 'Q2', 'revenue', 999, 'USD', 'actual', 1,
                'sec_xbrl', NULL)
        """
    )

    result = overlay_quarterly_records(
        conn, ticker="TEST", statement="income", records=[_income_row()]
    )

    assert result.records[0]["revenue"] == 1_000_000
    assert result.applied == ()
    assert result.rejected[0].reason == "source_not_primary"


def test_issuer_ir_source_type_is_primary_even_with_legacy_normalized_tier() -> None:
    conn = _db()
    conn.execute(
        "UPDATE documents SET source_type='ir_doc', source_quality_tier='fmp_normalized' WHERE id=1"
    )
    conn.execute(
        """
        INSERT INTO financial_facts
            (id, ticker, period_end, fiscal_period_type, line_item, value, currency, unit,
             source_doc_id, extracted_by, locator)
        VALUES (12, 'TEST', '2026-06-30', 'Q2', 'revenue', 111, 'USD', 'actual', 1,
                'issuer_ir', NULL)
        """
    )

    result = overlay_quarterly_records(
        conn, ticker="TEST", statement="income", records=[_income_row()]
    )

    assert result.records[0]["revenue"] == 111
    assert result.applied[0].source_type == "ir_doc"


def test_overlay_reads_only_the_canonical_resolved_fact_relation() -> None:
    conn = _db()
    conn.executemany(
        """
        INSERT INTO financial_facts
            (id, ticker, period_end, fiscal_period_type, line_item, value, currency, unit,
             source_doc_id, extracted_by, locator)
        VALUES (?, 'TEST', '2026-06-30', 'Q2', 'revenue', ?, 'USD', 'actual', 1,
                'sec_xbrl', NULL)
        """,
        ((20, 1_100_000), (21, 1_300_000)),
    )
    conn.execute(
        "CREATE VIEW v_financial_facts_resolved_current AS "
        "SELECT * FROM financial_facts WHERE id = 21"
    )

    result = overlay_quarterly_records(
        conn, ticker="TEST", statement="income", records=[_income_row()]
    )

    assert result.records[0]["revenue"] == 1_300_000
    assert [item.fact_id for item in result.applied] == [21]


def test_exact_primary_cash_and_debt_bridge_fields_overlay_without_synthesis() -> None:
    conn = _db()
    conn.executemany(
        """
        INSERT INTO financial_facts
            (id, ticker, period_end, fiscal_period_type, line_item, value, currency, unit,
             source_doc_id, extracted_by, locator)
        VALUES (?, 'TEST', '2026-06-30', 'Q2', ?, ?, 'USD', 'actual', 1,
                'issuer_ir', NULL)
        """,
        (
            (30, "cash_and_short_term_investments", 700_000),
            (31, "total_debt", 500_000),
        ),
    )
    row = {
        "date": "2026-06-30",
        "period": "Q2",
        "reportedCurrency": "USD",
        "cashAndShortTermInvestments": 600_000,
        "totalDebt": 900_000,
    }

    result = overlay_quarterly_records(conn, ticker="TEST", statement="balance", records=[row])

    assert result.records[0]["cashAndShortTermInvestments"] == 700_000
    assert result.records[0]["totalDebt"] == 500_000
    assert {item.line_item for item in result.applied} == {
        "cash_and_short_term_investments",
        "total_debt",
    }


def test_builder_receipt_preserves_primary_source_and_as_of_for_refresh_provenance() -> None:
    receipt = """{"event":"dcf_primary_fact_overlay","ticker":"TEST","statement":"income","status":"ok","applied":[{"fmp_field":"revenue","source_url":"https://www.sec.gov/example","as_of":"2026-08-20T12:00:00+00:00"}],"conflicts":[],"rejected":[]}"""
    detail = primary_fact_overlay_from_builder(receipt)

    statements = detail.get("statements")
    assert isinstance(statements, dict)
    income = cast("dict[str, object]", statements).get("income")
    assert isinstance(income, dict)
    applied = cast("dict[str, object]", income).get("applied")
    assert isinstance(applied, list) and applied and isinstance(applied[0], dict)
    first = cast("dict[str, object]", applied[0])
    assert first["source_url"] == "https://www.sec.gov/example"
    assert first["as_of"] == "2026-08-20T12:00:00+00:00"


def test_builder_receipt_aggregates_degraded_and_partial_coverage_truthfully() -> None:
    degraded = """{"event":"dcf_primary_fact_overlay","ticker":"TEST","statement":"income","status":"degraded","degraded_reason":"db unavailable","applied":[],"conflicts":[],"rejected":[]}"""

    detail = primary_fact_overlay_from_builder(degraded, expected_ticker="TEST")

    assert detail["status"] == "degraded"
    assert "missing_statement_receipts" in cast("list[str]", detail["reasons"])
    assert "statement_degraded" in cast("list[str]", detail["reasons"])


def test_builder_receipt_rejects_ticker_mismatch_and_duplicate_statement() -> None:
    lines = "\n".join(
        [
            '{"event":"dcf_primary_fact_overlay","ticker":"OTHER","statement":"income","status":"ok","applied":[],"conflicts":[],"rejected":[]}',
            '{"event":"dcf_primary_fact_overlay","ticker":"TEST","statement":"income","status":"ok","applied":[],"conflicts":[],"rejected":[]}',
            '{"event":"dcf_primary_fact_overlay","ticker":"TEST","statement":"income","status":"ok","applied":[],"conflicts":[],"rejected":[]}',
        ]
    )

    detail = primary_fact_overlay_from_builder(lines, expected_ticker="TEST")

    assert detail["status"] == "degraded"
    reasons = cast("list[str]", detail["reasons"])
    assert "ticker_mismatch" in reasons
    assert "duplicate_statement_receipt" in reasons


def test_primary_fact_as_of_participates_in_dcf_inputs_as_of(tmp_path: Path) -> None:
    workbook = tmp_path / "TEST.xlsx"
    workbook.write_bytes(b"workbook")
    primary_as_of = datetime(2030, 1, 2, 3, 4, tzinfo=UTC)
    overlay: dict[str, object] = {
        "status": "ok",
        "statements": {
            "income": {
                "status": "ok",
                "applied": [{"as_of": primary_as_of.isoformat()}],
            }
        },
    }

    provenance = build_dcf_provenance(
        ticker="TEST",
        repo_root=tmp_path,
        workbook_path=workbook,
        input_payload={},
        assumption_snapshot_json="{}",
        live_price=None,
        live_price_at=None,
        live_price_source=None,
        mos_bar=None,
        primary_fact_overlay=overlay,
    )

    assert provenance.inputs_as_of == primary_as_of


def test_generated_fcff_workbook_does_not_fake_current_input_observation(tmp_path: Path) -> None:
    workbook = tmp_path / "TEST.xlsx"
    workbook.write_bytes(b"new output")

    provenance = build_dcf_provenance(
        ticker="TEST",
        repo_root=tmp_path,
        workbook_path=workbook,
        input_payload={},
        assumption_snapshot_json="{}",
        live_price=None,
        live_price_at=None,
        live_price_source=None,
        mos_bar=None,
    )

    assert provenance.inputs_as_of == datetime(1970, 1, 1, tzinfo=UTC)
    assert provenance.detail is not None
    assert provenance.detail["inputs_as_of_status"] == "unavailable"
