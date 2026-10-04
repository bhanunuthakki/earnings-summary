"""ask ↔ ViewSpec ↔ report consistency for company-doc overrides (S2).

Investigation finding G4: ``fact_overrides`` was invisible to the ask engine on
BOTH retrieval paths — the narrative fact channel (``ask.grounding``) and the
ViewSpec/DIY data loaders (``timeseries.loaders.*_with_provenance``) read raw
kpi_facts/financial_facts with NO override overlay, so ask answered the STALE FMP
number while the report (which DOES overlay) showed the CORRECTED company-doc
figure.

These tests seed active ``replace`` and ``drop`` overrides and prove the
surfaces follow the same authority contract:

* financial-fact overrides remain visible consistently in ask, ViewSpec, and
  report readers;
* unreviewed KPI scalar overrides are decision-grade unresolved everywhere
  until a source-reviewed superseding fact has its own admitted semantic head.

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
from report.sections.financials import to_cell_source
from timeseries.loaders import (
    load_financial_series,
    load_financial_series_with_provenance,
    load_kpi_series_with_provenance,
)
from ui.source_chip import viewer_href
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
    CREATE TABLE kpi_fact_semantic_contexts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kpi_fact_id INTEGER NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1,
        supersedes_context_id INTEGER,
        status TEXT NOT NULL,
        publication_lane TEXT NOT NULL,
        metric_name_as_reported TEXT NOT NULL,
        accounting_basis TEXT NOT NULL,
        consolidation_scope TEXT NOT NULL,
        dimensions_json TEXT NOT NULL,
        unit_scale TEXT NOT NULL
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


def _seed_kpi_override(conn: sqlite3.Connection, *, action: OverrideAction) -> None:
    overrides.record_override(
        conn,
        ticker="GOOG",
        period_end="2025-12-31",
        fiscal_period_type="Q4",
        fact_kind=overrides.KPI,
        fact_key=_KPI,
        action=action,
        value=_OV_GCP_GROWTH_Q4 if action is OverrideAction.REPLACE else None,
        unit="percent",
        source_doc_type="ir_press_release",
        created_by="test",
    )
    conn.commit()


def _classify_kpi_facts(conn: sqlite3.Connection, *, status: str) -> None:
    rows = conn.execute(
        "SELECT id,period_end FROM kpi_facts WHERE kpi_definition_id=1 ORDER BY id"
    ).fetchall()
    conn.executemany(
        "INSERT INTO kpi_fact_semantic_contexts (kpi_fact_id,status,publication_lane,"
        "metric_name_as_reported,accounting_basis,consolidation_scope,dimensions_json,"
        "unit_scale) VALUES (?, ?, 'current_actual', ?, 'management','consolidated',"
        "'{}', 'none')",
        [(int(row[0]), status, _KPI) for row in rows],
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


def _ask_kpi_fact_ref_item(db: Path):
    items = gather_evidence(
        "GOOG Google Cloud revenue growth — kpi:GOOG:1",
        repo_root=db.parent.parent,
        db_path=db,
        scope_tickers=["GOOG"],
    )
    return next((i for i in items if i.fact_ref == "kpi:GOOG:1"), None)


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
    assert item is not None and "canonical_financial_schema_unavailable" in item.text
    assert item.value is None and item.href is None
    view = execute_view(
        ViewSpec.from_dict({"tickers": ["GOOG"], "metrics": ["fin:revenue"]}), db_path=db
    )
    assert view.rows == [] and "canonical_financial_schema_unavailable" in view.warnings[0]

    series = load_financial_series("GOOG", "revenue", db_path=db)
    by_date = {str(o.period_end)[:10]: o.value for o in series}
    assert by_date["2025-12-31"] == _FMP_REVENUE_Q4


def test_replace_override_agrees_across_ask_viewspec_report(db: Path) -> None:
    conn = _conn(db)
    _seed_revenue_override(conn, action=OverrideAction.REPLACE)
    conn.close()

    item = _ask_revenue_item(db)
    assert item is not None and "unreviewed_scalar_override" in item.text
    assert item.value is None and item.href is None
    view = execute_view(
        ViewSpec.from_dict({"tickers": ["GOOG"], "metrics": ["fin:revenue"]}), db_path=db
    )
    assert view.rows == [] and "unreviewed_scalar_override" in view.warnings[0]
    # The legacy reader retains the exact overlay value for its remaining callers.
    series = load_financial_series("GOOG", "revenue", db_path=db)
    assert {str(o.period_end)[:10]: o.value for o in series}["2025-12-31"] == _OV_REVENUE_Q4


@pytest.mark.parametrize("action", [OverrideAction.REPLACE, OverrideAction.DROP])
def test_unreviewed_kpi_override_fails_closed_across_all_readers(
    db: Path, action: OverrideAction
) -> None:
    conn = _conn(db)
    _classify_kpi_facts(conn, status="admitted")
    _seed_kpi_override(conn, action=action)
    conn.close()

    assert _ask_kpi_item(db) is None
    assert _ask_kpi_fact_ref_item(db) is None
    result = execute_view(
        ViewSpec.from_dict(
            {
                "tickers": ["GOOG"],
                "metrics": [{"domain": "kpi", "key": _KPI}],
                "transform": "level",
                "cadence": "quarterly",
                "periods": 8,
            }
        ),
        db_path=db,
    )
    assert result.rows == []
    assert result.warnings == [f"GOOG: no data for kpi:{_KPI}"]
    assert load_kpi_series_with_provenance("GOOG", _KPI, db_path=db) == []


def test_non_admitted_kpi_fails_closed_across_all_readers(db: Path) -> None:
    conn = _conn(db)
    _classify_kpi_facts(conn, status="quarantined")
    conn.close()

    assert _ask_kpi_item(db) is None
    assert _ask_kpi_fact_ref_item(db) is None
    result = execute_view(
        ViewSpec.from_dict(
            {
                "tickers": ["GOOG"],
                "metrics": [{"domain": "kpi", "key": _KPI}],
                "transform": "level",
                "cadence": "quarterly",
                "periods": 8,
            }
        ),
        db_path=db,
    )
    assert result.rows == []
    assert load_kpi_series_with_provenance("GOOG", _KPI, db_path=db) == []


def test_admitted_kpi_without_override_agrees_across_all_readers(db: Path) -> None:
    conn = _conn(db)
    _classify_kpi_facts(conn, status="admitted")
    conn.close()

    named = _ask_kpi_item(db)
    pinned = _ask_kpi_fact_ref_item(db)
    assert named is not None and "Q4'25 75" in named.text
    assert pinned is not None and "Q4'25 75" in pinned.text
    cell = _viewspec_q4_cell(db, {"domain": "kpi", "key": _KPI})
    assert cell.raw == _FMP_GCP_GROWTH_Q4
    sourced = load_kpi_series_with_provenance("GOOG", _KPI, db_path=db)
    q4 = next(row for row in sourced if str(row.period_end)[:10] == "2025-12-31")
    assert q4.value == _FMP_GCP_GROWTH_Q4


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
    _seed_kpi_override(kpi_conn, action=OverrideAction.REPLACE)
    kpi_conn.close()
    kpi_sourced = load_kpi_series_with_provenance("GOOG", _KPI, db_path=db)
    assert kpi_sourced == []


# ---------------------------------------------------------------------------
# drop: the period is omitted everywhere
# ---------------------------------------------------------------------------


def test_drop_override_omits_period_across_surfaces(db: Path) -> None:
    conn = _conn(db)
    _seed_revenue_override(conn, action=OverrideAction.DROP)
    _classify_kpi_facts(conn, status="admitted")
    conn.close()

    item = _ask_revenue_item(db)
    assert item is not None and "unreviewed_scalar_override" in item.text
    assert item.value is None and item.href is None

    # The admitted KPI keeps the Q4 column alive, so the financial drop must
    # remain visible as an empty revenue cell rather than a vanished period.
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
    assert not any(row.metric.domain == "fin" for row in result.rows)
    assert result.rows[0].cells[idx].raw == _FMP_GCP_GROWTH_Q4
    assert any("unreviewed_scalar_override" in warning for warning in result.warnings)

    series = load_financial_series("GOOG", "revenue", db_path=db)
    dates = {str(o.period_end)[:10] for o in series}
    assert "2025-12-31" not in dates
    assert "2025-09-30" in dates


def test_replacement_structured_chip_never_opens_superseded_fact(db: Path) -> None:
    conn = _conn(db)
    _seed_revenue_override(conn, action=OverrideAction.REPLACE)
    conn.execute(
        "UPDATE fact_overrides SET locator = ? WHERE fact_key = 'revenue'",
        ('{"kind":"pdf_slide","pdf_page":2}',),
    )
    conn.commit()
    conn.close()
    series = load_financial_series_with_provenance("GOOG", "revenue", db_path=db)
    point = next(item for item in series if item.period_end.date().isoformat() == "2025-12-31")
    assert point.value == _OV_REVENUE_Q4
    source = to_cell_source(point.provenance)
    assert source is not None
    assert source.doc_id == 2
    assert viewer_href(source) == "/source/2?page=2"
    assert source.fact_id is None
