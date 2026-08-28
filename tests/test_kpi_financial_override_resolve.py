"""Read-path coverage for governed financial and KPI correction behavior.

Financial overrides remain an attributable legacy projection. KPI overrides do
not carry independent semantic admission, so decision/display consumers ignore
or fail closed on them until a source-reviewed superseding fact is persisted.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from compute.fmp_derived_kpis import (
    _fetch_full_kpi_series,  # pyright: ignore[reportPrivateUsage]  # internal seam under test
)
from compute.thesis_evaluator import (
    _fetch_kpi_history,  # pyright: ignore[reportPrivateUsage]  # internal seam under test
)
from models.facts import Unit
from provenance import overrides
from provenance.overrides import OverrideAction
from report.sections.financials import (
    _kpi_series_for,  # pyright: ignore[reportPrivateUsage]  # internal seam under test
)
from timeseries.loaders import load_financial_series, load_kpi_series

_KPI = "Google Cloud revenue growth"

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
        raw_bytes_size INTEGER NOT NULL,
        source_quality_tier TEXT NOT NULL DEFAULT 'fmp_normalized'
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
        value NUMERIC(24, 6) NOT NULL,
        unit TEXT NOT NULL,
        source_doc_id INTEGER NOT NULL
    );
    CREATE TABLE kpi_fact_semantic_contexts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kpi_fact_id INTEGER NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1,
        supersedes_context_id INTEGER,
        metric_name_as_reported TEXT NOT NULL,
        reported_period_end TEXT,
        period_role TEXT NOT NULL DEFAULT 'current',
        publication_lane TEXT NOT NULL DEFAULT 'current_actual',
        accounting_basis TEXT NOT NULL DEFAULT 'management',
        consolidation_scope TEXT NOT NULL DEFAULT 'consolidated',
        dimensions_json TEXT NOT NULL DEFAULT '{}',
        unit_scale TEXT NOT NULL DEFAULT 'none',
        source_row_label TEXT,
        source_column_header TEXT,
        status TEXT NOT NULL DEFAULT 'admitted',
        reason_code TEXT
    );
    CREATE TABLE financial_facts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        period_end TIMESTAMP NOT NULL,
        fiscal_period_type TEXT NOT NULL,
        line_item TEXT NOT NULL,
        value NUMERIC(24, 6) NOT NULL,
        unit TEXT NOT NULL DEFAULT 'actual',
        source_doc_id INTEGER NOT NULL
    );
    """
    + _OVERRIDES_DDL
)


def _seed(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_DDL)
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256, "
        "fetched_at, fetch_status, raw_bytes_size) VALUES "
        "(1, 'GOOG', 'fmp', 'fmp_key_metrics', 'x', ?, '2026-01-01 00:00:00', 'ok', 1)",
        ("0" * 64,),
    )
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256, "
        "fetched_at, fetch_status, raw_bytes_size) VALUES "
        "(2, 'GOOG', 'ir_doc', 'ir_press_release', 'goog-q4-release.html', ?, "
        "'2026-02-01 00:00:00', 'ok', 1)",
        ("1" * 64,),
    )
    conn.execute(
        "INSERT INTO kpi_definitions (id, ticker, name, unit) VALUES (1, 'GOOG', ?, 'percent')",
        (_KPI,),
    )
    # FMP's (wrong) values: GCP growth 75% for Q4, 70% for Q3.
    conn.executemany(
        "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, "
        "value, unit, source_doc_id) VALUES ('GOOG', ?, ?, 1, ?, 'percent', 1)",
        [("2025-12-31 00:00:00", "Q4", 75), ("2025-09-30 00:00:00", "Q3", 70)],
    )
    conn.execute(
        "INSERT INTO kpi_fact_semantic_contexts "
        "(kpi_fact_id, metric_name_as_reported, reported_period_end) "
        "SELECT id, ?, substr(period_end, 1, 10) FROM kpi_facts",
        (_KPI,),
    )
    # FMP's (contaminated) revenue for Q4.
    conn.execute(
        "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item, "
        "value, unit, source_doc_id) VALUES ('GOOG', '2025-12-31 00:00:00', 'Q4', 'revenue', "
        "20941000000, 'actual', 1)"
    )
    conn.commit()


def _seed_kpi_override(
    conn: sqlite3.Connection, *, action: OverrideAction, period: str = "Q4"
) -> None:
    date = "2025-12-31" if period == "Q4" else "2025-09-30"
    overrides.record_override(
        conn,
        ticker="GOOG",
        period_end=date,
        fiscal_period_type=period,
        fact_kind=overrides.KPI,
        fact_key=_KPI,
        action=action,
        value=48 if action == OverrideAction.REPLACE else None,
        unit="percent",
        source_doc_type="ir_press_release",
        source_doc_id=2,
        created_by="test",
    )
    conn.commit()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "p.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _seed(conn)
    conn.close()
    return path


def _conn(db: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    return c


def test_load_kpi_series_ignores_unreviewed_override(db: Path) -> None:
    conn = _conn(db)
    _seed_kpi_override(conn, action=OverrideAction.REPLACE)
    conn.close()
    series = load_kpi_series("GOOG", _KPI, db_path=db)
    by_date = {str(o.period_end)[:10]: o.value for o in series}
    assert by_date["2025-12-31"] == 75.0
    assert by_date["2025-09-30"] == 70.0


def test_load_financial_series_honors_override(db: Path) -> None:
    conn = _conn(db)
    overrides.record_override(
        conn,
        ticker="GOOG",
        period_end="2025-12-31",
        fiscal_period_type="Q4",
        fact_kind=overrides.FINANCIAL_FACT,
        fact_key="revenue",
        action=OverrideAction.REPLACE,
        value=17664000000,
        unit="actual",
        source_doc_type="sec_8k",
        created_by="test",
    )
    conn.commit()
    conn.close()
    series = load_financial_series("GOOG", "revenue", db_path=db)
    by_date = {str(o.period_end)[:10]: o.value for o in series}
    assert by_date["2025-12-31"] == 17664000000.0


def test_thesis_fetch_kpi_history_fails_closed_before_semantic_cutover(db: Path) -> None:
    conn = _conn(db)
    _seed_kpi_override(conn, action=OverrideAction.REPLACE)
    hist = _fetch_kpi_history(conn, "GOOG", _KPI, 4)
    conn.close()
    # Thesis evaluation is decision-grade: this legacy fixture has neither an
    # admitted semantic head nor a source-reviewed superseding fact.
    assert hist is None


def test_fmp_derived_full_series_fails_closed_on_unreviewed_override(db: Path) -> None:
    conn = _conn(db)
    _seed_kpi_override(conn, action=OverrideAction.REPLACE)
    points = _fetch_full_kpi_series(conn, "GOOG", _KPI, base_unit=Unit.PERCENT)
    conn.close()
    assert points == []


def test_financials_kpi_series_ignores_unreviewed_override(db: Path) -> None:
    conn = _conn(db)
    _seed_kpi_override(conn, action=OverrideAction.REPLACE)
    series = _kpi_series_for(conn, "GOOG", _KPI, ["2025 Q4"], ["2025 Q4"])
    conn.close()
    assert series is not None
    assert series.values[0] == 75.0


def test_unreviewed_drop_override_does_not_omit_admitted_period(db: Path) -> None:
    conn = _conn(db)
    _seed_kpi_override(conn, action=OverrideAction.DROP, period="Q3")
    conn.close()
    series = load_kpi_series("GOOG", _KPI, db_path=db)
    dates = {str(o.period_end)[:10] for o in series}
    assert "2025-09-30" in dates
    assert "2025-12-31" in dates


def test_no_override_is_unchanged(db: Path) -> None:
    series = load_kpi_series("GOOG", _KPI, db_path=db)
    by_date = {str(o.period_end)[:10]: o.value for o in series}
    assert by_date["2025-12-31"] == 75.0  # FMP value, no override
