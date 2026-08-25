"""Phase-1 Wave-3 tests: the thesis-edit artifact (draft + gated append-only apply)."""

from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace

import pytest

import research.apply as apply_mod
from research.thesis_artifact import (
    apply_thesis_proposal,
    draft_thesis_entry,
    draft_thesis_proposal,
)


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


def test_apply_routes_earnings_prep_to_open_question() -> None:
    prop = SimpleNamespace(
        kind="thesis",
        ticker="NU",
        body_md="x",
        artifact_json=json.dumps(
            {"entry_kind": "earnings_prep_append", "body": "Re-check NIM next quarter."}
        ),
    )
    created: dict[str, object] = {}
    receipt = apply_thesis_proposal(
        9,
        get_fn=lambda _pid, **_k: prop,
        append_fn=lambda **_kw: pytest.fail("earnings prep must not write the thesis ledger"),
        note_fn=lambda **kw: created.update(kw) or SimpleNamespace(id=77),
    )

    assert created["kind"] == "question"
    assert created["body"] == "Re-check NIM next quarter."
    assert created["source_ref"] == "research_proposal:9:earnings_prep"
    assert "open analyst question #77" in receipt


def test_apply_rejects_a_non_thesis_proposal() -> None:
    prop = SimpleNamespace(kind="memo", ticker="NU", artifact_json="{}")
    with pytest.raises(ValueError, match="not a thesis"):
        apply_thesis_proposal(1, get_fn=lambda _pid, **_k: prop, append_fn=lambda **_k: None)


def test_apply_requires_an_artifact_payload() -> None:
    prop = SimpleNamespace(kind="thesis", ticker="NU", body_md="x", artifact_json=None)
    with pytest.raises(ValueError, match="no entry"):
        apply_thesis_proposal(1, get_fn=lambda _pid, **_k: prop, append_fn=lambda **_k: None)


# --- the governed thesis_entry_draft generator ----------------------------------------


def _struct(result: dict[str, object]) -> Callable[..., dict[str, object]]:
    def caller(prompt: str, *, purpose: str, required_keys: tuple[str, ...]) -> dict[str, object]:
        return result

    return caller


def test_thesis_entry_draft_returns_entry_kind_and_body() -> None:
    out = draft_thesis_entry(
        question="do NU margins hold?",
        memo_md="Take-rate inflected up; NIMAL stable.",
        ticker="NU",
        struct=_struct(
            {"entry_kind": "thesis_update", "body": "Take-rate firmed; I'm holding the overweight."}
        ),
    )
    assert out == {
        "entry_kind": "thesis_update",
        "body": "Take-rate firmed; I'm holding the overweight.",
    }


def test_thesis_entry_draft_clamps_an_unknown_kind_to_revision() -> None:
    out = draft_thesis_entry(
        question="q",
        memo_md="m",
        struct=_struct({"entry_kind": "wild_guess", "body": "Something changed."}),
    )
    assert out is not None and out["entry_kind"] == "revision"


def test_thesis_entry_draft_empty_body_degrades_to_none() -> None:
    assert (
        draft_thesis_entry(
            question="q", memo_md="m", struct=_struct({"entry_kind": "revision", "body": "  "})
        )
        is None
    )
    assert draft_thesis_entry(question="q", memo_md="m", struct=_struct({})) is None


def test_thesis_entry_draft_frames_evidence_and_verdict_in_the_prompt() -> None:
    seen: dict[str, str] = {}

    def caller(prompt: str, *, purpose: str, required_keys: tuple[str, ...]) -> dict[str, object]:
        seen["prompt"] = prompt
        seen["purpose"] = purpose
        return {"entry_kind": "bear_append", "body": "Credit risk sharpened; trimming conviction."}

    out = draft_thesis_entry(
        question="do NU margins hold?",
        memo_md="NIMAL stable",
        ticker="NU",
        quarantined_evidence="NPL formation ticked up to 4.5%",
        adversarial_verdict=json.dumps(
            {"refuted": True, "confidence": "high", "rationale": "credit softening"}
        ),
        struct=caller,
    )
    assert out is not None and out["entry_kind"] == "bear_append"
    assert seen["purpose"] == "thesis_entry_draft"
    assert "REFUTED" in seen["prompt"]  # the refuting verdict reached the prompt
    assert "UNTRUSTED DATA" in seen["prompt"] and "NPL formation" in seen["prompt"]


def test_thesis_entry_draft_default_degrades_on_a_parse_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm import structured as structured_mod

    def boom(*_a: object, **_k: object) -> object:
        raise structured_mod.StructuredParseError("unusable", raw_head="{...")

    monkeypatch.setattr(structured_mod, "call_llm_structured", boom)
    # default struct catches StructuredParseError -> {} -> empty body -> None
    assert draft_thesis_entry(question="q", memo_md="m") is None
