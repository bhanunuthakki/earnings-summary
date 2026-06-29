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

    def send(self, token: str, chat_id: int, text: str, *, reply_markup: object = None) -> object:
        self.sends.append((chat_id, text, reply_markup))
        return {}

    def answer(self, token: str, cqid: str, *, text: str | None = None) -> object:
        self.answers.append((cqid, text))
        return {}


def _cb(data: str) -> telegram.Update:
    return telegram.Update(
        update_id=1, kind="callback", chat_id=5, callback_data=data, callback_query_id="cq"
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
