"""Time-series queries against `thesis_evaluations`.

Status transitions (when a holding flipped from OK→WARN or WARN→BREACH),
current streak length, and a portfolio-level summary. Pure read-only —
all writes go through src/compute/thesis_evaluator.py.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise

from models.kpis import BreachStatus


@dataclass(frozen=True)
class EvaluationSnapshot:
    """One row from thesis_evaluations, projected to the fields we care about."""

    evaluated_at: datetime
    status: BreachStatus
    run_id: str | None


@dataclass(frozen=True)
class StatusTransition:
    """A change in overall_status between two consecutive evaluations."""

    ticker: str
    from_status: BreachStatus
    to_status: BreachStatus
    transitioned_at: datetime


@dataclass(frozen=True)
class StreakSummary:
    """Per-ticker rollup of the most-recent evaluation history."""

    ticker: str
    current_status: BreachStatus
    streak_length: int
    streak_started_at: datetime
    last_evaluated_at: datetime
    total_evaluations: int


def fetch_history(
    conn: sqlite3.Connection, ticker: str
) -> list[EvaluationSnapshot]:
    """Return all evaluations for a ticker, oldest-first."""
    cur = conn.execute(
        "SELECT evaluated_at, overall_status, run_id "
        "FROM thesis_evaluations WHERE ticker = ? "
        "ORDER BY evaluated_at ASC",
        (ticker.upper(),),
    )
    out: list[EvaluationSnapshot] = []
    for row in cur.fetchall():
        ts = row["evaluated_at"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        out.append(
            EvaluationSnapshot(
                evaluated_at=ts,
                status=BreachStatus(row["overall_status"]),
                run_id=row["run_id"],
            )
        )
    return out


def transitions_for(
    conn: sqlite3.Connection, ticker: str
) -> list[StatusTransition]:
    """Return only the evaluations where the status changed from the prior one."""
    history = fetch_history(conn, ticker)
    out: list[StatusTransition] = []
    for prev, curr in pairwise(history):
        if curr.status is prev.status:
            continue
        out.append(
            StatusTransition(
                ticker=ticker.upper(),
                from_status=prev.status,
                to_status=curr.status,
                transitioned_at=curr.evaluated_at,
            )
        )
    return out


def streak_summary(
    conn: sqlite3.Connection, ticker: str
) -> StreakSummary | None:
    """Return current-status streak length + when it started.

    Streak = consecutive most-recent evaluations sharing the same status.
    Returns None if the ticker has no evaluations yet.
    """
    history = fetch_history(conn, ticker)
    if not history:
        return None
    current = history[-1].status
    streak: list[EvaluationSnapshot] = [history[-1]]
    for snap in reversed(history[:-1]):
        if snap.status is not current:
            break
        streak.append(snap)
    streak.reverse()
    return StreakSummary(
        ticker=ticker.upper(),
        current_status=current,
        streak_length=len(streak),
        streak_started_at=streak[0].evaluated_at,
        last_evaluated_at=history[-1].evaluated_at,
        total_evaluations=len(history),
    )


def portfolio_summary(
    conn: sqlite3.Connection,
) -> list[StreakSummary]:
    """Return per-ticker streak summary across every ticker in thesis_evaluations."""
    cur = conn.execute(
        "SELECT DISTINCT ticker FROM thesis_evaluations ORDER BY ticker"
    )
    tickers = [row["ticker"] for row in cur.fetchall()]
    out: list[StreakSummary] = []
    for ticker in tickers:
        summary = streak_summary(conn, ticker)
        if summary is not None:
            out.append(summary)
    return out
