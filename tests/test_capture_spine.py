"""The Ledger capture spine: note enums + 0115/0116 migrations + audit writer.

A fresh tmp_path DB stamped at an early head and upgraded to head, so
``analyst_notes`` (0074) and the two new capture tables (0115/0116) exist
exactly as alembic builds them in production.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from capture import audit
from user_state import notes

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "ledger.db"
    cfg = _cfg(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    return db


def test_musing_note_round_trips(db_path: Path) -> None:
    row = notes.create_note(
        ticker="NU",
        kind="musing",
        body="NPL formation worries me but the guide was for seasonality",
        source="capture",
        db_path=db_path,
    )
    assert row.id > 0
    assert row.kind == "musing"
    assert row.source == "capture"
    got = notes.get_note(row.id, db_path=db_path)
    assert got is not None
    assert got.kind == "musing"
    assert got.body.startswith("NPL formation")


def test_capture_tables_exist_then_downgrade_drops_them(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "raw_capture_sessions" in names
    assert "capture_audit_log" in names

    command.downgrade(_cfg(db_path), "0114_decision_process_quality")
    conn = sqlite3.connect(str(db_path))
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "raw_capture_sessions" not in names
    assert "capture_audit_log" not in names


def test_raw_capture_external_ref_dedup(db_path: Path) -> None:
    ins = (
        "INSERT INTO raw_capture_sessions "
        "(channel, media_kind, transcript, status, external_ref, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)"
    )
    now = "2026-06-28 00:00:00"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(ins, ("telegram", "voice", "raw", "captured", "tg:42", now, now))
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(ins, ("telegram", "text", "again", "captured", "tg:42", now, now))
            conn.commit()
    finally:
        conn.close()

    # NULL external_ref must NOT collide (the unique index is partial).
    conn = sqlite3.connect(str(db_path))
    try:
        ins2 = (
            "INSERT INTO raw_capture_sessions (channel, transcript, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)"
        )
        conn.execute(ins2, ("tray", "a", now, now))
        conn.execute(ins2, ("tray", "b", now, now))
        conn.commit()
    finally:
        conn.close()


def test_audit_stores_summary_not_raw_and_sums_cost(db_path: Path) -> None:
    raw = "NPL formation worries me. " * 50  # a long, raw-looking utterance
    sha = audit.prompt_hash("the full prompt body that must never be stored")
    aid = audit.write_audit(
        channel="telegram",
        action="structured",
        utterance_summary="NPL seasonality wondering on NU",
        purpose="musing_structure",
        model="claude-haiku-4-5",
        prompt_sha256=sha,
        cost_usd=0.012,
        db_path=db_path,
    )
    assert aid > 0
    audit.write_audit(
        channel="telegram",
        action="landed",
        utterance_summary="landed the musing",
        cost_usd=0.0,
        db_path=db_path,
    )

    conn = sqlite3.connect(str(db_path))
    try:
        summary, stored_sha, cost = conn.execute(
            "SELECT utterance_summary, prompt_sha256, cost_usd FROM capture_audit_log WHERE id=?",
            (aid,),
        ).fetchone()
    finally:
        conn.close()
    assert summary == "NPL seasonality wondering on NU"
    assert raw not in summary  # the raw utterance is NEVER stored
    assert stored_sha == sha and sha is not None and len(sha) == 64
    assert cost == 0.012

    assert abs(audit.audit_cost_in_window(db_path=db_path) - 0.012) < 1e-9
    assert audit.audit_cost_in_window(since="2099-01-01 00:00:00", db_path=db_path) == 0.0


def test_audit_clips_oversize_summary(db_path: Path) -> None:
    huge = "x" * 5000
    aid = audit.write_audit(
        channel="tray", action="captured", utterance_summary=huge, db_path=db_path
    )
    conn = sqlite3.connect(str(db_path))
    try:
        stored = conn.execute(
            "SELECT utterance_summary FROM capture_audit_log WHERE id=?", (aid,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert len(stored) <= 600  # hard cap, raw can never be dumped verbatim


def test_summarize_utterance_collapses_and_clips() -> None:
    long = "  ".join(["word"] * 200)
    s = audit.summarize_utterance(long)
    assert len(s) <= 160
    assert s.endswith("…")
    assert "\n" not in s


def test_audit_rejects_unknown_action(db_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown audit action"):
        audit.write_audit(channel="tray", action="bogus", utterance_summary="x", db_path=db_path)
