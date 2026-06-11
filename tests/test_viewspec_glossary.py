"""Metric definition tooltips (Ask v4): the glossary module, ViewResult's
definitions map (row-label titles in the rendered fragment), and catalog
entry titles for the picker."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from viewspec.engine import execute_view, metric_catalog
from viewspec.glossary import (
    fin_definition,
    kpi_definition_titles,
    metric_definitions,
    seg_definition,
)
from viewspec.render import render_view_fragment
from viewspec.spec import MetricRef, ViewSpec

_DDL = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, source_type TEXT NOT NULL, doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL, sha256 TEXT NOT NULL, fetched_at TIMESTAMP NOT NULL,
    fetch_status TEXT NOT NULL, source_url TEXT,
    source_quality_tier TEXT NOT NULL DEFAULT 'fmp_normalized'
);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, period_end TIMESTAMP NOT NULL,
    fiscal_period_type TEXT NOT NULL, line_item TEXT NOT NULL, value TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'actual', source_doc_id INTEGER NOT NULL, locator TEXT
);
CREATE TABLE kpi_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, name TEXT NOT NULL, unit TEXT NOT NULL DEFAULT 'actual',
    primary_source TEXT, fallback_source TEXT, notes TEXT
);
CREATE TABLE kpi_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, period_end TIMESTAMP NOT NULL,
    fiscal_period_type TEXT NOT NULL, kpi_definition_id INTEGER NOT NULL,
    value TEXT NOT NULL, unit TEXT NOT NULL DEFAULT 'actual',
    source_doc_id INTEGER NOT NULL, locator TEXT
);
"""


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "g.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_DDL)
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256,"
        " fetched_at, fetch_status) VALUES (1, 'TST', 'fmp', 'fmp_income_statement',"
        " 'f.json', 'a', '2026-01-05 10:00:00', 'ok')"
    )
    conn.execute(
        "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item,"
        " value, source_doc_id) VALUES ('TST', '2025-12-31 00:00:00', 'Q4', 'revenue', 100, 1)"
    )
    conn.execute(
        "INSERT INTO kpi_definitions (id, ticker, name, unit, primary_source, notes)"
        " VALUES (7, 'TST', 'NIM', 'percent', 'ir_doc',"
        " 'Net interest margin on interest-earning assets.')"
    )
    conn.execute(
        "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id,"
        " value, unit, source_doc_id) VALUES ('TST', '2025-12-31 00:00:00', 'Q4', 7,"
        " 18.4, 'percent', 1)"
    )
    conn.commit()
    conn.close()
    return path


def test_fin_and_seg_definitions_are_static() -> None:
    assert "top line" in fin_definition("revenue")
    assert "'weird item'" in fin_definition("weird_item")  # generic fallback
    seg = seg_definition("product", "AWS", "revenue")
    assert "AWS revenue" in seg
    assert "product axis" in seg


def test_kpi_definition_titles_carry_unit_source_notes(db: Path) -> None:
    titles = kpi_definition_titles(db, ["NIM", "missing"], ["TST"])
    assert "unit: percent" in titles["NIM"]
    assert "source: ir_doc" in titles["NIM"]
    assert "interest-earning assets" in titles["NIM"]
    assert "missing" not in titles
    # Missing DB degrades to empty, never raises.
    assert kpi_definition_titles(db.parent / "nope.db", ["NIM"]) == {}


def test_metric_definitions_keyed_by_token(db: Path) -> None:
    refs = [
        MetricRef(domain="fin", key="revenue"),
        MetricRef(domain="kpi", key="NIM"),
        MetricRef(domain="seg", key="revenue", dim_type="product", dim_name="AWS"),
    ]
    defs = metric_definitions(refs, db_path=db, tickers=["TST"])
    assert "top line" in defs["fin:revenue"]
    assert "interest-earning assets" in defs["kpi:NIM"]
    assert "AWS revenue" in defs["seg:product:AWS:revenue"]


def test_execute_view_fills_definitions_and_render_titles_rows(db: Path) -> None:
    spec = ViewSpec.from_dict(
        {"tickers": ["TST"], "metrics": ["fin:revenue", "kpi:NIM"], "periods": 4}
    )
    result = execute_view(spec, db_path=db)
    assert "top line" in result.definitions["fin:revenue"]
    assert "interest-earning assets" in result.definitions["kpi:NIM"]
    html = render_view_fragment(result, include_chart=False)
    assert 'class="vx-label" title="' in html
    assert "interest-earning assets" in html


def test_metric_catalog_entries_carry_titles(db: Path) -> None:
    catalog = metric_catalog(db, ["TST"])
    fin = next(e for e in catalog["fin"] if e["token"] == "fin:revenue")
    assert "top line" in str(fin["title"])
    kpi = next(e for e in catalog["kpi"] if e["token"] == "kpi:NIM")
    assert "interest-earning assets" in str(kpi["title"])
