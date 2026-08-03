"""Tests for execution/backfill_strip_markdown_scalars.py — the one-shot
sweep stripping inline markdown out of pre-existing LLM-persisted scalars."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

from backfill_strip_markdown_scalars import sweep_db, sweep_holdings  # noqa: E402


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(
        """
        CREATE TABLE management_commitments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kpi_name TEXT NOT NULL
        );
        CREATE TABLE advisor_memos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL
        );
        CREATE TABLE analyst_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            body TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE analyst_notes_fts
            USING fts5(body, content='analyst_notes', content_rowid='id');
        CREATE TRIGGER analyst_notes_fts_ai AFTER INSERT ON analyst_notes BEGIN
            INSERT INTO analyst_notes_fts(rowid, body) VALUES (new.id, new.body); END;
        CREATE TRIGGER analyst_notes_fts_au AFTER UPDATE ON analyst_notes BEGIN
            INSERT INTO analyst_notes_fts(analyst_notes_fts, rowid, body)
                VALUES ('delete', old.id, old.body);
            INSERT INTO analyst_notes_fts(rowid, body) VALUES (new.id, new.body); END;
        CREATE TABLE standup_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            headline TEXT,
            conclusion TEXT
        );
        CREATE TABLE predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kpi_name TEXT
        );
        """
    )
    c.execute("INSERT INTO management_commitments (kpi_name) VALUES ('**Risk-adj. NIM**')")
    c.execute("INSERT INTO management_commitments (kpi_name) VALUES ('Deposits')")
    c.execute("INSERT INTO advisor_memos (title) VALUES ('## Where the next dollar works')")
    # analyst_notes.body: a legacy feed scalar leaking a `##` heading + `**` lead.
    c.execute(
        "INSERT INTO analyst_notes (body) VALUES "
        "('## Where the next dollar works hardest **MELI — deepen.** conclusion.')"
    )
    c.execute("INSERT INTO analyst_notes (body) VALUES ('already plain note.')")
    # standup scalars carry a `≥` (math symbol) — must not crash the sweep.
    c.execute(
        "INSERT INTO standup_messages (headline, conclusion) VALUES "
        "('**NU** deposits grew', '**Revenue YoY ≥ 25%** — the signal is *real*.')"
    )
    c.execute("INSERT INTO predictions (kpi_name) VALUES ('**Take rate**')")
    c.commit()
    return c


def test_db_dry_run_reports_but_writes_nothing(conn: sqlite3.Connection) -> None:
    tally = sweep_db(conn, apply=False)
    assert tally["management_commitments.kpi_name dirty"] == 1
    assert tally["advisor_memos.title dirty"] == 1
    assert "updated" not in " ".join(tally)
    (kpi,) = conn.execute("SELECT kpi_name FROM management_commitments WHERE id = 1").fetchone()
    assert kpi == "**Risk-adj. NIM**"


def test_db_apply_strips_and_is_idempotent(conn: sqlite3.Connection) -> None:
    tally = sweep_db(conn, apply=True)
    assert tally["management_commitments.kpi_name updated"] == 1
    assert tally["advisor_memos.title updated"] == 1
    (kpi,) = conn.execute("SELECT kpi_name FROM management_commitments WHERE id = 1").fetchone()
    assert kpi == "Risk-adj. NIM"
    (title,) = conn.execute("SELECT title FROM advisor_memos WHERE id = 1").fetchone()
    assert title == "Where the next dollar works"
    # Second pass finds nothing dirty.
    again = sweep_db(conn, apply=True)
    assert again["management_commitments.kpi_name dirty"] == 0
    assert again["advisor_memos.title dirty"] == 0


def test_db_apply_strips_new_targets_and_rebuilds_fts(conn: sqlite3.Connection) -> None:
    """The #1144 follow-up targets — analyst_notes.body, standup_messages
    headline/conclusion, predictions.kpi_name — strip, and the external-content
    analyst_notes_fts mirror is rebuilt so it matches the stripped bodies."""
    tally = sweep_db(conn, apply=True)
    assert tally["analyst_notes.body updated"] == 1
    assert tally["standup_messages.headline updated"] == 1
    assert tally["standup_messages.conclusion updated"] == 1
    assert tally["predictions.kpi_name updated"] == 1
    assert tally["analyst_notes_fts rebuilt"] == 1

    (body,) = conn.execute("SELECT body FROM analyst_notes WHERE id = 1").fetchone()
    assert body == "Where the next dollar works hardest MELI — deepen. conclusion."
    # A `≥`-bearing conclusion strips its markers without crashing.
    (concl,) = conn.execute("SELECT conclusion FROM standup_messages WHERE id = 1").fetchone()
    assert concl == "Revenue YoY ≥ 25% — the signal is real."
    # FTS mirror is consistent with the stripped base table and searchable.
    hits = conn.execute(
        "SELECT rowid FROM analyst_notes_fts WHERE analyst_notes_fts MATCH 'MELI'"
    ).fetchall()
    assert [r[0] for r in hits] == [1]


def test_db_sweep_skips_absent_tables(conn: sqlite3.Connection) -> None:
    """A target whose table is missing degrades to a skip, not a crash."""
    conn.execute("DROP TABLE predictions")
    conn.commit()
    tally = sweep_db(conn, apply=False)
    assert tally["predictions.kpi_name table absent"] == 1
    assert "predictions.kpi_name scanned" not in tally


def test_holdings_sweep_dry_run_then_apply(tmp_path: Path) -> None:
    holdings = tmp_path / "holdings"
    holdings.mkdir()
    dirty = {
        "ticker": "NU",
        "thesis": "Keep **this** markdown.",
        "chart_priorities": ["**Priority #1 — Mexico momentum**"],
    }
    (holdings / "NU.json").write_text(json.dumps(dirty, indent=2), encoding="utf-8")
    (holdings / "CLEAN.json").write_text(
        json.dumps({"ticker": "CLEAN", "chart_priorities": ["ROE"]}), encoding="utf-8"
    )

    tally = sweep_holdings(holdings, apply=False)
    assert tally["holdings scanned"] == 2
    assert tally["holdings dirty"] == 1
    assert json.loads((holdings / "NU.json").read_text(encoding="utf-8")) == dirty

    tally = sweep_holdings(holdings, apply=True)
    assert tally["holdings updated"] == 1
    after = json.loads((holdings / "NU.json").read_text(encoding="utf-8"))
    assert after["chart_priorities"] == ["Priority #1 — Mexico momentum"]
    # Prose thesis keeps its markdown.
    assert after["thesis"] == "Keep **this** markdown."

    again = sweep_holdings(holdings, apply=True)
    assert again["holdings dirty"] == 0
