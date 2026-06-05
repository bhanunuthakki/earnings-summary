"""Tests for src/pipeline/thesis_ledger_panel.py — the Thesis Ledger dashboard tab.

Renders the append-only thesis_ledger_entries history (the populated decision
history) as a command-center fragment, so it is discoverable in the app and not
only via the /digest route (v6 re-grade, Richness & surfacing).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pipeline.thesis_ledger_panel import render_thesis_ledger_panel  # noqa: E402
from user_state.ledger import append_entry  # noqa: E402

_CREATE = """
CREATE TABLE thesis_ledger_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL, ticker TEXT NOT NULL, entry_kind TEXT NOT NULL,
    body TEXT NOT NULL, source_alert_id INTEGER,
    created_at TEXT NOT NULL, accepted_at TEXT NOT NULL
)
"""


def _db_with_table(tmp_path: Path) -> Path:
    db = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(_CREATE)
        conn.commit()
    finally:
        conn.close()
    return db


def test_panel_renders_entries(tmp_path: Path) -> None:
    db = _db_with_table(tmp_path)
    append_entry(
        user_id="bhanu",
        ticker="NU",
        entry_kind="thesis_update",
        body="NIM compressed q/q",
        db_path=db,
    )
    append_entry(
        user_id="bhanu",
        ticker="NU",
        entry_kind="bear_append",
        body="Asset quality risk",
        db_path=db,
    )
    append_entry(
        user_id="bhanu",
        ticker="MELI",
        entry_kind="earnings_prep_append",
        body="Watch take rate",
        db_path=db,
    )

    html = render_thesis_ledger_panel(db, user_id="bhanu")
    assert "Thesis ledger" in html
    assert "Ledger entries" in html
    assert "NU" in html and "MELI" in html
    assert "NIM compressed q/q" in html
    assert "Thesis update" in html and "Bear case" in html  # kind labels


def test_panel_scopes_to_user(tmp_path: Path) -> None:
    db = _db_with_table(tmp_path)
    append_entry(user_id="bhanu", ticker="NU", entry_kind="thesis_update", body="mine", db_path=db)
    append_entry(
        user_id="someone", ticker="ZZ", entry_kind="thesis_update", body="theirs", db_path=db
    )
    html = render_thesis_ledger_panel(db, user_id="bhanu")
    assert "mine" in html
    assert "theirs" not in html


def test_panel_empty_state(tmp_path: Path) -> None:
    db = _db_with_table(tmp_path)
    html = render_thesis_ledger_panel(db, user_id="bhanu")
    assert "No accepted thesis changes yet" in html
