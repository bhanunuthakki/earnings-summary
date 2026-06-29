"""Phase-1 W1-3: the research_tasks store + the fire-and-forget detection tap."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from capture import ingest
from capture.matcher import build_roster_index
from research import proposals
from user_state import notes

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"
ROSTER = build_roster_index(symbols=["NU"], phrases={"nubank": "NU"})


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


def _land(db_path: Path, text: str) -> int:
    return (
        ingest.ingest_capture(channel="tray", text=text, roster=ROSTER, db_path=db_path).note_id
        or 0
    )


def _yes(text: str) -> dict[str, object]:
    return {"is_wondering": True, "claim": "do NU margins hold?", "ticker": "NU"}


def test_create_list_and_status(db_path: Path) -> None:
    tid = proposals.create_task(note_id=1, claim="do margins hold?", ticker="NU", db_path=db_path)
    assert tid > 0
    tasks = proposals.list_tasks(status="proposed", db_path=db_path)
    assert len(tasks) == 1
    assert tasks[0].claim == "do margins hold?"
    assert tasks[0].ticker == "NU"
    proposals.set_task_status(tid, "running", db_path=db_path)
    got = proposals.get_task(tid, db_path=db_path)
    assert got is not None and got.status == "running"


def test_tap_creates_task_for_wondering(db_path: Path) -> None:
    note_id = _land(db_path, "do NU's margins still hold up here?")
    tid = proposals.detect_and_create_task(note_id, db_path=db_path, call=_yes)
    assert tid is not None
    task = proposals.get_task(tid, db_path=db_path)
    assert task is not None
    assert task.status == "proposed"
    assert task.note_id == note_id
    assert task.ticker == "NU"


def test_tap_skips_flat_observation_zero_llm(db_path: Path) -> None:
    note_id = _land(db_path, "NU's NPL formation ticked up to 4.5% this quarter")
    calls = {"n": 0}

    def spy(text: str) -> dict[str, object]:
        calls["n"] += 1
        return _yes(text)

    assert proposals.detect_and_create_task(note_id, db_path=db_path, call=spy) is None
    assert calls["n"] == 0  # regex pre-gate short-circuits — no LLM, no task
    assert proposals.list_tasks(db_path=db_path) == []


def test_tap_skips_non_musing(db_path: Path) -> None:
    row = notes.create_note(
        ticker="NU",
        kind="decision",
        body="research this: do margins hold?",
        source="manual",
        db_path=db_path,
    )
    assert proposals.detect_and_create_task(row.id, db_path=db_path, call=_yes) is None


def test_tap_enabled_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEDGER_RESEARCH_TAP", raising=False)
    assert proposals.tap_enabled() is True
    monkeypatch.setenv("LEDGER_RESEARCH_TAP", "0")
    assert proposals.tap_enabled() is False
