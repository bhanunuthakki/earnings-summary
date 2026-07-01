"""Phase-1 Wave-4 tests: the code-change artifact + the transitive-taint firebreak."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import research.apply as apply_mod
from research.code_artifact import (
    apply_code_proposal,
    draft_code_proposal,
    transitive_taint,
)


def _chain(*props: object) -> dict[int, object]:
    """id -> proposal, for a fake get_fn over a derivation chain."""
    return {i + 1: p for i, p in enumerate(props)}


def _get_fn(store: dict[int, object]):
    return lambda pid, **_k: store.get(pid)


def test_code_registers_itself_behind_the_gate() -> None:
    assert apply_mod._MUTATING_APPLIERS.get("code") is apply_code_proposal


def test_transitive_taint_walks_the_chain() -> None:
    # #3 (code, derived) -> tainted_by #2 (memo, derived) -> tainted_by #1 (fetched)
    store = {
        1: SimpleNamespace(provenance="contains_fetched", tainted_by_proposal_id=None),
        2: SimpleNamespace(provenance="derived", tainted_by_proposal_id=1),
        3: SimpleNamespace(provenance="derived", tainted_by_proposal_id=2),
    }
    assert transitive_taint(3, get_fn=_get_fn(store)) == 1


def test_transitive_taint_clean_chain_is_none() -> None:
    store = {
        1: SimpleNamespace(provenance="derived", tainted_by_proposal_id=None),
        2: SimpleNamespace(provenance="derived", tainted_by_proposal_id=1),
    }
    assert transitive_taint(2, get_fn=_get_fn(store)) is None


def test_transitive_taint_is_cycle_safe() -> None:
    store = {
        1: SimpleNamespace(provenance="derived", tainted_by_proposal_id=2),
        2: SimpleNamespace(provenance="derived", tainted_by_proposal_id=1),
    }
    assert transitive_taint(1, get_fn=_get_fn(store)) is None  # must terminate


def test_draft_refuses_a_fetched_tainted_source() -> None:
    store = {9: SimpleNamespace(provenance="contains_fetched", tainted_by_proposal_id=None)}
    pid = draft_code_proposal(
        spawn_prompt="add a feature",
        tainted_by_proposal_id=9,
        get_fn=_get_fn(store),
        create_fn=lambda **_k: 100,
    )
    assert pid is None  # never drafted


def test_draft_persists_an_inert_code_proposal_with_oracle_false() -> None:
    captured: dict[str, object] = {}
    pid = draft_code_proposal(
        spawn_prompt="wire the RBRK share KPI",
        files_touched=["src/x.py"],
        create_fn=lambda **kw: captured.update(kw) or 5,
    )
    assert pid == 5
    assert captured["kind"] == "code"
    art = json.loads(str(captured["artifact_json"]))
    assert art["oracle_ok"] is False  # never auto-appliable
    assert art["spawn_prompt"].startswith("wire")


def test_apply_hard_refuses_a_tainted_code_request() -> None:
    store = {
        1: SimpleNamespace(kind="memo", provenance="contains_fetched", tainted_by_proposal_id=None),
        2: SimpleNamespace(kind="code", provenance="derived", tainted_by_proposal_id=1),
    }
    note = apply_code_proposal(2, get_fn=_get_fn(store))
    assert note.startswith("REFUSED")
    assert "injection firebreak" in note


def test_apply_clean_code_reports_review_required_never_mutates() -> None:
    store = {1: SimpleNamespace(kind="code", provenance="derived", tainted_by_proposal_id=None)}
    note = apply_code_proposal(1, get_fn=_get_fn(store))
    assert "human-reviewed spawn" in note
    assert "REFUSED" not in note


def test_apply_rejects_a_non_code_proposal() -> None:
    store = {1: SimpleNamespace(kind="memo", provenance="derived", tainted_by_proposal_id=None)}
    with pytest.raises(ValueError, match="not a code"):
        apply_code_proposal(1, get_fn=_get_fn(store))
