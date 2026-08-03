"""capture.coach_reply — free-text replies to a governed coach ping (B3).

A coach ping (``execution/run_coach_pings.py``) was send-only: the owner's
free-text reply had nothing to route back to, so it fell through to ordinary
capture with the finding linkage lost. This module tests the fix: the reply
ALWAYS lands verbatim (safety invariant), then gets classified into a small
deterministic-outcome enum and applied.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from alembic.config import Config

import db as dbmod
from alembic import command
from capture import coach_reply, telegram
from capture.matcher import build_roster_index
from research import governor
from user_state import notes
from user_state._db import now_naive_utc

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"
PRE_MIGRATION_HEAD = (
    "0187_wealth_context_snapshot_history"  # one revision before this PR's migration
)
ROSTER = build_roster_index(symbols=["NU", "MELI"], phrases={"nubank": "NU"})


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "ledger.db", stamp=PRIOR_HEAD)


@pytest.fixture
def pre_migration_db_path(tmp_path: Path) -> Path:
    """A fully-migrated DB stopping ONE revision short of 0188 — coach_pings
    exists (0131) but has no ``telegram_message_id`` column yet."""
    db = tmp_path / "pre.db"
    cfg = _cfg(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, PRE_MIGRATION_HEAD)
    return db


def _seed_ping(
    db_path: Path,
    *,
    class_: str = "calibration_finding",
    ticker: str | None = None,
    body: str = "33% of your high-conviction calls graded below the bar.",
    source_ref: str | None = "calibration:2026-07",
    status: str = "sent",
    mid: int | None = None,
    age: timedelta = timedelta(hours=1),
) -> int:
    """Insert one coach_pings row, ``age`` old (default: 1h ago -> inside the
    reply window)."""
    stamp = (now_naive_utc() - age).isoformat()
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "INSERT INTO coach_pings (class_, key, ticker, body, status, source_ref, "
            "telegram_message_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                class_,
                f"key:{class_}:{mid if mid is not None else 'none'}:{age}",
                ticker,
                body,
                status,
                source_ref,
                mid,
                stamp,
                stamp,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _ping_status(db_path: Path, ping_id: int) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT status FROM coach_pings WHERE id = ?", (ping_id,)).fetchone()
        return str(row[0])
    finally:
        conn.close()


def _direct_update(update_id: int, text: str, reply_to: int) -> telegram.Update:
    return telegram.Update(
        update_id=update_id, kind="text", chat_id=1, text=text, reply_to_message_id=reply_to
    )


def _window_update(update_id: int, text: str) -> telegram.Update:
    return telegram.Update(update_id=update_id, kind="text", chat_id=1, text=text)


def _stub_classify(monkeypatch: pytest.MonkeyPatch, intent: str) -> None:
    monkeypatch.setattr(
        coach_reply,
        "classify_reply",
        lambda ping, text, **kw: coach_reply.ReplyVerdict(intent=intent),
    )


def _stub_send(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sent: list[str] = []
    monkeypatch.setattr(
        telegram, "send_message", lambda token, chat_id, text, **k: sent.append(text)
    )
    return sent


# ---------------------------------------------------------------------------
# find_reply_ping
# ---------------------------------------------------------------------------


def test_find_reply_ping_direct_match(db_path: Path) -> None:
    ping_id = _seed_ping(db_path, mid=500)
    found = coach_reply.find_reply_ping(
        telegram.Update(update_id=1, kind="text", chat_id=1, text="ok", reply_to_message_id=500),
        db_path=db_path,
    )
    assert found is not None
    ping, mode = found
    assert ping.id == ping_id and mode == "direct"


def test_find_reply_ping_window_match_within_6h(db_path: Path) -> None:
    ping_id = _seed_ping(db_path, age=timedelta(hours=2))
    found = coach_reply.find_reply_ping(_window_update(2, "hmm"), db_path=db_path)
    assert found is not None
    ping, mode = found
    assert ping.id == ping_id and mode == "window"


def test_find_reply_ping_stale_ping_no_reply_to_returns_none(db_path: Path) -> None:
    _seed_ping(db_path, age=timedelta(hours=8))  # older than the 6h window
    assert coach_reply.find_reply_ping(_window_update(3, "hmm"), db_path=db_path) is None


def test_find_reply_ping_unknown_reply_to_returns_none(db_path: Path) -> None:
    _seed_ping(db_path, mid=500)
    found = coach_reply.find_reply_ping(
        _direct_update(4, "hmm", 999),  # 999 stamped on no ping
        db_path=db_path,
    )
    assert found is None


# ---------------------------------------------------------------------------
# dispatch — direct mode, outcomes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("intent", "expected_receipt_fragment"),
    [
        ("acknowledge", "Logged against the calibration_finding finding."),
        ("note", "Noted — linked to the calibration_finding finding."),
    ],
)
def test_dispatch_direct_reply_lands_musing_and_applies_outcome(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, intent: str, expected_receipt_fragment: str
) -> None:
    ping_id = _seed_ping(db_path, mid=500)
    _stub_classify(monkeypatch, intent)
    sent = _stub_send(monkeypatch)

    update = _direct_update(10, "yeah I saw that, thanks", 500)
    consumed = coach_reply.dispatch("tok", update, roster=ROSTER, db_path=db_path)

    assert consumed is True
    assert sent == [expected_receipt_fragment]
    musings = notes.list_notes(kind="musing", db_path=db_path)
    assert len(musings) == 1
    landed = musings[0]
    assert landed.body == "yeah I saw that, thanks"
    assert (landed.context or {}).get("coach_ping_id") == ping_id
    assert (landed.context or {}).get("coach_ping_class") == "calibration_finding"
    if intent == "acknowledge":
        assert _ping_status(db_path, ping_id) == "acted"


def test_dispatch_dismiss_routes_through_record_dismissal(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ping_id = _seed_ping(db_path, mid=501, class_="retro_annotation")
    _stub_classify(monkeypatch, "dismiss")
    sent = _stub_send(monkeypatch)

    consumed = coach_reply.dispatch(
        "tok", _direct_update(11, "drop it", 501), roster=ROSTER, db_path=db_path
    )

    assert consumed is True
    assert sent == ["Dismissed."]
    assert _ping_status(db_path, ping_id) == "dismissed"


def test_dispatch_annotate_decision_with_decision_source_ref(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ping_id = _seed_ping(db_path, mid=502, class_="retro_annotation", source_ref="decision:42")
    _stub_classify(monkeypatch, "annotate_decision")
    sent = _stub_send(monkeypatch)

    coach_reply.dispatch(
        "tok",
        _direct_update(12, "conviction: high, falsifier: NPL>5%", 502),
        roster=ROSTER,
        db_path=db_path,
    )

    assert sent == ["Filed against decision #42."]
    assert _ping_status(db_path, ping_id) == "acted"
    landed = notes.list_notes(kind="musing", db_path=db_path)[0]
    assert landed.decision_id == 42


def test_dispatch_annotate_decision_without_decision_ref_falls_back_to_note(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_ping(db_path, mid=503, class_="intent_followup", source_ref="note:7")
    _stub_classify(monkeypatch, "annotate_decision")
    sent = _stub_send(monkeypatch)

    coach_reply.dispatch(
        "tok", _direct_update(13, "still live", 503), roster=ROSTER, db_path=db_path
    )

    assert sent == ["Noted — linked to the intent_followup finding."]


def test_dispatch_profile_fact_falls_back_to_note(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No generic staging path exists for a raw-text profile fact (see
    ``_apply_outcome``'s comment) — it degrades to a plain filed note rather
    than a silent no-op."""
    _seed_ping(db_path, mid=504)
    _stub_classify(monkeypatch, "profile_fact")
    sent = _stub_send(monkeypatch)

    coach_reply.dispatch(
        "tok", _direct_update(14, "I never add on red days", 504), roster=ROSTER, db_path=db_path
    )

    assert sent == ["Noted — linked to the calibration_finding finding."]


# ---------------------------------------------------------------------------
# dispatch — window mode
# ---------------------------------------------------------------------------


def test_dispatch_window_match_within_6h_consumed(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ping_id = _seed_ping(db_path, age=timedelta(hours=3))
    _stub_classify(monkeypatch, "note")
    sent = _stub_send(monkeypatch)

    consumed = coach_reply.dispatch(
        "tok", _window_update(20, "still thinking about that"), roster=ROSTER, db_path=db_path
    )

    assert consumed is True
    assert sent == ["Noted — linked to the calibration_finding finding."]
    landed = notes.list_notes(kind="musing", db_path=db_path)[0]
    assert (landed.context or {}).get("coach_ping_id") == ping_id


def test_dispatch_window_match_unrelated_releases_to_ordinary_capture(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Window mode classifies BEFORE landing: 'unrelated' hands the message
    back untouched so the poller's full capture chain (confirm / wondering tap
    / ledger answer) runs — nothing landed here, no receipt sent."""
    _seed_ping(db_path, age=timedelta(hours=1))
    _stub_classify(monkeypatch, "unrelated")
    sent = _stub_send(monkeypatch)

    consumed = coach_reply.dispatch(
        "tok",
        _window_update(21, "completely different thought: NU earnings soon"),
        roster=ROSTER,
        db_path=db_path,
    )

    assert consumed is False
    assert sent == []
    assert notes.list_notes(kind="musing", db_path=db_path) == []


def test_dispatch_window_classifier_failure_releases_to_ordinary_capture(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A classifier that died can't vouch for a window match — the message goes
    back to ordinary capture instead of being consumed on zero evidence."""
    _seed_ping(db_path, age=timedelta(hours=1))

    def _boom(prompt: str) -> dict[str, object]:
        raise RuntimeError("model down")

    monkeypatch.setattr(coach_reply, "_default_call", _boom)
    sent = _stub_send(monkeypatch)

    consumed = coach_reply.dispatch(
        "tok", _window_update(24, "some passing thought"), roster=ROSTER, db_path=db_path
    )

    assert consumed is False
    assert sent == []
    assert notes.list_notes(kind="musing", db_path=db_path) == []


def test_dispatch_stale_ping_no_reply_to_returns_false(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_ping(db_path, age=timedelta(hours=8))
    sent = _stub_send(monkeypatch)

    consumed = coach_reply.dispatch(
        "tok", _window_update(22, "just a random thought"), roster=ROSTER, db_path=db_path
    )

    assert consumed is False
    assert sent == []
    assert notes.list_notes(kind="musing", db_path=db_path) == []


def test_dispatch_classifier_exception_in_direct_mode_falls_back_to_note(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ping_id = _seed_ping(db_path, mid=505)

    def _boom(*a: object, **kw: object) -> coach_reply.ReplyVerdict:
        raise RuntimeError("model down")

    monkeypatch.setattr(coach_reply, "classify_reply", _boom)
    sent = _stub_send(monkeypatch)

    consumed = coach_reply.dispatch(
        "tok", _direct_update(23, "hmm", 505), roster=ROSTER, db_path=db_path
    )

    assert consumed is True  # the musing already landed — never re-captured
    assert sent == ["Noted — linked to the calibration_finding finding."]
    assert len(notes.list_notes(kind="musing", db_path=db_path)) == 1
    assert _ping_status(db_path, ping_id) == "sent"  # no outcome applied — nothing to undo


def test_classify_reply_unknown_intent_from_llm_falls_open_to_note(db_path: Path) -> None:
    """``classify_reply`` itself, not just ``dispatch`` — an out-of-enum or
    'unrelated'-in-direct-mode intent must fail open to 'note'."""
    ping = coach_reply.PingLike(
        id=1,
        class_="calibration_finding",
        ticker=None,
        body="x",
        source_ref=None,
        status="sent",
        created_at=now_naive_utc().isoformat(),
    )
    verdict = coach_reply.classify_reply(
        ping,
        "whatever",
        mode="direct",
        db_path=db_path,
        call=lambda prompt: {"intent": "unrelated"},  # not valid outside window mode
    )
    assert verdict.intent == "note"

    verdict2 = coach_reply.classify_reply(
        ping,
        "whatever",
        mode="direct",
        db_path=db_path,
        call=lambda prompt: {"intent": "gibberish"},
    )
    assert verdict2.intent == "note"


# ---------------------------------------------------------------------------
# record_ping_message_id
# ---------------------------------------------------------------------------


def test_record_ping_message_id_writes_column(db_path: Path) -> None:
    ping_id = _seed_ping(db_path, mid=None)
    ok = governor.record_ping_message_id(ping_id, 777, db_path=db_path)
    assert ok is True
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT telegram_message_id FROM coach_pings WHERE id = ?", (ping_id,)
        ).fetchone()
        assert int(row[0]) == 777
    finally:
        conn.close()


def test_record_ping_message_id_noops_on_pre_migration_schema(
    pre_migration_db_path: Path,
) -> None:
    conn = sqlite3.connect(str(pre_migration_db_path))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(coach_pings)").fetchall()}
        assert "telegram_message_id" not in cols  # confirms the fixture's premise
        stamp = now_naive_utc().isoformat()
        cur = conn.execute(
            "INSERT INTO coach_pings (class_, key, ticker, body, status, source_ref, "
            "created_at, updated_at) VALUES ('calibration_finding', 'k', NULL, 'x', 'sent', "
            "NULL, ?, ?)",
            (stamp, stamp),
        )
        conn.commit()
        ping_id = int(cur.lastrowid or 0)
    finally:
        conn.close()
    ok = governor.record_ping_message_id(ping_id, 777, db_path=pre_migration_db_path)
    assert ok is False  # column missing — no-op, no raise


# ---------------------------------------------------------------------------
# execution/run_coach_pings.py integration — _send captures the message id
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    """(repo_root, db_path) — a real, fully-migrated DB at
    <repo_root>/data/portfolio.db, mirroring test_capacity_moments.py's fixture
    (``db.init_db()`` for the inline-managed baseline tables, then alembic
    ``0000_baseline`` -> head). ``run_coach_pings.main()`` calls
    ``synthesis.auto_reconcile.auto_reconcile``, which queries ``decisions``/
    ``tracked_companies`` UNCONDITIONALLY (no missing-table guard) — the
    lighter stamp-past-a-prior-head fixture used above leaves those
    init_db-owned tables absent and would raise ``OperationalError`` here."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_file = data_dir / "portfolio.db"
    saved = (dbmod.DB_PATH, dbmod.DATA_DIR, dbmod.FMP_DIR)
    dbmod.set_db_path(str(db_file))
    dbmod.init_db()
    cfg = _cfg(db_file)
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, "head")
    try:
        yield tmp_path, db_file
    finally:
        dbmod.DB_PATH, dbmod.DATA_DIR, dbmod.FMP_DIR = saved


def _seed_open_intent(
    db_path: Path, *, body: str = "far-OTM LEAP sleeve on the next washout"
) -> None:
    """An intent_followup moment fires for a standing 'intent' note open >14d —
    zero-LLM, so run_governor's send path is exercised with no mocking needed
    beyond Telegram itself."""
    old = (now_naive_utc() - timedelta(days=20)).isoformat()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO analyst_notes (ticker, kind, status, body, source, "
            "created_at, updated_at) VALUES (NULL, 'intent', 'open', ?, 'manual', ?, ?)",
            (body, old, old),
        )
        conn.commit()
    finally:
        conn.close()


def test_run_coach_pings_send_records_message_id(
    repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``execution.run_coach_pings.main()``'s ``_send`` closure must capture
    the sendMessage result's ``message_id`` and persist it via
    ``governor.record_ping_message_id`` so a later free-text reply can route
    back to this finding (``coach_reply.find_reply_ping``, direct mode)."""
    import sys

    from execution import run_coach_pings

    root, db_file = repo
    _seed_open_intent(db_file)

    monkeypatch.setattr("capture.token_store.load_token", lambda path=None: "tok")
    monkeypatch.setattr("capture.token_store.load_chat_id", lambda path=None: 1)
    monkeypatch.setattr(
        "capture.telegram.send_message",
        lambda token, chat_id, text, reply_markup=None, **k: {"message_id": 777},
    )

    monkeypatch.setattr(sys, "argv", ["run_coach_pings.py", "--repo-root", str(root)])
    rc = run_coach_pings.main()
    assert rc == 0

    conn = sqlite3.connect(str(db_file))
    try:
        row = conn.execute(
            "SELECT telegram_message_id, status FROM coach_pings WHERE class_ = 'intent_followup'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[1] == "sent"
    assert int(row[0]) == 777
