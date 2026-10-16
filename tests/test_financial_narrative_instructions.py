"""Actual standard prompt delivery; these checks do not certify model prose."""

from __future__ import annotations

import pytest

import llm_client
from llm import style
from research.method_contract import ResearchMode, load_research_method


def test_transcript_prompt_keeps_grounding_rules_and_source_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def capture(prompt: str, **_kwargs: object) -> str:
        captured.append(prompt)
        return "Synthetic interpretation; no financial claims."

    monkeypatch.setattr(llm_client, "call_llm", capture)
    llm_client.generate_summary("Synthetic transcript with unavailable financial figures.")
    assert len(captured) == 1
    prompt = captured[0]
    assert style.FINANCIAL_GROUNDING_BLOCK in prompt
    assert "Synthetic transcript with unavailable financial figures." in prompt
    assert "Do not infer that a populated table exists" in prompt
    assert "FMP-driven" not in prompt
    assert "missing verified financial context" in prompt


def test_lens_prompt_and_cache_identity_change_with_grounding_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_token = style.style_block_cache_token()
    prompt = style.compose_brief_prompt("Interpret the supplied facts; missing facts stay missing.")
    assert style.FINANCIAL_GROUNDING_BLOCK in prompt
    monkeypatch.setattr(
        style, "NUMBER_FORMATTING_BLOCK", style.NUMBER_FORMATTING_BLOCK + "\nNew source limit."
    )
    assert style.style_block_cache_token() != original_token
    assert "New source limit." in style.compose_brief_prompt("Interpret the supplied facts.")


@pytest.mark.parametrize("mode", ["company", "earnings", "thesis", "language"])
def test_research_method_delivers_same_financial_boundary(mode: ResearchMode) -> None:
    method = load_research_method(mode)
    assert "Existing verified facts are the first source" in method.instructions
    assert "before generation" in method.instructions
    assert "complete output against the selected corpus" in method.instructions
    assert "actual/forecast" in method.instructions
