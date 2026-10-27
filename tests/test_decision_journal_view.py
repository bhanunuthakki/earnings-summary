"""Tests for the retained decision series in the journal view."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path


def _build_db(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    db_dir = tmp_path / "data"
    db_dir.mkdir(parents=True, exist_ok=True)
    return migrated_db(db_dir / "portfolio.db")


def _row(conn: sqlite3.Connection, decision_id: int) -> sqlite3.Row:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM v_decision_journal WHERE decision_id = ?", (decision_id,)
    ).fetchone()
    assert row is not None
    return row


def test_view_exposes_decision_series_and_owner_inputs(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = _build_db(tmp_path, migrated_db)
    with sqlite3.connect(str(db_path)) as conn:
        cols = {d[0] for d in conn.execute("SELECT * FROM v_decision_journal LIMIT 0").description}

    assert {
        "decision_id",
        "ticker",
        "scope",
        "decided_by",
        "recommendation_kind",
        "recommendation_value",
        "conviction",
        "instrument",
        "account",
        "size_usd",
        "size_pct",
        "falsifier",
        "made_at",
        "user_action_kind",
        "user_acted_at",
        "outcome_label",
        "outcome_pct",
        "outcome_at",
        "process_quality",
        "advice_artifact_id",
    } <= cols
    assert (
        not {
            "linked_memo_id",
            "linked_memo_kind",
            "advice_before_memo_id",
            "guard_override_flag",
            "owner_attested_change",
            "coach_ping_class",
            "decision_nudge_id",
            "stance_verdict",
            "source_draft_id",
        }
        & cols
    )


def test_decision_series_keeps_owner_action_outcome_and_grade(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = _build_db(tmp_path, migrated_db)
    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, recommendation_value, conviction, "
            "decided_by, made_at, created_at, user_action_kind, user_acted_at, outcome_label, "
            "outcome_pct, outcome_at, process_quality, advice_artifact_id) VALUES "
            "('FLKR','add',12.5,'high','owner','2026-06-10T00:00:00',"
            "'2026-06-10T00:00:00','followed','2026-06-11T00:00:00','correct',5.5,"
            "'2026-09-10T00:00:00','sound',42)"
        )
        decision_id = int(cur.lastrowid or 0)
        conn.commit()
        row = _row(conn, decision_id)

    assert row["decided_by"] == "owner"
    assert row["recommendation_value"] == 12.5
    assert row["conviction"] == "high"
    assert row["user_action_kind"] == "followed"
    assert row["user_acted_at"] == "2026-06-11T00:00:00"
    assert row["outcome_label"] == "correct"
    assert row["outcome_pct"] == 5.5
    assert row["process_quality"] == "sound"
    assert row["advice_artifact_id"] == 42


def test_missing_view_degrades_gracefully_on_pre_0179_db(tmp_path: Path) -> None:
    db_path = tmp_path / "bare.db"
    with sqlite3.connect(str(db_path)) as conn:
        try:
            conn.execute("SELECT * FROM v_decision_journal").fetchall()
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError("expected missing view to raise")


def test_downgrade_drops_view(tmp_path: Path, migrated_db: Callable[..., Path]) -> None:
    from alembic.config import Config

    from alembic import command

    project_root = Path(__file__).resolve().parents[1]
    db_path = migrated_db(
        tmp_path / "data" / "portfolio.db",
        stamp="0059_kpi_facts_restatement",
        archived=True,
    )
    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(project_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.downgrade(cfg, "0174_behavior_distill_budget")
    with sqlite3.connect(str(db_path)) as conn:
        view = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view' AND name='v_decision_journal'"
        ).fetchone()
    assert view is None
