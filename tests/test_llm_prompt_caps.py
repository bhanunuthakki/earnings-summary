"""Tests for the prompt-size caps in P2-12 (recent_developments) + P2-13 (event_brief).

Both were "uncapped corpus" gaps in the audit: WebSearch results could
bloat the news prompt to 50KB; investor-day decks could push event_brief
past 40KB. Hard caps keep latency + cost predictable.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import llm_client  # noqa: E402
from llm_client import (  # noqa: E402
    _MAX_EVENT_TEXT_CHARS,
    _MAX_EXCERPT_CHARS_PER_NEWS_ITEM,
    _MAX_WEB_RESULTS_PER_NEWS_CALL,
    _truncate_event_text,
)


def test_recent_developments_caps_are_sane() -> None:
    """The audit recommended 7 results / 500-char excerpt. Defaults must match
    (the exact value can be tuned; the assertion guards against drift to 0
    or to absurdly large values that defeat the cap.)"""
    assert 3 <= _MAX_WEB_RESULTS_PER_NEWS_CALL <= 15
    assert 200 <= _MAX_EXCERPT_CHARS_PER_NEWS_ITEM <= 2_000


def test_event_text_cap_is_sane() -> None:
    """20KB ≈ 5-7 pages of dense management narrative — enough for the
    event_brief schema, well below the 40KB pre-cap pathology."""
    assert 5_000 <= _MAX_EVENT_TEXT_CHARS <= 40_000


def test_truncate_short_text_returns_unchanged() -> None:
    short_text = "Investor Day 2026: Strategic targets through 2030..."
    out = _truncate_event_text(short_text)
    assert out == short_text


def test_truncate_long_text_keeps_head_and_tail_marker() -> None:
    """A 60KB investor day deck should produce a head + truncation marker + tail."""
    payload = "A" * 60_000
    out = _truncate_event_text(payload)
    assert len(out) < 60_000
    assert "document truncated for prompt-size budget" in out
    # First N-250 chars come from the head; last ~200 from the tail.
    assert out.startswith("A")
    assert out.endswith("A")


def test_truncate_preserves_distinct_head_and_tail_content() -> None:
    """The truncation marker should appear between the head segment and the
    tail segment — not lose either."""
    head = "HEADLINE: Investor Day · multi-year targets through 2030."
    body = "X" * 30_000
    tail = "Q&A wrap: management reiterated buyback authorization."
    payload = head + body + tail
    out = _truncate_event_text(payload, max_chars=5_000)
    assert head in out
    assert tail in out
    assert "document truncated" in out


def test_recent_developments_prompt_carries_web_budget_lines() -> None:
    """The prompt body must include the explicit hard-cap instructions to the
    model — otherwise the caps are documentation-only."""
    # Build the prompt by inspecting the wired f-string template directly.
    # We can't call the LLM, but the prompt is composed from the literal
    # string in the function body — read it via the inline default.
    src = Path(PROJECT_ROOT / "src" / "llm_client.py").read_text(encoding="utf-8")
    # The hard-cap stanza must appear in the source so a future refactor
    # that drops it surfaces here.
    assert "WEB BUDGET (HARD CAPS" in src
    assert "AT MOST 2 web_search queries" in src
    assert "AT MOST {max_web_results}" in src
    assert "AT MOST {max_excerpt_chars} characters" in src


def test_event_brief_uses_truncated_text(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """generate_event_brief should pass the truncated text to call_llm, not
    the raw input. We monkeypatch call_llm to capture what it actually
    received and confirm the truncation marker is present for an over-cap
    input."""
    captured: dict[str, str] = {}

    def _fake_call_llm(prompt: str, purpose: str, ticker: str | None = None) -> str:  # type: ignore[no-untyped-def]
        captured["prompt"] = prompt
        return "ok"

    monkeypatch.setattr(llm_client, "call_llm", _fake_call_llm)

    huge_text = "Z" * (_MAX_EVENT_TEXT_CHARS + 5_000)
    llm_client.generate_event_brief(huge_text, anchor_block="", ticker="META")

    assert "prompt" in captured
    # Truncation marker should appear in the prompt body.
    assert "document truncated" in captured["prompt"]
    # And the prompt should be shorter than head_template + raw_text.
    assert len(captured["prompt"]) < len("Event Document Text:") + len(huge_text)
