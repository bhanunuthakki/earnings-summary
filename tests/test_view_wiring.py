"""Phase-1 Wave-2b tests: the approve->write dispatch and tier-gated view drafting.

The verb action core stays write-free; ``apply_approved_proposal`` is the separate
write step both surfaces call on approve. ``_draft_supplementary_view`` is the
run-orchestration hook that adds a view artifact on standard/deep tiers.
"""

from __future__ import annotations

from types import SimpleNamespace

import research.apply as apply_mod
import research.view_artifact as view_artifact
from research.apply import apply_approved_proposal
from research.run import _draft_supplementary_view


def test_apply_view_writes_the_saved_view_and_returns_a_note(monkeypatch) -> None:
    prop = SimpleNamespace(kind="view", title="NU revenue")
    monkeypatch.setattr(apply_mod, "get_proposal", lambda _pid, **_k: prop)
    calls: list[int] = []
    monkeypatch.setattr(
        view_artifact,
        "apply_view_proposal",
        lambda pid, **_k: calls.append(pid) or SimpleNamespace(name="NU revenue"),
    )
    note = apply_approved_proposal(42)
    assert calls == [42]  # the real write path was invoked
    assert note == "saved view: NU revenue"


def test_apply_memo_is_a_noop(monkeypatch) -> None:
    monkeypatch.setattr(
        apply_mod, "get_proposal", lambda _pid, **_k: SimpleNamespace(kind="memo", title="m")
    )
    assert apply_approved_proposal(1) == ""


def test_apply_mutating_kind_without_a_cleared_gate_is_blocked(monkeypatch) -> None:
    # dcf is a MUTATING kind: a bare proposal (no evidence/verdict/oracle) must be
    # blocked by the higher-bar gate, never silently "not yet wired".
    monkeypatch.setattr(
        apply_mod, "get_proposal", lambda _pid, **_k: SimpleNamespace(kind="dcf", title="d")
    )
    assert "blocked (higher bar)" in apply_approved_proposal(1)


def test_apply_missing_proposal_returns_empty(monkeypatch) -> None:
    monkeypatch.setattr(apply_mod, "get_proposal", lambda _pid, **_k: None)
    assert apply_approved_proposal(99) == ""


def test_supplementary_view_drafts_on_standard_tier() -> None:
    calls: list[dict[str, object]] = []
    task = SimpleNamespace(claim="NU revenue 8 quarters", ticker="NU", note_id=3, id=7)
    tier = SimpleNamespace(name="standard")
    _draft_supplementary_view(task, tier, drafter=lambda **kw: calls.append(kw))
    assert len(calls) == 1
    assert calls[0]["ticker"] == "NU"
    assert calls[0]["task_id"] == 7


def test_supplementary_view_skipped_on_cheap_tier() -> None:
    calls: list[dict[str, object]] = []
    _draft_supplementary_view(
        SimpleNamespace(claim="x", ticker=None, note_id=None, id=1),
        SimpleNamespace(name="cheap"),
        drafter=lambda **kw: calls.append(kw),
    )
    assert not calls


def test_supplementary_view_swallows_a_drafter_error() -> None:
    def boom(**_kw: object) -> None:
        raise RuntimeError("compile blew up")

    # must NOT raise — a view-draft failure never disturbs the memo
    _draft_supplementary_view(
        SimpleNamespace(claim="x", ticker="NU", note_id=None, id=1),
        SimpleNamespace(name="deep"),
        drafter=boom,
    )
