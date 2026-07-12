"""The point-of-intent decision nudge (PR2, Deliverable 2) — scan, send-once
dedupe, the fill-in reply capture, and the await's 24h expiry."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from capture import decision_nudge, pending_replies, poller, research_notify, telegram
from user_state._db import now_naive_utc

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Self-contained schema — see tests/test_weekly_packet.py's fixture docstring
# for why this repo's tables can't come from an alembic stamp-then-upgrade
# replay (`decisions` predates the alembic history entirely).
_SCHEMA = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT,
    recommendation_kind TEXT,
    conviction TEXT,
    falsifier TEXT,
    decided_by TEXT,
    outcome_label TEXT,
    made_at TEXT,
    user_notes TEXT DEFAULT '',
    created_at TEXT
);
CREATE TABLE decision_nudges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL UNIQUE,
    chat_id INTEGER,
    status TEXT NOT NULL DEFAULT 'sent',
    sent_at TEXT NOT NULL,
    awaiting_until TEXT,
    filled_at TEXT
);
CREATE TABLE pending_telegram_replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    ref_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    consumed_at TEXT
);
"""


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "ledger.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return db


def _conn(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def _seed_stub(db: Path, *, ticker: str = "AGX", days_ago: float = 1.0) -> int:
    made_at = (now_naive_utc() - timedelta(days=days_ago)).isoformat()
    conn = _conn(db)
    try:
        cur = conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, made_at, "
            "outcome_label, created_at) VALUES (?, 'add', 'owner', ?, 'pending', ?)",
            (ticker, made_at, made_at),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


class _Spy:
    def __init__(self) -> None:
        self.sends: list[tuple[int, str, object]] = []

    def send(self, token: str, chat_id: int, text: str, *, reply_markup: object = None) -> object:
        self.sends.append((chat_id, text, reply_markup))
        return {}


# --------------------------------------------------------------------------
# Scan + send-once dedupe
# --------------------------------------------------------------------------


def test_find_stub_decisions_finds_fresh_ungraded_stub(db_path: Path) -> None:
    did = _seed_stub(db_path)
    stubs = decision_nudge.find_stub_decisions(db_path=db_path)
    assert [s.id for s in stubs] == [did]


def test_find_stub_decisions_ignores_old_and_complete(db_path: Path) -> None:
    _seed_stub(db_path, ticker="OLD", days_ago=30)  # outside the 7d lookback
    conn = _conn(db_path)
    try:
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, made_at, "
            "conviction, falsifier, outcome_label, created_at) VALUES "
            "('DONE', 'add', 'owner', ?, 'high', 'x', 'pending', ?)",
            (now_naive_utc().isoformat(), now_naive_utc().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    assert decision_nudge.find_stub_decisions(db_path=db_path) == []


def test_send_nudges_one_message_per_stub_then_never_again(db_path: Path) -> None:
    _seed_stub(db_path, ticker="AGX")
    _seed_stub(db_path, ticker="BKNG")
    spy = _Spy()
    sent = decision_nudge.send_nudges("tok", 5, db_path=db_path, send=spy.send)
    assert sent == 2
    assert len(spy.sends) == 2
    assert "AGX" in spy.sends[0][1] and "conviction" in spy.sends[0][1]

    # UNIQUE(decision_id) — a second run sends nothing (already nudged).
    spy2 = _Spy()
    sent2 = decision_nudge.send_nudges("tok", 5, db_path=db_path, send=spy2.send)
    assert sent2 == 0
    assert spy2.sends == []


def test_skip_never_re_nudges(db_path: Path) -> None:
    did = _seed_stub(db_path)
    decision_nudge._record_nudge(did, chat_id=5, db_path=db_path)  # simulate already-sent
    decision_nudge.mark_skipped(did, db_path=db_path)
    conn = _conn(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM decision_nudges WHERE decision_id = ?", (did,)
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "skipped"


# --------------------------------------------------------------------------
# The fill-in reply capture
# --------------------------------------------------------------------------


def test_fill_in_reply_writes_conviction_and_falsifier_write_once(db_path: Path) -> None:
    did = _seed_stub(db_path)
    decision_nudge.start_fill_in(did, chat_id=5, db_path=db_path)
    pending = pending_replies.peek(5, db_path=db_path)
    assert pending is not None and pending.kind == decision_nudge.FILL_IN_KIND

    spy = _Spy()
    decision_nudge.handle_fill_in_reply(
        "tok", 5, "high conviction\nNPL exceeds 5% for 2Q", pending, db_path=db_path, send=spy.send
    )
    conn = _conn(db_path)
    try:
        row = conn.execute(
            "SELECT conviction, falsifier FROM decisions WHERE id = ?", (did,)
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "high conviction"
    assert row[1] == "NPL exceeds 5% for 2Q"
    assert spy.sends and "Recorded" in spy.sends[0][1]
    assert pending_replies.peek(5, db_path=db_path) is None  # one-shot

    # write-once: a later reply must never overwrite an already-set field
    decision_nudge.start_fill_in(did, chat_id=5, db_path=db_path)
    pending2 = pending_replies.peek(5, db_path=db_path)
    decision_nudge.handle_fill_in_reply(
        "tok", 5, "low conviction\nsomething else", pending2, db_path=db_path, send=spy.send
    )
    conn = _conn(db_path)
    try:
        row2 = conn.execute(
            "SELECT conviction, falsifier FROM decisions WHERE id = ?", (did,)
        ).fetchone()
    finally:
        conn.close()
    assert row2[0] == "high conviction"  # unchanged
    assert row2[1] == "NPL exceeds 5% for 2Q"  # unchanged


def test_expiry_restores_default_capture(db_path: Path) -> None:
    """An expired await must never permanently hijack capture — the poller's
    own gate (``poller._dispatch_pending_reply``) must return False so the
    caller falls through to the default musing route. (Exercised at this
    seam rather than through the full ``poll_once`` -> ``ingest_capture``
    chain, which needs the full capture schema — raw_capture_sessions etc —
    out of scope for this table-light test file.)"""
    did = _seed_stub(db_path)
    # stash an award that's already expired
    pending_replies.stash(5, decision_nudge.FILL_IN_KIND, did, expiry_hours=-1, db_path=db_path)
    assert pending_replies.peek(5, db_path=db_path) is None  # expired -> invisible

    update = telegram.Update(update_id=1, kind="text", chat_id=5, text="a normal musing")
    consumed = poller._dispatch_pending_reply("tok", update, db_path)
    assert consumed is False  # capture is never hijacked past the 24h expiry

    # the decision itself is untouched — the expired award never applied anything
    conn = _conn(db_path)
    try:
        row = conn.execute(
            "SELECT conviction, falsifier FROM decisions WHERE id = ?", (did,)
        ).fetchone()
    finally:
        conn.close()
    assert row[0] is None and row[1] is None


# --------------------------------------------------------------------------
# Callback dispatch (research_notify.dispatch_callback -> the dn: branch)
# --------------------------------------------------------------------------


def _cb(data: str, *, chat_id: int = 5) -> telegram.Update:
    return telegram.Update(
        update_id=1, kind="callback", chat_id=chat_id, callback_data=data, callback_query_id="cq"
    )


def test_dispatch_dn_fill_stashes_await(db_path: Path) -> None:
    did = _seed_stub(db_path)
    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok", _cb(f"dn:fill:{did}"), db_path=db_path, send=spy.send, answer=spy.send
    )
    assert status == "dn_awaiting"
    pending = pending_replies.peek(5, db_path=db_path)
    assert (
        pending is not None
        and pending.kind == decision_nudge.FILL_IN_KIND
        and pending.ref_id == did
    )


def test_dispatch_dn_skip_marks_skipped(db_path: Path) -> None:
    did = _seed_stub(db_path)
    decision_nudge._record_nudge(did, chat_id=5, db_path=db_path)
    status = research_notify.dispatch_callback(
        "tok",
        _cb(f"dn:skip:{did}"),
        db_path=db_path,
        send=lambda *a, **k: {},
        answer=lambda *a, **k: {},
    )
    assert status == "dn_skipped"
    conn = _conn(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM decision_nudges WHERE decision_id = ?", (did,)
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "skipped"


def test_poller_routes_text_to_pending_reply_before_default_capture(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    did = _seed_stub(db_path)
    decision_nudge.start_fill_in(did, chat_id=5, db_path=db_path)
    monkeypatch.setattr(
        poller.telegram,
        "get_updates",
        lambda *a, **k: [
            telegram.Update(update_id=1, kind="text", chat_id=5, text="high\nfalsifier: NPL breach")
        ],
    )
    counts = poller.poll_once(
        "tok",
        db_path=db_path,
        offset_path=db_path.parent / "off.json",
        audio_dir=db_path.parent / "audio",
        confirm=False,
    )
    assert counts.get("pending_reply") == 1
    assert "landed" not in counts  # never fell through to the default musing route

    conn = _conn(db_path)
    try:
        row = conn.execute(
            "SELECT conviction, falsifier FROM decisions WHERE id = ?", (did,)
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "high"
    assert row[1] == "falsifier: NPL breach"
