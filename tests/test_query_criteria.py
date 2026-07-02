"""Tests for the per-query criteria layer (src/llm/query_criteria.py + the
backend_judge checklist extension — PR4 of meta_eval_governance.md §3). All LLM
calls are DI'd fakes / monkeypatched; the suite never spends."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from llm.backend_judge import (
    FACETS,
    build_judge_prompt,
    parse_pair_verdict,
)
from llm.query_criteria import (
    CRITERIA_SENTINEL,
    Criterion,
    derive_or_load,
    load_cached_criteria,
    render_criteria_block,
)
from llm.structured import StructuredParseError


def _db(tmp_path: Path) -> Path:
    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE query_criteria (
            purpose TEXT NOT NULL, prompt_sha256 TEXT NOT NULL,
            criteria_version TEXT NOT NULL, criteria_json TEXT NOT NULL,
            derived_by_model TEXT NOT NULL, derived_at TEXT NOT NULL,
            PRIMARY KEY (purpose, prompt_sha256, criteria_version)
        );
        """
    )
    conn.commit()
    conn.close()
    return db_path


_GOOD_PAYLOAD: dict[str, object] = {
    "criteria": [
        {"id": "c1", "kind": "content", "weight": 2, "statement": "Names >=2 failure modes"},
        {"id": "c2", "kind": "format", "weight": 1, "statement": "Single JSON object"},
        {"id": "c3", "kind": "grounding", "weight": 3, "statement": "Cites supplied figures"},
        {"id": "c4", "kind": "constraint", "weight": 1, "statement": "Under the 400-word cap"},
    ]
}


# ---------------------------------------------------------------------------
# Derive + cache
# ---------------------------------------------------------------------------


def test_derive_validates_and_caches(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    calls: list[str] = []

    def struct(prompt: str, **kwargs: object) -> object:
        calls.append(prompt)
        assert kwargs.get("purpose") == "query_criteria_derive"
        assert kwargs.get("scope") == "meta_eval"
        return _GOOD_PAYLOAD

    crits = derive_or_load(
        db_path, "bear_case", "sha1", "TASK PROMPT BODY", criteria_version="v1", struct=struct
    )
    assert crits is not None and len(crits) == 4
    assert crits[0] == Criterion(
        id="c1", kind="content", weight=2, statement="Names >=2 failure modes"
    )
    assert len(calls) == 1
    # Second call: cache hit, no derivation.
    again = derive_or_load(
        db_path, "bear_case", "sha1", "TASK PROMPT BODY", criteria_version="v1", struct=struct
    )
    assert again == crits
    assert len(calls) == 1
    assert load_cached_criteria(db_path, "bear_case", "sha1", criteria_version="v1") == crits


def test_derive_version_forks_cache(tmp_path: Path) -> None:
    db_path = _db(tmp_path)

    def struct(prompt: str, **kwargs: object) -> object:
        return _GOOD_PAYLOAD

    derive_or_load(db_path, "bear_case", "sha1", "T", criteria_version="v1", struct=struct)
    assert load_cached_criteria(db_path, "bear_case", "sha1", criteria_version="v2") is None


def test_derive_failure_returns_none(tmp_path: Path) -> None:
    db_path = _db(tmp_path)

    def broken(prompt: str, **kwargs: object) -> object:
        raise StructuredParseError("bad", raw_head="")

    assert (
        derive_or_load(db_path, "bear_case", "sha1", "T", criteria_version="v1", struct=broken)
        is None
    )

    def malformed(prompt: str, **kwargs: object) -> object:
        return {"criteria": [{"id": "c1", "kind": "vibes", "weight": 2, "statement": "x"}]}

    assert (
        derive_or_load(db_path, "bear_case", "sha2", "T", criteria_version="v1", struct=malformed)
        is None
    )
    # Failures are never cached — a later sweep retries.
    assert load_cached_criteria(db_path, "bear_case", "sha2", criteria_version="v1") is None


def test_validation_clamps_weight_and_dedups_ids(tmp_path: Path) -> None:
    db_path = _db(tmp_path)

    def struct(prompt: str, **kwargs: object) -> object:
        return {
            "criteria": [
                {"id": "c1", "kind": "content", "weight": 9, "statement": "x"},
                {"id": "c1", "kind": "format", "weight": 1, "statement": "dup id"},
                {"id": "c2", "kind": "format", "weight": 0, "statement": "y"},
            ]
        }

    crits = derive_or_load(db_path, "p", "s", "T", criteria_version="v1", struct=struct)
    assert crits is not None
    assert [c.id for c in crits] == ["c1", "c2"]
    assert crits[0].weight == 3  # clamped down
    assert crits[1].weight == 1  # clamped up


# ---------------------------------------------------------------------------
# The judge-prompt block + the anti-leak sentinel
# ---------------------------------------------------------------------------


def _crits() -> tuple[Criterion, ...]:
    return (
        Criterion(id="c1", kind="content", weight=2, statement="Names >=2 failure modes"),
        Criterion(id="c2", kind="format", weight=1, statement="Single JSON object"),
    )


def test_render_block_carries_sentinel_and_contract() -> None:
    block = render_criteria_block(_crits())
    assert CRITERIA_SENTINEL in block
    assert "c1 (content, w2): Names >=2 failure modes" in block
    assert '"checklist"' in block  # the per-item output contract


def test_judge_prompt_with_and_without_block() -> None:
    with_block = build_judge_prompt(
        "bear_case", "TASK", "resp A", "resp B", criteria_block=render_criteria_block(_crits())
    )
    assert CRITERIA_SENTINEL in with_block
    legacy = build_judge_prompt("bear_case", "TASK", "resp A", "resp B")
    assert CRITERIA_SENTINEL not in legacy
    # The checklist sits between the responses and the facet instructions.
    assert with_block.index("RESPONSE B") < with_block.index(CRITERIA_SENTINEL)
    assert with_block.index(CRITERIA_SENTINEL) < with_block.index("Judge on four facets")


def test_generation_prompt_never_contains_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    """The I1 anti-leak rule (§3.4): run_model sends case.prompt byte-identical —
    monkeypatch the transport and assert no checklist text reaches generation."""
    import llm.model_eval as me

    sent: list[str] = []

    def fake_call_llm(prompt: str, **kwargs: object) -> str:
        sent.append(prompt)
        return "response"

    monkeypatch.setattr(me, "call_llm", fake_call_llm)
    me.run_model("THE PRODUCTION PROMPT", model_id="claude-haiku-4-5-20251001", purpose="p")
    assert sent == ["THE PRODUCTION PROMPT"]
    assert CRITERIA_SENTINEL not in sent[0]


# ---------------------------------------------------------------------------
# parse_pair_verdict: tolerant checklist
# ---------------------------------------------------------------------------


def _verdict_json(checklist: object = None, include: bool = False) -> str:
    payload: dict[str, object] = {
        "winner": "A",
        "margin": 0.6,
        "rationale": "A is tighter",
        **{facet: "A" for facet in FACETS},
    }
    if include:
        payload["checklist"] = checklist
    return json.dumps(payload)


def test_parse_verdict_without_checklist_is_legacy() -> None:
    v = parse_pair_verdict(_verdict_json())
    assert v is not None
    assert v.checklist is None


def test_parse_verdict_with_checklist() -> None:
    v = parse_pair_verdict(_verdict_json({"c1": "A", "c2": "tie"}, include=True))
    assert v is not None
    assert v.checklist == {"c1": "A", "c2": "tie"}


def test_parse_verdict_malformed_checklist_fails_closed() -> None:
    assert parse_pair_verdict(_verdict_json({"c1": "Z"}, include=True)) is None
    assert parse_pair_verdict(_verdict_json("not a dict", include=True)) is None


# ---------------------------------------------------------------------------
# judge_pair consolidation (monkeypatched transport)
# ---------------------------------------------------------------------------


def test_judge_pair_consolidates_checklist(monkeypatch: pytest.MonkeyPatch) -> None:
    import llm.backend_judge as bj

    # Pass 1 (claude in A): c1 -> A (claude), c2 -> B (gemini).
    # Pass 2 (swapped, gemini in A): c1 -> B (claude), c2 -> B (claude).
    # Consolidated: c1 agrees on claude; c2 flips -> tie.
    responses = iter(
        [
            _verdict_json({"c1": "A", "c2": "B"}, include=True),
            _verdict_json({"c1": "B", "c2": "B"}, include=True),
        ]
    )

    def fake_call_llm(prompt: str, **kwargs: object) -> str:
        assert CRITERIA_SENTINEL in prompt  # the block reached the judge
        return next(responses)

    monkeypatch.setattr(bj, "call_llm", fake_call_llm)
    jp = bj.judge_pair(
        purpose="bear_case",
        label="case1",
        ticker="META",
        claude_response="incumbent out",
        gemini_response="candidate out",
        task_prompt="TASK",
        judge_backend="claude",
        criteria_block=render_criteria_block(_crits()),
    )
    assert jp.checklist_winners == {"c1": "claude", "c2": "tie"}


def test_judge_pair_without_block_has_no_checklist(monkeypatch: pytest.MonkeyPatch) -> None:
    import llm.backend_judge as bj

    def fake_call_llm(prompt: str, **kwargs: object) -> str:
        assert CRITERIA_SENTINEL not in prompt
        return _verdict_json()

    monkeypatch.setattr(bj, "call_llm", fake_call_llm)
    jp = bj.judge_pair(
        purpose="bear_case",
        label="case1",
        ticker=None,
        claude_response="a",
        gemini_response="b",
        task_prompt="TASK",
        judge_backend="claude",
    )
    assert jp.checklist_winners is None
