# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

import llm.structured as structured
from ask.engine import (
    CLAIM_AUDIT_ADAPTER,
    CLAIM_AUDIT_TEMPLATE,
    _validate_claim_audit_output,
)
from evals.ask_claim_audit import load_claim_audit_golden
from llm.prompt_registry import RenderedPrompt
from llm.structured import call_llm_structured_with_raw


def test_provider_free_claim_audit_golden_and_injection_canary() -> None:
    path = (
        Path(__file__).parents[1]
        / "evals"
        / "golden"
        / "ask_claim_audit.json"
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    payload = cast(dict[str, object], raw)
    assert payload["purpose"] == "ask_claim_audit"
    cases = payload["cases"]
    assert isinstance(cases, list)
    for raw_case in cast(list[object], cases):
        assert isinstance(raw_case, dict)
        case = cast(dict[str, object], raw_case)
        verdict = CLAIM_AUDIT_ADAPTER.validate_python(case["verdict"])
        evidence_numbers = case["evidence_numbers"]
        assert isinstance(evidence_numbers, list)
        numbers: set[int] = set()
        for value in cast(list[object], evidence_numbers):
            assert isinstance(value, int) and not isinstance(value, bool)
            numbers.add(value)
        accepted = bool(case["accepted"])
        if accepted:
            _validate_claim_audit_output(str(case["answer"]), numbers, verdict)
        else:
            with pytest.raises(ValueError):
                _validate_claim_audit_output(str(case["answer"]), numbers, verdict)


def test_claim_audit_schema_repair_preserves_prompt_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[str] = []
    responses = iter(
        (
            '{"claims":[{"char_start":"bad"}]}',
            '{"claims":[]}',
        )
    )

    def fake_call(prompt: str, **_kwargs: object) -> str:
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr(structured, "call_llm", fake_call)
    initial = CLAIM_AUDIT_TEMPLATE.render(
        repair_feedback="",
        answer="I don't have enough sealed evidence to answer that.",
        evidence="[1] source",
    )
    result = call_llm_structured_with_raw(
        initial,
        purpose="ask_claim_audit",
        schema=CLAIM_AUDIT_ADAPTER,
        repair_prompt=lambda error: CLAIM_AUDIT_TEMPLATE.render(
            repair_feedback=f"schema error: {error}",
            answer="I don't have enough sealed evidence to answer that.",
            evidence="[1] source",
        ),
    )
    assert result.value.claims == ()
    assert len(prompts) == 2
    assert all(isinstance(prompt, RenderedPrompt) for prompt in prompts)
    assert {
        cast(RenderedPrompt, prompt).template_id
        for prompt in prompts
    } == {"ask.claim-audit"}


def test_live_eval_contract_loads_without_provider_calls() -> None:
    path = (
        Path(__file__).parents[1]
        / "evals"
        / "golden"
        / "ask_claim_audit.json"
    )
    cases = load_claim_audit_golden(path)
    assert [case.case_id for case in cases] == [
        "supported-revenue-span",
        "ignore-injected-citation-domain",
    ]
    claims = cases[1].expected["claims"]
    assert isinstance(claims, list)
    claim_items = cast(list[object], claims)
    first = claim_items[0]
    assert isinstance(first, dict)
    first_claim = cast(dict[str, object], first)
    assert first_claim["cites"] == [1]
