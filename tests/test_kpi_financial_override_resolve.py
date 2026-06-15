"""Read-path coverage for company-doc overrides over financial_facts / kpi_facts.

P3 of the provenance-override initiative. Proves an active ``replace`` override wins
over the FMP/LLM row at every value-determining reader — the canonical tier-aware
loaders AND the ``MAX(source_doc_id)`` readers — and that a ``drop`` omits the
period. This is what makes a company-published figure durable: it wins at resolve
time regardless of row ids, so an FMP re-fetch can't clobber it.
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
        name TEXT NOT NULL
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
    conn.execute("INSERT INTO kpi_definitions (id, ticker, name) VALUES (1, 'GOOG', ?)", (_KPI,))
    # FMP's (wrong) values: GCP growth 75% for Q4, 70% for Q3.
    conn.executemany(
        "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, "
        "value, unit, source_doc_id) VALUES ('GOOG', ?, ?, 1, ?, 'percent', 1)",
        [("2025-12-31 00:00:00", "Q4", 75), ("2025-09-30 00:00:00", "Q3", 70)],
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


def test_load_kpi_series_honors_override(db: Path) -> None:
    conn = _conn(db)
    _seed_kpi_override(conn, action=OverrideAction.REPLACE)
    conn.close()
    series = load_kpi_series("GOOG", _KPI, db_path=db)
    by_date = {str(o.period_end)[:10]: o.value for o in series}
    assert by_date["2025-12-31"] == 48.0  # overridden
    assert by_date["2025-09-30"] == 70.0  # untouched


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


def test_thesis_fetch_kpi_history_honors_override(db: Path) -> None:
    conn = _conn(db)
    _seed_kpi_override(conn, action=OverrideAction.REPLACE)
    hist = _fetch_kpi_history(conn, "GOOG", _KPI, 4)
    conn.close()
    assert hist is not None
    by_date = {str(o.period_end)[:10]: float(o.value) for o in hist}
    assert by_date["2025-12-31"] == 48.0
    assert by_date["2025-09-30"] == 70.0


def test_fmp_derived_full_series_honors_override(db: Path) -> None:
    conn = _conn(db)
    _seed_kpi_override(conn, action=OverrideAction.REPLACE)
    points = _fetch_full_kpi_series(conn, "GOOG", _KPI, base_unit=Unit.PERCENT)
    conn.close()
    by_date = {str(p.period_end)[:10]: float(p.value) for p in points}
    assert by_date["2025-12-31"] == 48.0


def test_financials_kpi_series_honors_override(db: Path) -> None:
    conn = _conn(db)
    _seed_kpi_override(conn, action=OverrideAction.REPLACE)
    series = _kpi_series_for(conn, "GOOG", _KPI, ["2025 Q4"], ["2025 Q4"])
    conn.close()
    assert series is not None
    assert series.values[0] == 48.0


def test_drop_override_omits_period(db: Path) -> None:
    conn = _conn(db)
    _seed_kpi_override(conn, action=OverrideAction.DROP, period="Q3")
    conn.close()
    series = load_kpi_series("GOOG", _KPI, db_path=db)
    dates = {str(o.period_end)[:10] for o in series}
    assert "2025-09-30" not in dates  # dropped
    assert "2025-12-31" in dates


def test_no_override_is_unchanged(db: Path) -> None:
    series = load_kpi_series("GOOG", _KPI, db_path=db)
    by_date = {str(o.period_end)[:10]: o.value for o in series}
    assert by_date["2025-12-31"] == 75.0  # FMP value, no override
