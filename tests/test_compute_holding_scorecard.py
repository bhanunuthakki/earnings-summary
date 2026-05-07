"""Tests for src/compute/holding_scorecard.py — combines thesis status, Say-Do, and recent KPIs."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal

import pytest

from compute.holding_scorecard import portfolio_scorecards, scorecard_for
from models.kpis import BreachStatus


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE thesis_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL UNIQUE,
            thesis TEXT,
            last_updated TIMESTAMP,
            breach_status TEXT,
            raw_json TEXT NOT NULL,
            ingested_at TIMESTAMP NOT NULL
        );
        CREATE TABLE thesis_evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            evaluated_at TIMESTAMP NOT NULL,
            overall_status TEXT NOT NULL,
            rule_evaluations_json TEXT NOT NULL,
            run_id TEXT
        );
        CREATE TABLE management_commitments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_made TIMESTAMP NOT NULL,
            transcript_segment_id INTEGER NOT NULL,
            period_target TIMESTAMP NOT NULL,
            kpi_name TEXT NOT NULL,
            comparator TEXT NOT NULL,
            target_value NUMERIC(24, 6) NOT NULL,
            unit TEXT NOT NULL,
            narrative TEXT NOT NULL,
            realized_value NUMERIC(24, 6),
            realized_doc_id INTEGER,
            outcome TEXT,
            evaluated_at TIMESTAMP
        );
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            unit TEXT NOT NULL,
            primary_source TEXT NOT NULL,
            UNIQUE(ticker, name)
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
        """
    )
    conn.commit()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _create_schema(c)
    return c


def _seed_state(
    conn: sqlite3.Connection, ticker: str, status: BreachStatus
) -> None:
    conn.execute(
        "INSERT INTO thesis_state (ticker, raw_json, breach_status, ingested_at, last_updated) "
        "VALUES (?, '{}', ?, ?, ?)",
        (ticker, status.value, datetime.now(), datetime.now()),
    )
    conn.execute(
        "INSERT INTO thesis_evaluations "
        "(ticker, evaluated_at, overall_status, rule_evaluations_json, run_id) "
        "VALUES (?, ?, ?, '[]', 'r1')",
        (ticker, datetime.now(), status.value),
    )
    conn.commit()


def _seed_commitment(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    outcome: str | None,
    target: float = 10,
    realized: float = 11,
) -> None:
    conn.execute(
        "INSERT INTO management_commitments "
        "(ticker, period_made, transcript_segment_id, period_target, kpi_name, "
        " comparator, target_value, unit, narrative, realized_value, outcome) "
        "VALUES (?, ?, 1, ?, 'X', 'ge', ?, 'percent', 'n', ?, ?)",
        (
            ticker,
            datetime(2024, 9, 30),
            datetime(2024, 12, 31),
            str(target),
            str(realized) if outcome is not None else None,
            outcome,
        ),
    )
    conn.commit()


def _seed_kpi(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    name: str,
    value: float,
    period_end: datetime,
) -> None:
    existing = conn.execute(
        "SELECT id FROM kpi_definitions WHERE ticker = ? AND name = ?", (ticker, name)
    ).fetchone()
    if existing is None:
        cur = conn.execute(
            "INSERT INTO kpi_definitions (ticker, name, unit, primary_source) "
            "VALUES (?, ?, 'percent', 'fmp')",
            (ticker, name),
        )
        kpi_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    else:
        kpi_id = int(dict(existing)["id"])
    conn.execute(
        "INSERT INTO kpi_facts "
        "(ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, source_doc_id) "
        "VALUES (?, ?, 'Q4', ?, ?, 'percent', 1)",
        (ticker, period_end, kpi_id, str(value)),
    )
    conn.commit()


def test_scorecard_breach_status_propagates(conn: sqlite3.Connection) -> None:
    _seed_state(conn, "X", BreachStatus.BREACH)
    sc = scorecard_for(conn, "X")
    assert sc.breach_status == BreachStatus.BREACH


def test_scorecard_no_thesis_state_returns_none_status(conn: sqlite3.Connection) -> None:
    sc = scorecard_for(conn, "Y")
    assert sc.breach_status is None
    assert sc.streak is None
    assert sc.commitments.total == 0


def test_scorecard_commitment_hit_rate(conn: sqlite3.Connection) -> None:
    """2 HIT + 1 BEAT + 1 MISS = 75% hit rate."""
    _seed_state(conn, "X", BreachStatus.OK)
    _seed_commitment(conn, ticker="X", outcome="hit")
    _seed_commitment(conn, ticker="X", outcome="hit")
    _seed_commitment(conn, ticker="X", outcome="beat")
    _seed_commitment(conn, ticker="X", outcome="miss")
    sc = scorecard_for(conn, "X")
    assert sc.commitments.hit == 2
    assert sc.commitments.beat == 1
    assert sc.commitments.miss == 1
    assert sc.commitments.hit_rate_pct == Decimal("75.0")


def test_scorecard_no_data_excluded_from_hit_rate(conn: sqlite3.Connection) -> None:
    """NO_DATA commitments don't dilute the hit rate denominator."""
    _seed_state(conn, "X", BreachStatus.OK)
    _seed_commitment(conn, ticker="X", outcome="hit")
    _seed_commitment(conn, ticker="X", outcome="no_data")
    sc = scorecard_for(conn, "X")
    assert sc.commitments.no_data == 1
    assert sc.commitments.hit_rate_pct == Decimal("100.0")


def test_scorecard_hit_rate_none_when_no_matched(conn: sqlite3.Connection) -> None:
    """No matched commitments -> hit_rate = None (not 0)."""
    _seed_state(conn, "X", BreachStatus.OK)
    _seed_commitment(conn, ticker="X", outcome="no_data")
    sc = scorecard_for(conn, "X")
    assert sc.commitments.hit_rate_pct is None


def test_scorecard_recent_kpis_one_per_name(conn: sqlite3.Connection) -> None:
    """Most-recent observation per KPI name is returned (not all history)."""
    _seed_state(conn, "X", BreachStatus.OK)
    # Same KPI name across two periods — only the newer one should appear
    _seed_kpi(conn, ticker="X", name="Margin", value=10, period_end=datetime(2024, 12, 31))
    _seed_kpi(conn, ticker="X", name="Margin", value=12, period_end=datetime(2025, 12, 31))
    sc = scorecard_for(conn, "X")
    margins = [k for k in sc.recent_kpis if k.name == "Margin"]
    assert len(margins) == 1
    assert margins[0].value == Decimal("12")
    assert margins[0].period_end == datetime(2025, 12, 31)


def test_portfolio_scorecards_one_per_thesis_state_row(conn: sqlite3.Connection) -> None:
    _seed_state(conn, "A", BreachStatus.OK)
    _seed_state(conn, "B", BreachStatus.WARN)
    _seed_state(conn, "C", BreachStatus.BREACH)
    cards = portfolio_scorecards(conn)
    assert {c.ticker for c in cards} == {"A", "B", "C"}
