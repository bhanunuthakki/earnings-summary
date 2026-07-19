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

from models.facts import (  # noqa: E402
    DerivedInputRef,
    DerivedRef,
    FactLocator,
    LocatorKind,
    TableCellRef,
    VendorFieldRef,
)
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
    extracted_by TEXT,
    computed_from TEXT,
    formula_id INTEGER
);
CREATE TABLE segment_periods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    source_doc_id INTEGER
);
CREATE TABLE segment_dimensions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id INTEGER NOT NULL,
    dim_type TEXT NOT NULL,
    dim_name TEXT NOT NULL,
    metric TEXT NOT NULL,
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

    # --- Phase C (§2.5): a derived kpi_fact whose inputs are a financial_fact
    # (leaf, id=1 -- the fmp_json_table row seeded above) and a segment_fact
    # (leaf, segment_dimensions id=1) -----------------------------------------
    period_id = conn.execute(
        "INSERT INTO segment_periods (ticker, source_doc_id) VALUES ('GOOG', 1)"
    ).lastrowid
    # Reuses the SAME cited cell as financial_facts:1's locator (the only
    # row/column combination that actually exists in the cached statement
    # JSON seeded below) so this leaf's inline render is deterministic --
    # the point under test is "a leaf renders inline, not a doorway", not a
    # second distinct cell match.
    segment_loc = FactLocator(
        json_path="[0].revenue",
        locator_version=2,
        kind=LocatorKind.FMP_JSON_TABLE,
        table_cell=TableCellRef(
            row_label="revenue", column_header="2025-12-31", json_path="[0].revenue"
        ),
        verbatim_snippet="Google Cloud segment: 17664000000",
    )
    conn.execute(
        "INSERT INTO segment_dimensions (id, period_id, dim_type, dim_name, metric, locator, "
        "confidence, extracted_by) VALUES (1, ?, 'product', 'Google Cloud', 'revenue', ?, 0.9, "
        "'llm:test')",
        (period_id, segment_loc.to_json()),
    )

    derived_loc = FactLocator(
        locator_version=2,
        kind=LocatorKind.DERIVED,
        derived=DerivedRef(
            formula_id=7,
            display="Cloud revenue ÷ total revenue (%)",
            inputs=[
                DerivedInputRef(
                    ref="financial_fact", fact_id=1, item="revenue", tier="fmp_normalized"
                ),
                DerivedInputRef(
                    ref="segment_fact", fact_id=1, item="Google Cloud revenue", tier="llm_extracted"
                ),
            ],
        ),
    )
    conn.execute("INSERT INTO kpi_definitions (id, ticker, name) VALUES (2, 'GOOG', 'Cloud mix %')")
    conn.execute(
        "INSERT INTO kpi_facts (id, ticker, kpi_definition_id, source_doc_id, locator, "
        "confidence, extracted_by) VALUES (2, 'GOOG', 2, NULL, ?, 0.9, 'derived')",
        (derived_loc.to_json(),),
    )

    # A pre-#905 derived row: no `locator`, only the legacy `computed_from`
    # blob (pipeline.locators.derived_locator_from_computed_from's target).
    conn.execute(
        "INSERT INTO kpi_definitions (id, ticker, name) VALUES (3, 'GOOG', 'Legacy derived %')"
    )
    conn.execute(
        "INSERT INTO kpi_facts (id, ticker, kpi_definition_id, source_doc_id, locator, "
        "confidence, extracted_by, computed_from, formula_id) VALUES "
        "(3, 'GOOG', 3, NULL, NULL, 0.9, 'derived', ?, 9)",
        (
            json.dumps(
                {
                    "display": "Legacy formula",
                    "inputs": [{"ref": "financial_fact", "item": "revenue", "fact_id": 1}],
                }
            ),
        ),
    )

    # A derived row whose own input is ANOTHER derived row (id=2) -- proves
    # the recursive doorway (not an eager inline embed) for a non-leaf input.
    nested_loc = FactLocator(
        locator_version=2,
        kind=LocatorKind.DERIVED,
        derived=DerivedRef(
            formula_id=8,
            display="Something derived from the derived Cloud mix",
            inputs=[
                DerivedInputRef(ref="kpi_fact", fact_id=2, item="Cloud mix %", tier="llm_extracted")
            ],
        ),
    )
    conn.execute(
        "INSERT INTO kpi_definitions (id, ticker, name) VALUES (4, 'GOOG', 'Nested derived')"
    )
    conn.execute(
        "INSERT INTO kpi_facts (id, ticker, kpi_definition_id, source_doc_id, locator, "
        "confidence, extracted_by) VALUES (4, 'GOOG', 4, NULL, ?, 0.9, 'derived')",
        (nested_loc.to_json(),),
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


def test_peek_route_serves_derived_formula_tree(client: FlaskClient) -> None:
    resp = client.get("/api/peek/provenance/kpi_facts:2")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Cloud revenue ÷ total revenue (%)" in body
    assert "sv-cell-hit" in body


# ---------------------------------------------------------------------------
# Phase C (§2.5): the recursive derived-formula-tree peek
# ---------------------------------------------------------------------------


def test_derived_peek_shows_formula_header_and_leaf_inputs_inline(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    html = render_fact_provenance_peek(db, tmp_path, "kpi_facts:2")
    assert html is not None
    assert "Cloud revenue ÷ total revenue (%)" in html
    assert "formula #7" in html
    # Both inputs are leaves (financial_fact + segment_fact) -> their own
    # evidence renders INLINE (both hit the same cited cell -> two
    # highlighted-cell renders), not as a doorway link.
    assert html.count("sv-cell-hit") == 2
    assert "402963000000" in html
    assert "Google Cloud revenue" in html  # the segment input's item label
    assert 'data-peek-url="/api/peek/provenance/financial_facts:1"' not in html
    assert 'data-peek-url="/api/peek/provenance/segment_dimensions:1"' not in html


def test_derived_peek_recovers_legacy_computed_from_row(tmp_path: Path) -> None:
    """A pre-#905 row with NO `locator` at all but a `computed_from` blob
    still gets the recursive peek, not the legacy floor -- proves
    pipeline.locators.derived_locator_from_computed_from is actually wired
    into the dispatcher now."""
    db = _seed(tmp_path)
    html = render_fact_provenance_peek(db, tmp_path, "kpi_facts:3")
    assert html is not None
    assert "Legacy formula" in html
    assert "provenance: legacy" not in html


def test_derived_peek_nested_input_is_a_doorway_not_inline(tmp_path: Path) -> None:
    """A DERIVED input (kpi_facts:2, itself derived) becomes a clickable
    data-peek-url doorway -- NOT eagerly rendered inline -- so a wide/deep
    tree costs O(inputs-at-this-level), not an exponential eager walk."""
    db = _seed(tmp_path)
    html = render_fact_provenance_peek(db, tmp_path, "kpi_facts:4")
    assert html is not None
    assert "Something derived from the derived Cloud mix" in html
    assert 'data-peek-url="/api/peek/provenance/kpi_facts:2"' in html
    # NOT eagerly expanded -- the doorway's OWN inputs (financial_facts:1 /
    # segment_dimensions:1) are not rendered at this level.
    assert "sv-cell-hit" not in html


def test_derived_peek_depth_cap_shows_notice_not_infinite_recursion(tmp_path: Path) -> None:
    """kpi_facts:2 IS a real derived row -- starting the walk at
    depth=_MAX_DERIVED_DEPTH-1 means resolving it would exceed the cap, so
    it must degrade to a notice, never recurse (and never a doorway link,
    since a link the reader can't click deeper into is worse than saying so)."""
    from pipeline.peeks import _MAX_DERIVED_DEPTH, render_derived_peek

    db = _seed(tmp_path)
    derived = DerivedRef(
        formula_id=1,
        display="Wrapping formula",
        inputs=[DerivedInputRef(ref="kpi_fact", fact_id=2, item="Cloud mix %")],
    )
    html = render_derived_peek(db, tmp_path, derived, depth=_MAX_DERIVED_DEPTH - 1)
    assert "max depth reached" in html
    assert 'data-peek-url="/api/peek/provenance/kpi_facts:2"' not in html


def test_derived_peek_cycle_guard() -> None:
    from pipeline.peeks import render_derived_peek

    derived = DerivedRef(
        formula_id=1,
        display="Cyclical formula",
        inputs=[DerivedInputRef(ref="kpi_fact", fact_id=2, item="x")],
    )
    html = render_derived_peek(
        Path("nonexistent.db"), Path("."), derived, depth=0, visited=frozenset({"kpi_facts:2"})
    )
    assert "cycle detected" in html
