"""Phase-1 Wave-3 tests: the thesis-edit artifact (draft + gated append-only apply)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import research.apply as apply_mod
from research.thesis_artifact import apply_thesis_proposal, draft_thesis_proposal


def test_thesis_registers_itself_behind_the_gate() -> None:
    # importing the module (above) self-registers the gated applier
    assert apply_mod._MUTATING_APPLIERS.get("thesis") is apply_thesis_proposal


def test_draft_persists_a_thesis_proposal_with_oracle_ok() -> None:
    captured: dict[str, object] = {}
    pid = draft_thesis_proposal(
        ticker="nu",
        body="NPLs stabilized this quarter; conviction intact",
        entry_kind="revision",
        evidence_json='[{"source_url": "https://ir.nu"}]',
        create_fn=lambda **kw: captured.update(kw) or 12,
    )
    assert pid == 12
    assert captured["kind"] == "thesis"
    assert captured["ticker"] == "NU"  # upper-cased
    art = json.loads(str(captured["artifact_json"]))
    assert art["oracle_ok"] is True
    assert art["entry_kind"] == "revision"
    assert "NPLs" in art["body"]


def test_draft_skips_empty_body_or_ticker() -> None:
    assert draft_thesis_proposal(ticker="NU", body="   ", create_fn=lambda **_k: 1) is None
    assert draft_thesis_proposal(ticker="", body="x", create_fn=lambda **_k: 1) is None


def test_apply_appends_the_entry() -> None:
    prop = SimpleNamespace(
        kind="thesis",
        ticker="NU",
        body_md="x",
        artifact_json=json.dumps({"entry_kind": "revision", "body": "seasoning holds"}),
    )
    appended: dict[str, object] = {}
    row = apply_thesis_proposal(
        3,
        get_fn=lambda _pid, **_k: prop,
        append_fn=lambda **kw: appended.update(kw) or SimpleNamespace(id=55),
    )
    assert appended["ticker"] == "NU"
    assert appended["entry_kind"] == "revision"
    assert appended["body"] == "seasoning holds"
    assert "55" in row


def test_apply_rejects_a_non_thesis_proposal() -> None:
    prop = SimpleNamespace(kind="memo", ticker="NU", artifact_json="{}")
    with pytest.raises(ValueError, match="not a thesis"):
        apply_thesis_proposal(1, get_fn=lambda _pid, **_k: prop, append_fn=lambda **_k: None)


def test_apply_requires_an_artifact_payload() -> None:
    prop = SimpleNamespace(kind="thesis", ticker="NU", body_md="x", artifact_json=None)
    with pytest.raises(ValueError, match="no entry"):
        apply_thesis_proposal(1, get_fn=lambda _pid, **_k: prop, append_fn=lambda **_k: None)
