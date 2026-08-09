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
    CANDIDATE_ERRORED,
    HOLD,
    INSUFFICIENT_DATA,
    KEEP_INCUMBENT,
    SWITCH_DOWN,
    PromptCase,
    decide_switch,
)

HAIKU = "claude-haiku-4-5-20251001"
HAIKU_ALIAS = "claude-haiku-4-5"  # rolling alias, registered 2026-07 as an eval candidate
SONNET = "claude-sonnet-4-6"
SONNET5 = "claude-sonnet-5"  # registered 2026-07 as an eval candidate for SONNET
OPUS = "claude-opus-4-8"
GFLASH = "gemini-2.5-flash"
GFLASH3 = "gemini-3-flash-preview"
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
    assert set(cands) == {GFLASH, GFLASH3, GPRO, HAIKU, HAIKU_ALIAS, SONNET5}
    # Cheapest-first; current list pricing makes Gemini Pro the priciest
    # candidate still below the incumbent Sonnet 4.6.
    assert set(cands[:2]) == {GFLASH, GFLASH3}
    assert cands[-1] == GPRO
    assert OPUS not in cands  # opus is dearer, not a downgrade


def test_cheaper_candidates_haiku_has_only_gemini() -> None:
    cands = model_ladder.cheaper_candidates(HAIKU)
    assert set(cands) == {GFLASH, GFLASH3}  # both Flash ids cheaper than Haiku; Pro > Haiku
    assert model_ladder.cheaper_candidates(HAIKU, include_gemini=False) == []


def test_cheaper_candidates_opus_same_family() -> None:
    assert model_ladder.cheaper_candidates(OPUS, include_gemini=False) == [
        HAIKU,
        HAIKU_ALIAS,
        SONNET5,
        SONNET,
    ]


def test_cheaper_candidates_unknown_incumbent() -> None:
    assert model_ladder.cheaper_candidates("made-up-model") == []


def test_estimated_call_usd() -> None:
    # Sonnet: 25k in @ $3/M + 5k out @ $15/M = 0.075 + 0.075 = 0.15.
    assert model_ladder.estimated_call_usd(SONNET, 25_000, 5_000) == pytest.approx(0.15)
    # Gemini Pro current standard tier: $2/M input + $12/M output.
    assert model_ladder.estimated_call_usd(GPRO, 25_000, 5_000) == pytest.approx(0.11)
    # Cached input is billed at its lower rate and is not double-counted as
    # ordinary prompt input.
    assert model_ladder.estimated_call_usd(
        GPRO, 25_000, 5_000, cached_input_tokens=5_000
    ) == pytest.approx(0.101)
    # Gemini Pro switches both uncached/cached input and output rates above
    # Google's 200k prompt threshold.
    assert model_ladder.estimated_call_usd(
        GPRO, 250_000, 5_000, cached_input_tokens=50_000
    ) == pytest.approx(0.91)
    assert model_ladder.estimated_call_usd("unknown", 1, 1) == 0.0
    assert model_ladder.estimated_call_usd(None, 1, 1) == 0.0


def test_current_gemini_standard_price_constants() -> None:
    flash25 = model_ladder.MODEL_LADDER[GFLASH]
    flash3 = model_ladder.MODEL_LADDER[GFLASH3]
    pro31 = model_ladder.MODEL_LADDER[GPRO]
    assert (
        flash25.input_usd_per_mtok,
        flash25.cached_input_usd_per_mtok,
        flash25.output_usd_per_mtok,
    ) == (0.30, 0.03, 2.50)
    assert (
        flash3.input_usd_per_mtok,
        flash3.cached_input_usd_per_mtok,
        flash3.output_usd_per_mtok,
    ) == (0.50, 0.05, 3.00)
    assert (
        pro31.input_usd_per_mtok,
        pro31.cached_input_usd_per_mtok,
        pro31.output_usd_per_mtok,
    ) == (2.00, 0.20, 12.00)
    assert (
        pro31.long_context_threshold_tokens,
        pro31.long_input_usd_per_mtok,
        pro31.long_cached_input_usd_per_mtok,
        pro31.long_output_usd_per_mtok,
    ) == (200_000, 4.00, 0.40, 18.00)


def test_current_dated_haiku_45_price_constants() -> None:
    haiku = model_ladder.MODEL_LADDER[HAIKU]
    assert (haiku.input_usd_per_mtok, haiku.output_usd_per_mtok) == (1.00, 5.00)


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


def test_decide_candidate_errored_overrides_quality_tallies() -> None:
    """A candidate that failed operationally on most cases gets CANDIDATE_ERRORED,
    not KEEP_INCUMBENT — errored cases were booked as incumbent wins, so the
    tallies alone would (wrongly) read as a measured quality loss. This is the
    2026-06-28 sweep failure mode: Gemini CLI erroring 60-100% of runs, every
    Gemini candidate recorded at parity=0.0 across every purpose."""
    v = decide_switch(
        purpose="qa_topics",
        incumbent=SONNET,
        candidate="gemini-3-flash-preview",
        # every "incumbent win" here is actually an errored case
        per_judge={"claude": (0, 4, 0), "gemini": (0, 4, 0)},
        judge_agreement=1.0,
        min_n=4,
        parity_threshold=0.8,
        n_cases_attempted=4,
        n_candidate_errors=4,
    )
    assert v.recommendation == CANDIDATE_ERRORED
    assert v.candidate_error_rate == 1.0
    assert v.n_candidate_errors == 4
    assert "infrastructure" in v.reason


def test_decide_errors_below_threshold_keep_normal_flow() -> None:
    """One error out of four attempted cases stays below the 0.5 threshold —
    the normal quality gate applies (and the error still counted as an
    incumbent win inside the tallies)."""
    v = decide_switch(
        purpose="qa_topics",
        incumbent=SONNET,
        candidate=HAIKU,
        per_judge={"claude": (3, 1, 0), "gemini": (3, 1, 0)},
        judge_agreement=1.0,
        min_n=4,
        parity_threshold=0.8,
        n_cases_attempted=4,
        n_candidate_errors=1,
    )
    assert v.recommendation != CANDIDATE_ERRORED
    assert v.n_candidate_errors == 1
    assert v.candidate_error_rate == 0.25


def test_decide_no_error_tracking_is_backward_compatible() -> None:
    """Callers that don't pass the error params (older paths) get the
    pre-existing behavior unchanged."""
    v = decide_switch(
        purpose="bear_case",
        incumbent=SONNET,
        candidate=HAIKU,
        per_judge={"claude": (0, 4, 0), "gemini": (0, 4, 0)},
        judge_agreement=1.0,
        min_n=4,
        parity_threshold=0.8,
    )
    assert v.recommendation == KEEP_INCUMBENT
    assert v.n_candidate_errors == 0
    assert v.candidate_error_rate == 0.0
