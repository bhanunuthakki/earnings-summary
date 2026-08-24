"""Tests for src/timeseries/signal_writer.py — persistence of primitive output.

Mirrors the synthetic-data fixture pattern in test_timeseries.py: build a
minimal portfolio.db with financial_facts + kpi_facts + kpi_definitions,
seed a few series, run compute_and_persist_signals, then assert on rows in
timeseries_signals."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from timeseries import compute_and_persist_signals
from timeseries.signal_writer import classify_investment_direction
from user_state.kpi_polarity import Polarity


def _build_db(path: Path) -> None:
    """Create the slimmest schema the writer + loaders need to operate.

    Mirrors the table shapes in alembic/versions/0004_facts_tables.py +
    0007_kpi_definitions.py + 0053_timeseries_signals.py — kept inline so
    the test is independent of alembic running.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE financial_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker VARCHAR NOT NULL,
                period_end DATETIME NOT NULL,
                fiscal_period_type VARCHAR NOT NULL,
                line_item VARCHAR NOT NULL,
                value NUMERIC(24,6) NOT NULL,
                currency VARCHAR(3),
                unit VARCHAR NOT NULL,
                source_doc_id INTEGER NOT NULL,
                confidence FLOAT NOT NULL DEFAULT 1.0
            );
            CREATE TABLE kpi_definitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker VARCHAR NOT NULL,
                name TEXT NOT NULL,
                unit VARCHAR NOT NULL DEFAULT 'actual',
                primary_source VARCHAR NOT NULL,
                fallback_source VARCHAR,
                ir_url VARCHAR,
                threshold_tier VARCHAR,
                threshold_low FLOAT,
                threshold_high FLOAT,
                notes TEXT,
                UNIQUE(ticker, name)
            );
            CREATE TABLE kpi_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker VARCHAR NOT NULL,
                period_end DATETIME NOT NULL,
                fiscal_period_type VARCHAR NOT NULL,
                kpi_definition_id INTEGER NOT NULL,
                value NUMERIC(24,6) NOT NULL,
                unit VARCHAR NOT NULL,
                source_doc_id INTEGER NOT NULL
            );
            CREATE TABLE segment_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker VARCHAR NOT NULL,
                period_end DATETIME NOT NULL,
                fiscal_period_type VARCHAR NOT NULL,
                segment_name VARCHAR NOT NULL,
                metric VARCHAR NOT NULL,
                value NUMERIC(24,6) NOT NULL,
                currency VARCHAR(3),
                unit VARCHAR NOT NULL,
                source_doc_id INTEGER NOT NULL
            );
            CREATE TABLE timeseries_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker VARCHAR(16) NOT NULL,
                metric_name VARCHAR(128) NOT NULL,
                metric_kind VARCHAR(16) NOT NULL,
                signal_type VARCHAR(32) NOT NULL,
                value_json TEXT NOT NULL,
                severity VARCHAR(8) NOT NULL,
                narrative TEXT,
                computed_at DATETIME NOT NULL,
                run_id VARCHAR(64),
                CONSTRAINT uq_timeseries_signals_logical
                    UNIQUE (ticker, metric_name, metric_kind, signal_type),
                CHECK (metric_kind IN ('financial', 'kpi', 'segment')),
                CHECK (signal_type IN ('trend', 'inflection', 'anomaly',
                                      'yoy_acceleration', 'seasonal', 'correlation')),
                CHECK (severity IN ('green', 'yellow', 'red'))
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _insert_financial(
    db: Path,
    ticker: str,
    line_item: str,
    values: Sequence[float],
    *,
    start: str = "2020-03-31",
) -> None:
    base = datetime.fromisoformat(start)
    conn = sqlite3.connect(str(db))
    try:
        for i, v in enumerate(values):
            period_end = (base + timedelta(days=90 * i)).strftime("%Y-%m-%d %H:%M:%S")
            quarter = f"Q{(i % 4) + 1}"
            conn.execute(
                "INSERT INTO financial_facts(ticker, period_end, fiscal_period_type, "
                "line_item, value, unit, source_doc_id) VALUES (?,?,?,?,?,?,?)",
                (ticker, period_end, quarter, line_item, float(v), "USD", 1),
            )
        conn.commit()
    finally:
        conn.close()


def _insert_kpi(
    db: Path,
    ticker: str,
    kpi_name: str,
    values: Sequence[float],
    *,
    start: str = "2020-03-31",
) -> None:
    base = datetime.fromisoformat(start)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT OR IGNORE INTO kpi_definitions(ticker, name, unit, primary_source) "
            "VALUES (?,?,?,?)",
            (ticker, kpi_name, "actual", "ir_doc"),
        )
        kpi_def_id = conn.execute(
            "SELECT id FROM kpi_definitions WHERE ticker = ? AND name = ?",
            (ticker, kpi_name),
        ).fetchone()[0]
        for i, v in enumerate(values):
            period_end = (base + timedelta(days=90 * i)).strftime("%Y-%m-%d %H:%M:%S")
            quarter = f"Q{(i % 4) + 1}"
            conn.execute(
                "INSERT INTO kpi_facts(ticker, period_end, fiscal_period_type, "
                "kpi_definition_id, value, unit, source_doc_id) VALUES (?,?,?,?,?,?,?)",
                (ticker, period_end, quarter, int(kpi_def_id), float(v), "actual", 1),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_goog(repo_root: Path) -> Path:
    """Seed a GOOG fixture: portfolio.db with a few financial series + KPIs,
    and a stripped holdings JSON that exercises both chart_priorities and
    tier_1_kpis paths."""
    data_dir = repo_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "portfolio.db"
    _build_db(db_path)

    # 16 quarters of synthetic data — enough for every primitive to fire.
    # Mix of growth shapes so the test exercises green/yellow/red branches.
    _insert_financial(db_path, "GOOG", "revenue", [100.0 + 5 * i for i in range(16)])
    _insert_financial(db_path, "GOOG", "operating_income", [25.0 + 2 * i for i in range(16)])
    # FCF with a 1.5-sigma-ish anomaly in the last quarter to catch the writer's
    # anomaly branch — values are in the ~30-50 band, end with 70 to land an
    # observable z-score against the trailing window.
    fcf_vals = [30.0 + math.sin(i) * 3 for i in range(15)] + [70.0]
    _insert_financial(db_path, "GOOG", "free_cash_flow", fcf_vals)
    _insert_financial(db_path, "GOOG", "gross_profit", [60.0 + 3 * i for i in range(16)])
    _insert_financial(db_path, "GOOG", "net_income", [20.0 + 1.5 * i for i in range(16)])

    # Two KPIs matching holdings tier_1 entries
    _insert_kpi(db_path, "GOOG", "GCP revenue growth (YoY)", [0.35 - 0.01 * i for i in range(16)])
    _insert_kpi(
        db_path, "GOOG", "GCP operating margin trajectory", [0.05 + 0.005 * i for i in range(16)]
    )

    # Minimal holdings JSON covering both code paths.
    holdings_dir = repo_root / "micro_thesis" / "holdings"
    holdings_dir.mkdir(parents=True, exist_ok=True)
    (holdings_dir / "GOOG.json").write_text(
        json.dumps(
            {
                "ticker": "GOOG",
                "chart_priorities": [
                    "Revenue",
                    "Operating income",
                    "Free cash flow",
                ],
                "tier_1_kpis": [
                    {"name": "GCP revenue growth (YoY)", "break_condition": "TBV"},
                    {"name": "GCP operating margin trajectory", "break_condition": "TBV"},
                ],
            }
        ),
        encoding="utf-8",
    )
    return db_path


def test_writer_persists_signals_for_seeded_ticker(tmp_path: Path) -> None:
    """compute_and_persist_signals writes >= 5 rows for a fully seeded ticker,
    severity values are within the valid enum, and at least one narrative is non-empty."""
    db_path = _seed_goog(tmp_path)

    conn = sqlite3.connect(str(db_path))
    try:
        n = compute_and_persist_signals(ticker="GOOG", db=conn, repo_root=tmp_path)
    finally:
        conn.close()

    assert n >= 5, f"expected at least 5 rows, got {n}"

    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ticker, metric_name, metric_kind, signal_type, severity, narrative, value_json "
            "FROM timeseries_signals WHERE ticker = 'GOOG'"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == n, "row count must match return value"
    valid_sev = {"green", "yellow", "red"}
    for r in rows:
        assert r["severity"] in valid_sev
        assert r["ticker"] == "GOOG"
        assert r["metric_kind"] in {"financial", "kpi"}
        assert r["signal_type"] in {
            "trend",
            "inflection",
            "anomaly",
            "yoy_acceleration",
            "seasonal",
        }

    # At least one signal carried a rendered narrative (the writer always
    # emits one — guard against an accidental refactor that strips them).
    assert any(r["narrative"] for r in rows)
    assert all(json.loads(str(r["value_json"]))["source_period"] == "2023-12-11" for r in rows)


def test_direction_requires_known_polarity_and_significant_trend() -> None:
    """Statistical direction alone must never be rendered as investment direction."""
    assert (
        classify_investment_direction(
            "trend",
            {"slope": -1.0, "statistical_significance": True},
            Polarity.HIGHER_IS_BETTER,
        )
        == "unfavorable"
    )
    assert (
        classify_investment_direction(
            "trend",
            {"slope": -1.0, "statistical_significance": False},
            Polarity.HIGHER_IS_BETTER,
        )
        == "ambiguous"
    )
    assert classify_investment_direction("trend", {"slope": -1.0}, None) == "ambiguous"
    assert (
        classify_investment_direction(
            "trend",
            {"slope": 1.0, "statistical_significance": True},
            Polarity.HIGHER_IS_BETTER,
        )
        == "favorable"
    )


def test_writer_is_idempotent(tmp_path: Path) -> None:
    """Re-running the writer leaves the row count unchanged — unique
    constraint forces upsert-in-place, not append. Also assert the most
    recent computed_at advances on the second run."""
    db_path = _seed_goog(tmp_path)

    conn = sqlite3.connect(str(db_path))
    try:
        first = compute_and_persist_signals(ticker="GOOG", db=conn, repo_root=tmp_path)
    finally:
        conn.close()

    # Count after first run
    conn = sqlite3.connect(str(db_path))
    try:
        first_count = conn.execute(
            "SELECT COUNT(*) FROM timeseries_signals WHERE ticker = 'GOOG'"
        ).fetchone()[0]
        first_computed_at = conn.execute(
            "SELECT MAX(computed_at) FROM timeseries_signals WHERE ticker = 'GOOG'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert first == first_count

    conn = sqlite3.connect(str(db_path))
    try:
        second = compute_and_persist_signals(ticker="GOOG", db=conn, repo_root=tmp_path)
    finally:
        conn.close()

    assert first == second, "re-run must yield the same row count (upsert, not append)"

    conn = sqlite3.connect(str(db_path))
    try:
        second_count = conn.execute(
            "SELECT COUNT(*) FROM timeseries_signals WHERE ticker = 'GOOG'"
        ).fetchone()[0]
        second_computed_at = conn.execute(
            "SELECT MAX(computed_at) FROM timeseries_signals WHERE ticker = 'GOOG'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert second_count == first_count
    assert second_computed_at >= first_computed_at


def test_writer_run_id_is_persisted(tmp_path: Path) -> None:
    """run_id passed in is recorded on every row written in that run."""
    db_path = _seed_goog(tmp_path)
    conn = sqlite3.connect(str(db_path))
    try:
        compute_and_persist_signals(ticker="GOOG", db=conn, repo_root=tmp_path, run_id="abc123")
    finally:
        conn.close()

    conn = sqlite3.connect(str(db_path))
    try:
        run_ids = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT run_id FROM timeseries_signals WHERE ticker = 'GOOG'"
            ).fetchall()
        ]
    finally:
        conn.close()

    assert run_ids == ["abc123"]


def test_writer_skips_when_no_data(tmp_path: Path) -> None:
    """Empty DB / no holdings JSON → 0 rows, no crash."""
    db_path = tmp_path / "data" / "portfolio.db"
    db_path.parent.mkdir(parents=True)
    _build_db(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        n = compute_and_persist_signals(ticker="ZZZZ", db=conn, repo_root=tmp_path)
    finally:
        conn.close()

    assert n == 0


def test_writer_raises_when_table_missing(tmp_path: Path) -> None:
    """Migration not applied → caller-visible OperationalError, not silent skip.
    The pipeline stage wraps this and degrades; ad-hoc callers should learn."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    try:
        # Build everything EXCEPT timeseries_signals
        conn.executescript(
            """
            CREATE TABLE financial_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker VARCHAR NOT NULL,
                period_end DATETIME NOT NULL,
                fiscal_period_type VARCHAR NOT NULL,
                line_item VARCHAR NOT NULL,
                value NUMERIC(24,6) NOT NULL,
                unit VARCHAR NOT NULL,
                source_doc_id INTEGER NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    _insert_financial(db_path, "GOOG", "revenue", [100.0 + 5 * i for i in range(16)])

    conn = sqlite3.connect(str(db_path))
    try:
        with pytest.raises(sqlite3.OperationalError):
            compute_and_persist_signals(ticker="GOOG", db=conn, repo_root=tmp_path)
    finally:
        conn.close()
