"""Hermetic issuer-document fact coverage and provenance replay tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from models.facts import Currency, Unit
from pipeline.issuer_document_coverage import (
    OPERATIONS_GOVERNANCE_DISPOSITION,
    ExpectedIssuerFact,
    ExtractorFactPopulationFrame,
    IssuerFactKind,
    build_document_coverage_receipt,
    persist_document_coverage_receipt,
    portfolio_coverage_report,
    reconcile_extractor_fact_population,
)
from provenance.source_coverage import IssuerFactCoverageReceiptRecord, SourceCoverageLedger
from report.rules import TickerRules
from report.sections.financials import kpi_series_for
from report.sections.segments import apply_segment_rules, build_grids, prefer_issuer_rows


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
            source_url TEXT,
            fetched_at TEXT NOT NULL
        );
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            unit TEXT NOT NULL
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            period_end TEXT NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            kpi_definition_id INTEGER NOT NULL,
            value TEXT NOT NULL,
            unit TEXT NOT NULL,
            currency TEXT,
            source_doc_id INTEGER NOT NULL
        );
        CREATE TABLE segment_periods (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            period_end TEXT NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL,
            currency TEXT,
            unit TEXT NOT NULL
        );
        CREATE TABLE segment_dimensions (
            id INTEGER PRIMARY KEY,
            period_id INTEGER NOT NULL,
            dim_type TEXT NOT NULL,
            dim_name TEXT NOT NULL,
            metric TEXT NOT NULL,
            value TEXT NOT NULL,
            unit TEXT
        );
        """
    )
    return conn


def _document(
    conn: sqlite3.Connection,
    *,
    doc_id: int,
    ticker: str,
    source_type: str,
    fetched_at: str,
) -> None:
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, source_url, fetched_at) "
        "VALUES (?, ?, ?, 'ir_presentation', ?, ?)",
        (doc_id, ticker, source_type, f"https://example.test/{ticker}/{doc_id}", fetched_at),
    )


def _kpi(
    conn: sqlite3.Connection,
    *,
    definition_id: int,
    fact_id: int,
    ticker: str,
    name: str,
    value: str,
    doc_id: int,
) -> None:
    conn.execute(
        "INSERT INTO kpi_definitions (id, ticker, name, unit) VALUES (?, ?, ?, 'actual')",
        (definition_id, ticker, name),
    )
    conn.execute(
        "INSERT INTO kpi_facts "
        "(id, ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, currency, source_doc_id) "
        "VALUES (?, ?, '2026-06-30', 'Q2', ?, ?, 'actual', 'USD', ?)",
        (fact_id, ticker, definition_id, value, doc_id),
    )


def _segment(
    conn: sqlite3.Connection,
    *,
    period_id: int,
    dimension_id: int,
    ticker: str,
    name: str,
    doc_id: int,
) -> None:
    conn.execute(
        "INSERT INTO segment_periods "
        "(id, ticker, period_end, fiscal_period_type, source_doc_id, currency, unit) "
        "VALUES (?, ?, '2026-06-30', 'Q2', ?, 'USD', 'actual')",
        (period_id, ticker, doc_id),
    )
    conn.execute(
        "INSERT INTO segment_dimensions "
        "(id, period_id, dim_type, dim_name, metric, value) "
        "VALUES (?, ?, 'business_unit', ?, 'revenue', '100')",
        (dimension_id, period_id, name),
    )


def _expected(
    ticker: str,
    name: str,
    *,
    kind: IssuerFactKind = IssuerFactKind.KPI,
    currency: Currency | None = Currency.USD,
) -> ExpectedIssuerFact:
    return ExpectedIssuerFact(
        ticker=ticker,
        kind=kind,
        canonical_name=name,
        period_end=date(2026, 6, 30),
        fiscal_period_type="Q2",
        unit=Unit.ACTUAL,
        currency=currency,
        segment_dim_type="business_unit" if kind is IssuerFactKind.SEGMENT else None,
        segment_name=name if kind is IssuerFactKind.SEGMENT else None,
        metric="revenue" if kind is IssuerFactKind.SEGMENT else None,
    )


def test_receipt_accounts_for_captured_rejected_and_missing_facts() -> None:
    conn = _conn()
    try:
        _document(conn, doc_id=10, ticker="MELI", source_type="ir_doc", fetched_at="2026-08-05")
        _kpi(
            conn,
            definition_id=1,
            fact_id=100,
            ticker="MELI",
            name="Total Payment Volume (USD)",
            value="1000",
            doc_id=10,
        )
        _segment(conn, period_id=30, dimension_id=300, ticker="MELI", name="Commerce", doc_id=10)
        expected = (
            _expected("MELI", "Total Payment Volume"),
            _expected("MELI", "Commerce", kind=IssuerFactKind.SEGMENT),
            _expected("MELI", "Credit loss ratio"),
            _expected("MELI", "Payments take rate"),
        )
        receipt = reconcile_extractor_fact_population(
            conn,
            ExtractorFactPopulationFrame(
                document_id=10,
                ticker="MELI",
                expected=expected,
                rejected={expected[2].identity_key: "source_column_is_guidance"},
                extracted_at=datetime(2026, 8, 5, tzinfo=UTC),
            ),
        )
        by_name = {row.expected.canonical_name: row for row in receipt.results}
        assert receipt.expected_count == 4
        assert receipt.captured_count == 2
        assert receipt.rejected_count == 1
        assert receipt.missing_count == 1
        assert by_name["Total Payment Volume"].coverage_status == "captured"
        assert by_name["Total Payment Volume"].downstream.status == "available"
        assert by_name["Commerce"].captured_fact_ids == [300]
        assert by_name["Credit loss ratio"].rejection_reason == "source_column_is_guidance"
        assert by_name["Payments take rate"].downstream.status == "missing"

        report = portfolio_coverage_report([receipt])
        assert report.rows[0].ticker == "MELI"
        assert report.rows[0].period_end == date(2026, 6, 30)
        assert report.rows[0].expected_count == 4
        assert report.rows[0].downstream_available_count == 2
        assert report.rows[0].downstream_missing_count == 2
        assert report.rows[0].downstream_unverifiable_count == 0
    finally:
        conn.close()


def test_coverage_observation_has_an_explicit_no_surface_change_disposition() -> None:
    assert OPERATIONS_GOVERNANCE_DISPOSITION == "no_surface_change_read_only_coverage_observation"


def test_segment_currency_mismatch_and_stale_downstream_are_not_available() -> None:
    conn = _conn()
    try:
        _document(conn, doc_id=1, ticker="MELI", source_type="ir_doc", fetched_at="2026-07-01")
        _segment(conn, period_id=1, dimension_id=1, ticker="MELI", name="Commerce", doc_id=1)
        mismatch = _expected("MELI", "Commerce", kind=IssuerFactKind.SEGMENT, currency=Currency.BRL)
        receipt = build_document_coverage_receipt(
            conn,
            document_id=1,
            expected=(mismatch,),
            stale_before=datetime(2026, 8, 1, tzinfo=UTC),
            extracted_at=datetime(2026, 8, 5, tzinfo=UTC),
        )
        assert receipt.results[0].coverage_status == "missing"
        assert receipt.results[0].downstream.status == "missing"

        matched = _expected("MELI", "Commerce", kind=IssuerFactKind.SEGMENT)
        stale = build_document_coverage_receipt(
            conn,
            document_id=1,
            expected=(matched,),
            stale_before=datetime(2026, 8, 1, tzinfo=UTC),
            extracted_at=datetime(2026, 8, 5, tzinfo=UTC),
        )
        assert stale.results[0].downstream.status == "stale"
    finally:
        conn.close()


def test_rejection_identity_does_not_cross_apply_same_named_expected_fact() -> None:
    conn = _conn()
    try:
        _document(conn, doc_id=1, ticker="NU", source_type="ir_doc", fetched_at="2026-08-05")
        current = _expected("NU", "Monthly active customers")
        older = current.model_copy(update={"period_end": date(2026, 3, 31)})
        receipt = reconcile_extractor_fact_population(
            conn,
            ExtractorFactPopulationFrame(
                document_id=1,
                ticker="NU",
                expected=(current, older),
                rejected={current.identity_key: "extractor_rejected"},
                extracted_at=datetime(2026, 8, 5, tzinfo=UTC),
            ),
        )
        assert [item.coverage_status for item in receipt.results] == ["rejected", "missing"]
    finally:
        conn.close()


def test_migrated_receipt_ledger_replays_idempotently_and_rejects_conflicts(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    db_path = migrated_db(tmp_path / "coverage.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "INSERT INTO documents (id,ticker,source_type,doc_type,file_path,sha256,fetched_at,fetch_status,raw_bytes_size) "
            "VALUES (1,'NVO','ir_doc','ir_presentation','fixture','"
            + "c" * 64
            + "','2026-08-05','fetched',1)"
        )
        receipt = build_document_coverage_receipt(
            conn,
            document_id=1,
            expected=(_expected("NVO", "Obesity care sales"),),
            extracted_at=datetime(2026, 8, 5, tzinfo=UTC),
        )
        assert persist_document_coverage_receipt(conn, receipt)[0].created
        assert not persist_document_coverage_receipt(conn, receipt)[0].created
        row = conn.execute(
            "SELECT idempotency_key,reconciliation_key,receipt_json,receipt_sha256 "
            "FROM issuer_fact_coverage_receipts"
        ).fetchone()
        assert row is not None
        conflicting = IssuerFactCoverageReceiptRecord(
            record_id="issuer-fact-coverage:" + "b" * 64,
            idempotency_key="b" * 64,
            reconciliation_key=str(row[1]),
            document_id=1,
            ticker="NVO",
            fact_identity=_expected("NVO", "Obesity care sales").identity_key,
            receipt_json=str(row[2]),
            receipt_sha256=str(row[3]),
            recorded_at=datetime(2026, 8, 5, tzinfo=UTC),
        )
        with pytest.raises(ValueError, match="immutable issuer_fact_coverage_receipts identity"):
            SourceCoverageLedger(conn).persist_many((conflicting,))
    finally:
        conn.close()


def test_zero_expected_receipt_needs_matching_authoritative_extractor_frame(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    conn = sqlite3.connect(migrated_db(tmp_path / "zero-expected.db"))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "INSERT INTO documents (id,ticker,source_type,doc_type,file_path,sha256,fetched_at,fetch_status,raw_bytes_size) "
            "VALUES (1,'NVO','ir_doc','ir_presentation','fixture','"
            + "f" * 64
            + "','2026-08-05T00:00:00Z','fetched',1)"
        )
        with pytest.raises(ValueError, match="authoritative extractor frame"):
            build_document_coverage_receipt(
                conn,
                document_id=1,
                expected=(),
                extracted_at=datetime(2026, 8, 5, tzinfo=UTC),
            )

        frame = ExtractorFactPopulationFrame(
            document_id=1,
            ticker="NVO",
            expected=(),
            extracted_at=datetime(2026, 8, 5, tzinfo=UTC),
            expected_population_status="zero_expected",
        )
        receipt = reconcile_extractor_fact_population(conn, frame)
        assert persist_document_coverage_receipt(conn, receipt)[0].created

        forged_ticker = receipt.model_copy(
            update={
                "population_frame_json": frame.model_copy(
                    update={"ticker": "NU"}
                ).model_dump_json(),
                "population_frame_sha256": hashlib.sha256(
                    frame.model_copy(update={"ticker": "NU"}).model_dump_json().encode("utf-8")
                ).hexdigest(),
            }
        )
        with pytest.raises(ValueError, match="document and ticker"):
            persist_document_coverage_receipt(conn, forged_ticker)
        forged_document_frame = frame.model_copy(update={"document_id": 2}).model_dump_json()
        forged_document = receipt.model_copy(
            update={
                "population_frame_json": forged_document_frame,
                "population_frame_sha256": hashlib.sha256(
                    forged_document_frame.encode("utf-8")
                ).hexdigest(),
            }
        )
        with pytest.raises(ValueError, match="document and ticker"):
            persist_document_coverage_receipt(conn, forged_document)
        with pytest.raises(ValueError, match="population frame hash"):
            persist_document_coverage_receipt(
                conn,
                receipt.model_copy(update={"population_frame_sha256": "0" * 64}),
            )
    finally:
        conn.close()


def test_typed_receipt_rejects_mismatched_ticker_kind_period_unit_and_currency(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    """The persistence envelope is checked against parsed receipt semantics."""
    conn = sqlite3.connect(migrated_db(tmp_path / "receipt-integrity.db"))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "INSERT INTO documents (id,ticker,source_type,doc_type,file_path,sha256,fetched_at,fetch_status,raw_bytes_size) "
            "VALUES (1,'NVO','ir_doc','ir_presentation','fixture','"
            + "d" * 64
            + "','2026-08-05','fetched',1)"
        )
        receipt = build_document_coverage_receipt(
            conn,
            document_id=1,
            expected=(_expected("NVO", "Obesity care sales"),),
            extracted_at=datetime(2026, 8, 5, tzinfo=UTC),
        )
        public_result = persist_document_coverage_receipt(conn, receipt)
        assert public_result[0].created
        raw = {
            "receipt": receipt.model_dump(mode="json", exclude={"results"}),
            "result": receipt.results[0].model_dump(mode="json"),
        }
        expected_identity = receipt.results[0].expected.identity_key
        for field, value in (
            ("ticker", "NU"),
            ("kind", "segment"),
            ("period_end", "2026-03-31"),
            ("unit", "count"),
            ("currency", "BRL"),
        ):
            candidate = json.loads(json.dumps(raw))
            target = candidate["receipt"] if field == "ticker" else candidate["result"]["expected"]
            target[field] = value
            receipt_json = json.dumps(candidate, sort_keys=True, separators=(",", ":"))
            with pytest.raises(ValueError):
                IssuerFactCoverageReceiptRecord(
                    record_id="issuer-fact-coverage:" + "e" * 64,
                    idempotency_key="e" * 64,
                    reconciliation_key="integrity:" + field,
                    document_id=1,
                    ticker="NVO",
                    fact_identity=expected_identity,
                    receipt_json=receipt_json,
                    receipt_sha256=hashlib.sha256(receipt_json.encode()).hexdigest(),
                    recorded_at=datetime(2026, 8, 5, tzinfo=UTC),
                )
    finally:
        conn.close()


def test_receipt_rejects_fabricated_captured_fact_id(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    conn = sqlite3.connect(migrated_db(tmp_path / "fabricated-fact.db"))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "INSERT INTO documents (id,ticker,source_type,doc_type,file_path,sha256,fetched_at,fetch_status,raw_bytes_size) "
            "VALUES (1,'NVO','ir_doc','ir_presentation','fixture','"
            + "f" * 64
            + "','2026-08-05','fetched',1)"
        )
        conn.execute(
            "INSERT INTO kpi_definitions (id,ticker,name,unit,primary_source) "
            "VALUES (999,'NVO','Obesity care sales','actual','ir_doc')"
        )
        # This receipt-validation fixture deliberately materializes the legacy
        # fact row without invoking the unrelated evidence-observation trigger.
        # The receipt path under test independently verifies the fact/document
        # identity below.
        conn.execute("DROP TRIGGER trg_kpi_facts_observation_insert")
        conn.execute(
            "INSERT INTO kpi_facts (id,ticker,period_end,fiscal_period_type,kpi_definition_id,value,unit,currency,source_doc_id) "
            "VALUES (1,'NVO','2026-06-30','Q2',999,'100','actual','USD',1)"
        )
        receipt = build_document_coverage_receipt(
            conn,
            document_id=1,
            expected=(_expected("NVO", "Obesity care sales"),),
            extracted_at=datetime(2026, 8, 5, tzinfo=UTC),
        )
        captured = receipt.results[0]
        claimed_missing = receipt.model_copy(
            update={
                "results": [
                    captured.model_copy(
                        update={"coverage_status": "missing", "captured_fact_ids": []}
                    )
                ]
            }
        )
        with pytest.raises(ValueError, match="cannot mark an existing"):
            persist_document_coverage_receipt(conn, claimed_missing)
        with pytest.raises(ValueError, match="document header"):
            persist_document_coverage_receipt(
                conn, receipt.model_copy(update={"source_url": "https://forged.example"})
            )
        forged_expected = captured.expected.model_copy(update={"ticker": "NU"})
        with pytest.raises(ValueError, match="expected-fact ticker"):
            persist_document_coverage_receipt(
                conn,
                receipt.model_copy(
                    update={"results": [captured.model_copy(update={"expected": forged_expected})]}
                ),
            )
        raw = {
            "receipt": receipt.model_dump(mode="json", exclude={"results"}),
            "result": receipt.results[0].model_dump(mode="json"),
        }
        raw["result"]["captured_fact_ids"] = [999999]
        receipt_json = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        record = IssuerFactCoverageReceiptRecord(
            record_id="issuer-fact-coverage:" + "f" * 64,
            idempotency_key="f" * 64,
            reconciliation_key="fabricated-fact",
            document_id=1,
            ticker="NVO",
            fact_identity=receipt.results[0].expected.identity_key,
            receipt_json=receipt_json,
            receipt_sha256=hashlib.sha256(receipt_json.encode()).hexdigest(),
            recorded_at=datetime(2026, 8, 5, tzinfo=UTC),
        )
        with pytest.raises(ValueError, match="captured receipt fact ids"):
            SourceCoverageLedger(conn).persist_many((record,))
    finally:
        conn.close()


def test_migrated_kpi_observation_triggers_project_new_currency(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    conn = sqlite3.connect(migrated_db(tmp_path / "currency-trigger.db"))
    try:
        triggers = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name IN ('trg_kpi_facts_observation_insert','trg_kpi_facts_observation_update')"
        ).fetchall()
        assert len(triggers) == 2
        assert all("NEW.currency" in str(row[0]) for row in triggers)
    finally:
        conn.close()


def test_duplicate_kpi_definitions_choose_issuer_currently_but_preserve_as_of_vendor_lineage() -> (
    None
):
    conn = _conn()
    try:
        _document(conn, doc_id=1, ticker="MELI", source_type="fmp", fetched_at="2026-07-20")
        _document(conn, doc_id=2, ticker="MELI", source_type="ir_doc", fetched_at="2026-08-05")
        _kpi(
            conn,
            definition_id=1,
            fact_id=101,
            ticker="MELI",
            name="Total Payment Volume",
            value="900",
            doc_id=1,
        )
        _kpi(
            conn,
            definition_id=2,
            fact_id=102,
            ticker="MELI",
            name="Total Payment Volume (USD)",
            value="1000",
            doc_id=2,
        )
        expected = (_expected("MELI", "Total Payment Volume"),)
        current = build_document_coverage_receipt(
            conn,
            document_id=2,
            expected=expected,
            extracted_at=datetime(2026, 8, 5, tzinfo=UTC),
        )
        historical = build_document_coverage_receipt(
            conn,
            document_id=1,
            expected=expected,
            as_of=datetime(2026, 7, 31, tzinfo=UTC),
            extracted_at=datetime(2026, 7, 31, tzinfo=UTC),
        )
        assert current.results[0].downstream.document_id == 2
        assert current.results[0].downstream.source_type == "ir_doc"
        assert historical.results[0].downstream.document_id == 1
        assert historical.results[0].downstream.source_type == "fmp"
    finally:
        conn.close()


def test_as_of_compares_sqlite_timestamp_instants_not_text_spellings() -> None:
    conn = _conn()
    try:
        _document(
            conn,
            doc_id=1,
            ticker="NVO",
            source_type="ir_doc",
            fetched_at="2026-08-05T00:00:00Z",
        )
        _kpi(
            conn,
            definition_id=1,
            fact_id=1,
            ticker="NVO",
            name="Obesity care sales",
            value="100",
            doc_id=1,
        )
        receipt = build_document_coverage_receipt(
            conn,
            document_id=1,
            expected=(_expected("NVO", "Obesity care sales"),),
            as_of=datetime.fromisoformat("2026-08-05 00:00:00+00:00"),
            extracted_at=datetime(2026, 8, 5, tzinfo=UTC),
        )
        assert receipt.results[0].downstream.status == "available"
        assert receipt.results[0].downstream.document_id == 1
    finally:
        conn.close()


def test_as_of_later_fact_requires_exact_expected_currency() -> None:
    conn = _conn()
    try:
        _document(
            conn,
            doc_id=1,
            ticker="NVO",
            source_type="ir_doc",
            fetched_at="2026-08-06T00:00:00Z",
        )
        _kpi(
            conn,
            definition_id=1,
            fact_id=1,
            ticker="NVO",
            name="Obesity care sales",
            value="100",
            doc_id=1,
        )
        receipt = build_document_coverage_receipt(
            conn,
            document_id=1,
            expected=(_expected("NVO", "Obesity care sales", currency=Currency.BRL),),
            as_of=datetime(2026, 8, 5, tzinfo=UTC),
            extracted_at=datetime(2026, 8, 6, tzinfo=UTC),
        )
        assert receipt.results[0].downstream.status == "not_available_as_of"
        assert (
            receipt.results[0].downstream.reason
            == "no canonical fact matched the document expectation"
        )
    finally:
        conn.close()


def test_report_segment_reader_prefers_issuer_breakouts_and_labels_vendor_fallback() -> None:
    rows: list[dict[str, object]] = [
        {
            "period_end": "2026-06-30",
            "segment_name": "Product/Service",
            "metric": "revenue_by_product",
            "source_type": "fmp",
        },
        {
            "period_end": "2026-06-30",
            "segment_name": "Commerce",
            "metric": "revenue_by_product",
            "source_type": "ir_doc",
        },
        {
            "period_end": "2026-03-31",
            "segment_name": "Product/Service",
            "metric": "revenue_by_product",
            "source_type": "fmp",
        },
    ]
    preferred = prefer_issuer_rows(rows)
    names = {(str(row["period_end"]), str(row["segment_name"])) for row in preferred}
    # Commerce is a partial issuer breakout, not a replacement for the
    # distinct vendor Product/Service cell.
    assert ("2026-06-30", "Product/Service") in names
    assert ("2026-06-30", "Commerce") in names
    assert ("2026-03-31", "Product/Service") in names
    fallback_grid = build_grids(
        [
            {
                "period_end": "2026-03-31",
                "segment_name": "Product/Service",
                "metric": "revenue_by_product",
                "value": 100_000_000,
                "source_type": "fmp",
            }
        ],
        [(2026, 1)],
        ["2026 Q1"],
    )
    assert fallback_grid["revenue_by_product"][0].source_label == "vendor fallback"


def test_report_segment_grid_keeps_brl_out_of_usd_display_units() -> None:
    grid = build_grids(
        [
            {
                "period_end": "2026-03-31",
                "segment_name": "Commerce",
                "metric": "revenue_by_product",
                "value": 100_000_000,
                "unit": "actual",
                "currency": "BRL",
                "source_type": "ir_doc",
            }
        ],
        [(2026, 1)],
        ["2026 Q1"],
    )
    series = grid["revenue_by_product"][0]
    assert series.unit == "BRL millions"
    assert series.values == [100.0]


def test_report_segment_other_never_combines_currency_or_source_provenance() -> None:
    grid = build_grids(
        [
            {
                "period_end": "2026-03-31",
                "segment_name": "Tiny USD",
                "metric": "revenue_by_product",
                "value": 500_000,
                "unit": "actual",
                "currency": "USD",
                "source_type": "ir_doc",
            },
            {
                "period_end": "2026-03-31",
                "segment_name": "Tiny BRL",
                "metric": "revenue_by_product",
                "value": 500_000,
                "unit": "actual",
                "currency": "BRL",
                "source_type": "fmp",
            },
            {
                "period_end": "2026-03-31",
                "segment_name": "Material USD",
                "metric": "revenue_by_product",
                "value": 100_000_000,
                "unit": "actual",
                "currency": "USD",
                "source_type": "ir_doc",
            },
        ],
        [(2026, 1)],
        ["2026 Q1"],
    )
    others = [row for row in grid["revenue_by_product"] if row.segment_name.startswith("Other")]
    assert {(row.unit, row.source_label, row.values[0]) for row in others} == {
        ("USD millions", "issuer-reported", 0.5),
    }
    assert any(
        row.segment_name == "Tiny BRL"
        and row.unit == "BRL millions"
        and row.source_label == "vendor fallback"
        for row in grid["revenue_by_product"]
    )


def test_canonicalized_segment_alias_collision_stays_tier_first() -> None:
    rows: list[dict[str, object]] = [
        {
            "period_end": "2026-06-30",
            "segment_name": "Merchant",
            "metric": "revenue_by_product",
            "source_type": "ir_doc",
            "source_quality_tier": "llm_extracted",
        },
        {
            "period_end": "2026-06-30",
            "segment_name": "Commerce",
            "metric": "revenue_by_product",
            "source_type": "fmp",
            "source_quality_tier": "fmp_normalized",
        },
    ]
    selected = apply_segment_rules(rows, TickerRules(segment_name_aliases={"Merchant": "Commerce"}))
    assert len(selected) == 1
    assert selected[0]["source_type"] == "fmp"


def test_segment_revision_winner_is_deterministic_under_input_reversal() -> None:
    rows: list[dict[str, object]] = [
        {
            "period_end": "2026-06-30",
            "segment_name": "Commerce",
            "metric": "revenue_by_product",
            "unit": "actual",
            "currency": "BRL",
            "source_type": "ir_doc",
            "source_quality_tier": "ir_doc",
            "fetched_at": "2026-08-01T00:00:00+00:00",
            "document_id": 11,
            "segment_period_id": 21,
            "segment_dimension_id": 31,
        },
        {
            "period_end": "2026-06-30",
            "segment_name": "Commerce",
            "metric": "revenue_by_product",
            "unit": "actual",
            "currency": "BRL",
            "source_type": "ir_doc",
            "source_quality_tier": "ir_doc",
            "fetched_at": "2026-08-02T00:00:00+00:00",
            "document_id": 12,
            "segment_period_id": 22,
            "segment_dimension_id": 32,
        },
    ]
    assert prefer_issuer_rows(rows)[0]["segment_dimension_id"] == 32
    assert prefer_issuer_rows(list(reversed(rows)))[0]["segment_dimension_id"] == 32


def test_report_kpi_reader_prefers_issuer_document_over_later_vendor_row() -> None:
    conn = _conn()
    try:
        _document(conn, doc_id=1, ticker="NU", source_type="ir_doc", fetched_at="2026-08-05")
        _document(conn, doc_id=2, ticker="NU", source_type="fmp", fetched_at="2026-08-06")
        _kpi(
            conn,
            definition_id=1,
            fact_id=1,
            ticker="NU",
            name="Monthly active customers",
            value="100",
            doc_id=1,
        )
        conn.execute(
            "INSERT INTO kpi_facts "
            "(id, ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, currency, source_doc_id) "
            "VALUES (2, 'NU', '2026-06-30', 'Q2', 1, '90', 'actual', 'USD', 2)"
        )
        series = kpi_series_for(conn, "NU", "Monthly active customers", ["2026 Q2"], ["2026 Q2"])
        assert series is not None
        assert series.values == [100.0]
    finally:
        conn.close()


def test_report_kpi_reconciles_aliases_before_tier_first_selection() -> None:
    conn = _conn()
    try:
        conn.execute("ALTER TABLE documents ADD COLUMN source_quality_tier TEXT")
        _document(conn, doc_id=1, ticker="MELI", source_type="ir_doc", fetched_at="2026-08-05")
        _document(conn, doc_id=2, ticker="MELI", source_type="fmp", fetched_at="2026-08-06")
        conn.execute("UPDATE documents SET source_quality_tier = 'llm_extracted' WHERE id = 1")
        conn.execute("UPDATE documents SET source_quality_tier = 'fmp_normalized' WHERE id = 2")
        _kpi(
            conn,
            definition_id=1,
            fact_id=1,
            ticker="MELI",
            name="Total Payment Volume",
            value="900",
            doc_id=1,
        )
        _kpi(
            conn,
            definition_id=2,
            fact_id=2,
            ticker="MELI",
            name="Total Payment Volume (USD)",
            value="1000",
            doc_id=2,
        )
        series = kpi_series_for(conn, "MELI", "Total Payment Volume", ["2026 Q2"], ["2026 Q2"])
        assert series is not None
        # Higher quality vendor source wins; issuer origin only resolves a tie.
        assert series.values == [1000.0]
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("ticker", "name", "source_type"),
    [
        ("MELI", "Commerce", "ir_doc"),
        ("NVO", "Obesity care sales", "ir_doc"),
        ("NU", "Monthly active customers", "ir_doc"),
        ("SPARSE", "Reported deposits", "sec_xbrl"),
    ],
)
def test_foreign_and_sparse_issuer_replays_are_deterministic(
    ticker: str, name: str, source_type: str
) -> None:
    conn = _conn()
    try:
        _document(conn, doc_id=1, ticker=ticker, source_type=source_type, fetched_at="2026-08-05")
        _kpi(
            conn,
            definition_id=1,
            fact_id=1,
            ticker=ticker,
            name=name,
            value="100",
            doc_id=1,
        )
        first = build_document_coverage_receipt(
            conn,
            document_id=1,
            expected=(_expected(ticker, name),),
            extracted_at=datetime(2026, 8, 5, tzinfo=UTC),
        )
        second = build_document_coverage_receipt(
            conn,
            document_id=1,
            expected=(_expected(ticker, name),),
            extracted_at=datetime(2026, 8, 5, tzinfo=UTC),
        )
        assert first.model_dump(mode="json") == second.model_dump(mode="json")
        assert first.results[0].coverage_status == "captured"
    finally:
        conn.close()
