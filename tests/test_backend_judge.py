# pyright: reportPrivateUsage=false
#
# These tests reach module-private surface (_side_to_backend, _recommend,
# _judge_once) — that IS the unit under test. Module-scoped directive per the
# repo's cli.py / test_gemini_backend.py precedent.
"""Tests for the pairwise Claude-vs-Gemini backend judge (src/llm/backend_judge.py).

Every test monkeypatches ``call_llm`` — the suite never spawns a real CLI and
never spends. Coverage:

  * fail-closed verdict parsing (fence-strip, every malformed shape -> None);
  * position-space -> backend-space mapping;
  * judge_pair position-swap: consistent win, a position flip -> non-robust tie,
    a failed/unparseable pass -> fail-closed tie with the error preserved;
  * facet consolidation across the swap;
  * aggregation tallies + the advisory recommendation thresholds
    (PROMOTE_CANDIDATE / HOLD / REJECT / INSUFFICIENT_DATA) + signed margin;
  * cross-judge agreement;
  * corpus-record reduction (both-ok vs a failed backend -> skip);
  * the brand-blind prompt + purpose registration + package re-exports.
"""

from __future__ import annotations

import json

import pytest

from llm import backend_judge
from llm.backend_judge import (
    CLAUDE,
    FACETS,
    GEMINI,
    HOLD,
    INSUFFICIENT_DATA,
    JUDGE_PURPOSE,
    PROMOTE_CANDIDATE,
    REJECT,
    JudgedPair,
    aggregate_by_purpose,
    build_judge_prompt,
    cross_judge_agreement,
    gradable_from_record,
    judge_pair,
    parse_pair_verdict,
)

# ---------------------------------------------------------------------------
# Helpers


def _verdict_json(
    winner: str,
    *,
    margin: float = 0.5,
    faithfulness: str = "tie",
    accuracy: str = "tie",
    fmt: str = "tie",
    conciseness: str = "tie",
    rationale: str = "the deciding difference",
) -> str:
    return json.dumps(
        {
            "winner": winner,
            "margin": margin,
            "faithfulness": faithfulness,
            "accuracy": accuracy,
            "format": fmt,
            "conciseness": conciseness,
            "rationale": rationale,
        }
    )


def _queue_calls(monkeypatch: pytest.MonkeyPatch, responses: list[str]) -> dict[str, int]:
    """Install a call_llm that pops queued responses in order; returns a counter
    dict so a test can assert exactly how many judge calls fired."""
    queue = list(responses)
    counter = {"n": 0}

    def _fake(
        prompt: str,
        *,
        purpose: str | None = None,
        scope: str | None = None,
        run_id: str | None = None,
        backend: str | None = None,
    ) -> str:
        counter["n"] += 1
        return queue.pop(0)

    monkeypatch.setattr(backend_judge, "call_llm", _fake)
    return counter


def _raising_call(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    def _fake(
        prompt: str,
        *,
        purpose: str | None = None,
        scope: str | None = None,
        run_id: str | None = None,
        backend: str | None = None,
    ) -> str:
        raise exc

    monkeypatch.setattr(backend_judge, "call_llm", _fake)


def _pair(
    *,
    winner: str,
    margin: float = 0.5,
    judge: str = CLAUDE,
    purpose: str = "viewspec_compile",
    label: str = "c1",
    consistent: bool = True,
    facets: dict[str, str] | None = None,
) -> JudgedPair:
    return JudgedPair(
        purpose=purpose,
        label=label,
        ticker=None,
        judge_backend=judge,
        judge_model="m",
        winner=winner,
        margin=margin,
        facet_winners=facets or {f: "tie" for f in FACETS},
        position_consistent=consistent,
        rationales=["a", "b"],
    )


# ---------------------------------------------------------------------------
# Verdict parsing — fail closed


def test_parse_valid_verdict() -> None:
    v = parse_pair_verdict(_verdict_json("A", margin=0.7, accuracy="A"))
    assert v is not None
    assert v.winner == "A"
    assert v.margin == pytest.approx(0.7)
    assert v.facets["accuracy"] == "A"
    assert v.rationale


def test_parse_strips_fences() -> None:
    raw = "```json\n" + _verdict_json("B") + "\n```"
    v = parse_pair_verdict(raw)
    assert v is not None and v.winner == "B"


def test_parse_clamps_margin() -> None:
    hi = parse_pair_verdict(_verdict_json("A", margin=9.0))
    lo = parse_pair_verdict(_verdict_json("A", margin=-3.0))
    assert hi is not None and hi.margin == 1.0
    assert lo is not None and lo.margin == 0.0


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        json.dumps(["a", "list"]),
        json.dumps(
            {
                "winner": "C",
                "margin": 0.5,
                "faithfulness": "tie",
                "accuracy": "tie",
                "format": "tie",
                "conciseness": "tie",
                "rationale": "x",
            }
        ),  # bad winner
        json.dumps(
            {
                "winner": "A",
                "margin": True,
                "faithfulness": "tie",
                "accuracy": "tie",
                "format": "tie",
                "conciseness": "tie",
                "rationale": "x",
            }
        ),  # margin bool
        json.dumps(
            {
                "winner": "A",
                "margin": 0.5,
                "faithfulness": "tie",
                "accuracy": "tie",
                "format": "tie",
                "conciseness": "tie",
            }
        ),  # missing rationale
        json.dumps(
            {
                "winner": "A",
                "margin": 0.5,
                "faithfulness": "maybe",
                "accuracy": "tie",
                "format": "tie",
                "conciseness": "tie",
                "rationale": "x",
            }
        ),  # bad facet
        json.dumps(
            {
                "winner": "A",
                "margin": 0.5,
                "faithfulness": "tie",
                "accuracy": "tie",
                "format": "tie",
                "conciseness": "tie",
                "rationale": "   ",
            }
        ),  # blank rationale
    ],
)
def test_parse_fails_closed(raw: str) -> None:
    assert parse_pair_verdict(raw) is None


def test_parse_missing_facet_fails() -> None:
    obj = {
        "winner": "A",
        "margin": 0.5,
        "faithfulness": "A",
        "accuracy": "A",
        "format": "A",
        "rationale": "x",
    }
    assert parse_pair_verdict(json.dumps(obj)) is None  # conciseness absent


# ---------------------------------------------------------------------------
# Position-space -> backend-space mapping


def test_side_to_backend_pass1() -> None:
    # Pass 1: A is Claude, B is Gemini.
    assert backend_judge._side_to_backend("A", a_is=CLAUDE, b_is=GEMINI) == CLAUDE
    assert backend_judge._side_to_backend("B", a_is=CLAUDE, b_is=GEMINI) == GEMINI
    assert backend_judge._side_to_backend("tie", a_is=CLAUDE, b_is=GEMINI) == "tie"


def test_side_to_backend_pass2() -> None:
    # Pass 2: A is Gemini, B is Claude (swapped).
    assert backend_judge._side_to_backend("A", a_is=GEMINI, b_is=CLAUDE) == GEMINI
    assert backend_judge._side_to_backend("B", a_is=GEMINI, b_is=CLAUDE) == CLAUDE


# ---------------------------------------------------------------------------
# judge_pair — the position-swap contract


def _judge(monkeypatch: pytest.MonkeyPatch, responses: list[str]) -> JudgedPair:
    _queue_calls(monkeypatch, responses)
    return judge_pair(
        purpose="viewspec_compile",
        label="c1",
        ticker="NU",
        claude_response="claude text",
        gemini_response="gemini text",
        task_prompt="the task",
        judge_backend=CLAUDE,
    )


def test_gemini_wins_consistently(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pass1 picks B (=Gemini); pass2 picks A (=Gemini). Both -> Gemini.
    jp = _judge(monkeypatch, [_verdict_json("B", margin=0.8), _verdict_json("A", margin=0.6)])
    assert jp.winner == GEMINI
    assert jp.position_consistent is True
    assert jp.margin == pytest.approx(0.7)  # mean of the two passes
    assert jp.error is None


def test_claude_wins_consistently(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pass1 picks A (=Claude); pass2 picks B (=Claude).
    jp = _judge(monkeypatch, [_verdict_json("A", margin=0.4), _verdict_json("B", margin=0.4)])
    assert jp.winner == CLAUDE
    assert jp.position_consistent is True


def test_position_flip_is_non_robust_tie(monkeypatch: pytest.MonkeyPatch) -> None:
    # The judge always picks position "A" regardless of content -> the passes
    # disagree in backend space -> a tie flagged not-consistent.
    jp = _judge(monkeypatch, [_verdict_json("A"), _verdict_json("A")])
    assert jp.winner == "tie"
    assert jp.position_consistent is False
    assert jp.margin == 0.0


def test_double_tie_is_consistent(monkeypatch: pytest.MonkeyPatch) -> None:
    jp = _judge(monkeypatch, [_verdict_json("tie"), _verdict_json("tie")])
    assert jp.winner == "tie"
    assert jp.position_consistent is True


def test_unparseable_pass_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    jp = _judge(monkeypatch, [_verdict_json("B"), "garbage not json"])
    assert jp.winner == "tie"
    assert jp.position_consistent is False
    assert jp.error is not None


def test_call_exception_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _raising_call(monkeypatch, RuntimeError("backend down"))
    jp = judge_pair(
        purpose="bear_case",
        label="c1",
        ticker=None,
        claude_response="a",
        gemini_response="b",
        task_prompt="t",
        judge_backend=GEMINI,
    )
    assert jp.winner == "tie"
    assert jp.error is not None and "backend down" in jp.error


def test_facet_consolidation(monkeypatch: pytest.MonkeyPatch) -> None:
    # accuracy: pass1 says B(=Gemini), pass2 says A(=Gemini) -> Gemini.
    # format:   pass1 says A(=Claude), pass2 says A(=Gemini)  -> disagree -> tie.
    p1 = _verdict_json("tie", accuracy="B", fmt="A")
    p2 = _verdict_json("tie", accuracy="A", fmt="A")
    jp = _judge(monkeypatch, [p1, p2])
    assert jp.facet_winners["accuracy"] == GEMINI
    assert jp.facet_winners["format"] == "tie"


def test_judge_pair_makes_two_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    counter = _queue_calls(monkeypatch, [_verdict_json("A"), _verdict_json("B")])
    judge_pair(
        purpose="p",
        label="c1",
        ticker=None,
        claude_response="a",
        gemini_response="b",
        task_prompt="t",
        judge_backend=CLAUDE,
    )
    assert counter["n"] == 2  # exactly the position swap, nothing more


# ---------------------------------------------------------------------------
# Aggregation + recommendation


def test_promote_candidate_when_gemini_parity(monkeypatch: pytest.MonkeyPatch) -> None:
    pairs = [_pair(winner=GEMINI, label=f"c{i}") for i in range(4)]
    rollups = aggregate_by_purpose(pairs)
    assert len(rollups) == 1
    r = rollups[0]
    assert r.gemini_wins == 4 and r.claude_wins == 0
    assert r.recommendation == PROMOTE_CANDIDATE


def test_reject_when_claude_majority() -> None:
    pairs = [_pair(winner=CLAUDE, label=f"c{i}") for i in range(3)]
    r = aggregate_by_purpose(pairs)[0]
    assert r.recommendation == REJECT


def test_insufficient_data_below_min_n() -> None:
    pairs = [_pair(winner=GEMINI, label="c1"), _pair(winner=GEMINI, label="c2")]
    r = aggregate_by_purpose(pairs, min_n=3)[0]
    assert r.recommendation == INSUFFICIENT_DATA


def test_hold_when_inconsistent() -> None:
    # Gemini "wins" every pair but none are position-consistent -> not robust.
    pairs = [_pair(winner=GEMINI, label=f"c{i}", consistent=False) for i in range(4)]
    r = aggregate_by_purpose(pairs)[0]
    assert r.recommendation == HOLD
    assert "robust" in r.reason


def test_hold_when_mixed_below_threshold() -> None:
    # 2 gemini, 2 tie, 0 claude across 5 -> wait, build 5: parity rate below 0.8.
    pairs = [
        _pair(winner=GEMINI, label="c1"),
        _pair(winner=CLAUDE, label="c2"),
        _pair(winner="tie", label="c3"),
        _pair(winner="tie", label="c4"),
        _pair(winner=GEMINI, label="c5"),
    ]
    # claude_wins(1) !> gemini_wins(2); win_or_tie = 4/5 = 0.8 -> meets default.
    # Tighten the bar so this lands in HOLD.
    r = aggregate_by_purpose(pairs, promote_win_or_tie_rate=0.9)[0]
    assert r.recommendation == HOLD


def test_signed_margin_direction() -> None:
    pairs = [
        _pair(winner=GEMINI, margin=0.6, label="c1"),
        _pair(winner=CLAUDE, margin=0.4, label="c2"),
        _pair(winner="tie", margin=0.0, label="c3"),
    ]
    r = aggregate_by_purpose(pairs)[0]
    # (+0.6 - 0.4 + 0.0) / 3
    assert r.signed_margin == pytest.approx((0.6 - 0.4) / 3)


def test_facet_gemini_loss_counts() -> None:
    pairs = [
        _pair(winner="tie", label="c1", facets={**{f: "tie" for f in FACETS}, "format": CLAUDE}),
        _pair(winner="tie", label="c2", facets={**{f: "tie" for f in FACETS}, "format": CLAUDE}),
    ]
    r = aggregate_by_purpose(pairs)[0]
    assert r.facet_gemini_loss["format"] == 2
    assert r.facet_gemini_loss["accuracy"] == 0


def test_aggregate_groups_by_judge() -> None:
    pairs = [
        _pair(winner=GEMINI, judge=CLAUDE, label="c1"),
        _pair(winner=GEMINI, judge=GEMINI, label="c1"),
    ]
    rollups = aggregate_by_purpose(pairs)
    assert {r.judge_backend for r in rollups} == {CLAUDE, GEMINI}


# ---------------------------------------------------------------------------
# Cross-judge agreement


def test_cross_judge_agreement_counts() -> None:
    pairs = [
        _pair(winner=GEMINI, judge=CLAUDE, label="c1"),
        _pair(winner=GEMINI, judge=GEMINI, label="c1"),  # agree
        _pair(winner=GEMINI, judge=CLAUDE, label="c2"),
        _pair(winner=CLAUDE, judge=GEMINI, label="c2"),  # disagree
    ]
    agg = cross_judge_agreement(pairs)
    assert len(agg) == 1
    c = agg[0]
    assert c.n_pairs == 2
    assert c.n_agree == 1
    assert c.agreement_rate == pytest.approx(0.5)


def test_cross_judge_skips_single_judge() -> None:
    pairs = [_pair(winner=GEMINI, judge=CLAUDE, label="c1")]
    assert cross_judge_agreement(pairs) == []


# ---------------------------------------------------------------------------
# Corpus-record reduction


def _record(claude_ok: bool, gemini_ok: bool) -> dict[str, object]:
    return {
        "purpose": "viewspec_compile",
        "label": "c1",
        "ticker": "NU",
        "prompt": "the task prompt",
        "claude": {"ok": claude_ok, "response": "claude answer" if claude_ok else None},
        "gemini": {"ok": gemini_ok, "response": "gemini answer" if gemini_ok else None},
    }


def test_gradable_both_ok() -> None:
    g = gradable_from_record(_record(True, True))
    assert g.skip_reason is None
    assert g.claude_response == "claude answer"
    assert g.gemini_response == "gemini answer"
    assert g.purpose == "viewspec_compile" and g.ticker == "NU"


def test_gradable_claude_failed() -> None:
    g = gradable_from_record(_record(False, True))
    assert g.skip_reason is not None and "claude" in g.skip_reason


def test_gradable_gemini_failed() -> None:
    g = gradable_from_record(_record(True, False))
    assert g.skip_reason is not None and "gemini" in g.skip_reason


def test_gradable_missing_backend_blocks() -> None:
    g = gradable_from_record({"purpose": "p", "label": "c1", "prompt": "x"})
    assert g.skip_reason is not None  # no claude/gemini dicts at all


# ---------------------------------------------------------------------------
# Brand-blind prompt + registration + re-exports


def test_judge_prompt_is_brand_blind() -> None:
    # Neutral response text so the check measures the TEMPLATE's framing, not the
    # responses' own words.
    prompt = build_judge_prompt("bear_case", "TASK", "alpha answer", "beta answer")
    assert "RESPONSE A" in prompt and "RESPONSE B" in prompt
    # The framing must not reveal which brand produced which side.
    lower = prompt.lower()
    assert "claude" not in lower
    assert "gemini" not in lower


def test_judge_prompt_truncates() -> None:
    big = "x" * 50_000
    prompt = build_judge_prompt("p", big, "a", "b", max_prompt_chars=100)
    assert "[truncated" in prompt
    assert len(prompt) < 5_000


def test_judge_purpose_registered() -> None:
    from llm.cli import LLM_MODELS
    from llm.gemini_backend import (
        GEMINI_BACKEND_DEFAULT_MODEL,
        GEMINI_BACKEND_FAST_MODEL,
        gemini_model_for,
    )
    from llm.prompt_versions import prompt_version_for

    assert LLM_MODELS[JUDGE_PURPOSE] == "claude-opus-4-8"  # Opus judge pin
    # Gemini side resolves to the strong (Pro) tier, NOT the fast-classifier tier.
    assert gemini_model_for(JUDGE_PURPOSE) == GEMINI_BACKEND_DEFAULT_MODEL
    assert gemini_model_for(JUDGE_PURPOSE) != GEMINI_BACKEND_FAST_MODEL
    assert prompt_version_for(JUDGE_PURPOSE) == "v1"


def test_package_reexports() -> None:
    from llm import (
        BACKEND_COMPARE_JUDGE_PURPOSE,
        aggregate_by_purpose,
        cross_judge_agreement,
        judge_pair,
    )

    assert BACKEND_COMPARE_JUDGE_PURPOSE == JUDGE_PURPOSE
    assert callable(judge_pair)
    assert callable(aggregate_by_purpose)
    assert callable(cross_judge_agreement)


def test_no_real_call_llm_in_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    # Guard: judge_pair must route through the module-level call_llm seam so the
    # suite can never spend. If someone inlines a provider call, this breaks.
    sentinel: list[str] = []

    def _fake(prompt: str, **kw: object) -> str:
        b = kw.get("backend")
        sentinel.append(b if isinstance(b, str) else "?")
        return _verdict_json("tie")

    monkeypatch.setattr(backend_judge, "call_llm", _fake)
    judge_pair(
        purpose="p",
        label="c1",
        ticker=None,
        claude_response="a",
        gemini_response="b",
        task_prompt="t",
        judge_backend=GEMINI,
    )
    assert sentinel == [GEMINI, GEMINI]  # both passes used the forced judge backend
