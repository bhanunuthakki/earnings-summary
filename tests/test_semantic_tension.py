"""The semantic tenet-tension probe (B5, src/synthesis/semantic_tension.py) —
the shared classifier both distill paths (tenet_distill, session_distill)
call when the free slug-only overlap check finds nothing. Injected call, no
live LLM.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from synthesis.semantic_tension import detect_semantic_tension
from synthesis.tenets import record_tenet

PRIOR_HEAD = "0059_kpi_facts_restatement"


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "ledger.db", stamp=PRIOR_HEAD)


def test_zero_tenets_short_circuits_with_no_call(db_path: Path) -> None:
    calls = {"n": 0}

    def spy(body: str, tenets_text: str) -> dict[str, object]:
        calls["n"] += 1
        return {"tension_with": None}

    result = detect_semantic_tension("a brand new belief", db_path=db_path, call=spy)
    assert result is None
    assert calls["n"] == 0  # deterministic $0 short-circuit — the model never ran


def test_grounded_match_resolves_the_row(db_path: Path) -> None:
    """The prod 20/31 shape: two tenets on the SAME underlying belief under
    DIFFERENT scope_key slugs — the free slug probe can't see this; the
    semantic probe (injected here) does."""
    other = record_tenet(
        body_md="I never let a retirement-account position lapse without review.",
        scope_key="retirement-account-hold-discipline",
        db_path=db_path,
    )

    def call(body: str, tenets_text: str) -> dict[str, object]:
        assert f"T{other.id}" in tenets_text
        assert "retirement-account-hold-discipline" in tenets_text
        return {"tension_with": f"T{other.id}", "why": "same underlying belief"}

    result = detect_semantic_tension(
        "I hold my tax-advantaged positions through drawdowns without exception.",
        exclude_scope_key="tenet:tax-account-holding-discipline",
        db_path=db_path,
        call=call,
    )
    assert result is not None
    assert result.id == other.id


def test_fabricated_token_returns_none(db_path: Path) -> None:
    record_tenet(body_md="Let winning theses run.", scope_key="exit-discipline", db_path=db_path)

    def call(body: str, tenets_text: str) -> dict[str, object]:
        return {"tension_with": "T999999"}  # never rendered — fabricated

    result = detect_semantic_tension("a candidate belief", db_path=db_path, call=call)
    assert result is None


def test_null_tension_with_returns_none(db_path: Path) -> None:
    record_tenet(body_md="Let winning theses run.", scope_key="exit-discipline", db_path=db_path)

    def call(body: str, tenets_text: str) -> dict[str, object]:
        return {"tension_with": None, "why": "genuinely new topic"}

    result = detect_semantic_tension("an unrelated new belief", db_path=db_path, call=call)
    assert result is None


def test_exclude_scope_key_drops_own_row_and_short_circuits(db_path: Path) -> None:
    """A revision on the SAME scope_key is a supersede, never a "tension"
    against itself — excluding it can even empty the comparison set."""
    record_tenet(body_md="Let winning theses run.", scope_key="exit-discipline", db_path=db_path)
    calls = {"n": 0}

    def spy(body: str, tenets_text: str) -> dict[str, object]:
        calls["n"] += 1
        return {"tension_with": None}

    result = detect_semantic_tension(
        "a revision of the same belief",
        exclude_scope_key="tenet:exit-discipline",
        db_path=db_path,
        call=spy,
    )
    assert result is None
    assert calls["n"] == 0  # the only current tenet is the caller's own — nothing left to compare


def test_exception_from_call_returns_none(db_path: Path) -> None:
    record_tenet(body_md="Let winning theses run.", scope_key="exit-discipline", db_path=db_path)

    def boom(body: str, tenets_text: str) -> dict[str, object]:
        raise RuntimeError("llm CLI down")

    result = detect_semantic_tension("a candidate belief", db_path=db_path, call=boom)
    assert result is None  # never raises


def test_caps_shown_tenets_at_max(db_path: Path) -> None:
    for i in range(20):
        record_tenet(body_md=f"belief number {i}", scope_key=f"belief-{i}", db_path=db_path)

    captured: dict[str, str] = {}

    def call(body: str, tenets_text: str) -> dict[str, object]:
        captured["text"] = tenets_text
        return {"tension_with": None}

    detect_semantic_tension("a new candidate belief", db_path=db_path, call=call)
    shown_tokens = [line for line in captured["text"].splitlines() if line.startswith("[T")]
    assert len(shown_tokens) == 15
