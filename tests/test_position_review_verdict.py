"""Tests for the Slice 2 governed verdict layer.

The behavioral guardrail is the crux — it deterministically enforces the owner's
"never trim an intact, non-oversized, non-overvalued name on price alone" rule
regardless of what the LLM returns — so every branch of it is pinned here. Output
validation, the encode-first degrade path, and the review_position orchestration
(with the LLM stubbed) round it out. No real LLM or DB.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from advisor import position_review as pr
from advisor.position_review import (
    PreAnalysis,
    VerdictOutput,
    apply_behavioral_guard,
    encode_first_output,
    parse_verdict_output,
    review_position,
)
from advisor.store import MEMO_KINDS

_PRE_DEFAULT = PreAnalysis(
    ticker="RBRK",
    weight_pct=10.0,
    weight_source="live",
    market_value_usd=150_000.0,
    unrealized_pnl_usd=60_000.0,
    target_weight_pct=None,
    target_band=None,
    weight_vs_band="no_band",
    conviction_1_5=None,
    concentration_flag=False,
    thesis_present=True,
    verdict_label="Intact",
    key_driver="Net new subscription ARR cadence",
    break_rule_status="intact",
    tripped_rules=(),
    dcf_gap_pct=-10.0,
    npv_per_share=91.09,
    dcf_live_price=80.35,
    dcf_date="2026-06-15",
    at_price=82.0,
    mos_bar=0.30,
    valuation_verdict="fair",
    conviction_encoded=True,
    has_stance=True,
    has_decision_note=True,
    is_index_instrument=False,
)

_OUT_DEFAULT = VerdictOutput(
    verdict="trim",
    size="trim ~2pp",
    reason="it has run hard and looks expensive",
    confidence="medium",
    behavioral_check="n/a",
    suggested_expression="trim-to-target",
)


def _pre(**overrides: object) -> PreAnalysis:
    return replace(_PRE_DEFAULT, **overrides)


def _out(**overrides: object) -> VerdictOutput:
    return replace(_OUT_DEFAULT, **overrides)


# --------------------------------------------------------------------------- #
# parse_verdict_output
# --------------------------------------------------------------------------- #


def test_parse_verdict_output_valid() -> None:
    out = parse_verdict_output(
        {
            "verdict": "Hold",  # case-insensitive
            "size": "none",
            "reason": "framework intact",
            "confidence": "high",
            "behavioral_check": "sell-winners-too-early",
            "suggested_expression": "do-nothing",
        }
    )
    assert out.verdict == "hold"
    assert out.confidence == "high"
    assert out.suggested_expression == "do-nothing"
    assert out.verdict_source == "llm"


def test_parse_verdict_output_rejects_out_of_vocab_verdict() -> None:
    with pytest.raises(ValueError, match="not in"):
        parse_verdict_output(
            {
                "verdict": "yolo",
                "size": "x",
                "reason": "x",
                "confidence": "high",
                "behavioral_check": "x",
                "suggested_expression": "do-nothing",
            }
        )


def test_parse_verdict_output_coerces_soft_fields() -> None:
    out = parse_verdict_output(
        {
            "verdict": "add",
            "size": "1%",
            "reason": "catalyst + floor",
            "confidence": "banana",  # bad -> low
            "behavioral_check": "catalyst test",
            "suggested_expression": "moon",  # bad -> do-nothing
        }
    )
    assert out.confidence == "low"
    assert out.suggested_expression == "do-nothing"


def test_parse_verdict_output_raises_on_empty_required_string() -> None:
    with pytest.raises(ValueError, match="reason"):
        parse_verdict_output(
            {
                "verdict": "hold",
                "size": "none",
                "reason": "   ",
                "confidence": "high",
                "behavioral_check": "x",
                "suggested_expression": "do-nothing",
            }
        )


# --------------------------------------------------------------------------- #
# apply_behavioral_guard — the crux
# --------------------------------------------------------------------------- #


def test_guard_overrides_price_only_trim_on_intact_in_band_name() -> None:
    pre = _pre(weight_vs_band="in_band", target_weight_pct=10.0, concentration_flag=False)
    guarded = apply_behavioral_guard(pre, _out(verdict="trim"))
    assert guarded.verdict == "hold"
    assert guarded.verdict_source == "guard_override"
    assert guarded.suggested_expression == "do-nothing"


def test_guard_overrides_price_only_trim_when_no_band_and_not_concentrated() -> None:
    pre = _pre(weight_vs_band="no_band", concentration_flag=False, weight_pct=5.0)
    guarded = apply_behavioral_guard(pre, _out(verdict="sell"))
    assert guarded.verdict == "hold"
    assert guarded.verdict_source == "guard_override"


def test_guard_allows_trim_when_concentrated_sizing_reason() -> None:
    # RBRK case: intact + fair + no target band, but a flagged single-name
    # concentration IS a legitimate (sizing) reason to trim — not overridden.
    pre = _pre(weight_vs_band="no_band", concentration_flag=True, weight_pct=10.0)
    guarded = apply_behavioral_guard(pre, _out(verdict="trim"))
    assert guarded.verdict == "trim"
    assert guarded.verdict_source == "llm"


def test_guard_allows_trim_when_above_target_band() -> None:
    pre = _pre(weight_vs_band="above_band", target_weight_pct=6.0, weight_pct=10.0)
    guarded = apply_behavioral_guard(pre, _out(verdict="trim"))
    assert guarded.verdict == "trim"


def test_guard_allows_trim_when_overvalued() -> None:
    pre = _pre(valuation_verdict="sell", dcf_gap_pct=25.0, weight_vs_band="in_band")
    guarded = apply_behavioral_guard(pre, _out(verdict="trim"))
    assert guarded.verdict == "trim"  # valuation ladder justifies it


def test_guard_allows_trim_when_thesis_breached() -> None:
    pre = _pre(break_rule_status="breach", tripped_rules=("nrr_below_115",))
    guarded = apply_behavioral_guard(pre, _out(verdict="sell"))
    assert guarded.verdict == "sell"


def test_guard_leaves_non_trim_verdicts_untouched() -> None:
    for verdict in ("hold", "buy", "add"):
        pre = _pre()
        guarded = apply_behavioral_guard(pre, _out(verdict=verdict))
        assert guarded.verdict == verdict
        assert guarded.verdict_source == "llm"


# --------------------------------------------------------------------------- #
# encode_first_output
# --------------------------------------------------------------------------- #


def test_encode_first_output_for_index_instrument() -> None:
    pre = _pre(thesis_present=False, is_index_instrument=True, verdict_label=None)
    out = encode_first_output(pre)
    assert out.verdict == "hold"
    assert out.confidence == "low"
    assert out.suggested_expression == "encode-thesis-first"
    assert out.verdict_source == "encode_first"
    assert "index instrument" in out.reason


# --------------------------------------------------------------------------- #
# memo kind registration
# --------------------------------------------------------------------------- #


def test_position_review_is_a_registered_memo_kind() -> None:
    assert "position_review" in MEMO_KINDS


# --------------------------------------------------------------------------- #
# review_position orchestration (LLM + build_pre_analysis stubbed)
# --------------------------------------------------------------------------- #


def test_review_position_short_circuits_unencoded_without_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_pre(*_a: object, **_k: object) -> PreAnalysis:
        return _pre(thesis_present=False)

    def _boom(*_a: object, **_k: object) -> object:  # must NOT be called
        raise AssertionError("LLM must not be called for an unencoded name")

    import llm.structured

    monkeypatch.setattr(pr, "build_pre_analysis", _fake_pre)
    monkeypatch.setattr(llm.structured, "call_llm_structured", _boom)
    review = review_position(Path("."), "FLKR", persist=False)
    assert review.output.verdict_source == "encode_first"
    assert review.output.suggested_expression == "encode-thesis-first"
    assert review.memo_id is None


def test_review_position_applies_guard_to_llm_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    price_only_trim: dict[str, str] = {
        "verdict": "trim",
        "size": "trim 2pp",
        "reason": "it's up 60% and feels toppy",
        "confidence": "medium",
        "behavioral_check": "none",
        "suggested_expression": "trim-to-target",
    }

    def _fake_pre(*_a: object, **_k: object) -> PreAnalysis:
        return _pre(weight_vs_band="in_band", concentration_flag=False)

    def _fake_block(*_a: object, **_k: object) -> str:
        return ""

    def _fake_llm(*_a: object, **_k: object) -> object:
        return price_only_trim

    import llm.structured

    monkeypatch.setattr(pr, "build_pre_analysis", _fake_pre)
    monkeypatch.setattr(pr, "_convictions_block", _fake_block)
    monkeypatch.setattr(llm.structured, "call_llm_structured", _fake_llm)
    review = review_position(Path("."), "RBRK", persist=False)
    # The guard must flip the price-only trim on an intact, in-band name to hold.
    assert review.output.verdict == "hold"
    assert review.output.verdict_source == "guard_override"
