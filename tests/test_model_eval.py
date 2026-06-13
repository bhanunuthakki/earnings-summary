# pyright: reportPrivateUsage=false
"""Tests for the model-downgrade eval: cost ladder + engine.

The LLM-touching paths (run_model, judge_case) are monkeypatched — the suite
never spends. Coverage:
  * model_ladder: cost ranking, is_cheaper, cheaper_candidates (Gemini cheapest,
    same-family filter), estimated_call_usd, unknown-model tolerance;
  * run_model: family -> backend dispatch (Gemini model -> backend=gemini),
    success + failure capture;
  * judge_case: the incumbent->slot-A / candidate->slot-B mapping;
  * decide_switch: SWITCH_DOWN / KEEP_INCUMBENT / HOLD / INSUFFICIENT_DATA.
"""

from __future__ import annotations

import pytest

from llm import model_eval, model_ladder
from llm.model_eval import (
    HOLD,
    INSUFFICIENT_DATA,
    KEEP_INCUMBENT,
    SWITCH_DOWN,
    PromptCase,
    decide_switch,
)

HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-6"
OPUS = "claude-opus-4-8"
GFLASH = "gemini-3.5-flash"
GPRO = "gemini-3.1-pro-preview"


# ---------------------------------------------------------------------------
# Cost ladder


def test_rank_ordering() -> None:
    # Ladder (cheapest first): Gemini Flash < Haiku < Gemini Pro < Sonnet < Opus.
    gflash_rank = model_ladder.model_rank(GFLASH)
    gpro_rank = model_ladder.model_rank(GPRO)
    haiku_rank = model_ladder.model_rank(HAIKU)
    sonnet_rank = model_ladder.model_rank(SONNET)
    opus_rank = model_ladder.model_rank(OPUS)
    assert None not in (gflash_rank, gpro_rank, haiku_rank, sonnet_rank, opus_rank)
    assert gflash_rank < haiku_rank  # type: ignore[operator]
    assert haiku_rank < gpro_rank  # type: ignore[operator]
    assert gpro_rank < sonnet_rank  # type: ignore[operator]
    assert sonnet_rank < opus_rank  # type: ignore[operator]


def test_is_cheaper() -> None:
    assert model_ladder.is_cheaper(HAIKU, SONNET) is True
    assert model_ladder.is_cheaper(SONNET, HAIKU) is False
    assert model_ladder.is_cheaper(GFLASH, OPUS) is True
    assert model_ladder.is_cheaper(SONNET, SONNET) is False  # same cost is not cheaper


def test_is_cheaper_unknown_model() -> None:
    assert model_ladder.is_cheaper("made-up-model", SONNET) is False
    assert model_ladder.is_cheaper(HAIKU, "made-up-model") is False


def test_cheaper_candidates_sonnet() -> None:
    cands = model_ladder.cheaper_candidates(SONNET)
    assert set(cands) == {GFLASH, GPRO, HAIKU}
    # Cheapest-first: Flash < Haiku < Pro (all cheaper than Sonnet)
    assert cands[0] == GFLASH
    assert cands[-1] == GPRO  # priciest of the three cheaper options
    assert OPUS not in cands  # opus is dearer, not a downgrade


def test_cheaper_candidates_haiku_has_only_gemini() -> None:
    cands = model_ladder.cheaper_candidates(HAIKU)
    assert set(cands) == {GFLASH}  # only Gemini Flash is cheaper than Haiku; Pro > Haiku
    assert model_ladder.cheaper_candidates(HAIKU, include_gemini=False) == []


def test_cheaper_candidates_opus_same_family() -> None:
    assert model_ladder.cheaper_candidates(OPUS, include_gemini=False) == [HAIKU, SONNET]


def test_cheaper_candidates_unknown_incumbent() -> None:
    assert model_ladder.cheaper_candidates("made-up-model") == []


def test_estimated_call_usd() -> None:
    # Sonnet: 25k in @ $3/M + 5k out @ $15/M = 0.075 + 0.075 = 0.15.
    assert model_ladder.estimated_call_usd(SONNET, 25_000, 5_000) == pytest.approx(0.15)
    # Gemini Pro: 25k in @ $1.25/M + 5k out @ $10/M = 0.03125 + 0.05 = 0.08125.
    assert model_ladder.estimated_call_usd(GPRO, 25_000, 5_000) == pytest.approx(0.08125)
    assert model_ladder.estimated_call_usd("unknown", 1, 1) == 0.0
    assert model_ladder.estimated_call_usd(None, 1, 1) == 0.0


# ---------------------------------------------------------------------------
# run_model dispatch


def test_run_model_dispatches_gemini_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def _fake(prompt: str, **kw: object) -> str:
        seen.update(kw)
        return "answer"

    monkeypatch.setattr(model_eval, "call_llm", _fake)
    res = model_eval.run_model("p", model_id=GPRO, purpose="bear_case")
    assert res.ok and res.response == "answer"
    assert seen["backend"] == "gemini"  # Gemini family -> gemini backend
    assert seen["model"] == GPRO
    assert seen["force_budget_bypass"] is True  # measurement, never throttled


def test_run_model_dispatches_claude_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def _fake(prompt: str, **kw: object) -> str:
        seen.update(kw)
        return "answer"

    monkeypatch.setattr(model_eval, "call_llm", _fake)
    res = model_eval.run_model("p", model_id=HAIKU, purpose="bear_case")
    assert res.ok
    assert seen["backend"] == "claude"
    assert seen["scope"] == "model_eval"


def test_run_model_records_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(prompt: str, **kw: object) -> str:
        raise RuntimeError("model down")

    monkeypatch.setattr(model_eval, "call_llm", _boom)
    res = model_eval.run_model("p", model_id=HAIKU, purpose="bear_case")
    assert res.ok is False
    assert res.error is not None and "model down" in res.error


# ---------------------------------------------------------------------------
# judge_case slot mapping


def test_judge_case_slot_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _fake_judge(**kw: object) -> object:
        captured.update(kw)
        # Return a minimal object; the wrapper just passes it through.
        return "VERDICT"

    monkeypatch.setattr(model_eval, "judge_pair", _fake_judge)
    case = PromptCase(label="c1", prompt="TASK", ticker="NU", incumbent_response="INC")
    out = model_eval.judge_case(case, "CAND", purpose="bear_case", judge_backend="claude")
    assert out == "VERDICT"
    assert captured["claude_response"] == "INC"  # incumbent -> slot A
    assert captured["gemini_response"] == "CAND"  # candidate -> slot B
    assert captured["judge_backend"] == "claude"


# ---------------------------------------------------------------------------
# decide_switch


def test_decide_switch_down() -> None:
    # Candidate wins-or-ties everything, both judges, agreement high.
    v = decide_switch(
        purpose="bear_case",
        incumbent=SONNET,
        candidate=HAIKU,
        per_judge={"claude": (4, 0, 0), "gemini": (3, 0, 1)},
        judge_agreement=1.0,
        min_n=4,
        parity_threshold=0.8,
    )
    assert v.recommendation == SWITCH_DOWN
    assert v.candidate_wins == 7 and v.incumbent_wins == 0


def test_decide_keep_incumbent_on_majority() -> None:
    # One judge has the incumbent winning a majority -> never downgrade.
    v = decide_switch(
        purpose="bear_case",
        incumbent=SONNET,
        candidate=HAIKU,
        per_judge={"claude": (1, 3, 0), "gemini": (2, 2, 0)},
        judge_agreement=0.5,
        min_n=4,
        parity_threshold=0.8,
    )
    assert v.recommendation == KEEP_INCUMBENT


def test_decide_hold_low_agreement() -> None:
    v = decide_switch(
        purpose="bear_case",
        incumbent=SONNET,
        candidate=HAIKU,
        per_judge={"claude": (4, 0, 0), "gemini": (4, 0, 0)},
        judge_agreement=0.3,  # judges disagree case-by-case despite matching totals
        min_n=4,
        parity_threshold=0.8,
    )
    assert v.recommendation == HOLD
    assert "disagree" in v.reason


def test_decide_hold_mixed() -> None:
    v = decide_switch(
        purpose="bear_case",
        incumbent=SONNET,
        candidate=HAIKU,
        per_judge={"claude": (2, 1, 1), "gemini": (3, 1, 0)},  # claude parity 0.75 < 0.8
        judge_agreement=0.8,
        min_n=4,
        parity_threshold=0.8,
    )
    assert v.recommendation == HOLD


def test_decide_insufficient_data() -> None:
    v = decide_switch(
        purpose="bear_case",
        incumbent=SONNET,
        candidate=HAIKU,
        per_judge={"claude": (1, 0, 0), "gemini": (1, 0, 0)},
        judge_agreement=1.0,
        min_n=4,
        parity_threshold=0.8,
    )
    assert v.recommendation == INSUFFICIENT_DATA
