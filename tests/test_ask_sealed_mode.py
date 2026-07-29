# pyright: reportPrivateUsage=false
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

import ask.engine as engine
import ask.store as store
from ask.context import ContextPack
from ask.engine import AskTurn


def _pack() -> ContextPack:
    return ContextPack(
        scope="portfolio",
        default_tickers=["ACME"],
        system_context="context",
    )


def test_shadow_preserves_the_exact_legacy_event_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: list[object] = []
    expected: list[dict[str, object]] = [
        {"type": "delta", "text": "legacy"},
        {"type": "final", "text": "legacy", "route": "narrative"},
    ]

    def fake_shadow(
        text: str,
        turn: AskTurn,
        pack: ContextPack,
        *,
        db_path: Path,
    ) -> None:
        observed.extend((text, turn, pack, db_path))

    def fake_legacy(
        text: str,
        turn: AskTurn,
        pack: ContextPack,
        *,
        repo_root: Path,
        db_path: Path,
        emit_stage: bool,
    ) -> Iterator[dict[str, object]]:
        observed.extend((text, turn, pack, repo_root, db_path, emit_stage))
        yield from expected

    monkeypatch.setattr(engine, "_shadow_retrieval", fake_shadow)
    monkeypatch.setattr(engine, "_narrative_events", fake_legacy)
    turn = AskTurn(text="question")
    pack = _pack()
    actual = list(
        engine._sealed_or_shadow_narrative_events(
            "question",
            turn,
            pack,
            repo_root=tmp_path,
            db_path=tmp_path / "db.sqlite",
            mode="shadow",
        )
    )
    assert actual == expected
    assert observed[0] == "question"
    assert observed[4] == "question"


def test_sealed_mode_fails_before_any_llm_without_authoritative_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("LLM must not run before sealed readiness")

    monkeypatch.setattr(engine, "call_llm", forbidden)
    events = list(
        engine._sealed_or_shadow_narrative_events(
            "question",
            AskTurn(text="question"),
            _pack(),
            repo_root=tmp_path,
            db_path=tmp_path / "missing.sqlite",
            mode="sealed",
        )
    )
    assert events == [
        {
            "type": "error",
            "error": "sealed Ask requires an authoritative portfolio session",
        }
    ]


def test_invalid_mode_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASK_RETRIEVAL_MODE", "permissive")
    with pytest.raises(ValueError, match="legacy, shadow, or sealed"):
        engine.ask_retrieval_mode()


def test_sealed_policy_gates_before_router_catalog_or_compiler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("sealed policy must gate before routing or LLM work")

    monkeypatch.setattr(engine, "route_turn", forbidden)
    monkeypatch.setattr(engine, "metric_catalog", forbidden)
    monkeypatch.setattr(engine, "call_llm", forbidden)
    events = list(
        engine.respond_turn(
            AskTurn(text="/view revenue", session_id="session-1"),
            _pack(),
            db_path=tmp_path / "missing.sqlite",
            repo_root=tmp_path,
            retrieval_mode="sealed",
        )
    )
    assert events == [
        {
            "type": "error",
            "error": "sealed Ask accepts narrative questions, not commands or view directives",
        }
    ]


def test_final_assistant_append_is_exact_tail_compare_and_swap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ask.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE ask_sessions (
          id TEXT PRIMARY KEY, updated_at TEXT NOT NULL
        );
        CREATE TABLE ask_turns (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL,
          citations_json TEXT, model TEXT, created_at TEXT NOT NULL
        );
        INSERT INTO ask_sessions VALUES ('session-1','2026-01-01');
        INSERT INTO ask_turns(session_id,role,text,created_at)
          VALUES ('session-1','user','first','2026-01-01');
        INSERT INTO ask_turns(session_id,role,text,created_at)
          VALUES ('session-1','user','concurrent','2026-01-02');
        """
    )
    conn.commit()
    conn.close()

    def open_test(path: Path) -> sqlite3.Connection:
        opened = sqlite3.connect(path)
        opened.row_factory = sqlite3.Row
        return opened

    monkeypatch.setattr(store, "_open", open_test)
    with pytest.raises(ValueError, match="session advanced"):
        store.append_assistant_turn_if_user_tail(
            session_id="session-1",
            user_turn_id=1,
            user_text="first",
            text="stale answer",
            db_path=db_path,
        )
    assert [item.text for item in store.load_turns("session-1", db_path=db_path)] == [
        "first",
        "concurrent",
    ]


def test_claim_gate_rejects_omitted_clause_and_injected_citation_number() -> None:
    answer = "Revenue grew [1]. Margin fell [2]."
    first = engine._AuditClaim(
        char_start=0,
        char_end=17,
        quote="Revenue grew [1].",
        cites=(1,),
        supported=True,
    )
    with pytest.raises(ValueError, match="every substantive clause"):
        engine._validate_claim_audit_output(
            answer,
            {1, 2},
            engine._ClaimAuditOutput(claims=(first,)),
        )
    injected = engine._AuditClaim(
        char_start=0,
        char_end=len("Answer cites [999]."),
        quote="Answer cites [999].",
        cites=(999,),
        supported=True,
    )
    with pytest.raises(ValueError, match="outside the sealed prompt"):
        engine._validate_claim_audit_output(
            "Answer cites [999].",
            {1},
            engine._ClaimAuditOutput(claims=(injected,)),
        )


def test_only_exact_fail_closed_no_answer_is_claim_exempt() -> None:
    exact = "I don't have enough sealed evidence to answer that."
    engine._validate_claim_audit_output(
        exact,
        set(),
        engine._ClaimAuditOutput(claims=()),
    )
    with pytest.raises(ValueError, match="every substantive clause"):
        engine._validate_claim_audit_output(
            exact + " Probably.",
            set(),
            engine._ClaimAuditOutput(claims=()),
        )
