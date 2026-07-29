# pyright: reportPrivateUsage=false
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import ask.engine as engine
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
