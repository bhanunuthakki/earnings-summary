"""Phase-1 W1-8: the Telegram research dispatch (inline-button callbacks → the
action core) + the poller routing the callback branch."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from capture import research_notify, telegram
from research.proposals import ResearchTask, create_proposal, get_proposal

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


class _Spy:
    def __init__(self) -> None:
        self.sends: list[tuple[int, str, object]] = []
        self.answers: list[tuple[str, str | None]] = []
        self.edits: list[tuple[int, int, str, object]] = []

    def send(self, token: str, chat_id: int, text: str, *, reply_markup: object = None) -> object:
        self.sends.append((chat_id, text, reply_markup))
        return {}

    def answer(self, token: str, cqid: str, *, text: str | None = None) -> object:
        self.answers.append((cqid, text))
        return {}

    def edit(
        self, token: str, chat_id: int, message_id: int, text: str, *, reply_markup: object = None
    ) -> object:
        self.edits.append((chat_id, message_id, text, reply_markup))
        return {}


def _cb(
    data: str, *, message_id: int | None = None, message_text: str | None = None
) -> telegram.Update:
    return telegram.Update(
        update_id=1,
        kind="callback",
        chat_id=5,
        callback_data=data,
        callback_query_id="cq",
        message_id=message_id,
        message_text=message_text,
    )


def test_parse_callback_valid_and_malformed() -> None:
    assert research_notify.parse_callback("rp:approve:42") == ("rp", "approve", 42)
    assert research_notify.parse_callback("rt:run:7") == ("rt", "run", 7)
    for bad in (None, "", "x", "a:b", "rp:approve:x", "a:b:c:d"):
        assert research_notify.parse_callback(bad) is None


def test_keyboards_carry_the_callback_data() -> None:
    kb = research_notify.proposal_keyboard(9)
    flat = str(kb)
    for verb in ("rp:approve:9", "rp:further:9", "rp:steer:9", "rp:reject:9"):
        assert verb in flat
    assert "rt:run:3" in str(research_notify.task_keyboard(3))


def test_dispatch_approve_flips_status(db_path: Path) -> None:
    pid = create_proposal(
        task_id=None, kind="memo", ticker="NU", title="t", body_md="b", db_path=db_path
    )
    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok", _cb(f"rp:approve:{pid}"), db_path=db_path, send=spy.send, answer=spy.answer
    )
    assert status == "approved"
    prop = get_proposal(pid, db_path=db_path)
    assert prop is not None and prop.status == "approved"
    assert spy.answers and spy.answers[0][1] is not None and "approved" in spy.answers[0][1]


# --------------------------------------------------------------------------- #
# editMessage state stamps (PR10) — a handled card gets stamped + de-fanged
# --------------------------------------------------------------------------- #


def test_dispatch_approve_edits_original_card_and_strips_keyboard(db_path: Path) -> None:
    pid = create_proposal(
        task_id=None, kind="memo", ticker="NU", title="t", body_md="b", db_path=db_path
    )
    spy = _Spy()
    update = _cb(f"rp:approve:{pid}", message_id=77, message_text="NU - t\n\nexcerpt")
    status = research_notify.dispatch_callback(
        "tok", update, db_path=db_path, send=spy.send, answer=spy.answer, edit=spy.edit
    )
    assert status == "approved"
    assert len(spy.edits) == 1
    chat_id, message_id, text, reply_markup = spy.edits[0]
    assert (chat_id, message_id) == (5, 77)
    assert text.startswith("NU - t\n\nexcerpt\n- approved ")
    assert reply_markup is None  # the keyboard is stripped


def test_dispatch_no_edit_without_message_id(db_path: Path) -> None:
    """No originating message id (e.g. an older/synthetic update) -> the edit
    is skipped silently; the action itself still succeeds."""
    pid = create_proposal(
        task_id=None, kind="memo", ticker="NU", title="t", body_md="b", db_path=db_path
    )
    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok",
        _cb(f"rp:approve:{pid}"),  # no message_id
        db_path=db_path,
        send=spy.send,
        answer=spy.answer,
        edit=spy.edit,
    )
    assert status == "approved"
    assert spy.edits == []


def test_dispatch_stale_dismiss_edits_nothing(db_path: Path) -> None:
    """'Already handled' re-presses (a ping not in sent/digest anymore) edit
    nothing — only a genuinely-recorded dismissal stamps the card."""
    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok",
        _cb("cp:dismiss:999999", message_id=42, message_text="a ping"),
        db_path=db_path,
        send=spy.send,
        answer=spy.answer,
        edit=spy.edit,
    )
    assert status == "cp_stale"
    assert spy.answers and spy.answers[0][1] == "Already handled."
    assert spy.edits == []


def test_dispatch_failed_run_edits_nothing(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGER_RESEARCH_RUN", "1")
    spy = _Spy()

    def _boom(task_id: int, **kw: object) -> int:
        raise RuntimeError("web fetch failed")

    status = research_notify.dispatch_callback(
        "tok",
        _cb("rt:run:9", message_id=1, message_text="a wondering"),
        db_path=db_path,
        send=spy.send,
        answer=spy.answer,
        edit=spy.edit,
        run=_boom,
    )
    assert status == "run_failed"
    assert spy.edits == []


def test_dispatch_edit_failure_never_breaks_the_action(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A TelegramError from the edit call is suppressed — the action's own
    status/answer must be unaffected."""
    pid = create_proposal(
        task_id=None, kind="memo", ticker="NU", title="t", body_md="b", db_path=db_path
    )
    spy = _Spy()

    def _broken_edit(*a: object, **k: object) -> object:
        raise telegram.TelegramError("boom")

    status = research_notify.dispatch_callback(
        "tok",
        _cb(f"rp:approve:{pid}", message_id=5, message_text="NU - t"),
        db_path=db_path,
        send=spy.send,
        answer=spy.answer,
        edit=_broken_edit,
    )
    assert status == "approved"
    prop = get_proposal(pid, db_path=db_path)
    assert prop is not None and prop.status == "approved"


def test_dispatch_cp_dismiss_stamps_and_strips(db_path: Path) -> None:
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO coach_pings (class_, key, ticker, body, status, source_ref, "
            "created_at, updated_at) VALUES ('intent_followup','intent:1:0',NULL,'x','sent',"
            "'note:1','2026-07-10','2026-07-10')"
        )
        conn.commit()
        ping_id = int(conn.execute("SELECT id FROM coach_pings").fetchone()[0])
    finally:
        conn.close()

    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok",
        _cb(f"cp:dismiss:{ping_id}", message_id=3, message_text="x"),
        db_path=db_path,
        send=spy.send,
        answer=spy.answer,
        edit=spy.edit,
    )
    assert status == "cp_dismissed"
    assert len(spy.edits) == 1
    assert "- dismissed" in spy.edits[0][2]
    assert spy.edits[0][3] is None


def test_dispatch_run_sends_proposal_card(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGER_RESEARCH_RUN", "1")
    pid = create_proposal(
        task_id=None, kind="memo", ticker="NU", title="NU memo", body_md="body", db_path=db_path
    )
    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok",
        _cb("rt:run:9"),
        db_path=db_path,
        send=spy.send,
        answer=spy.answer,
        run=lambda task_id, **kw: pid,
    )
    assert status == "ran"
    assert spy.sends and "NU memo" in spy.sends[0][1]
    assert spy.sends[0][2] is not None  # the 4-action keyboard rides the card


def test_dispatch_run_disabled_never_runs(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEDGER_RESEARCH_RUN", raising=False)
    spy = _Spy()
    ran = {"n": 0}

    def _run(task_id: int, **kw: object) -> int:
        ran["n"] += 1
        return 1

    status = research_notify.dispatch_callback(
        "tok", _cb("rt:run:9"), db_path=db_path, send=spy.send, answer=spy.answer, run=_run
    )
    assert status == "run_disabled"
    assert ran["n"] == 0
    assert not spy.sends


def test_dispatch_malformed_is_acknowledged(db_path: Path) -> None:
    spy = _Spy()
    assert (
        research_notify.dispatch_callback(
            "tok", _cb("garbage"), db_path=db_path, send=spy.send, answer=spy.answer
        )
        is None
    )
    assert spy.answers and spy.answers[0][1] == "Unrecognized action."


def test_notify_new_task_button_only_when_run_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    task = ResearchTask(
        id=7, note_id=None, claim="do margins hold?", ticker="NU", status="proposed"
    )
    spy = _Spy()
    monkeypatch.setenv("LEDGER_RESEARCH_RUN", "1")
    research_notify.notify_new_task("tok", 5, task, send=spy.send)
    assert spy.sends[0][2] is not None  # Research button offered

    monkeypatch.delenv("LEDGER_RESEARCH_RUN", raising=False)
    spy2 = _Spy()
    research_notify.notify_new_task("tok", 5, task, send=spy2.send)
    assert spy2.sends[0][2] is None  # detection-only: no button


def test_ticker_keyboard_one_button_per_candidate() -> None:
    kb = research_notify.ticker_keyboard(12, ["nu", "MELI", "NU", ""])
    flat = str(kb)
    assert "st:NU:12" in flat and "st:MELI:12" in flat
    assert flat.count("st:NU:12") == 1  # deduped, case-folded
    assert research_notify.ticker_keyboard(12, []) is None
    assert research_notify.ticker_keyboard(12, ["", "  "]) is None


def test_dispatch_st_attributes_needs_ticker_musing(db_path: Path) -> None:
    """The st:<ticker>:<note_id> branch — the same write-once set_ticker action
    the Ledger chips call, fired from the thread's candidate buttons."""
    from user_state import notes

    note = notes.create_note(
        ticker=None,
        kind="musing",
        body="NU vs MELI - add to which?",
        source="capture",
        context={"needs_ticker": True, "ticker_candidates": ["MELI", "NU"]},
        db_path=db_path,
    )
    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok",
        _cb(f"st:NU:{note.id}", message_id=9, message_text="Captured. Which ticker?"),
        db_path=db_path,
        send=spy.send,
        answer=spy.answer,
        edit=spy.edit,
    )
    assert status == "st_set"
    row = notes.get_note(note.id, db_path=db_path)
    assert row is not None and row.ticker == "NU"
    ctx = row.context or {}
    assert "needs_ticker" not in ctx and "ticker_candidates" not in ctx
    assert spy.answers and spy.answers[0][1] == "Attributed to NU."
    assert len(spy.edits) == 1
    assert "- attributed NU" in spy.edits[0][2]
    assert spy.edits[0][3] is None  # buttons stripped — no re-press

    # A stray second tap can never silently reassign (write-once)
    spy2 = _Spy()
    status2 = research_notify.dispatch_callback(
        "tok",
        _cb(f"st:MELI:{note.id}", message_id=9, message_text="x"),
        db_path=db_path,
        send=spy2.send,
        answer=spy2.answer,
        edit=spy2.edit,
    )
    assert status2 == "st_stale"
    assert spy2.answers and spy2.answers[0][1] == "Already attributed."
    assert spy2.edits == []
    row2 = notes.get_note(note.id, db_path=db_path)
    assert row2 is not None and row2.ticker == "NU"


def test_dispatch_st_unknown_note_is_acknowledged(db_path: Path) -> None:
    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok", _cb("st:NU:999999"), db_path=db_path, send=spy.send, answer=spy.answer
    )
    assert status is None
    assert spy.answers and spy.answers[0][1] == "Unrecognized action."


def test_dispatch_cp_review_sends_pre_analysis(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Answer button on a falsifier_breach ping: answers the callback and
    sends the same instant pre-analysis reply /review would, without the
    owner retyping the command."""
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO coach_pings (class_, key, ticker, body, status, source_ref, "
            "created_at, updated_at) VALUES ('falsifier_breach','alert:1','NU','x','sent',"
            "'decision:1','2026-07-10','2026-07-10')"
        )
        conn.commit()
        ping_id = int(conn.execute("SELECT id FROM coach_pings").fetchone()[0])
    finally:
        conn.close()

    seen: list[tuple[object, str, bool]] = []

    def _fake_reply_text(repo_root: object, text: str, *, plain: bool = False) -> str:
        seen.append((repo_root, text, plain))
        return "NU - position review (deterministic read)"

    monkeypatch.setattr(
        "advisor.position_review.review_reply_text", _fake_reply_text, raising=False
    )
    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok", _cb(f"cp:review:{ping_id}"), db_path=db_path, send=spy.send, answer=spy.answer
    )
    assert status == "cp_reviewed"
    assert spy.answers and spy.answers[0][1] == "Reviewing NU..."
    assert spy.sends and "NU" in spy.sends[0][1]
    assert seen and seen[0][1] == "/review NU"
    assert seen[0][2] is True  # the Telegram cp:review reply uses the plain renderer


def test_dispatch_cp_review_stamps_with_ticker(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO coach_pings (class_, key, ticker, body, status, source_ref, "
            "created_at, updated_at) VALUES ('falsifier_breach','alert:2','NU','x','sent',"
            "'decision:2','2026-07-10','2026-07-10')"
        )
        conn.commit()
        ping_id = int(conn.execute("SELECT id FROM coach_pings").fetchone()[0])
    finally:
        conn.close()

    monkeypatch.setattr(
        "advisor.position_review.review_reply_text",
        lambda repo_root, text, **kw: "review body",
        raising=False,
    )
    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok",
        _cb(f"cp:review:{ping_id}", message_id=8, message_text="falsifier tripped for NU"),
        db_path=db_path,
        send=spy.send,
        answer=spy.answer,
        edit=spy.edit,
    )
    assert status == "cp_reviewed"
    assert len(spy.edits) == 1
    assert "- reviewed NU" in spy.edits[0][2]
    assert spy.edits[0][3] is None


def test_dispatch_cp_review_no_ticker(db_path: Path) -> None:
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO coach_pings (class_, key, ticker, body, status, source_ref, "
            "created_at, updated_at) VALUES ('intent_followup','intent:1:0',NULL,'x','sent',"
            "'note:1','2026-07-10','2026-07-10')"
        )
        conn.commit()
        ping_id = int(conn.execute("SELECT id FROM coach_pings").fetchone()[0])
    finally:
        conn.close()

    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok", _cb(f"cp:review:{ping_id}"), db_path=db_path, send=spy.send, answer=spy.answer
    )
    assert status == "cp_review_no_ticker"
    assert spy.answers and spy.answers[0][1] == "No ticker on this ping."
    assert not spy.sends


def test_dispatch_cp_review_unknown_ping(db_path: Path) -> None:
    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok", _cb("cp:review:999999"), db_path=db_path, send=spy.send, answer=spy.answer
    )
    assert status is None
    assert spy.answers and spy.answers[0][1] == "Unrecognized action."


def test_poller_routes_callback_to_dispatch(
    tmp_path: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from capture import poller

    seen: list[str | None] = []
    monkeypatch.setattr(poller.telegram, "get_updates", lambda *a, **k: [_cb("rp:approve:1")])
    monkeypatch.setattr(
        poller.research_notify,
        "dispatch_callback",
        lambda token, update, **kw: seen.append(update.callback_data),
    )
    counts = poller.poll_once(
        "tok",
        db_path=db_path,
        offset_path=tmp_path / "off.json",
        audio_dir=tmp_path / "audio",
        confirm=False,
    )
    assert counts.get("callback") == 1
    assert seen == ["rp:approve:1"]
