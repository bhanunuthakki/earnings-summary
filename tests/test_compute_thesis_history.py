"""Tests for src/compute/thesis_history.py — time-series queries on thesis_evaluations."""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from compute.thesis_history import (
    fetch_history,
    portfolio_summary,
    streak_summary,
    transitions_for,
)
from models.kpis import BreachStatus


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE thesis_evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            evaluated_at TIMESTAMP NOT NULL,
            overall_status TEXT NOT NULL,
            rule_evaluations_json TEXT NOT NULL,
            run_id TEXT
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


def _seed(
    conn: sqlite3.Connection, ticker: str, sequence: list[tuple[datetime, BreachStatus]]
) -> None:
    """Insert evaluations in order."""
    for evaluated_at, status in sequence:
        conn.execute(
            "INSERT INTO thesis_evaluations "
            "(ticker, evaluated_at, overall_status, rule_evaluations_json, run_id) "
            "VALUES (?, ?, ?, '[]', 'test-run')",
            (ticker, evaluated_at, status.value),
        )
    conn.commit()


def test_fetch_history_returns_oldest_first(conn: sqlite3.Connection) -> None:
    """Rows come back in ascending timestamp order regardless of insert order."""
    _seed(
        conn,
        "X",
        [
            (datetime(2026, 5, 1), BreachStatus.OK),
            (datetime(2026, 4, 1), BreachStatus.WARN),
            (datetime(2026, 6, 1), BreachStatus.BREACH),
        ],
    )
    history = fetch_history(conn, "X")
    assert [s.status for s in history] == [BreachStatus.WARN, BreachStatus.OK, BreachStatus.BREACH]


def test_fetch_history_empty_for_unknown_ticker(conn: sqlite3.Connection) -> None:
    assert fetch_history(conn, "NOPE") == []


def test_transitions_skips_unchanged_consecutive_status(conn: sqlite3.Connection) -> None:
    """OK -> OK -> WARN -> WARN -> BREACH -> 2 transitions only (OK->WARN, WARN->BREACH)."""
    _seed(
        conn,
        "X",
        [
            (datetime(2026, 1, 1), BreachStatus.OK),
            (datetime(2026, 2, 1), BreachStatus.OK),
            (datetime(2026, 3, 1), BreachStatus.WARN),
            (datetime(2026, 4, 1), BreachStatus.WARN),
            (datetime(2026, 5, 1), BreachStatus.BREACH),
        ],
    )
    transitions = transitions_for(conn, "X")
    assert len(transitions) == 2
    assert (transitions[0].from_status, transitions[0].to_status) == (
        BreachStatus.OK,
        BreachStatus.WARN,
    )
    assert transitions[0].transitioned_at == datetime(2026, 3, 1)
    assert (transitions[1].from_status, transitions[1].to_status) == (
        BreachStatus.WARN,
        BreachStatus.BREACH,
    )


def test_transitions_empty_when_status_never_changes(conn: sqlite3.Connection) -> None:
    _seed(
        conn,
        "X",
        [(datetime(2026, 1, 1), BreachStatus.OK), (datetime(2026, 2, 1), BreachStatus.OK)],
    )
    assert transitions_for(conn, "X") == []


def test_streak_summary_counts_only_consecutive_recent(conn: sqlite3.Connection) -> None:
    """Old runs that match the current status but are interrupted DON'T count."""
    _seed(
        conn,
        "X",
        [
            (datetime(2026, 1, 1), BreachStatus.WARN),  # not part of current streak
            (datetime(2026, 2, 1), BreachStatus.OK),  # interrupts
            (datetime(2026, 3, 1), BreachStatus.WARN),  # current streak starts here
            (datetime(2026, 4, 1), BreachStatus.WARN),
            (datetime(2026, 5, 1), BreachStatus.WARN),
        ],
    )
    summary = streak_summary(conn, "X")
    assert summary is not None
    assert summary.current_status == BreachStatus.WARN
    assert summary.streak_length == 3
    assert summary.streak_started_at == datetime(2026, 3, 1)
    assert summary.total_evaluations == 5


def test_streak_summary_returns_none_for_empty_history(conn: sqlite3.Connection) -> None:
    assert streak_summary(conn, "X") is None


def test_streak_summary_single_eval(conn: sqlite3.Connection) -> None:
    """One evaluation -> streak_length=1, streak_started_at = that timestamp."""
    _seed(conn, "Y", [(datetime(2026, 5, 1), BreachStatus.BREACH)])
    summary = streak_summary(conn, "Y")
    assert summary is not None
    assert summary.streak_length == 1
    assert summary.streak_started_at == datetime(2026, 5, 1)


def test_portfolio_summary_returns_one_per_ticker(conn: sqlite3.Connection) -> None:
    """A ticker with multiple evals appears once in the rollup."""
    _seed(conn, "A", [(datetime(2026, 1, 1), BreachStatus.OK)])
    _seed(
        conn,
        "B",
        [
            (datetime(2026, 1, 1), BreachStatus.OK),
            (datetime(2026, 2, 1), BreachStatus.WARN),
        ],
    )
    rollups = portfolio_summary(conn)
    assert len(rollups) == 2
    by_ticker = {r.ticker: r for r in rollups}
    assert by_ticker["A"].current_status == BreachStatus.OK
    assert by_ticker["B"].current_status == BreachStatus.WARN
