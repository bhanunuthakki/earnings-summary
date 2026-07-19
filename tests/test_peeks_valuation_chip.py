"""Phase 3 (bottoms-up metrics engine) UI wiring: the P/E (TTM) row on the
ticker hover mini-card (``pipeline.peeks.render_ticker_peek``).

Synthetic in-memory schema only, mirroring tests/test_metrics_engine_io.py's
_create_schema pattern (no prod data, no alembic migration run -- a minimal
schema covering exactly what render_ticker_peek's readers touch).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from compute.metrics_engine.io import compute_for_ticker
from pipeline.peeks import (
    _latest_pe_ttm,  # pyright: ignore[reportPrivateUsage] -- the unit under test
    render_ticker_peek,
)
from sources.price import LivePrice


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL, source_type TEXT NOT NULL, doc_type TEXT NOT NULL,
            file_path TEXT NOT NULL, sha256 TEXT NOT NULL, fetched_at TIMESTAMP NOT NULL,
            fetch_status TEXT NOT NULL, raw_bytes_size INTEGER NOT NULL,
            source_quality_tier VARCHAR(32) DEFAULT 'fmp_normalized' NOT NULL
        );
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL, period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL, line_item TEXT NOT NULL,
            value NUMERIC(24, 6) NOT NULL, currency TEXT, unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL, confidence REAL DEFAULT 1.0
        );
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL, name TEXT NOT NULL, unit TEXT NOT NULL,
            primary_source TEXT NOT NULL, fallback_source TEXT, ir_url TEXT,
            threshold_tier TEXT, threshold_low REAL, threshold_high REAL, notes TEXT,
            UNIQUE(ticker, name)
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL, period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL, kpi_definition_id INTEGER NOT NULL,
            value NUMERIC(24, 6) NOT NULL, unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL, confidence REAL DEFAULT 1.0,
            extracted_by VARCHAR(64), supersedes_id INTEGER, locator TEXT,
            computed_from TEXT, formula_id INTEGER, formula_version INTEGER
        );
        CREATE UNIQUE INDEX uq_kpi_facts_provenance
        ON kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, source_doc_id);
        CREATE TABLE tracked_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL, name TEXT, archived_at TEXT,
            business_model_class TEXT NOT NULL DEFAULT 'operating_company',
            accounting_standard TEXT NOT NULL DEFAULT 'us_gaap'
        );
        CREATE TABLE formula_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            formula_key TEXT NOT NULL, version INTEGER NOT NULL, category TEXT NOT NULL,
            display_formula TEXT NOT NULL, method_notes TEXT NOT NULL,
            period_grid TEXT NOT NULL, unit TEXT NOT NULL, created_at TIMESTAMP NOT NULL,
            UNIQUE(formula_key, version)
        );
        CREATE TABLE metric_computation_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL, period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL, formula_id INTEGER NOT NULL,
            status TEXT NOT NULL, reason_code TEXT, reason_detail TEXT,
            kpi_fact_id INTEGER, input_fingerprint TEXT NOT NULL,
            engine_version TEXT NOT NULL, computed_at TIMESTAMP NOT NULL
        );
        CREATE UNIQUE INDEX uq_metric_computation_attempts_logical
        ON metric_computation_attempts (ticker, period_end, fiscal_period_type, formula_id);
        CREATE TABLE alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, status TEXT NOT NULL
        );
        CREATE TABLE thesis_evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL,
            evaluated_at TEXT NOT NULL, overall_status TEXT, rule_evaluations_json TEXT
        );
        """
    )
    conn.commit()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _create_schema(c)
    return c


def _insert_doc(conn: sqlite3.Connection, ticker: str, period_end: datetime) -> int:
    cur = conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256, "
        "fetched_at, fetch_status, raw_bytes_size) "
        "VALUES (?, 'fmp', 'fmp_income_statement', ?, ?, ?, 'ok', 1)",
        (ticker, f"{ticker}_{period_end.date()}.json", "a" * 64, datetime.now()),
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


_QUARTERS = ["2023-03-31", "2023-06-30", "2023-09-30", "2023-12-31"]


def _seed_pe_inputs(conn: sqlite3.Connection, ticker: str) -> None:
    """Just enough financial_facts for pe_ttm (eps_diluted TTM sum +
    weighted_avg_shares_diluted for the market-cap leg -- unused by pe_ttm
    itself, but _resolve_valuation_spot needs it to fetch a spot price)."""
    for pe_str in _QUARTERS:
        pe = datetime.fromisoformat(pe_str)
        doc_id = _insert_doc(conn, ticker, pe)
        for line_item, value in (
            ("eps_diluted", "1.10"),
            ("weighted_avg_shares_diluted", "100"),
        ):
            conn.execute(
                "INSERT INTO financial_facts "
                "(ticker, period_end, fiscal_period_type, line_item, value, currency, "
                "unit, source_doc_id) VALUES (?, ?, 'Q4', ?, ?, 'USD', 'actual', ?)",
                (ticker, pe, line_item, value, doc_id),
            )
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, business_model_class, accounting_standard) "
        "VALUES (?, ?, 'operating_company', 'us_gaap')",
        (ticker, f"{ticker} Inc"),
    )
    conn.commit()


def _stub_price(repo_root: Path, ticker: str) -> LivePrice:
    return LivePrice(price=55.0, fetched_at=datetime.now(UTC), source_name="stub")


def test_latest_pe_ttm_returns_none_before_engine_has_run(conn: sqlite3.Connection) -> None:
    assert _latest_pe_ttm(conn, "TEST") is None


def test_latest_pe_ttm_returns_computed_value_after_engine_runs(
    conn: sqlite3.Connection,
) -> None:
    _seed_pe_inputs(conn, "TEST")
    compute_for_ticker(conn, "TEST", repo_root=Path("."), price_reader=_stub_price)
    value = _latest_pe_ttm(conn, "TEST")
    assert value is not None
    # eps_diluted_ttm = 1.10 * 4 = 4.40; price 55 / 4.40 = 12.5.
    assert abs(value - 12.5) < 0.001


def test_render_ticker_peek_shows_pe_ttm_row_when_computed(conn: sqlite3.Connection) -> None:
    _seed_pe_inputs(conn, "TEST")
    compute_for_ticker(conn, "TEST", repo_root=Path("."), price_reader=_stub_price)
    html = render_ticker_peek(conn, Path("."), "TEST")
    assert html is not None
    assert "P/E (TTM)" in html
    assert "12.5x" in html


def test_render_ticker_peek_omits_pe_row_when_not_yet_computed(
    conn: sqlite3.Connection,
) -> None:
    """Hide-don't-stub, same as every other optional row on this card: no
    kpi_facts row yet (compute_derived_metrics hasn't run for this ticker)
    drops the row rather than showing an em-dash."""
    conn.execute("INSERT INTO tracked_companies (ticker, name) VALUES ('TEST', 'Test Inc')")
    conn.commit()
    html = render_ticker_peek(conn, Path("."), "TEST")
    assert html is not None
    assert "P/E (TTM)" not in html


def test_engine_valuation_metric_resolves_through_shared_kpi_resolver(
    conn: sqlite3.Connection,
) -> None:
    """The read-side contract behind the Ask engine / break rules / chart
    loader wiring: an engine-computed valuation metric persists under
    kpi_definitions.name == formula_key, so the SHARED resolver
    (compute.kpi_resolver.resolve_kpi_definition_name -- the single lookup
    every kpi_facts consumer routes through) resolves it like any other KPI
    the moment compute_for_ticker has run. No consumer-side special-casing."""
    from compute.kpi_resolver import resolve_kpi_definition_name

    _seed_pe_inputs(conn, "TEST")
    compute_for_ticker(conn, "TEST", repo_root=Path("."), price_reader=_stub_price)
    assert resolve_kpi_definition_name(conn, "TEST", "pe_ttm") == "pe_ttm"
    # A metric whose inputs were NOT seeded (enterprise_value_strict needs
    # debt/cash facts this fixture omits) has no kpi_facts row -- the
    # resolver correctly returns None, and the WHY is a queryable
    # metric_computation_attempts row, not silence.
    assert resolve_kpi_definition_name(conn, "TEST", "enterprise_value_strict") is None
    row = conn.execute(
        "SELECT mca.status, mca.reason_code FROM metric_computation_attempts mca "
        "JOIN formula_definitions fd ON fd.id = mca.formula_id "
        "WHERE mca.ticker = 'TEST' AND fd.formula_key = 'enterprise_value_strict'"
    ).fetchone()
    assert row["status"] == "not_computable"
    assert row["reason_code"] == "missing_input"
