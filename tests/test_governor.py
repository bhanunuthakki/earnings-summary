"""The nag governor — moment collection, freshness gate, caps, auto-mute (W2)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from research.governor import (
    DAILY_CAP,
    MUTE_AFTER,
    Moment,
    collect_moments,
    digest_pings,
    get_ping,
    record_dismissal,
    run_governor,
    unmute,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0130_owner_decision_extension"
HEAD = "0131_coach_pings"

_PRE_DDL = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16),
    recommendation_kind VARCHAR(32) NOT NULL,
    conviction VARCHAR(16),
    decided_by VARCHAR(16) NOT NULL DEFAULT 'advisor',
    scope VARCHAR(16) NOT NULL DEFAULT 'ticker',
    falsifier TEXT,
    size_usd FLOAT,
    user_notes TEXT,
    made_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL
);
CREATE TABLE alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT,
    trigger_kind TEXT NOT NULL,
    fired_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    evidence_json TEXT,
    dismissed_at TEXT
);
CREATE TABLE analyst_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    body TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE tracked_companies (ticker TEXT PRIMARY KEY, list_type TEXT NOT NULL);
INSERT INTO tracked_companies VALUES ('NU','portfolio');
"""

_NOW = datetime(2026, 7, 10, 12, 0, 0)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "gov.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_PRE_DDL)
        conn.commit()
    finally:
        conn.close()
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, HEAD)
    return path


def _seed_breach(db: Path, *, falsifier: str = "15-90d NPL >5% for 2Q") -> int:
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, falsifier, "
            "made_at, created_at) VALUES ('NU','add','owner',?,'2026-03-15','2026-03-15')",
            (falsifier,),
        )
        did = int(cur.lastrowid or 0)
        conn.execute(
            "INSERT INTO alerts (ticker, trigger_kind, fired_at, evidence_json) VALUES "
            "('NU','decision_condition','2026-07-10T08:00:00',?)",
            (json.dumps({"decision_id": did}),),
        )
        conn.commit()
        return did
    finally:
        conn.close()


def _seed_stub(db: Path) -> int:
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, size_usd, "
            "user_notes, made_at, created_at) VALUES ('NU','initiate','owner',9000,"
            "'retro-net:NU:2026-07-08:buy · unannounced', '2026-07-08','2026-07-08T10:00:00')"
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def test_collect_and_send_falsifier_breach(db: Path) -> None:
    _seed_breach(db)
    sent: list[Moment] = []
    tally = run_governor(db, send_fn=lambda pid, m: sent.append(m) or True, now=_NOW)
    assert tally["sent"] == 1
    assert sent[0].class_ == "falsifier_breach"
    assert "NPL" in sent[0].body and "NU" in sent[0].body
    # A moment is considered exactly once — rerun sees nothing new
    again = run_governor(db, send_fn=lambda pid, m: True, now=_NOW)
    assert again["seen"] == 0


def test_freshness_gate_blocks_stale_and_inferred(db: Path) -> None:
    did = _seed_breach(db, falsifier="Memory cycle rolls over. (inferred)")
    tally = run_governor(db, send_fn=lambda pid, m: True, now=_NOW)
    assert tally["skipped_stale"] == 1 and tally["sent"] == 0

    # Ratify (marker stripped) → a NEW alert moment passes the gate
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE decisions SET falsifier='Memory cycle rolls over.' WHERE id=?", (did,))
        conn.execute(
            "INSERT INTO alerts (ticker, trigger_kind, fired_at, evidence_json) VALUES "
            "('NU','decision_condition','2026-07-10T09:00:00',?)",
            (json.dumps({"decision_id": did}),),
        )
        conn.commit()
    finally:
        conn.close()
    tally2 = run_governor(db, send_fn=lambda pid, m: True, now=_NOW)
    assert tally2["sent"] == 1


def test_daily_cap_overflows_to_digest(db: Path) -> None:
    _seed_breach(db)
    _seed_stub(db)  # a second moment the same day
    tally = run_governor(db, send_fn=lambda pid, m: True, now=_NOW)
    assert tally["sent"] == DAILY_CAP
    assert tally["digest"] == 1
    assert len(digest_pings(db)) == 1


def test_failed_send_downgrades_to_digest(db: Path) -> None:
    _seed_breach(db)
    tally = run_governor(db, send_fn=lambda pid, m: False, now=_NOW)
    assert tally["digest"] == 1 and tally["sent"] == 0


def test_three_consecutive_dismissals_mute_the_class(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    try:
        for i in range(MUTE_AFTER):
            conn.execute(
                "INSERT INTO coach_pings (class_, key, body, status, source_ref, "
                "created_at, updated_at) VALUES ('intent_followup', ?, 'x', 'sent', "
                "'note:1', '2026-07-09', '2026-07-09')",
                (f"k{i}",),
            )
        conn.commit()
        ids = [int(r[0]) for r in conn.execute("SELECT id FROM coach_pings ORDER BY id")]
    finally:
        conn.close()

    muted_class = None
    for pid in ids:
        recorded, muted = record_dismissal(pid, db_path=db)
        assert recorded
        muted_class = muted or muted_class
    assert muted_class == "intent_followup"

    # Muted class: a fresh open intent gets skipped_muted, never sent
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO analyst_notes (kind, status, body, source, created_at, updated_at) "
            "VALUES ('intent','open','LEAP sleeve','capture','2026-06-01','2026-06-01')"
        )
        conn.commit()
    finally:
        conn.close()
    tally = run_governor(db, send_fn=lambda pid, m: True, now=_NOW)
    assert tally["skipped_muted"] == 1 and tally["sent"] == 0

    assert unmute("intent_followup", db_path=db)


def test_intent_followup_and_annotation_moments(db: Path) -> None:
    _seed_stub(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO analyst_notes (kind, status, body, source, created_at, updated_at) "
            "VALUES ('intent','open','far-OTM LEAP sleeve on the next washout','capture',"
            "'2026-06-01','2026-06-01')"
        )
        conn.commit()
    finally:
        conn.close()
    classes = {m.class_ for m in collect_moments(db, now=_NOW)}
    assert classes == {"retro_annotation", "intent_followup"}


def test_intent_followup_body_points_to_the_ledger_tab_not_ledger_land(db: Path) -> None:
    """/ledger-land does not exist on Telegram — the ping must not tell the
    owner to type a command that will silently vanish."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO analyst_notes (kind, status, body, source, created_at, updated_at) "
            "VALUES ('intent','open','far-OTM LEAP sleeve on the next washout','capture',"
            "'2026-06-01','2026-06-01')"
        )
        conn.commit()
    finally:
        conn.close()
    moments = [m for m in collect_moments(db, now=_NOW) if m.class_ == "intent_followup"]
    assert moments
    for m in moments:
        assert "/ledger-land" not in m.body
        assert "Ledger tab" in m.body


def test_get_ping_returns_row_or_none(db: Path) -> None:
    _seed_breach(db)
    tally = run_governor(db, send_fn=lambda pid, m: True, now=_NOW)
    assert tally["sent"] == 1
    conn = sqlite3.connect(str(db))
    try:
        ping_id = int(
            conn.execute("SELECT id FROM coach_pings WHERE class_ = 'falsifier_breach'").fetchone()[
                0
            ]
        )
    finally:
        conn.close()
    row = get_ping(ping_id, db_path=db)
    assert row is not None
    assert row.id == ping_id
    assert row.class_ == "falsifier_breach"
    assert row.ticker == "NU"
    assert row.status == "sent"
    assert get_ping(999999, db_path=db) is None


def test_falsifier_breach_ping_gets_the_two_button_keyboard() -> None:
    """execution/run_coach_pings.py's Answer button — a falsifier_breach moment
    with a ticker gets Answer+Dismiss; every other moment class (or a
    falsifier_breach with no ticker) keeps the original Dismiss-only rows."""
    from execution.run_coach_pings import ping_buttons

    breach = Moment(
        class_="falsifier_breach", key="alert:1", ticker="NU", body="x", source_ref="decision:1"
    )
    rows = ping_buttons(7, breach)
    assert rows == [[("Answer: review NU", "cp:review:7"), ("Dismiss", "cp:dismiss:7")]]

    no_ticker = Moment(
        class_="falsifier_breach", key="alert:2", ticker=None, body="x", source_ref="decision:2"
    )
    assert ping_buttons(8, no_ticker) == [[("Dismiss", "cp:dismiss:8")]]

    annotation = Moment(
        class_="retro_annotation", key="annot:1", ticker="NU", body="x", source_ref="decision:1"
    )
    assert ping_buttons(9, annotation) == [[("Dismiss", "cp:dismiss:9")]]

    intent = Moment(
        class_="intent_followup", key="intent:1:0", ticker=None, body="x", source_ref="note:1"
    )
    assert ping_buttons(10, intent) == [[("Dismiss", "cp:dismiss:10")]]
