"""Primary-fact overlays for the generic FCFF builder."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from dcf.primary_fact_overlay import overlay_quarterly_records
from execution.refresh_dcf import (
    build_dcf_provenance,
    country_risk_context_from_builder,
    equity_bridge_context_from_builder,
    primary_fact_overlay_from_builder,
)


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
        CREATE VIEW v_financial_facts_resolved_current AS
            SELECT * FROM financial_facts;
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
    assert result.records[0]["totalDebt"] == 900_000  # income overlays never touch balance fields
    assert len(result.applied) == 1
    assert result.applied[0].fmp_field == "revenue"
    assert result.applied[0].source_doc_id == 1
    assert result.applied[0].as_of == "2026-08-20T12:00:00+00:00"
    assert result.applied[0].currency == "USD"
    assert result.applied[0].unit == "actual"
    assert result.applied[0].reported_observation_id_status == "unavailable_in_canonical_relation"
    assert result.applied[0].resolution_id_status == "unavailable_in_canonical_relation"
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
    conn.execute("DROP VIEW v_financial_facts_resolved_current")
    conn.execute(
        "CREATE VIEW v_financial_facts_resolved_current AS "
        "SELECT * FROM financial_facts WHERE id = 21"
    )

    result = overlay_quarterly_records(
        conn, ticker="TEST", statement="income", records=[_income_row()]
    )

    assert result.records[0]["revenue"] == 1_300_000
    assert [item.fact_id for item in result.applied] == [21]


def test_overlay_degrades_instead_of_reading_a_pre_cutover_legacy_table() -> None:
    conn = _db()
    conn.execute("DROP VIEW v_financial_facts_resolved_current")
    conn.execute(
        """
        INSERT INTO financial_facts
            (id, ticker, period_end, fiscal_period_type, line_item, value, currency, unit,
             source_doc_id, extracted_by, locator)
        VALUES (22, 'TEST', '2026-06-30', 'Q2', 'revenue', 1300000, 'USD', 'actual', 1,
                'sec_xbrl', NULL)
        """
    )

    result = overlay_quarterly_records(
        conn, ticker="TEST", statement="income", records=[_income_row()]
    )

    assert result.records[0]["revenue"] == 1_000_000
    assert result.applied == ()
    assert result.degraded_reason is not None
    assert "refusing a legacy read" in result.degraded_reason


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


def test_complete_primary_cash_components_derive_the_aggregate_with_lineage() -> None:
    conn = _db()
    conn.executemany(
        """
        INSERT INTO financial_facts
            (id, ticker, period_end, fiscal_period_type, line_item, value, currency, unit,
             source_doc_id, extracted_by, locator)
        VALUES (?, 'TEST', '2026-06-30', 'Q2', ?, ?, 'USD', 'actual', 1,
                'sec_xbrl', ?)
        """,
        (
            (40, "cash_and_equivalents", 700_000, "cash-locator"),
            (41, "short_term_investments", 300_000, "investments-locator"),
        ),
    )
    row = {
        "date": "2026-06-30",
        "period": "Q2",
        "reportedCurrency": "USD",
        "cashAndShortTermInvestments": 900_000,
    }

    result = overlay_quarterly_records(conn, ticker="TEST", statement="balance", records=[row])

    assert result.records[0]["cashAndShortTermInvestments"] == 1_000_000
    aggregate = next(
        item for item in result.applied if item.line_item == "cash_and_short_term_investments"
    )
    assert aggregate.derivation is not None
    assert aggregate.derivation.formula == "cash_and_equivalents + short_term_investments"
    assert aggregate.derivation.version == "primary_fact_aggregate_v1"
    assert [
        (item.line_item, item.fact_id, item.locator) for item in aggregate.derivation.components
    ] == [
        ("cash_and_equivalents", 40, "cash-locator"),
        ("short_term_investments", 41, "investments-locator"),
    ]
    assert aggregate.derivation.components[0].source_url == "https://www.sec.gov/example"
    assert aggregate.derivation.components[1].as_of == "2026-08-20T12:00:00+00:00"
    provenance = result.to_provenance_dict()
    applied = cast("list[dict[str, object]]", provenance["applied"])
    derived = next(
        item for item in applied if item["line_item"] == "cash_and_short_term_investments"
    )
    assert (
        cast("dict[str, object]", derived["derivation"])["version"] == "primary_fact_aggregate_v1"
    )
    assert result.conflicts[-1].line_item == "cash_and_short_term_investments"
    assert result.conflicts[-1].primary_value == 1_000_000
    assert result.conflicts[-1].derivation == aggregate.derivation


def test_complete_primary_debt_components_preserve_zero_values_in_aggregate() -> None:
    conn = _db()
    conn.executemany(
        """
        INSERT INTO financial_facts
            (id, ticker, period_end, fiscal_period_type, line_item, value, currency, unit,
             source_doc_id, extracted_by, locator)
        VALUES (?, 'TEST', '2026-06-30', 'Q2', ?, ?, 'USD', 'actual', 1,
                'sec_xbrl', NULL)
        """,
        ((50, "long_term_debt", 500_000), (51, "short_term_debt", 0)),
    )
    row = {
        "date": "2026-06-30",
        "period": "Q2",
        "reportedCurrency": "USD",
        "totalDebt": 900_000,
    }

    result = overlay_quarterly_records(conn, ticker="TEST", statement="balance", records=[row])

    assert result.records[0]["totalDebt"] == 500_000
    aggregate = next(item for item in result.applied if item.line_item == "total_debt")
    assert aggregate.primary_value == 500_000
    assert aggregate.derivation is not None
    assert aggregate.derivation.components[1].primary_value == 0


def test_aggregate_component_lineage_handles_all_canonical_resolution_id_variants() -> None:
    for has_reported_observation_id, has_resolution_id in (
        (False, False),
        (True, False),
        (False, True),
        (True, True),
    ):
        conn = _db()
        if has_reported_observation_id:
            conn.execute("ALTER TABLE financial_facts ADD COLUMN reported_observation_id TEXT")
        if has_resolution_id:
            conn.execute("ALTER TABLE financial_facts ADD COLUMN resolution_id TEXT")
        conn.executemany(
            """
            INSERT INTO financial_facts
                (id, ticker, period_end, fiscal_period_type, line_item, value, currency, unit,
                 source_doc_id, extracted_by, locator)
            VALUES (?, 'TEST', '2026-06-30', 'Q2', ?, ?, 'USD', 'actual', 1,
                    'sec_xbrl', NULL)
            """,
            (
                (55, "long_term_debt", 500_000),
                (56, "short_term_debt", 100_000),
            ),
        )
        if has_reported_observation_id:
            conn.execute(
                "UPDATE financial_facts SET reported_observation_id = 'observation:' || id "
                "WHERE id IN (55, 56)"
            )
        if has_resolution_id:
            conn.execute(
                "UPDATE financial_facts SET resolution_id = 'resolution:' || id "
                "WHERE id IN (55, 56)"
            )

        result = overlay_quarterly_records(
            conn,
            ticker="TEST",
            statement="balance",
            records=[
                {
                    "date": "2026-06-30",
                    "period": "Q2",
                    "reportedCurrency": "USD",
                    "totalDebt": 900_000,
                }
            ],
        )

        aggregate = next(item for item in result.applied if item.line_item == "total_debt")
        assert aggregate.derivation is not None
        assert [
            (component.reported_observation_id, component.resolution_id)
            for component in aggregate.derivation.components
        ] == [
            (
                "observation:55" if has_reported_observation_id else None,
                "resolution:55" if has_resolution_id else None,
            ),
            (
                "observation:56" if has_reported_observation_id else None,
                "resolution:56" if has_resolution_id else None,
            ),
        ]
        assert [
            (component.reported_observation_id_status, component.resolution_id_status)
            for component in aggregate.derivation.components
        ] == [
            (
                "available" if has_reported_observation_id else "unavailable_in_canonical_relation",
                "available" if has_resolution_id else "unavailable_in_canonical_relation",
            ),
            (
                "available" if has_reported_observation_id else "unavailable_in_canonical_relation",
                "available" if has_resolution_id else "unavailable_in_canonical_relation",
            ),
        ]


def test_aggregate_is_not_derived_when_a_primary_component_is_missing_or_ineligible() -> None:
    conn = _db()
    conn.execute(
        """
        INSERT INTO financial_facts
            (id, ticker, period_end, fiscal_period_type, line_item, value, currency, unit,
             source_doc_id, extracted_by, locator)
        VALUES (60, 'TEST', '2026-06-30', 'Q2', 'cash_and_equivalents', 700000, 'USD',
                'actual', 1, 'sec_xbrl', NULL)
        """
    )
    row = {
        "date": "2026-06-30",
        "period": "Q2",
        "reportedCurrency": "USD",
        "cashAndShortTermInvestments": 900_000,
    }

    missing = overlay_quarterly_records(conn, ticker="TEST", statement="balance", records=[row])

    assert missing.records[0]["cashAndShortTermInvestments"] == 900_000
    assert all(item.line_item != "cash_and_short_term_investments" for item in missing.applied)

    conn.execute(
        """
        INSERT INTO financial_facts
            (id, ticker, period_end, fiscal_period_type, line_item, value, currency, unit,
             source_doc_id, extracted_by, locator)
        VALUES (61, 'TEST', '2026-06-30', 'Q2', 'short_term_investments', 300000, 'EUR',
                'actual', 1, 'sec_xbrl', NULL)
        """
    )
    mismatched = overlay_quarterly_records(conn, ticker="TEST", statement="balance", records=[row])

    assert mismatched.records[0]["cashAndShortTermInvestments"] == 900_000
    assert all(item.line_item != "cash_and_short_term_investments" for item in mismatched.applied)
    assert any(item.reason == "currency_mismatch" for item in mismatched.rejected)


def test_exact_primary_aggregate_takes_precedence_over_complete_components() -> None:
    conn = _db()
    conn.executemany(
        """
        INSERT INTO financial_facts
            (id, ticker, period_end, fiscal_period_type, line_item, value, currency, unit,
             source_doc_id, extracted_by, locator)
        VALUES (?, 'TEST', '2026-06-30', 'Q2', ?, ?, 'USD', 'actual', 1,
                'sec_xbrl', NULL)
        """,
        (
            (70, "total_debt", 600_000),
            (71, "long_term_debt", 500_000),
            (72, "short_term_debt", 200_000),
        ),
    )
    row = {
        "date": "2026-06-30",
        "period": "Q2",
        "reportedCurrency": "USD",
        "totalDebt": 900_000,
    }

    result = overlay_quarterly_records(conn, ticker="TEST", statement="balance", records=[row])

    assert result.records[0]["totalDebt"] == 600_000
    aggregate = next(item for item in result.applied if item.line_item == "total_debt")
    assert aggregate.derivation is None
    assert result.conflicts[-1].primary_value == 600_000


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


def test_equity_bridge_context_requires_one_matching_builder_receipt() -> None:
    context_line = (
        '{"event":"dcf_equity_bridge_context",'
        '"schema_version":"dcf_equity_bridge_context.v2",'
        '"ticker":"TEST","period_end":"2026-06-30",'
        '"fiscal_period_type":"Q2","reporting_currency":"USD",'
        '"cash_m":200.0,"total_debt_m":100.0,"diluted_shares_m":10.0,'
        '"cash_basis":"reported_aggregate","total_debt_basis":"reported_aggregate",'
        '"debt_scope":"interest_bearing_debt_only",'
        '"debt_calculation":"debt_and_capital_lease_obligations - finance_lease_liability",'
        '"debt_operations":[{"field":"totalDebt","sign":1},{"field":"financeLeaseLiability","sign":-1}],'
        '"debt_component_lineage":[{"fmp_field":"totalDebt","operation_sign":1}]}'
    )

    context = equity_bridge_context_from_builder(context_line, expected_ticker="test")

    assert context is not None
    assert context["ticker"] == "TEST"
    assert context["period_end"] == "2026-06-30"
    assert "event" not in context
    assert (
        equity_bridge_context_from_builder(
            f"{context_line}\n{context_line}", expected_ticker="TEST"
        )
        is None
    )
    assert equity_bridge_context_from_builder(context_line, expected_ticker="OTHER") is None


def test_country_risk_context_requires_one_matching_valid_builder_receipt() -> None:
    context_line = (
        '{"event":"dcf_country_risk_context",'
        '"schema_version":"dcf_country_risk_context.v1",'
        '"ticker":"TEST","premium":0.03,"authority":"systematic_geo",'
        '"source_record":{"role":"geographic_revenue",'
        '"path":"data/historical/fmp/TEST_geo_segments_annual.json",'
        '"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        '"bytes":123,"observed_at":"2026-08-20T12:00:00+00:00",'
        '"influences_calculation":true,"selection":"annual_latest_fiscal_year"}}'
    )

    context = country_risk_context_from_builder(context_line, expected_ticker="test")

    assert context is not None
    assert context["authority"] == "systematic_geo"
    assert (
        country_risk_context_from_builder(f"{context_line}\n{context_line}", expected_ticker="TEST")
        is None
    )
    assert country_risk_context_from_builder(context_line, expected_ticker="OTHER") is None
    preserved_line = (
        '{"event":"dcf_country_risk_context",'
        '"schema_version":"dcf_country_risk_context.v1",'
        '"ticker":"TEST","premium":0.0123,'
        '"authority":"preserved_dashboard_override","source_record":null}'
    )
    preserved = country_risk_context_from_builder(preserved_line, expected_ticker="TEST")
    assert preserved is not None
    assert preserved["authority"] == "preserved_dashboard_override"
    assert preserved["source_record"] is None
    invalid_source_less = (
        '{"event":"dcf_country_risk_context",'
        '"schema_version":"dcf_country_risk_context.v1",'
        '"ticker":"TEST","premium":0.03,'
        '"authority":"systematic_geo","source_record":null}'
    )
    assert country_risk_context_from_builder(invalid_source_less, expected_ticker="TEST") is None
    default_zero_line = (
        '{"event":"dcf_country_risk_context",'
        '"schema_version":"dcf_country_risk_context.v1",'
        '"ticker":"TEST","premium":0.0,'
        '"authority":"systematic_default_zero","source_record":null}'
    )
    default_zero = country_risk_context_from_builder(default_zero_line, expected_ticker="TEST")
    assert default_zero is not None
    assert default_zero["authority"] == "systematic_default_zero"


def test_country_risk_source_participates_in_dcf_provenance(tmp_path: Path) -> None:
    workbook = tmp_path / "TEST.xlsx"
    workbook.write_bytes(b"workbook")
    observed_at = "2026-08-20T12:00:00+00:00"
    context: dict[str, object] = {
        "schema_version": "dcf_country_risk_context.v1",
        "ticker": "TEST",
        "premium": 0.03,
        "authority": "systematic_geo",
        "source_record": {
            "role": "geographic_revenue",
            "path": "data/historical/fmp/TEST_geo_segments_annual.json",
            "sha256": "a" * 64,
            "bytes": 123,
            "observed_at": observed_at,
            "influences_calculation": True,
            "selection": "annual_latest_fiscal_year",
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
        country_risk_context=context,
    )

    assert provenance.inputs_as_of == datetime.fromisoformat(observed_at)
    assert provenance.detail is not None
    assert provenance.detail["country_risk_context"] == context
    sources = cast("list[dict[str, object]]", provenance.detail["sources"])
    assert any(source.get("role") == "geographic_revenue" for source in sources)


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
        equity_bridge_receipt={
            "schema_version": "dcf_equity_bridge_receipt.v3",
            "status": "verified",
        },
    )

    assert provenance.inputs_as_of == primary_as_of
    assert provenance.detail is not None
    assert provenance.detail["equity_bridge_receipt"] == {
        "schema_version": "dcf_equity_bridge_receipt.v3",
        "status": "verified",
    }


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
