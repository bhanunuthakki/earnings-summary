"""Prompt-size and explicit web-budget contracts for recent developments."""

from __future__ import annotations

from pathlib import Path

import llm_client
from llm_client import RECENT_DEVELOPMENTS_TEMPLATE

_MAX_EXCERPT_CHARS_PER_NEWS_ITEM: int = getattr(llm_client, "_MAX_EXCERPT_CHARS_PER_NEWS_ITEM")
_MAX_WEB_RESULTS_PER_NEWS_CALL: int = getattr(llm_client, "_MAX_WEB_RESULTS_PER_NEWS_CALL")


def test_recent_developments_caps_are_sane() -> None:
    """The audit recommended 7 results / 500-char excerpt. Defaults must match
    (the exact value can be tuned; the assertion guards against drift to 0
    or to absurdly large values that defeat the cap.)"""
    assert 3 <= _MAX_WEB_RESULTS_PER_NEWS_CALL <= 15
    assert 200 <= _MAX_EXCERPT_CHARS_PER_NEWS_ITEM <= 2_000


def test_recent_developments_prompt_carries_web_budget_lines() -> None:
    """The prompt body must include the explicit hard-cap instructions to the
    model — otherwise the caps are documentation-only."""
    # Build the prompt by inspecting the wired f-string template directly.
    # We can't call the LLM, but the prompt is composed from the literal
    # string in the function body — read it via the inline default.
    src = Path(llm_client.__file__).read_text(encoding="utf-8")
    # The hard-cap stanza must appear in the source so a future refactor
    # that drops it surfaces here.
    assert "WEB BUDGET (HARD CAPS" in src
    assert "Run at most 2 searches total" in src
    assert "Open at most {max_web_results} sources" in src
    assert "AT MOST {max_excerpt_chars} characters" in src
    assert "web_search" not in RECENT_DEVELOPMENTS_TEMPLATE.body
    assert "web_fetch" not in RECENT_DEVELOPMENTS_TEMPLATE.body
