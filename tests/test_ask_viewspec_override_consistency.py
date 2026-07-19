"""ask ↔ ViewSpec ↔ report consistency for company-doc overrides (S2).

Investigation finding G4: ``fact_overrides`` was invisible to the ask engine on
BOTH retrieval paths — the narrative fact channel (``ask.grounding``) and the
ViewSpec/DIY data loaders (``timeseries.loaders.*_with_provenance``) read raw
kpi_facts/financial_facts with NO override overlay, so ask answered the STALE FMP
number while the report (which DOES overlay) showed the CORRECTED company-doc
figure.

These tests seed an active ``replace`` (and a ``drop``) override and prove the
overridden value now flows through ALL THREE surfaces identically:

* ask narrative  — ``gather_evidence`` fact text + cited chip source,
* ViewSpec/DIY   — ``execute_view`` + ``load_*_series_with_provenance``,
* report         — ``load_financial_series`` (the financials section's reader).

The chip must describe the WINNING (override) row, never the FMP row whose value
was superseded — value/chip divergence is exactly what these readers prevent.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ask.grounding import gather_evidence
from provenance import overrides
from provenance.overrides import OverrideAction
from timeseries.loaders import (
    load_financial_series,
    load_financial_series_with_provenance,
    load_kpi_series_with_provenance,
)
from viewspec.engine import execute_view
from viewspec.spec import ViewSpec

_KPI = "Google Cloud revenue growth"

# FMP's contaminated Q4'25 numbers vs the company-document (8-K / IR) truth.
_FMP_REVENUE_Q4 = 20_941_000_000.0  # humanizes to "20.94B"
_OV_REVENUE_Q4 = 17_664_000_000.0  # humanizes to "17.66B"
_FMP_GCP_GROWTH_Q4 = 75.0
_OV_GCP_GROWTH_Q4 = 48.0

_OVERRIDES_DDL = """
CREATE TABLE fact_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    ticker TEXT NOT NULL,
    period_end TEXT NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    fact_kind TEXT NOT NULL,
    fact_key TEXT NOT NULL,
    action TEXT NOT NULL,
    value NUMERIC,
    unit TEXT,
    value_json TEXT,
    source_doc_type TEXT NOT NULL,
    source_accession TEXT,
    source_exhibit TEXT,
    source_url TEXT,
    source_excerpt TEXT,
    source_doc_id INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    confidence REAL,
    rationale TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    retired_at TEXT,
    locator TEXT,
    CHECK (fact_kind IN ('financial_fact', 'segment', 'kpi')),
    CHECK (action IN ('replace', 'drop', 'qualify')),
    CHECK (status IN ('active', 'retired'))
);
CREATE UNIQUE INDEX uq_fact_overrides_active ON fact_overrides
    (user_id, ticker, period_end, fiscal_period_type, fact_kind, fact_key)
    WHERE status = 'active';
"""

_SCHEMA_DDL = (
    """
    CREATE TABLE documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        source_type TEXT NOT NULL,
        doc_type TEXT NOT NULL,
        file_path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        fetched_at TIMESTAMP NOT NULL,
        fetch_status TEXT NOT NULL,
        raw_bytes_size INTEGER NOT NULL DEFAULT 0,
        source_url TEXT,
        source_quality_tier TEXT NOT NULL DEFAULT 'fmp_normalized',
        accession_number TEXT,
        filing_date TEXT
    );
    CREATE TABLE kpi_definitions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        name TEXT NOT NULL,
        unit TEXT NOT NULL DEFAULT 'actual'
    );
    CREATE TABLE kpi_facts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        period_end TIMESTAMP NOT NULL,
        fiscal_period_type TEXT NOT NULL,
        kpi_definition_id INTEGER NOT NULL,
        value TEXT NOT NULL,
        unit TEXT NOT NULL DEFAULT 'actual',
        source_doc_id INTEGER NOT NULL,
        locator TEXT,
        confidence REAL,
        extracted_by TEXT,
        computed_from TEXT
    );
    CREATE TABLE financial_facts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        period_end TIMESTAMP NOT NULL,
        fiscal_period_type TEXT NOT NULL,
        line_item TEXT NOT NULL,
        value TEXT NOT NULL,
        unit TEXT NOT NULL DEFAULT 'actual',
        source_doc_id INTEGER NOT NULL,
        locator TEXT,
        confidence REAL,
        extracted_by TEXT
    );
    """
    + _OVERRIDES_DDL
)


def _seed(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_DDL)
    # Doc 1: the FMP statement (the contaminated source). Doc 2: the SEC 8-K the
    # override cites — it must win the chip once an override is active.
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256, "
        "fetched_at, fetch_status, source_url, source_quality_tier) VALUES "
        "(1, 'GOOG', 'fmp', 'fmp_income_statement', 'data/historical/fmp/GOOG_inc.json', ?, "
        "'2026-01-05 10:00:00', 'ok', 'https://fmp.example/goog.json', 'fmp_normalized')",
        ("0" * 64,),
    )
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256, "
        "fetched_at, fetch_status, source_url, source_quality_tier, accession_number, "
        "filing_date) VALUES (2, 'GOOG', 'sec_edgar', 'sec_8k', 'data/sec/GOOG_8k.htm', ?, "
        "'2026-02-01 10:00:00', 'ok', 'https://sec.example/goog-8k', 'sec_official', "
        "'0001652044-26-000012', '2026-02-01')",
        ("1" * 64,),
    )
    # FMP's (contaminated) GOOG revenue: Q2/Q3/Q4 2025, all from the FMP doc.
    conn.executemany(
        "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item, "
        "value, unit, source_doc_id) VALUES ('GOOG', ?, ?, 'revenue', ?, 'actual', 1)",
        [
            ("2025-06-30 00:00:00", "Q2", 96_400_000_000.0),
            ("2025-09-30 00:00:00", "Q3", 88_300_000_000.0),
            ("2025-12-31 00:00:00", "Q4", _FMP_REVENUE_Q4),
        ],
    )
    # FMP's (wrong) Google Cloud growth KPI: Q3 70%, Q4 75%.
    conn.execute(
        "INSERT INTO kpi_definitions (id, ticker, name, unit) VALUES (1, 'GOOG', ?, ?)",
        (_KPI, "percent"),
    )
    conn.executemany(
        "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, "
        "value, unit, source_doc_id) VALUES ('GOOG', ?, ?, 1, ?, 'percent', 1)",
        [("2025-09-30 00:00:00", "Q3", 70.0), ("2025-12-31 00:00:00", "Q4", _FMP_GCP_GROWTH_Q4)],
    )
    conn.commit()


def _seed_revenue_override(conn: sqlite3.Connection, *, action: OverrideAction) -> None:
    """An active 8-K override on GOOG Q4'25 revenue (``replace`` or ``drop``)."""
    overrides.record_override(
        conn,
        ticker="GOOG",
        period_end="2025-12-31",
        fiscal_period_type="Q4",
        fact_kind=overrides.FINANCIAL_FACT,
        fact_key="revenue",
        action=action,
        value=_OV_REVENUE_Q4 if action == OverrideAction.REPLACE else None,
        unit="actual",
        source_doc_type="sec_8k",
        source_accession="0001652044-26-000012",
        source_url="https://sec.example/goog-8k",
        source_doc_id=2,
        confidence=1.0,
        created_by="test",
    )
    conn.commit()


def _seed_kpi_override(conn: sqlite3.Connection) -> None:
    overrides.record_override(
        conn,
        ticker="GOOG",
        period_end="2025-12-31",
        fiscal_period_type="Q4",
        fact_kind=overrides.KPI,
        fact_key=_KPI,
        action=OverrideAction.REPLACE,
        value=_OV_GCP_GROWTH_Q4,
        unit="percent",
        source_doc_type="ir_press_release",
        created_by="test",
    )
    conn.commit()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    # Mirror the production layout so load_financial_series's repo_root default
    # would resolve here too; tests pass db_path explicitly regardless.
    path = tmp_path / "data" / "portfolio.db"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _seed(conn)
    conn.close()
    return path


def _conn(db: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    return c


def _ask_revenue_item(db: Path):
    """The ask-narrative fact item for GOOG revenue (None if not retrieved)."""
    items = gather_evidence(
        "GOOG revenue and Google Cloud revenue growth this quarter",
        repo_root=db.parent.parent,
        db_path=db,
        scope_tickers=["GOOG"],
    )
    return next((i for i in items if i.label == "GOOG · Revenue"), None)


def _ask_kpi_item(db: Path):
    items = gather_evidence(
        "GOOG revenue and Google Cloud revenue growth this quarter",
        repo_root=db.parent.parent,
        db_path=db,
        scope_tickers=["GOOG"],
    )
    return next((i for i in items if i.label == f"GOOG · {_KPI}"), None)


def _viewspec_q4_cell(db: Path, metric: object):
    """The Q4'25 ViewCell for one metric (the /api/viewspec/run engine path).

    ``metric`` is a metric entry as the spec accepts it — a token string
    (``"fin:revenue"``) or a dict (``{"domain": "kpi", "key": ...}``)."""
    result = execute_view(
        ViewSpec.from_dict(
            {
                "tickers": ["GOOG"],
                "metrics": [metric],
                "transform": "level",
                "cadence": "quarterly",
                "periods": 8,
            }
        ),
        db_path=db,
    )
    assert result.rows, result.warnings
    idx = result.period_labels.index("Q4'25")
    return result.rows[0].cells[idx]


# ---------------------------------------------------------------------------
# replace: all three surfaces return the overridden value
# ---------------------------------------------------------------------------


def test_no_override_all_surfaces_show_fmp(db: Path) -> None:
    """Baseline: without an override, every surface shows the FMP figure."""
    item = _ask_revenue_item(db)
    assert item is not None
    assert "20.94B" in item.text  # FMP value

    cell = _viewspec_q4_cell(db, "fin:revenue")
    assert cell.raw == _FMP_REVENUE_Q4

    series = load_financial_series("GOOG", "revenue", db_path=db)
    by_date = {str(o.period_end)[:10]: o.value for o in series}
    assert by_date["2025-12-31"] == _FMP_REVENUE_Q4


def test_replace_override_agrees_across_ask_viewspec_report(db: Path) -> None:
    conn = _conn(db)
    _seed_revenue_override(conn, action=OverrideAction.REPLACE)
    conn.close()

    # 1) ask narrative — text carries the overridden value, NOT the FMP one, and
    #    the cited chip describes the 8-K (the winning row), not the FMP doc.
    item = _ask_revenue_item(db)
    assert item is not None
    assert "17.66B" in item.text  # overridden value
    assert "20.94B" not in item.text  # stale FMP value gone
    assert "source: sec_8k" in item.text
    assert item.doc_type == "sec_8k"
    assert item.source_url == "https://sec.example/goog-8k"
    assert item.href == "/source/2"  # the 8-K document, not the FMP doc (1)

    # 2) ViewSpec / DIY — the cell value AND its chip come from the 8-K.
    cell = _viewspec_q4_cell(db, "fin:revenue")
    assert cell.raw == _OV_REVENUE_Q4
    assert cell.source is not None
    assert cell.source.source == "sec_8k"
    assert cell.source.doc_id == 2

    # 3) report — the financials section's plain reader overlays the same value.
    series = load_financial_series("GOOG", "revenue", db_path=db)
    report_q4 = {str(o.period_end)[:10]: o.value for o in series}["2025-12-31"]

    # All three agree on the corrected figure.
    assert cell.raw == report_q4 == _OV_REVENUE_Q4


def test_replace_override_kpi_agrees_ask_and_viewspec(db: Path) -> None:
    conn = _conn(db)
    _seed_kpi_override(conn)
    conn.close()

    item = _ask_kpi_item(db)
    assert item is not None
    assert "Q4'25 48" in item.text  # overridden
    assert "Q4'25 75" not in item.text  # FMP value gone
    assert "Q3'25 70" in item.text  # untouched quarter

    cell = _viewspec_q4_cell(db, {"domain": "kpi", "key": _KPI})
    assert cell.raw == _OV_GCP_GROWTH_Q4
    assert cell.source is not None
    assert cell.source.source == "ir_press_release"


def test_with_provenance_loader_chip_describes_override(db: Path) -> None:
    """The DIY picker reader: the winning observation carries the 8-K provenance,
    never the superseded FMP row's — the divergence this loader exists to prevent."""
    conn = _conn(db)
    _seed_revenue_override(conn, action=OverrideAction.REPLACE)
    conn.close()

    sourced = load_financial_series_with_provenance("GOOG", "revenue", db_path=db)
    q4 = next(o for o in sourced if str(o.period_end)[:10] == "2025-12-31")
    assert q4.value == _OV_REVENUE_Q4
    assert q4.provenance["source"] == "sec_8k"
    assert q4.provenance["accession_number"] == "0001652044-26-000012"
    assert q4.provenance["source_doc_id"] == 2
    assert q4.provenance.get("override")  # the "overridden by" chip label
    # An untouched quarter keeps its FMP provenance.
    q3 = next(o for o in sourced if str(o.period_end)[:10] == "2025-09-30")
    assert q3.provenance["source"] == "fmp_normalized"

    kpi_conn = _conn(db)
    _seed_kpi_override(kpi_conn)
    kpi_conn.close()
    kpi_sourced = load_kpi_series_with_provenance("GOOG", _KPI, db_path=db)
    kpi_q4 = next(o for o in kpi_sourced if str(o.period_end)[:10] == "2025-12-31")
    assert kpi_q4.value == _OV_GCP_GROWTH_Q4
    assert kpi_q4.provenance["source"] == "ir_press_release"


# ---------------------------------------------------------------------------
# drop: the period is omitted everywhere
# ---------------------------------------------------------------------------


def test_drop_override_omits_period_across_surfaces(db: Path) -> None:
    conn = _conn(db)
    _seed_revenue_override(conn, action=OverrideAction.DROP)
    conn.close()

    item = _ask_revenue_item(db)
    assert item is not None
    assert "Q4'25" not in item.text  # dropped from the narrative series
    assert "Q3'25" in item.text  # earlier quarters remain

    # A two-metric view keeps the Q4'25 column alive (the KPI still has Q4), so
    # the dropped revenue datum reads as an empty cell, not a vanished column.
    result = execute_view(
        ViewSpec.from_dict(
            {
                "tickers": ["GOOG"],
                "metrics": ["fin:revenue", {"domain": "kpi", "key": _KPI}],
                "transform": "level",
                "cadence": "quarterly",
                "periods": 8,
            }
        ),
        db_path=db,
    )
    idx = result.period_labels.index("Q4'25")
    rev_row = next(r for r in result.rows if r.metric.domain == "fin")
    assert rev_row.cells[idx].raw is None  # dropped — no Q4 revenue datum

    series = load_financial_series("GOOG", "revenue", db_path=db)
    dates = {str(o.period_end)[:10] for o in series}
    assert "2025-12-31" not in dates
    assert "2025-09-30" in dates
