"""Tests for the fact-provenance peek (provenance click-through Phase A,
section 2): pipeline.peeks.render_fact_provenance_peek and its
GET /api/peek/provenance/<fact_ref> route."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest
from flask.testing import FlaskClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402

from models.facts import FactLocator, LocatorKind, TableCellRef, VendorFieldRef  # noqa: E402
from pipeline.peeks import render_fact_provenance_peek  # noqa: E402

_DDL = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    fetched_at TIMESTAMP,
    source_url TEXT,
    accession_number TEXT,
    filing_date TEXT
);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    line_item TEXT NOT NULL,
    source_doc_id INTEGER,
    locator TEXT,
    confidence REAL,
    extracted_by TEXT
);
CREATE TABLE kpi_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    name TEXT NOT NULL
);
CREATE TABLE kpi_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    kpi_definition_id INTEGER NOT NULL,
    source_doc_id INTEGER,
    locator TEXT,
    confidence REAL,
    extracted_by TEXT
);
CREATE TABLE fmp_endpoint_status (
    ticker TEXT, endpoint TEXT, period TEXT, status TEXT,
    file_path TEXT, last_pulled TEXT
);
"""


def _seed(repo: Path) -> Path:
    db = repo / "data" / "portfolio.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(_DDL)

    fmp_dir = repo / "data" / "historical" / "fmp"
    fmp_dir.mkdir(parents=True, exist_ok=True)
    (fmp_dir / "GOOG_income_statement_annual.json").write_text(
        json.dumps([{"date": "2025-12-31", "symbol": "GOOG", "revenue": 402963000000}]),
        encoding="utf-8",
    )
    (fmp_dir / "GOOG_quote.json").write_text(json.dumps([{"marketCap": 123456}]), encoding="utf-8")

    conn.execute(
        "INSERT INTO documents (id, ticker, doc_type, file_path, fetched_at) VALUES "
        "(1, 'GOOG', 'fmp_income_statement', "
        "'data/historical/fmp/GOOG_income_statement_annual.json', ?)",
        (datetime.now(),),
    )

    fmp_json_loc = FactLocator(
        json_path="[0].revenue",
        locator_version=2,
        kind=LocatorKind.FMP_JSON_TABLE,
        table_cell=TableCellRef(
            row_label="revenue", column_header="2025-12-31", json_path="[0].revenue"
        ),
        verbatim_snippet="402963000000",
    )
    conn.execute(
        "INSERT INTO financial_facts (id, ticker, line_item, source_doc_id, locator, "
        "confidence, extracted_by) VALUES (1, 'GOOG', 'revenue', 1, ?, 0.95, 'fmp')",
        (fmp_json_loc.to_json(),),
    )

    vendor_loc = FactLocator(
        locator_version=2,
        kind=LocatorKind.VENDOR_FIELD,
        vendor_field=VendorFieldRef(endpoint="quote", field="marketCap"),
    )
    conn.execute(
        "INSERT INTO financial_facts (id, ticker, line_item, source_doc_id, locator, "
        "confidence, extracted_by) VALUES (2, 'GOOG', 'market_cap', NULL, ?, 0.9, 'fmp')",
        (vendor_loc.to_json(),),
    )
    conn.execute(
        "INSERT INTO fmp_endpoint_status (ticker, endpoint, period, status, file_path, "
        "last_pulled) VALUES ('GOOG', 'quote', '', 'ok', "
        "'data/historical/fmp/GOOG_quote.json', '2026-07-01 00:00:00')"
    )

    # Legacy: locator is NULL entirely.
    conn.execute(
        "INSERT INTO financial_facts (id, ticker, line_item, source_doc_id, locator, "
        "confidence, extracted_by) VALUES (3, 'GOOG', 'old_field', 1, NULL, 0.8, 's1')"
    )

    conn.execute("INSERT INTO kpi_definitions (id, ticker, name) VALUES (1, 'GOOG', 'NIM')")
    conn.execute(
        "INSERT INTO kpi_facts (id, ticker, kpi_definition_id, source_doc_id, locator, "
        "confidence, extracted_by) VALUES (1, 'GOOG', 1, NULL, NULL, 0.7, 'llm:test')"
    )

    conn.commit()
    conn.close()
    return db


def test_fmp_json_table_peek_renders_highlighted_cell(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    html = render_fact_provenance_peek(db, tmp_path, "financial_facts:1")
    assert html is not None
    assert "sv-cell-hit" in html
    assert "402963000000" in html


def test_vendor_field_peek_shows_endpoint_and_value(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    html = render_fact_provenance_peek(db, tmp_path, "financial_facts:2")
    assert html is not None
    assert "quote" in html
    assert "marketCap" in html
    assert "123456" in html
    assert "no underlying filing" in html


def test_legacy_floor_never_dead_ends(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    html = render_fact_provenance_peek(db, tmp_path, "financial_facts:3")
    assert html is not None
    assert "provenance: legacy" in html
    assert "/source/1" in html  # still links the doc, even without a locator

    # A KPI fact with source_doc_id NULL and locator NULL -- the floor still
    # renders (no doc identity to show, but never None).
    html_kpi = render_fact_provenance_peek(db, tmp_path, "kpi_facts:1")
    assert html_kpi is not None
    assert "provenance: legacy" in html_kpi
    assert "NIM" in html_kpi


def test_unknown_fact_ref_returns_none(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    assert render_fact_provenance_peek(db, tmp_path, "financial_facts:999") is None
    assert render_fact_provenance_peek(db, tmp_path, "not_a_table:1") is None
    assert render_fact_provenance_peek(db, tmp_path, "financial_facts:abc") is None


@pytest.fixture
def client(tmp_path: Path) -> FlaskClient:
    _seed(tmp_path)
    return comments_server.create_app(tmp_path).test_client()


def test_peek_route_serves_fmp_json_table(client: FlaskClient) -> None:
    resp = client.get("/api/peek/provenance/financial_facts:1")
    assert resp.status_code == 200
    assert "sv-cell-hit" in resp.data.decode()


def test_peek_route_404s_on_missing_fact(client: FlaskClient) -> None:
    resp = client.get("/api/peek/provenance/financial_facts:999")
    assert resp.status_code == 404
