"""Provenance-chip reflection of company-doc overrides (P6).

When a `replace` override supersedes FMP for a cell, the source chip must describe
the FILING the displayed number came from (not the FMP row); a `qualify` override
adds a ⚠ note. Covers the resolver helpers, both cell-source builders (KPI +
line-item), and the chip renderer.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from provenance import overrides
from provenance.overrides import (
    OverrideAction,
    chip_override_map,
    override_chip_label,
    override_provenance,
    qualify_note,
)
from report.sections.financials import (
    _kpi_cell_sources_for,  # pyright: ignore[reportPrivateUsage]  # internal seam under test
)
from timeseries.loaders import load_financial_cell_provenance
from ui.source_chip import source_chip_html

_KPI = "Google Cloud revenue growth"

_OVERRIDES_DDL = """
CREATE TABLE fact_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL DEFAULT 'bhanu',
    ticker TEXT NOT NULL, period_end TEXT NOT NULL, fiscal_period_type TEXT NOT NULL,
    fact_kind TEXT NOT NULL, fact_key TEXT NOT NULL, action TEXT NOT NULL,
    value NUMERIC, unit TEXT, value_json TEXT, source_doc_type TEXT NOT NULL,
    source_accession TEXT, source_exhibit TEXT, source_url TEXT, source_excerpt TEXT,
    source_doc_id INTEGER, status TEXT NOT NULL DEFAULT 'active', confidence REAL,
    rationale TEXT, created_by TEXT NOT NULL, created_at TEXT NOT NULL, retired_at TEXT,
    locator TEXT,
    CHECK (fact_kind IN ('financial_fact','segment','kpi')),
    CHECK (action IN ('replace','drop','qualify')),
    CHECK (status IN ('active','retired'))
);
CREATE UNIQUE INDEX uq_fact_overrides_active ON fact_overrides
    (user_id, ticker, period_end, fiscal_period_type, fact_kind, fact_key) WHERE status='active';
"""

_SCHEMA_DDL = (
    """
    CREATE TABLE documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, source_type TEXT NOT NULL,
        doc_type TEXT NOT NULL, file_path TEXT NOT NULL, sha256 TEXT NOT NULL,
        fetched_at TIMESTAMP NOT NULL, fetch_status TEXT NOT NULL, raw_bytes_size INTEGER NOT NULL,
        source_quality_tier TEXT NOT NULL DEFAULT 'fmp_normalized', source_url TEXT,
        accession_number TEXT, filing_date TEXT
    );
    CREATE TABLE kpi_definitions (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, name TEXT);
    CREATE TABLE kpi_facts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, period_end TIMESTAMP NOT NULL,
        fiscal_period_type TEXT NOT NULL, kpi_definition_id INTEGER NOT NULL, value NUMERIC NOT NULL,
        unit TEXT NOT NULL, source_doc_id INTEGER NOT NULL, confidence REAL, extracted_by TEXT,
        locator TEXT, computed_from TEXT
    );
    CREATE TABLE financial_facts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, period_end TIMESTAMP NOT NULL,
        fiscal_period_type TEXT NOT NULL, line_item TEXT NOT NULL, value NUMERIC NOT NULL,
        unit TEXT NOT NULL DEFAULT 'actual', source_doc_id INTEGER NOT NULL, confidence REAL,
        extracted_by TEXT, locator TEXT
    );
    """
    + _OVERRIDES_DDL
)


def _seed(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_DDL)
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256, fetched_at, "
        "fetch_status, raw_bytes_size, source_quality_tier) VALUES "
        "(1,'GOOG','fmp','fmp_key_metrics','x',?, '2026-01-01', 'ok', 1, 'fmp_normalized')",
        ("0" * 64,),
    )
    conn.execute("INSERT INTO kpi_definitions (id, ticker, name) VALUES (1,'GOOG',?)", (_KPI,))
    conn.execute(
        "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, value, "
        "unit, source_doc_id, confidence, extracted_by) VALUES "
        "('GOOG','2025-12-31 00:00:00','Q4',1,75,'percent',1,0.94,'fmp')"
    )
    conn.execute(
        "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item, value, "
        "unit, source_doc_id, confidence, extracted_by) VALUES "
        "('GOOG','2025-12-31 00:00:00','Q4','revenue',20941000000,'actual',1,0.94,'fmp')"
    )
    conn.commit()


def _record(
    conn: sqlite3.Connection, *, fact_kind: str, fact_key: str, action: OverrideAction
) -> None:
    overrides.record_override(
        conn,
        ticker="GOOG",
        period_end="2025-12-31",
        fiscal_period_type="Q4",
        fact_kind=fact_kind,
        fact_key=fact_key,
        action=action,
        value=48 if action == OverrideAction.REPLACE else None,
        unit="percent",
        source_doc_type="sec_8k",
        source_accession="0001652044-26-000012",
        source_exhibit="googexhibit991q42025.htm",
        source_excerpt="Google Cloud: $17,664M",
        confidence=1.0,
        created_by="cli",
    )
    conn.commit()


def _conn(db: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    _seed(c)
    return c


def test_override_provenance_fields() -> None:
    ov = overrides.FactOverride(
        id=1,
        user_id="bhanu",
        ticker="GOOG",
        period_end="2025-12-31",
        fiscal_period_type="Q4",
        fact_kind="kpi",
        fact_key=_KPI,
        action="replace",
        value=48.0,
        unit="percent",
        value_json=None,
        source_doc_type="sec_8k",
        source_accession="0001652044-26-000012",
        source_exhibit="ex.htm",
        source_url="https://sec.gov/x",
        source_excerpt="q",
        source_doc_id=None,
        confidence=1.0,
        rationale=None,
        created_by="cli",
        created_at="2026-06-15",
        status="active",
    )
    prov = override_provenance(ov)
    assert prov["source"] == "sec_8k"
    assert prov["accession_number"] == "0001652044-26-000012"
    assert prov["extracted_by"] == "cli"
    assert prov["override"] == "sec_8k · 0001652044-26-000012 · ex.htm"
    assert prov["computed_from"] is None  # FMP lineage cleared
    assert override_chip_label(ov).startswith("sec_8k")
    assert qualify_note(ov).startswith("⚠ qualified by sec_8k")


def test_chip_override_map_keeps_qualify_drops_drop(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "p.db")
    _record(conn, fact_kind=overrides.KPI, fact_key=_KPI, action=OverrideAction.QUALIFY)
    m = chip_override_map(
        conn, ticker="GOOG", fact_kind=overrides.KPI, fact_key=_KPI, period_types=("Q4",)
    )
    assert "2025-12-31" in m and m["2025-12-31"].action == "qualify"
    # A different period type is excluded.
    assert (
        chip_override_map(
            conn, ticker="GOOG", fact_kind=overrides.KPI, fact_key=_KPI, period_types=("FY",)
        )
        == {}
    )
    conn.close()


def test_kpi_chip_reflects_replace(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "p.db")
    _record(conn, fact_kind=overrides.KPI, fact_key=_KPI, action=OverrideAction.REPLACE)
    sources = _kpi_cell_sources_for(conn, "GOOG", _KPI, ("Q1", "Q2", "Q3", "Q4"))
    conn.close()
    cs = sources["2025-12-31"]
    assert cs.override == "sec_8k · 0001652044-26-000012 · googexhibit991q42025.htm"
    assert cs.source == "sec_8k"  # no longer the FMP tier
    assert cs.accession_number == "0001652044-26-000012"
    assert cs.extracted_by == "cli"
    # The chip renders the override row.
    assert "overridden by" in source_chip_html(cs)


def test_kpi_chip_qualify_adds_warn(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "p.db")
    _record(conn, fact_kind=overrides.KPI, fact_key=_KPI, action=OverrideAction.QUALIFY)
    sources = _kpi_cell_sources_for(conn, "GOOG", _KPI, ("Q1", "Q2", "Q3", "Q4"))
    conn.close()
    cs = sources["2025-12-31"]
    assert cs.override is None  # qualify doesn't replace the source
    assert any("qualified by sec_8k" in s for s in cs.issues)
    assert "⚠" in source_chip_html(cs)


def test_line_item_chip_reflects_replace(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    conn = _conn(db)
    _record(
        conn, fact_kind=overrides.FINANCIAL_FACT, fact_key="revenue", action=OverrideAction.REPLACE
    )
    conn.close()
    prov = load_financial_cell_provenance("GOOG", ["revenue"], db_path=db)
    cell = prov["revenue"]["2025-12-31"]
    assert cell["override"] == "sec_8k · 0001652044-26-000012 · googexhibit991q42025.htm"
    assert cell["source"] == "sec_8k"
    assert cell["accession_number"] == "0001652044-26-000012"


def test_no_override_leaves_chip_fmp(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "p.db")
    sources = _kpi_cell_sources_for(conn, "GOOG", _KPI, ("Q1", "Q2", "Q3", "Q4"))
    conn.close()
    cs = sources["2025-12-31"]
    assert cs.override is None
    assert cs.source == "fmp_normalized"
    assert "overridden by" not in source_chip_html(cs)
