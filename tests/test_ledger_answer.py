"""The Ledger answer core (overhaul) — a question-shaped capture gets answered.

Covers the deterministic question heuristic, the LEDGER_ANSWER kill switch, the
answer tap that stores the engine's answer on the note WITHOUT a live LLM call
(``respond_turn`` is faked), and the feed card rendering the stored answer.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from capture import ingest
from capture.matcher import build_roster_index
from onmymind import respond
from pipeline.ledger_panel import render_onmymind_list
from user_state import notes

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"
ROSTER = build_roster_index(symbols=["MELI", "NU"], phrases={"mercadolibre": "MELI"})


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


@pytest.fixture(autouse=True)
def _answer_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEDGER_ANSWER", raising=False)


def _musing(db_path: Path, text: str) -> int:
    return (
        ingest.ingest_capture(channel="tray", text=text, roster=ROSTER, db_path=db_path).note_id
        or 0
    )


def _fake_engine(answer: str) -> None:
    """Return a stub for ``respond.respond_turn`` that yields one final answer —
    the real ``fold_events`` still runs, so the tap's plumbing is exercised
    without a live model call."""

    def _stub(*_a: object, **_k: object) -> Iterator[dict[str, object]]:
        yield {"type": "final", "text": answer, "route": "narrative"}

    return _stub  # type: ignore[return-value]


# --- the question heuristic --------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("What's my cost basis on MELI?", True),
        ("Why can't you tell me my cost basis? The project has this detail", True),
        ("Should I trim NU here", True),
        ("Explain the MELI thesis", True),
        ("Tell me my cost basis", True),
        ("We should think about trim thresholds for MELI given concentration", False),
        ("CPNG was a catalyst-test failure I entered anyway", False),
        ("This is a note to self about NU margins", False),
        ("", False),
    ],
)
def test_is_answerable_capture(text: str, expected: bool) -> None:
    assert respond.is_answerable_capture(text) is expected


# --- the kill switch ---------------------------------------------------------


def test_answer_enabled_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEDGER_ANSWER", raising=False)
    assert respond.answer_enabled() is True
    monkeypatch.setenv("LEDGER_ANSWER", "0")
    assert respond.answer_enabled() is False


def test_disabled_stores_nothing(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGER_ANSWER", "0")
    nid = _musing(db_path, "What's my cost basis on MELI?")
    assert respond.answer_capture(nid, repo_root=db_path.parent, db_path=db_path) is None
    note = notes.get_note(nid, db_path=db_path)
    assert note is not None and "ledger_answer" not in (note.context or {})


# --- the answer tap ----------------------------------------------------------


def test_answers_and_stores(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(respond, "respond_turn", _fake_engine("Your MELI cost basis is $1,240."))
    nid = _musing(db_path, "What's my cost basis on MELI?")
    out = respond.answer_capture(nid, repo_root=db_path.parent, db_path=db_path)
    assert out == "Your MELI cost basis is $1,240."
    note = notes.get_note(nid, db_path=db_path)
    assert note is not None
    stored = (note.context or {}).get("ledger_answer")
    assert isinstance(stored, dict) and stored.get("text") == "Your MELI cost basis is $1,240."


def test_ambiguous_capture_not_answered(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A needs_ticker capture ("NU vs MELI — add to which?") gets ticker-
    disambiguation, not a portfolio answer — the engine is never invoked."""
    called = {"n": 0}

    def _boom(*_a: object, **_k: object) -> Iterator[dict[str, object]]:
        called["n"] += 1
        yield {"type": "final", "text": "nope", "route": "narrative"}

    monkeypatch.setattr(respond, "respond_turn", _boom)
    nid = _musing(db_path, "NU vs MELI - add to which?")
    note = notes.get_note(nid, db_path=db_path)
    assert note is not None and (note.context or {}).get("needs_ticker")  # precondition
    assert respond.answer_capture(nid, repo_root=db_path.parent, db_path=db_path) is None
    assert called["n"] == 0


def test_non_question_not_answered(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"n": 0}

    def _boom(*_a: object, **_k: object) -> Iterator[dict[str, object]]:
        called["n"] += 1
        yield {"type": "final", "text": "should not run", "route": "narrative"}

    monkeypatch.setattr(respond, "respond_turn", _boom)
    nid = _musing(db_path, "We should think about trim thresholds for MELI given concentration")
    assert respond.answer_capture(nid, repo_root=db_path.parent, db_path=db_path) is None
    assert called["n"] == 0  # the engine is never invoked for a non-question


def test_engine_failure_never_raises(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_a: object, **_k: object) -> Iterator[dict[str, object]]:
        raise RuntimeError("model down")
        yield  # pragma: no cover

    monkeypatch.setattr(respond, "respond_turn", _raise)
    nid = _musing(db_path, "What's my cost basis on MELI?")
    # fire-and-forget: a broken engine must degrade to None, never propagate.
    assert respond.answer_capture(nid, repo_root=db_path.parent, db_path=db_path) is None


# --- the feed renders the stored answer --------------------------------------


def test_feed_card_renders_answer(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGER_ONMYMIND", "1")
    monkeypatch.setattr(respond, "respond_turn", _fake_engine("Your MELI cost basis is $1,240."))
    nid = _musing(db_path, "What's my cost basis on MELI?")
    respond.answer_capture(nid, repo_root=db_path.parent, db_path=db_path)
    html = render_onmymind_list(db_path)
    assert "om-answer" in html
    assert "Your MELI cost basis is $1,240." in html


# --- Phase B: the universal reply box ----------------------------------------


def test_question_card_gets_reply_box_and_dismiss(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every card carries ONE interaction — the reply box (routed by the
    ledger_reply_intent classifier) — plus Dismiss. The per-type verb menus
    (Research it / Save / Worldview / Ask more) are gone; the placeholder is
    the only per-type contextualization left."""
    monkeypatch.setenv("LEDGER_ONMYMIND", "1")
    monkeypatch.setattr(respond, "respond_turn", _fake_engine("Your MELI cost basis is $1,240."))
    nid = _musing(db_path, "What's my cost basis on MELI?")
    respond.answer_capture(nid, repo_root=db_path.parent, db_path=db_path)
    html = render_onmymind_list(db_path)
    assert "om-reply-input" in html and "data-om-reply" in html
    assert "Ask a follow-up" in html  # the question-card placeholder
    assert 'data-om-verb="dismiss"' in html
    assert 'data-om-verb="incorporate"' not in html
    assert 'data-om-verb="save"' not in html
    assert 'data-om-verb="worldview"' not in html
    assert "data-om-ask" not in html  # the old chat-opener button is gone


def test_reading_card_gets_reply_box(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGER_ONMYMIND", "1")
    ingest.ingest_reading(
        channel="tray", url="https://example.com/x", external_ref="tray:r1", db_path=db_path
    )
    html = render_onmymind_list(db_path)
    assert "om-reply-input" in html and "data-om-reply" in html
    assert "research it, save it, or ask about it" in html  # the reading placeholder
    assert 'data-om-verb="incorporate"' not in html


def test_plain_musing_gets_reply_box(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGER_ONMYMIND", "1")
    monkeypatch.delenv("LEDGER_WORLDVIEW", raising=False)
    _musing(db_path, "MELI looks cheap here")
    html = render_onmymind_list(db_path)
    assert "om-reply-input" in html and "data-om-reply" in html
    assert 'data-om-verb="dismiss"' in html
    assert 'data-om-verb="save"' not in html
