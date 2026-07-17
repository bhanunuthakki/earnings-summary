"""Tests for P3.2 extractor locator wiring: fact writers populate
FactLocator json_paths going forward, and the KPI persist path carries
source_excerpt + locator end-to-end (with pre-0075/pre-0033 schema
tolerance preserved).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from compute._common import extract_facts_with_spec
from compute.as_reported import extract_facts_from_record as extract_as_reported
from compute.income_statement import extract_income_statement_facts
from compute.kpi_extract_summaries import _build_manifest  # pyright: ignore[reportPrivateUsage]
from models.facts import FactLocator, FiscalPeriodType, Unit
from models.fmp_payloads import FmpAsReportedRecord, FmpIncomeStatementRecord
from pipeline.kpi_persistence import (
    KpiExtractionManifest,
    KpiValue,
    persist_manifest,
)
from pipeline.sec_xbrl import insert_facts_from_companyfacts

_INCOME_RECORD: dict[str, object] = {
    "date": "2025-12-31",
    "symbol": "GOOG",
    "reportedCurrency": "USD",
    "period": "FY",
    "revenue": 402_963_000_000,
    "netIncome": 132_170_000_000,
}


def _create_financial_schema(conn: sqlite3.Connection) -> None:
    """Post-0075 shape: audit columns + locator on financial_facts."""
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            source_type TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            period_end TIMESTAMP,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            fetched_at TIMESTAMP NOT NULL,
            fetch_status TEXT NOT NULL,
            raw_bytes_size INTEGER NOT NULL
        );
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            line_item TEXT NOT NULL,
            value NUMERIC(24, 6) NOT NULL,
            currency TEXT,
            unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL,
            confidence REAL NOT NULL DEFAULT 1.0,
            extracted_by TEXT,
            supersedes_id INTEGER,
            locator TEXT
        );
        CREATE UNIQUE INDEX uq_financial_facts_provenance
        ON financial_facts (ticker, period_end, fiscal_period_type, line_item, source_doc_id);
        """
    )
    conn.commit()


def _create_kpi_schema(conn: sqlite3.Connection, *, with_p32_columns: bool) -> None:
    """kpi_* tables; post-0075 when with_p32_columns, pre-0033/0075 otherwise."""
    extra = "" if not with_p32_columns else ", source_excerpt TEXT, locator TEXT"
    conn.executescript(
        f"""
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            unit TEXT NOT NULL,
            primary_source TEXT NOT NULL,
            fallback_source TEXT,
            ir_url TEXT,
            threshold_tier TEXT,
            threshold_low FLOAT,
            threshold_high FLOAT,
            notes TEXT,
            UNIQUE(ticker, name)
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            kpi_definition_id INTEGER NOT NULL,
            value NUMERIC(24, 6) NOT NULL,
            unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL,
            confidence FLOAT NOT NULL DEFAULT 1.0,
            extracted_by TEXT,
            supersedes_id INTEGER{extra}
        );
        CREATE UNIQUE INDEX uq_kpi_facts_provenance
        ON kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, source_doc_id);
        CREATE TABLE validation_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            source_doc_id INTEGER,
            ticker TEXT,
            severity TEXT NOT NULL,
            rule TEXT NOT NULL,
            raw_value TEXT,
            expected TEXT,
            raised_at TIMESTAMP NOT NULL,
            resolved_at TIMESTAMP
        );
        """
    )
    conn.commit()


def _mem_conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


# ----------------------------------------------------------------------------
# FMP statement extractors: [i].<field> json_path
# ----------------------------------------------------------------------------


def test_extract_facts_with_spec_builds_record_indexed_locator() -> None:
    record = FmpIncomeStatementRecord.model_validate(_INCOME_RECORD)
    spec: list[tuple[str, str, Unit]] = [("revenue", "revenue", Unit.ACTUAL)]

    with_locator = extract_facts_with_spec(record, 1, spec, record_index=3)
    loc = with_locator[0].locator
    assert loc is not None
    # locator_version=2, kind=fmp_json_table -- table_cell.row_label/
    # column_header enrich the bare v1 json_path (docs/design/
    # provenance_clickthrough.md section 3.2's per-extractor enrichment).
    assert loc.json_path == "[3].revenue"
    assert loc.locator_version == 2
    assert loc.table_cell is not None
    assert loc.table_cell.row_label == "revenue"
    assert loc.table_cell.column_header == "2025-12-31"
    assert loc.table_cell.json_path == "[3].revenue"
    assert loc.verbatim_snippet

    without = extract_facts_with_spec(record, 1, spec)
    assert without[0].locator is None


def test_income_statement_extractor_persists_locator(tmp_path: Path) -> None:
    conn = _mem_conn()
    _create_financial_schema(conn)
    fmp_dir = tmp_path / "data" / "historical" / "fmp"
    fmp_dir.mkdir(parents=True)
    second = {**_INCOME_RECORD, "date": "2024-12-31", "revenue": 350_000_000_000}
    (fmp_dir / "GOOG_income_statement_annual.json").write_text(
        json.dumps([_INCOME_RECORD, second]), encoding="utf-8"
    )
    conn.execute(
        "INSERT INTO documents "
        "(ticker, source_type, doc_type, file_path, sha256, fetched_at, fetch_status, raw_bytes_size) "
        "VALUES ('GOOG', 'fmp', 'fmp_income_statement', "
        "'data/historical/fmp/GOOG_income_statement_annual.json', 'a', ?, 'ok', 1)",
        (datetime.now(),),
    )
    doc_id = conn.execute("SELECT id FROM documents").fetchone()["id"]

    assert extract_income_statement_facts(conn, doc_id, project_root=tmp_path) > 0

    rows = conn.execute(
        "SELECT period_end, line_item, locator FROM financial_facts WHERE line_item = 'revenue' "
        "ORDER BY period_end DESC"
    ).fetchall()
    locators = [FactLocator.from_json(r["locator"]) for r in rows]
    assert all(
        loc is not None and loc.json_path == expected
        for loc, expected in zip(locators, ["[0].revenue", "[1].revenue"], strict=True)
    )
    loc = locators[0]
    assert loc is not None
    assert loc.json_path == "[0].revenue"
    assert loc.locator_version == 2
    assert loc.table_cell is not None
    assert loc.table_cell.row_label == "revenue"
    conn.close()


def test_as_reported_locator_points_into_data_dict() -> None:
    record = FmpAsReportedRecord.model_validate(
        {
            "date": "2025-09-30",
            "symbol": "NOW",
            "reportedCurrency": "USD",
            "period": "Q3",
            "data": {"revenueremainingperformanceobligation": 21_500_000_000},
        }
    )
    facts = extract_as_reported(record, source_doc_id=9, record_index=2)
    assert facts[0].line_item == "rpo"
    loc = facts[0].locator
    assert loc is not None
    assert loc.json_path == "[2].data.revenueremainingperformanceobligation"
    assert loc.locator_version == 2
    assert loc.table_cell is not None
    assert loc.table_cell.row_label == "revenueremainingperformanceobligation"
    assert extract_as_reported(record, source_doc_id=9)[0].locator is None


# ----------------------------------------------------------------------------
# sec_xbrl companyfacts: facts.<ns>.<tag>.units.<unit>[<i>] json_path
# ----------------------------------------------------------------------------


def test_sec_xbrl_companyfacts_locator(tmp_path: Path) -> None:
    conn = _mem_conn()
    _create_financial_schema(conn)
    conn.execute(
        "INSERT INTO documents "
        "(ticker, source_type, doc_type, file_path, sha256, fetched_at, fetch_status, raw_bytes_size) "
        "VALUES ('GOOG', 'sec_xbrl', 'sec_10q', 'data/historical/sec/GOOG_companyfacts.json#accn=0001-01-000001', "
        "'b', ?, 'ok', 1)",
        (datetime.now(),),
    )
    doc_id = conn.execute("SELECT id FROM documents").fetchone()["id"]

    payload: dict[str, object] = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            # Index 0: skipped (accession not registered).
                            {
                                "accn": "0009-99-999999",
                                "end": "2024-03-31",
                                "start": "2024-01-01",
                                "val": 1,
                                "fp": "Q1",
                            },
                            {
                                "accn": "0001-01-000001",
                                "end": "2025-03-31",
                                "start": "2025-01-01",
                                "val": 80_500_000_000,
                                "fp": "Q1",
                            },
                        ]
                    }
                }
            }
        }
    }
    inserted = insert_facts_from_companyfacts(
        conn,
        ticker="GOOG",
        payload=payload,
        accession_to_doc_id={"0001-01-000001": doc_id},
    )
    assert inserted == 1
    row = conn.execute("SELECT line_item, locator FROM financial_facts").fetchone()
    assert row["line_item"] == "revenue"
    loc = FactLocator.from_json(row["locator"])
    assert loc is not None
    # Index 1 in the source array — the skipped entry still advances the index.
    assert loc.json_path == "facts.us-gaap.Revenues.units.USD[1]"
    conn.close()


# ----------------------------------------------------------------------------
# KPI persist path: source_excerpt + locator
# ----------------------------------------------------------------------------


def _manifest(values: list[KpiValue]) -> KpiExtractionManifest:
    return KpiExtractionManifest(
        ticker="NU",
        period_end=datetime(2026, 3, 31),
        fiscal_period_type=FiscalPeriodType.Q1,
        source_doc_id=77,
        values=values,
    )


def test_persist_manifest_writes_excerpt_and_locator() -> None:
    conn = _mem_conn()
    _create_kpi_schema(conn, with_p32_columns=True)
    values = [
        KpiValue(
            name="NIM",
            value=Decimal("17.8"),
            unit=Unit.PERCENT,
            source_excerpt="Net interest margin reached 17.8% in the quarter",
            locator=FactLocator(pdf_page=7),
        ),
        KpiValue(name="ARPAC", value=Decimal("11.2"), unit=Unit.ACTUAL),
    ]
    result = persist_manifest(conn, run_id="r1", manifest=_manifest(values))
    assert result.inserted == 2

    rows = {
        r["name"]: r
        for r in conn.execute(
            "SELECT kd.name, kf.source_excerpt, kf.locator "
            "FROM kpi_facts kf JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id"
        )
    }
    assert rows["NIM"]["source_excerpt"] == "Net interest margin reached 17.8% in the quarter"
    assert FactLocator.from_json(rows["NIM"]["locator"]) == FactLocator(pdf_page=7)
    assert rows["ARPAC"]["source_excerpt"] is None
    assert rows["ARPAC"]["locator"] is None
    conn.close()


def test_persist_manifest_clips_oversized_excerpt() -> None:
    conn = _mem_conn()
    _create_kpi_schema(conn, with_p32_columns=True)
    huge = "x" * 5000
    values = [KpiValue(name="NIM", value=Decimal("1"), unit=Unit.PERCENT, source_excerpt=huge)]
    persist_manifest(conn, run_id="r1", manifest=_manifest(values))
    (stored,) = conn.execute("SELECT source_excerpt FROM kpi_facts").fetchone()
    assert len(stored) == 1024
    conn.close()


def test_persist_manifest_tolerates_pre_p32_schema() -> None:
    """A kpi_facts table without source_excerpt/locator (pre-0033/0075 test
    fixtures) must still accept the insert — the fields are dropped."""
    conn = _mem_conn()
    _create_kpi_schema(conn, with_p32_columns=False)
    values = [
        KpiValue(
            name="NIM",
            value=Decimal("17.8"),
            unit=Unit.PERCENT,
            source_excerpt="quote",
            locator=FactLocator(transcript_line=12),
        )
    ]
    result = persist_manifest(conn, run_id="r1", manifest=_manifest(values))
    assert result.inserted == 1
    conn.close()


# ----------------------------------------------------------------------------
# summaries extractor: LLM payload -> manifest passthrough
# ----------------------------------------------------------------------------


def test_build_manifest_passes_source_excerpt_through() -> None:
    extracted: dict[str, dict[str, object]] = {
        "NIM": {
            "value": 17.8,
            "unit": "percent",
            "confidence": 0.95,
            "source_excerpt": "  NIM expanded to 17.8%  ",
        },
        "ARPAC": {"value": 11.2, "unit": "actual", "confidence": 0.9},
        "Deposits": {"value": 1, "unit": "actual", "source_excerpt": 42},  # non-str -> dropped
    }
    manifest = _build_manifest("NU", datetime(2026, 3, 31), FiscalPeriodType.Q1, 5, extracted)
    by_name = {v.name: v for v in manifest.values}
    assert by_name["NIM"].source_excerpt == "NIM expanded to 17.8%"
    assert by_name["ARPAC"].source_excerpt is None
    assert by_name["Deposits"].source_excerpt is None


def test_kpi_value_accepts_manifest_json_locator() -> None:
    """The extract_kpis_from_ir manifest JSON shape parses nested locators."""
    manifest = KpiExtractionManifest.model_validate(
        {
            "ticker": "MELI",
            "period_end": "2025-09-30T00:00:00",
            "fiscal_period_type": "Q3",
            "source_doc_id": 13123,
            "values": [
                {
                    "name": "Revenue Growth (FXN)",
                    "value": "0.34",
                    "unit": "ratio",
                    "confidence": 0.95,
                    "source_excerpt": "FX-neutral revenue grew 34% YoY",
                    "locator": {"pdf_page": 7},
                }
            ],
        }
    )
    v = manifest.values[0]
    assert v.source_excerpt == "FX-neutral revenue grew 34% YoY"
    assert v.locator == FactLocator(pdf_page=7)
    assert v.locator is not None
    assert v.locator.to_json() == '{"pdf_page":7}'


def test_kpi_value_rejects_unknown_locator_type() -> None:
    with pytest.raises(ValueError):
        KpiValue.model_validate(
            {
                "name": "x",
                "value": "1",
                "unit": "percent",
                "locator": {"pdf_page": "seven"},
            }
        )
