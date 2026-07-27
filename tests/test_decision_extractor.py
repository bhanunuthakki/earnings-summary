"""Tests for src/decision_extractor.py — regex extraction of LLM
recommendations from `lens:five_min_reread` artifact markdown."""

from __future__ import annotations

import pytest

from decision_extractor import (
    DecisionCandidate,
    extract_recommendations_from_artifact,
)

# ---------------------------------------------------------------------------
# Happy paths — one recommendation per artifact
# ---------------------------------------------------------------------------


def test_extracts_trim_with_size() -> None:
    md = """## 1. What changed
Some narrative.

## 2. Recommended Action

**TRIM 20%**

Stock at $393 vs DCF NPV $183. The premium has expanded too far for the
thesis growth to keep up.

## 3. What would change my mind
..."""
    out = extract_recommendations_from_artifact(content_md=md)
    assert len(out) == 1
    assert out[0].kind == "trim"
    assert out[0].value == 20.0


def test_extracts_add_with_size() -> None:
    md = """## 2. Recommended action

**ADD 8%**

Stock trading at 19% discount to NPV, thesis intact, sizing modestly.
"""
    out = extract_recommendations_from_artifact(content_md=md)
    assert len(out) == 1
    assert out[0].kind == "add"
    assert out[0].value == pytest.approx(8.0)


def test_extracts_hold_without_size() -> None:
    md = """## 2. Recommended Action

**HOLD**

DCF at fair value, no MoS bar. Wait for the next print.
"""
    out = extract_recommendations_from_artifact(content_md=md)
    assert len(out) == 1
    assert out[0].kind == "hold"
    assert out[0].value is None


def test_extracts_hold_with_trailing_period() -> None:
    md = """## 2. Recommended Action

**HOLD.**

No MoS, thesis intact, nothing to do.
"""
    out = extract_recommendations_from_artifact(content_md=md)
    assert len(out) == 1
    assert out[0].kind == "hold"


def test_extracts_sell_when_present() -> None:
    md = """## 2. Recommended action

**SELL**

Premium has expanded past tolerance; the leading indicator turned.
"""
    out = extract_recommendations_from_artifact(content_md=md)
    assert len(out) == 1
    assert out[0].kind == "sell"
    assert out[0].value is None


def test_extracts_initiate_and_avoid() -> None:
    md_init = "## 2. Recommended Action\n\n**INITIATE 3%**\n\nFirst entry, asymmetric upside."
    md_avoid = "## 2. Recommended Action\n\n**AVOID**\n\nThesis hasn't formed yet."
    assert extract_recommendations_from_artifact(content_md=md_init)[0].kind == "initiate"
    assert extract_recommendations_from_artifact(content_md=md_avoid)[0].kind == "avoid"


def test_extracts_decimal_size() -> None:
    md = """## 2. Recommended Action

**ADD 2.5%**

Small initial position.
"""
    out = extract_recommendations_from_artifact(content_md=md)
    assert len(out) == 1
    assert out[0].value == pytest.approx(2.5)


# ---------------------------------------------------------------------------
# Section detection — varied header styles
# ---------------------------------------------------------------------------


def test_section_header_without_numbering() -> None:
    md = """## Recommended Action

**TRIM 15%**

Reasoning text.
"""
    out = extract_recommendations_from_artifact(content_md=md)
    assert len(out) == 1
    assert out[0].kind == "trim"


def test_section_header_case_insensitive() -> None:
    md = """## 2. RECOMMENDED ACTION

**ADD 10%**

Reasoning.
"""
    out = extract_recommendations_from_artifact(content_md=md)
    assert len(out) == 1
    assert out[0].kind == "add"


def test_no_recommendation_when_section_absent() -> None:
    md = """## 1. What changed
Some narrative without a recommendation section.

## 3. What would change my mind
..."""
    assert extract_recommendations_from_artifact(content_md=md) == []


def test_no_recommendation_when_body_empty() -> None:
    md = "## 2. Recommended Action\n\n\n\n## 3. Something else"
    assert extract_recommendations_from_artifact(content_md=md) == []


def test_empty_input_returns_empty() -> None:
    assert extract_recommendations_from_artifact(content_md="") == []
    assert extract_recommendations_from_artifact(content_md=None) == []


# ---------------------------------------------------------------------------
# Multi-match — first verdict wins
# ---------------------------------------------------------------------------


def test_first_recommendation_wins_when_multiple_present() -> None:
    md = """## 2. Recommended action

**ADD 8%**

Justification mentions: flip to **TRIM 20%** if Cloud growth slips.
"""
    out = extract_recommendations_from_artifact(content_md=md)
    assert len(out) == 1
    assert out[0].kind == "add"
    assert out[0].value == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# Conviction extraction
# ---------------------------------------------------------------------------


def test_conviction_high_extracted_from_asymmetric() -> None:
    md = """## 2. Recommended Action

**ADD 8%**

The asymmetric upside justifies sizing up here — the AWS backlog growth
is the cleanest leading indicator and it is accelerating.
"""
    out = extract_recommendations_from_artifact(content_md=md)
    assert out[0].conviction == "high"


def test_conviction_low_extracted_from_speculative() -> None:
    md = """## 2. Recommended Action

**INITIATE 1%**

Speculative entry — thesis is early-stage, optionality is the play.
"""
    out = extract_recommendations_from_artifact(content_md=md)
    assert out[0].conviction == "low"


def test_conviction_none_when_no_signal() -> None:
    md = """## 2. Recommended Action

**HOLD**

Fair value, nothing to do.
"""
    out = extract_recommendations_from_artifact(content_md=md)
    assert out[0].conviction is None


# ---------------------------------------------------------------------------
# Rationale excerpt — what gets captured
# ---------------------------------------------------------------------------


def test_rationale_excerpt_captures_justification_paragraph() -> None:
    md = """## 2. Recommended Action

**TRIM 20%**

Stock is at a 114% premium to DCF with no MoS bar. Trim to harvest excess;
hold the core because Cloud margin has runway.

## 3. What would change my mind
This should not be included in the excerpt.
"""
    out = extract_recommendations_from_artifact(content_md=md)
    assert "harvest excess" in out[0].rationale_excerpt
    assert "What would change my mind" not in out[0].rationale_excerpt
    assert "should not be included" not in out[0].rationale_excerpt


def test_rationale_excerpt_capped_at_512_chars() -> None:
    long_body = "Reasoning. " * 200  # ~2200 chars
    md = f"## 2. Recommended Action\n\n**ADD 5%**\n\n{long_body}"
    out = extract_recommendations_from_artifact(content_md=md)
    assert len(out[0].rationale_excerpt) <= 512


# ---------------------------------------------------------------------------
# Lens name propagation
# ---------------------------------------------------------------------------


def test_source_lens_default_is_five_min_reread() -> None:
    md = "## 2. Recommended Action\n\n**HOLD**\n\nThesis intact."
    out = extract_recommendations_from_artifact(content_md=md)
    assert out[0].source_lens == "five_min_reread"


def test_source_lens_can_be_overridden() -> None:
    md = "## 2. Recommended Action\n\n**HOLD**\n\nThesis intact."
    out = extract_recommendations_from_artifact(content_md=md, source_lens="custom_lens")
    assert out[0].source_lens == "custom_lens"


# ---------------------------------------------------------------------------
# Returned dataclass shape
# ---------------------------------------------------------------------------


def test_returns_decision_candidate_instances() -> None:
    md = "## 2. Recommended Action\n\n**TRIM 12%**\n\nReason."
    out = extract_recommendations_from_artifact(content_md=md)
    assert isinstance(out[0], DecisionCandidate)
    assert out[0].kind == "trim"
    assert out[0].value == 12.0
