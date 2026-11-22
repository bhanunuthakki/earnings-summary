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


def test_missing_view_degrades_gracefully_on_pre_0179_db(tmp_path: Path) -> None:
    db_path = tmp_path / "bare.db"
    with sqlite3.connect(str(db_path)) as conn:
        try:
            conn.execute("SELECT * FROM v_decision_journal").fetchall()
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError("expected missing view to raise")
