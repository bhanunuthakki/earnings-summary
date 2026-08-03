"""Tests for src/predictions_store.py and migration 0038."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from predictions_store import (
    grade,
    history,
    outcome_summary,
    pending_for_grading,
    record,
)


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR(16) NOT NULL,
            source_kind VARCHAR(32) NOT NULL,
            source_doc_id INTEGER, source_artifact_id INTEGER,
            source_excerpt TEXT,
            made_at DATETIME NOT NULL,
            target_period DATETIME,
            prediction_md TEXT NOT NULL,
            kpi_name VARCHAR(128), kpi_concept_id INTEGER,
            comparator VARCHAR(8), target_value NUMERIC(24,6), target_unit VARCHAR(32),
            realized_value NUMERIC(24,6), realized_doc_id INTEGER,
            outcome VARCHAR(24) NOT NULL DEFAULT 'pending',
            outcome_confidence FLOAT,
            evaluated_at DATETIME, evaluator_run_id VARCHAR(64),
            notes TEXT, created_at DATETIME NOT NULL
        );
        """
    )
    conn.commit()


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    p = tmp_path / "pred.db"
    conn = sqlite3.connect(str(p))
    try:
        _schema(conn)
    finally:
        conn.close()
    return p


def test_record_inserts_prediction(db: Path) -> None:
    pid = record(
        ticker="GOOG",
        source_kind="mgmt_commitment",
        prediction_md="Cloud margin expansion accelerating",
        made_at=datetime(2025, 11, 1, tzinfo=UTC),
        target_period=datetime(2026, 3, 31, tzinfo=UTC),
        kpi_name="cloud_operating_margin",
        comparator="ge",
        target_value=0.18,
        target_unit="pct",
        source_doc_id=42,
        db_path=db,
    )
    assert pid is not None and pid > 0


def test_record_idempotent_on_natural_key(db: Path) -> None:
    args = dict(
        ticker="GOOG",
        source_kind="mgmt_commitment",
        prediction_md="x",
        made_at=datetime(2025, 11, 1, tzinfo=UTC),
        target_period=datetime(2026, 3, 31, tzinfo=UTC),
        kpi_name="cloud_op_margin",
        source_doc_id=42,
        db_path=db,
    )
    pid1 = record(**args)  # type: ignore[arg-type]
    pid2 = record(**args)  # type: ignore[arg-type]
    assert pid1 == pid2


def test_pending_for_grading_filters_past_target(db: Path) -> None:
    now = datetime.now(UTC)
    # Past target — should be in pending list
    record(
        ticker="GOOG",
        source_kind="mgmt_commitment",
        prediction_md="past",
        made_at=now - timedelta(days=90),
        target_period=now - timedelta(days=30),
        db_path=db,
    )
    # Future target — should NOT be in pending list
    record(
        ticker="GOOG",
        source_kind="mgmt_commitment",
        prediction_md="future",
        made_at=now,
        target_period=now + timedelta(days=90),
        db_path=db,
    )
    pend = pending_for_grading(db_path=db)
    assert len(pend) == 1
    assert pend[0].prediction_md == "past"


def test_grade_updates_outcome(db: Path) -> None:
    pid = record(
        ticker="GOOG",
        source_kind="mgmt_commitment",
        prediction_md="cloud margin > 18%",
        made_at=datetime(2025, 11, 1, tzinfo=UTC),
        target_period=datetime(2026, 3, 31, tzinfo=UTC),
        db_path=db,
    )
    assert pid is not None
    ok = grade(
        prediction_id=pid,
        outcome="met",
        realized_value=0.184,
        outcome_confidence=0.95,
        db_path=db,
    )
    assert ok is True

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT outcome, realized_value, outcome_confidence FROM predictions WHERE id = ?",
            (pid,),
        ).fetchone()
        assert row[0] == "met"
        assert float(row[1]) == pytest.approx(0.184)
        assert float(row[2]) == pytest.approx(0.95)
    finally:
        conn.close()


def test_history_filters_source_kind(db: Path) -> None:
    record(
        ticker="GOOG",
        source_kind="mgmt_commitment",
        prediction_md="m1",
        made_at=datetime(2025, 11, 1, tzinfo=UTC),
        db_path=db,
    )
    record(
        ticker="GOOG",
        source_kind="llm_bear_case",
        prediction_md="b1",
        made_at=datetime(2025, 10, 1, tzinfo=UTC),
        db_path=db,
    )
    record(
        ticker="META",
        source_kind="mgmt_commitment",
        prediction_md="m2",
        made_at=datetime(2025, 11, 1, tzinfo=UTC),
        db_path=db,
    )

    goog_all = history(ticker="GOOG", db_path=db)
    goog_bear = history(ticker="GOOG", source_kinds=["llm_bear_case"], db_path=db)
    assert {p.prediction_md for p in goog_all} == {"m1", "b1"}
    assert {p.prediction_md for p in goog_bear} == {"b1"}


def test_outcome_summary_counts_by_outcome(db: Path) -> None:
    for outcome in ["met", "met", "missed", "mixed", "pending"]:
        pid = record(
            ticker="GOOG",
            source_kind="mgmt_commitment",
            prediction_md=f"p-{outcome}",
            made_at=datetime(2025, 11, 1, tzinfo=UTC),
            db_path=db,
        )
        if outcome != "pending":
            grade(prediction_id=pid or 0, outcome=outcome, db_path=db)  # type: ignore[arg-type]

    summary = outcome_summary(ticker="GOOG", db_path=db)
    assert summary == {"met": 2, "missed": 1, "mixed": 1, "pending": 1}


def test_record_strips_inline_markdown_from_kpi_name(db: Path) -> None:
    """kpi_name is a scalar (matcher key + rendered label); prediction_md is
    prose and must keep its markdown untouched."""
    pid = record(
        ticker="GOOG",
        source_kind="llm_bear_case",
        prediction_md="**Cloud margin** expansion accelerating",
        made_at=datetime(2025, 11, 1, tzinfo=UTC),
        kpi_name="**cloud_operating_margin**",
        db_path=db,
    )
    assert pid is not None
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT kpi_name, prediction_md FROM predictions WHERE id = ?", (pid,)
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "cloud_operating_margin"
    assert row[1] == "**Cloud margin** expansion accelerating"


def test_record_returns_none_when_db_missing(tmp_path: Path) -> None:
    missing = tmp_path / "no.db"
    pid = record(
        ticker="X",
        source_kind="mgmt_commitment",
        prediction_md="y",
        made_at=datetime.now(UTC),
        db_path=missing,
    )
    assert pid is None
