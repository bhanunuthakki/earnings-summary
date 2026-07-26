"""Phase-1 W1-8: the Telegram research dispatch (inline-button callbacks → the
action core) + the poller routing the callback branch."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from capture import research_notify, telegram
from research.proposals import ResearchTask, create_proposal, create_task, get_proposal, get_task
from synthesis.insights import InsightRow

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


def test_dispatch_legacy_spb_review_sends_private_inbox_link(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "EARNINGS_SUMMARY_PRIVATE_BASE_URL",
        "https://desktop.example.ts.net",
    )
    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok",
        _cb("spb:review"),
        db_path=db_path,
        send=spy.send,
        answer=spy.answer,
        edit=spy.edit,
    )
    assert status == "spb_review"
    assert spy.answers == [("cq", "Opening the private Inbox...")]
    assert spy.sends == [
        (5, "https://desktop.example.ts.net/mobile/inbox", None),
    ]


def test_dispatch_spb_dismiss_item_uses_shared_action_core(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[int] = []
    monkeypatch.setattr(
        "advisor.senior_partner_brief.dismiss_routed_moment",
        lambda ping_id, **kwargs: (called.append(ping_id) is None, None),
    )
    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok",
        _cb("spb:dismiss_item:42", message_id=77, message_text="Senior Partner Brief"),
        db_path=db_path,
        send=spy.send,
        answer=spy.answer,
        edit=spy.edit,
    )
    assert status == "spb_item_dismissed"
    assert called == [42]
    assert spy.answers == [("cq", "Dismissed.")]
    # A per-item dismissal must not strip the whole brief keyboard; the other
    # routed items and brief-level controls remain actionable.
    assert spy.edits == []


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


# --------------------------------------------------------------------------- #
# al:<verb>:<artifact_id> — the Incremental Dollar Recommendation card (P0.4b)
# --------------------------------------------------------------------------- #


def _insert_allocation_artifact(db_path: Path, *, content: dict[str, object]) -> int:
    """``db_path`` (this file's fixture) stamps straight to a post-0059
    revision, so migration 0035 (which creates ``llm_artifacts``) never
    actually ran — create the table here if it's missing, same hand-rolled
    pattern as tests/test_allocation_actions.py / test_recommendation_artifact.py."""
    import json
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR(16),
            scope VARCHAR(64) NOT NULL DEFAULT 'ticker',
            purpose VARCHAR(64) NOT NULL,
            fiscal_period VARCHAR(10),
            content_md TEXT,
            content_json TEXT,
            input_sha256 VARCHAR(64) NOT NULL,
            output_sha256 VARCHAR(64),
            model VARCHAR(64),
            prompt_version VARCHAR(32) NOT NULL DEFAULT 'v1',
            generated_at DATETIME NOT NULL,
            expires_at DATETIME,
            superseded_by_id INTEGER,
            dirty BOOLEAN NOT NULL DEFAULT 0,
            dirty_reason VARCHAR(128),
            source_doc_ids TEXT,
            parent_artifact_ids TEXT,
            llm_call_id INTEGER
        )
        """
    )
    cur = conn.execute(
        "INSERT INTO llm_artifacts (scope, purpose, content_json, input_sha256, generated_at) "
        "VALUES ('portfolio', 'incremental_dollar_recommendation', ?, 'sha', "
        "'2026-07-22T09:00:00')",
        (json.dumps(content),),
    )
    conn.commit()
    artifact_id = int(cur.lastrowid or 0)
    conn.close()
    return artifact_id


_ALLOC_PAYLOAD: dict[str, object] = {
    "as_of_date": "2026-07-22",
    "input_sha": "sha_al",
    "status": "deploy_partial",
    "preferred_plan": {
        "allocations": [
            {
                "ticker": "NU",
                "dollars": 6000.0,
                "pct_of_cash": 60.0,
                "resulting_weight_pct": 5.5,
                "zone": "ordinary",
            }
        ],
        "cash_retained_usd": 4000.0,
    },
    "best_alternative": None,
    "best_diversifier": None,
    "central_hypothesis": "NU has the best blended next-dollar score right now.",
    "personalization_why": "This deploys 60% of your new cash into the top-ranked name.",
    "supporting_evidence": [],
    "main_unknowns": ["how NU's next print reads on credit quality"],
    "disconfirming_evidence": ["a weak macro print could compress the multiple further"],
    "scenario_reasoning": "",
    "confidence_verbal": "moderate",
    "confidence_basis": "The main reason I could be wrong is a macro shock hitting credit names.",
    "followup_research": [],
    "frontier_plan_ids": ["balanced"],
    "source_refs": ["dcf"],
    "risk_snapshot_ref": None,
    "engine_version": "v1",
    "prompt_version": "v1",
    "selection_mode": "llm",
}


def test_dispatch_al_why_sends_uncertainty_and_disconfirmers(db_path: Path) -> None:
    artifact_id = _insert_allocation_artifact(db_path, content=_ALLOC_PAYLOAD)
    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok", _cb(f"al:why:{artifact_id}"), db_path=db_path, send=spy.send, answer=spy.answer
    )
    assert status == "al_why"
    assert spy.sends
    text = spy.sends[0][1]
    assert "credit quality" in text
    assert "macro print could compress" in text


def test_dispatch_al_why_no_artifact_is_acknowledged(db_path: Path) -> None:
    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok", _cb("al:why:999999"), db_path=db_path, send=spy.send, answer=spy.answer
    )
    assert status == "al_stale"
    assert spy.answers and spy.answers[0][1] == "No recommendation on file."
    assert not spy.sends


def test_dispatch_al_open_answers_with_deep_link(db_path: Path) -> None:
    artifact_id = _insert_allocation_artifact(db_path, content=_ALLOC_PAYLOAD)
    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok", _cb(f"al:open:{artifact_id}"), db_path=db_path, send=spy.send, answer=spy.answer
    )
    assert status == "al_open"
    assert spy.answers and "portfolio_allocation" in (spy.answers[0][1] or "")
    assert not spy.sends


def test_dispatch_al_dismiss_uses_the_same_action_core(db_path: Path) -> None:
    """Mirrors the web route's /adopt call — SAME action core
    (allocation.actions.act_on_recommendation), never a duplicate write path."""
    artifact_id = _insert_allocation_artifact(db_path, content=_ALLOC_PAYLOAD)
    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok",
        _cb(f"al:dismiss:{artifact_id}", message_id=9, message_text="card text"),
        db_path=db_path,
        send=spy.send,
        answer=spy.answer,
        edit=spy.edit,
    )
    assert status == "al_dismissed"
    assert spy.answers and spy.answers[0][1] == "Dismissed."
    assert len(spy.edits) == 1
    assert "- dismissed" in spy.edits[0][2]


def test_dispatch_al_unknown_verb_is_acknowledged(db_path: Path) -> None:
    artifact_id = _insert_allocation_artifact(db_path, content=_ALLOC_PAYLOAD)
    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok", _cb(f"al:bogus:{artifact_id}"), db_path=db_path, send=spy.send, answer=spy.answer
    )
    assert status is None
    assert spy.answers and spy.answers[0][1] == "Unrecognized action."


# --------------------------------------------------------------------------- #
# tr:revert:<insight_id> — undo an auto-adopted Tenet/stance (B4)
# --------------------------------------------------------------------------- #


def test_dispatch_tr_revert_stamps_card(db_path: Path) -> None:
    from synthesis.tenets import record_tenet, supersede_tenet

    old = record_tenet(body_md="Let winners run.", scope_key="exit-discipline", db_path=db_path)
    new = supersede_tenet(old.id, body_md="Trim on a double.", db_path=db_path)
    assert new is not None

    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok",
        _cb(f"tr:revert:{new.id}", message_id=4, message_text="Adopted: Trim on a double."),
        db_path=db_path,
        send=spy.send,
        answer=spy.answer,
        edit=spy.edit,
    )
    assert status == "tr_reverted"
    assert spy.answers and spy.answers[0][1] == "Reverted - prior belief restored."
    assert len(spy.edits) == 1
    assert "- reverted" in spy.edits[0][2]
    assert spy.edits[0][3] is None

    from synthesis.insights import get_insight

    restored = get_insight(old.id, db_path=db_path)
    assert restored is not None and restored.status == "current"


def test_dispatch_tr_revert_unknown_id_is_already_handled(db_path: Path) -> None:
    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok", _cb("tr:revert:999999"), db_path=db_path, send=spy.send, answer=spy.answer
    )
    assert status == "tr_stale"
    assert spy.answers and spy.answers[0][1] == "Already handled."


# --------------------------------------------------------------------------- #
# rx:<verb>:<task_id> — the weekly packet's "expiring research" line (B7)
# --------------------------------------------------------------------------- #


def test_dispatch_rx_run_respects_the_run_flag(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LEDGER_RESEARCH_RUN", raising=False)
    tid = create_task(note_id=None, claim="do margins hold?", ticker="NU", db_path=db_path)
    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok", _cb(f"rx:run:{tid}"), db_path=db_path, send=spy.send, answer=spy.answer
    )
    assert status == "run_disabled"
    assert not spy.sends
    task = get_task(tid, db_path=db_path)
    assert task is not None and task.status == "proposed"  # untouched


def test_dispatch_rx_run_enabled_runs_and_sends_proposal(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEDGER_RESEARCH_RUN", "1")
    tid = create_task(note_id=None, claim="do margins hold?", ticker="NU", db_path=db_path)
    pid = create_proposal(
        task_id=tid, kind="memo", ticker="NU", title="NU memo", body_md="body", db_path=db_path
    )
    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok",
        _cb(f"rx:run:{tid}"),
        db_path=db_path,
        send=spy.send,
        answer=spy.answer,
        run=lambda task_id, **kw: pid,
    )
    assert status == "rx_ran"
    assert spy.sends and "NU memo" in spy.sends[0][1]


def test_dispatch_rx_run_failure_is_reported(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEDGER_RESEARCH_RUN", "1")
    tid = create_task(note_id=None, claim="do margins hold?", ticker="NU", db_path=db_path)
    spy = _Spy()

    def _boom(task_id: int, **kw: object) -> int:
        raise RuntimeError("web fetch failed")

    status = research_notify.dispatch_callback(
        "tok", _cb(f"rx:run:{tid}"), db_path=db_path, send=spy.send, answer=spy.answer, run=_boom
    )
    assert status == "run_failed"


def test_dispatch_rx_session_sends_stored_prompt(db_path: Path) -> None:
    tid = create_task(
        note_id=None,
        claim="do margins hold?",
        ticker="NU",
        meta={"session_prompt": "# Research this wondering\n\nNU margins"},
        db_path=db_path,
    )
    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok", _cb(f"rx:session:{tid}"), db_path=db_path, send=spy.send, answer=spy.answer
    )
    assert status == "rx_session"
    assert spy.sends and "NU margins" in spy.sends[0][1]
    # session hand-off never changes task status — it can still be run/dropped.
    task = get_task(tid, db_path=db_path)
    assert task is not None and task.status == "proposed"


def test_dispatch_rx_session_no_prompt_on_file(db_path: Path) -> None:
    tid = create_task(note_id=None, claim="do margins hold?", ticker="NU", db_path=db_path)
    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok", _cb(f"rx:session:{tid}"), db_path=db_path, send=spy.send, answer=spy.answer
    )
    assert status == "rx_session"
    assert spy.sends and "no session prompt" in spy.sends[0][1].lower()


def test_dispatch_rx_drop_expires_immediately(db_path: Path) -> None:
    """The packet-ACKNOWLEDGED drop — expires a task right away, independent
    of the weekly sweep's second-unanswered-week rule."""
    tid = create_task(note_id=None, claim="do margins hold?", ticker="NU", db_path=db_path)
    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok", _cb(f"rx:drop:{tid}"), db_path=db_path, send=spy.send, answer=spy.answer
    )
    assert status == "rx_dropped"
    assert spy.answers and spy.answers[0][1] == "Dropped."
    task = get_task(tid, db_path=db_path)
    assert task is not None and task.status == "expired"


def test_dispatch_rx_stale_task_is_acknowledged(db_path: Path) -> None:
    tid = create_task(note_id=None, claim="do margins hold?", ticker="NU", db_path=db_path)
    from research.proposals import set_task_status

    set_task_status(tid, "drafted", db_path=db_path)
    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok", _cb(f"rx:drop:{tid}"), db_path=db_path, send=spy.send, answer=spy.answer
    )
    assert status == "rx_stale"
    assert spy.answers and spy.answers[0][1] == "Already handled."


def test_dispatch_rx_unknown_task_is_acknowledged(db_path: Path) -> None:
    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok", _cb("rx:drop:999999"), db_path=db_path, send=spy.send, answer=spy.answer
    )
    assert status == "rx_stale"


def test_dispatch_rx_unrecognized_verb(db_path: Path) -> None:
    tid = create_task(note_id=None, claim="do margins hold?", ticker="NU", db_path=db_path)
    spy = _Spy()
    status = research_notify.dispatch_callback(
        "tok", _cb(f"rx:bogus:{tid}"), db_path=db_path, send=spy.send, answer=spy.answer
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


# --------------------------------------------------------------------------- #
# synthesis.adoption_notify.announce_adoption (B4)
# --------------------------------------------------------------------------- #


def _write_telegram_config(repo_root: Path, *, token: str = "tok", chat_id: int = 42) -> None:
    secrets = repo_root / "data" / "secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    (secrets / "telegram_bot_token").write_text(token, encoding="utf-8")
    capture_dir = repo_root / "data" / "capture"
    capture_dir.mkdir(parents=True, exist_ok=True)
    (capture_dir / "telegram_chat_id.json").write_text(
        f'{{"chat_id": {chat_id}}}', encoding="utf-8"
    )


def _insight_row(**overrides: object) -> InsightRow:
    base = InsightRow(
        id=5,
        scope_key="tenet:exit-discipline",
        kind="tenet",
        body_md="Let a working thesis run.",
        source_note_ids=[1],
        meta={},
        as_of="2026-07-20T00:00:00",
        watermark_id=1,
        status="current",
        esi=None,
        provenance="session_distill",
    )
    return dataclasses.replace(base, **overrides)


def test_announce_adoption_sends_with_revert_keyboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from synthesis import adoption_notify

    _write_telegram_config(tmp_path)
    sent: list[tuple[str, int, str, object]] = []

    def _fake_send(token: str, chat_id: int, text: str, *, reply_markup: object = None) -> object:
        sent.append((token, chat_id, text, reply_markup))
        return {}

    monkeypatch.setattr(telegram, "send_message", _fake_send)
    row = _insight_row()
    adoption_notify.announce_adoption(
        row, kind="tenet", db_path=tmp_path / "data" / "portfolio.db", repo_root=tmp_path
    )
    assert len(sent) == 1
    token, chat_id, text, reply_markup = sent[0]
    assert token == "tok"
    assert chat_id == 42
    assert "Adopted (from your session)" in text
    assert "Let a working thesis run." in text
    assert "Revert" in text or "Tap Revert" in text
    assert "tr:revert:5" in str(reply_markup)


def test_announce_adoption_skips_silently_without_token_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from synthesis import adoption_notify

    sent: list[object] = []

    def _fake_send(*a: object, **k: object) -> object:
        sent.append((a, k))
        return {}

    monkeypatch.setattr(telegram, "send_message", _fake_send)
    row = _insight_row()
    # No data/secrets/telegram_bot_token, no data/capture/telegram_chat_id.json
    adoption_notify.announce_adoption(
        row, kind="tenet", db_path=tmp_path / "data" / "portfolio.db", repo_root=tmp_path
    )
    assert sent == []


def test_announce_adoption_never_raises_on_bad_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from synthesis import adoption_notify

    _write_telegram_config(tmp_path)
    sent: list[object] = []

    def _fake_send(*a: object, **k: object) -> object:
        sent.append((a, k))
        return {}

    monkeypatch.setattr(telegram, "send_message", _fake_send)
    row = _insight_row()
    # kind="theme" is not a revertible/announceable kind — must degrade quietly.
    adoption_notify.announce_adoption(
        row, kind="theme", db_path=tmp_path / "data" / "portfolio.db", repo_root=tmp_path
    )
    assert sent == []
