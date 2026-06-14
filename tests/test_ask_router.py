"""Pack router (src/ask/router.py, S4): structural pre-gates, the
budget-skip path, closed-enum validation of the classifier's output, and
the fail-closed error contract. The LLM transport is monkeypatched in every
test — the conftest blocker guarantees nothing here can spend."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

import ask.router as router
from ask.packs import PACK_KEYS
from ask.router import route_packs


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE tracked_companies (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ticker TEXT NOT NULL, name TEXT NOT NULL, list_type TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type) VALUES ('NU', 'Nu', 'portfolio')"
    )
    conn.commit()
    conn.close()
    return path


def _classifier(monkeypatch: pytest.MonkeyPatch, payload: object) -> list[str]:
    """Install a fake classifier returning ``payload``; records prompts."""
    prompts: list[str] = []

    def _fake(prompt: str, **_k: object) -> object:
        prompts.append(prompt)
        return payload

    monkeypatch.setattr(router, "call_llm_structured", _fake)
    return prompts


def test_ok_route_returns_canonical_order(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _classifier(monkeypatch, {"packs": ["dcf", "holdings", "weather", "dcf"]})
    route = route_packs("should I trim MELI to add TSM?", db_path=db)
    assert route.status == "ok"
    # Canonical PACKS order, unknown key dropped, duplicate collapsed.
    assert route.packs == ("holdings", "dcf")


def test_empty_selection_is_a_valid_answer(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _classifier(monkeypatch, {"packs": []})
    route = route_packs("what did management say about churn?", db_path=db)
    assert route.status == "ok"
    assert route.packs == ()


def test_prompt_carries_the_full_catalog(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prompts = _classifier(monkeypatch, {"packs": []})
    route_packs("anything", db_path=db)
    assert len(prompts) == 1
    for key in PACK_KEYS:
        assert f'"{key}"' in prompts[0]


def test_gated_on_empty_question_and_untracked_db(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _must_not_call(*_a: object, **_k: object) -> object:
        raise AssertionError("classifier called despite a structural gate")

    monkeypatch.setattr(router, "call_llm_structured", _must_not_call)
    assert route_packs("   ", db_path=db).status == "gated"
    # No DB at all.
    assert route_packs("trim NU?", db_path=tmp_path / "nope.db").status == "gated"
    # DB exists but holds no tracked companies.
    empty = tmp_path / "empty.db"
    conn = sqlite3.connect(str(empty))
    conn.execute(
        "CREATE TABLE tracked_companies (id INTEGER PRIMARY KEY, ticker TEXT, "
        "name TEXT, list_type TEXT)"
    )
    conn.commit()
    conn.close()
    gated = route_packs("trim NU?", db_path=empty)
    assert gated.status == "gated"
    assert gated.packs == ()


def test_budget_skip_spends_nothing(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    @dataclass
    class _Check:
        cap: float = 5.0

    def _must_not_call(*_a: object, **_k: object) -> object:
        raise AssertionError("classifier called despite a budget skip")

    def _over_cap(*_a: object, **_k: object) -> _Check:
        return _Check()

    monkeypatch.setattr(router, "call_llm_structured", _must_not_call)
    monkeypatch.setattr(router, "should_skip_for_budget", _over_cap)
    route = route_packs("trim NU?", db_path=db)
    assert route.status == "skipped"
    assert route.packs == ()


def test_transport_failure_fails_closed(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("CLI exploded")

    monkeypatch.setattr(router, "call_llm_structured", _boom)
    route = route_packs("trim NU?", db_path=db)
    assert route.status == "error"
    assert route.packs == ()


def test_malformed_packs_field_fails_closed(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _classifier(monkeypatch, {"packs": "holdings"})  # string, not a list
    route = route_packs("trim NU?", db_path=db)
    assert route.status == "error"
    assert route.packs == ()


def test_route_cache_skips_repeat_classifier_call(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L14: a repeated question reuses the prior decision and skips the
    interactive fast-model round-trip. The autouse cache-clear fixture
    guarantees a cold start, so the first call spends and the second does not."""
    prompts = _classifier(monkeypatch, {"packs": ["holdings", "dcf"]})
    r1 = route_packs("should I trim MELI?", db_path=db)
    r2 = route_packs("Should I   trim   MELI?", db_path=db)  # identical after normalization
    assert r1.packs == ("holdings", "dcf") == r2.packs
    assert r1.status == "ok"
    assert r2.status == "ok"
    assert r2.note == "cached"
    assert len(prompts) == 1  # the classifier ran exactly once
    # A genuinely different question is NOT a hit — it routes afresh.
    route_packs("how am I doing versus the index?", db_path=db)
    assert len(prompts) == 2


def test_error_and_skip_routes_are_not_cached(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the ``ok`` path caches — a transient failure must be retried, never
    frozen as a (cached) document-only degrade."""
    calls: list[int] = []

    def _boom(*_a: object, **_k: object) -> object:
        calls.append(1)
        raise RuntimeError("CLI exploded")

    monkeypatch.setattr(router, "call_llm_structured", _boom)
    assert route_packs("trim NU?", db_path=db).status == "error"
    assert route_packs("trim NU?", db_path=db).status == "error"
    assert len(calls) == 2  # error was never cached


def test_cached_route_still_respects_budget_skip(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The budget gate runs BEFORE the route cache is consulted, so a hard cap
    still degrades to no packs even for a question that routed successfully
    earlier (the cache never overrides the spend contract)."""
    from dataclasses import dataclass

    _classifier(monkeypatch, {"packs": ["holdings"]})
    assert route_packs("trim MELI?", db_path=db).packs == ("holdings",)  # cached now

    @dataclass
    class _Check:
        cap: float = 5.0

    monkeypatch.setattr(router, "should_skip_for_budget", lambda *_a, **_k: _Check())
    skipped = route_packs("trim MELI?", db_path=db)
    assert skipped.status == "skipped"
    assert skipped.packs == ()
