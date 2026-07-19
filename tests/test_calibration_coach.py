"""Calibration coach (close_the_loops L8 PR2): named biases + behavioural
experiment + the eval-gated monthly scorecard.

All LLM transport is monkeypatched (conftest enforces no real spend): the
structured-output seam ``calibration_coach.call_llm_structured`` for the
biases/experiment calls, and an injected ``judge_caller`` for the eval gate.
The deterministic substrate (cohorts, skill split) is built by hand so the
coach's grounding + min-n gate are exercised without a DB.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import calibration_coach as cc
from attribution import ConvictionAlphaRow, SkillDecomposition
from calibration_coach import (
    BehavioralExperiment,
    CalibrationScorecard,
    ClosedLesson,
    CoachInputs,
    NamedBias,
    Premortem,
    build_scorecard,
    compose_premortem,
    gate_premortem,
    gate_scorecard,
    load_latest_scorecard,
    premortem_block,
    propose_experiment,
    render_premortem_prose,
    render_scorecard_prose,
    save_scorecard,
    scorecard_from_dict,
    scorecard_to_dict,
    synthesize_biases,
)
from decision_calibration import CalibrationStats, CohortPeriod, ConvictionBucket
from decision_conditions import DecisionCondition
from llm.cli import LLMBudgetExceeded

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Hand-built grounding
# ---------------------------------------------------------------------------


def _calibration(*, graded: int) -> CalibrationStats:
    return CalibrationStats(
        total=graded + 2,
        graded=graded,
        overall_hit_rate=0.45,
        by_conviction=[
            ConvictionBucket("high", graded // 2, graded // 4, graded // 4, 0, 1, 0.4),
            ConvictionBucket("low", graded // 2, graded // 3, graded // 6, 0, 0, 0.6),
        ],
        action_mix={},
        reversals=[],
        reversals_vindicated=1,
        reversals_cost=3,
        time_to_outcome=[],
        cohorts=[
            CohortPeriod("2026-Q1", "2026Q1", 6, 6, 2, 0.33, 3, 0.3, 0.4, -0.1, 1, 1),
            CohortPeriod("2026-Q2", "2026Q2", 6, 6, 4, 0.67, 3, 0.7, 0.6, 0.1, 0, 0),
        ],
        cohort_granularity="quarter",
        hit_rate_delta=0.34,
        improving=True,
    )


def _skill() -> SkillDecomposition:
    return SkillDecomposition(
        window_start="2026-01-01",
        window_end="2026-06-30",
        n_names=8,
        total_alpha_usd=2000.0,
        selection_usd=5000.0,
        sizing_usd=-3000.0,  # the leak: under-sized the winners
        timing_usd=-500.0,
        n_timed=4,
        by_conviction=[
            ConvictionAlphaRow("high", 3, 4000.0, -2500.0, 4.7),
            ConvictionAlphaRow("low", 5, -2000.0, -500.0, 1.8),
        ],
        top_contributors=[],
        excluded_no_value=0,
        confident=False,
        notes=[],
    )


def _inputs(*, graded: int = 12, with_skill: bool = True) -> CoachInputs:
    return CoachInputs(
        calibration=_calibration(graded=graded),
        skill=_skill() if with_skill else None,
        closed_lessons=[
            ClosedLesson(
                "RBRK", "high", "broke", "thesis cracked on guidance", "sized up too fast"
            ),
            ClosedLesson("WIX", "medium", "played_out", None, "patience paid"),
        ],
        n_graded=graded,
    )


def _bias_json(n: int = 2) -> str:
    biases = [
        {
            "name": f"bias {i}",
            "pattern": f"a recurring pattern number {i}",
            "evidence": [f"high-conviction calls graded 40% (fact {i})"],
            "tell": "an add above your median weight at 5/5 conviction",
        }
        for i in range(n)
    ]
    return json.dumps({"biases": biases})


def _experiment_json() -> str:
    return json.dumps(
        {
            "hypothesis": "size new adds at most 1.2x median until the thesis prints",
            "rationale": "sizing is your biggest realized leak",
            "condition": {
                "metric": "sizing contribution",
                "metric_source": None,
                "op": "gt",
                "threshold": 0.0,
                "unit": "actual",
                "for_periods": 1,
                "note": "realized sizing contribution turns positive next period",
            },
        }
    )


# ---------------------------------------------------------------------------
# min-n gate
# ---------------------------------------------------------------------------


def test_can_coach_gate() -> None:
    assert _inputs(graded=12).can_coach is True
    assert _inputs(graded=9).can_coach is False
    assert _inputs(graded=0).can_coach is False


def test_gather_coach_inputs_degrades_on_empty_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Real DB-reading path (the other tests monkeypatch gather_coach_inputs):
    # an empty repo + offline tracker degrades to can_coach=False, no crash.
    from integrations.portfolio_tracker_client import PortfolioAnalytics

    monkeypatch.setattr(
        cc,
        "fetch_portfolio_analytics",
        lambda **_k: PortfolioAnalytics(available=False, api_url="http://x"),
    )
    (tmp_path / "data").mkdir()
    inputs = cc.gather_coach_inputs(tmp_path)
    assert inputs.can_coach is False
    assert inputs.n_graded == 0
    assert inputs.skill is None and inputs.closed_lessons == []


def test_grounding_block_is_first_person() -> None:
    block = cc.grounding_block(_inputs())
    assert "BY CONVICTION" in block and "high:" in block
    assert "REALIZED SKILL" in block and "sizing" in block
    assert "CLOSED POSITIONS" in block and "RBRK" in block
    assert "sized up too fast" in block


# ---------------------------------------------------------------------------
# named biases
# ---------------------------------------------------------------------------


def test_synthesize_biases_thin_ledger_skips_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def _boom(*_a: object, **_k: object) -> object:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(cc, "call_llm_structured", _boom)
    assert synthesize_biases(_inputs(graded=5)) == []
    assert called is False  # below the floor → no LLM call at all


def test_synthesize_biases_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cc, "call_llm_structured", lambda *_a, **_k: json.loads(_bias_json(3)))
    biases = synthesize_biases(_inputs())
    assert len(biases) == 3
    assert all(b.name and b.pattern and b.evidence for b in biases)
    assert biases[0].tell


def test_synthesize_biases_drops_unevidenced(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "biases": [
            {"name": "grounded", "pattern": "p", "evidence": ["a fact"], "tell": "t"},
            {"name": "ungrounded", "pattern": "generic wisdom", "evidence": [], "tell": "t"},
        ]
    }
    monkeypatch.setattr(cc, "call_llm_structured", lambda *_a, **_k: payload)
    biases = synthesize_biases(_inputs())
    assert [b.name for b in biases] == ["grounded"]  # the no-evidence bias is rejected


def test_synthesize_biases_transient_failure_defers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient LLM failure must DEFER the scorecard, never persist as a
    confident "no biases" — the 2026-07 incident (program review 2026-07-19)
    degraded a quota-dead-window CalledProcessError to [] and saved it."""

    def _raise(*_a: object, **_k: object) -> object:
        raise RuntimeError("transient")

    monkeypatch.setattr(cc, "call_llm_structured", _raise)
    with pytest.raises(cc.TransientCoachError):
        synthesize_biases(_inputs())


def test_synthesize_biases_hard_stop_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_a: object, **_k: object) -> object:
        raise LLMBudgetExceeded("cap")

    monkeypatch.setattr(cc, "call_llm_structured", _raise)
    with pytest.raises(LLMBudgetExceeded):
        synthesize_biases(_inputs())


# ---------------------------------------------------------------------------
# behavioural experiment
# ---------------------------------------------------------------------------


def test_propose_experiment_thin_ledger_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cc, "call_llm_structured", lambda *_a, **_k: {})
    assert propose_experiment(_inputs(graded=4), []) is None


def test_propose_experiment_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cc, "call_llm_structured", lambda *_a, **_k: json.loads(_experiment_json()))
    exp = propose_experiment(_inputs(), [NamedBias("b", "p", ["e"], "t")])
    assert exp is not None
    assert "size new adds" in exp.hypothesis
    assert exp.condition is not None and exp.condition.op == "gt"


def test_propose_experiment_malformed_condition_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"hypothesis": "do the thing", "rationale": "why", "condition": {"garbage": 1}}
    monkeypatch.setattr(cc, "call_llm_structured", lambda *_a, **_k: payload)
    exp = propose_experiment(_inputs(), [])
    assert exp is not None
    assert exp.hypothesis == "do the thing"
    assert exp.condition is None  # invalid condition → hypothesis survives, no gradeable test


# ---------------------------------------------------------------------------
# pre-mortem at the decision moment (L8 item d)
# ---------------------------------------------------------------------------


def _premortem_json(*, resemblances: int = 2, conditions: int = 1) -> str:
    return json.dumps(
        {
            "resemblances": [
                f"like RBRK, you are sizing this up on conviction before the thesis prints ({i})"
                for i in range(resemblances)
            ],
            "conditions": [
                {
                    "metric": "NPL",
                    "metric_source": "kpi",
                    "op": "gt",
                    "threshold": 7.0,
                    "unit": "percent",
                    "for_periods": 2,
                    "note": "NPL above 7% for two straight quarters",
                }
                for _ in range(conditions)
            ],
        }
    )


def test_compose_premortem_thin_skips_llm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("must not call the LLM below the floor")

    monkeypatch.setattr(cc, "call_llm_structured", _boom)
    assert compose_premortem(tmp_path, "NU", inputs=_inputs(graded=5)) is None


def test_compose_premortem_happy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cc, "call_llm_structured", lambda *_a, **_k: json.loads(_premortem_json()))
    pm = compose_premortem(tmp_path, "nu", stance="add", inputs=_inputs())
    assert pm is not None
    assert pm.ticker == "NU" and pm.stance == "add"
    assert len(pm.resemblances) == 2
    assert len(pm.drafted_conditions) == 1
    assert pm.drafted_conditions[0].metric == "NPL"


def test_compose_premortem_no_resemblance_is_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = {"resemblances": [], "conditions": []}
    monkeypatch.setattr(cc, "call_llm_structured", lambda *_a, **_k: payload)
    # A pre-mortem with no grounded parallel is rejected (the failure mode we guard).
    assert compose_premortem(tmp_path, "NU", inputs=_inputs()) is None


def test_compose_premortem_keeps_resemblances_drops_bad_conditions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = {"resemblances": ["a grounded parallel to RBRK"], "conditions": [{"garbage": 1}]}
    monkeypatch.setattr(cc, "call_llm_structured", lambda *_a, **_k: payload)
    pm = compose_premortem(tmp_path, "NU", inputs=_inputs())
    assert pm is not None
    assert pm.resemblances == ["a grounded parallel to RBRK"]
    assert pm.drafted_conditions == []  # malformed condition dropped, resemblance survives


def test_compose_premortem_hard_stop_propagates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _raise(*_a: object, **_k: object) -> object:
        raise LLMBudgetExceeded("cap")

    monkeypatch.setattr(cc, "call_llm_structured", _raise)
    with pytest.raises(LLMBudgetExceeded):
        compose_premortem(tmp_path, "NU", inputs=_inputs())


def test_render_premortem_prose() -> None:
    pm = Premortem(
        ticker="NU",
        stance="add",
        resemblances=["like RBRK, sizing up before the print"],
        drafted_conditions=[
            DecisionCondition("NPL", "kpi", "gt", 7.0, "percent", 2, "NPL above 7% for 2Q")
        ],
    )
    prose = render_premortem_prose(pm)
    assert "Pre-mortem" in prose and "NU" in prose
    assert "like RBRK" in prose
    assert "NPL above 7% for 2Q" in prose


def _premortem() -> Premortem:
    return Premortem("NU", "add", ["a grounded parallel"], [])


def test_gate_premortem_pass_and_fail() -> None:
    ok, score = gate_premortem(
        _premortem(), code_root=PROJECT_ROOT, judge_caller=lambda *_a, **_k: _verdict(1.0)
    )
    assert ok is True and score == pytest.approx(1.0)
    bad_ok, bad_score = gate_premortem(
        _premortem(), code_root=PROJECT_ROOT, judge_caller=lambda *_a, **_k: _verdict(0.0)
    )
    assert bad_ok is False and bad_score == pytest.approx(0.0)


def test_premortem_block_composed_and_gated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cc, "compose_premortem", lambda *_a, **_k: _premortem())
    monkeypatch.setattr(cc, "gate_premortem", lambda *_a, **_k: (True, 0.9))
    block = premortem_block(tmp_path, "NU", stance="add", code_root=tmp_path)
    assert "Pre-mortem" in block and "a grounded parallel" in block
    assert "ADD to NU" in block


def test_premortem_block_empty_when_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cc, "compose_premortem", lambda *_a, **_k: None)
    assert premortem_block(tmp_path, "NU") == ""


def test_premortem_block_suppressed_on_gate_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cc, "compose_premortem", lambda *_a, **_k: _premortem())
    monkeypatch.setattr(cc, "gate_premortem", lambda *_a, **_k: (False, 0.3))
    assert premortem_block(tmp_path, "NU", code_root=tmp_path) == ""


def test_premortem_block_ungated_when_no_code_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cc, "compose_premortem", lambda *_a, **_k: _premortem())

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("no code_root → no gate call")

    monkeypatch.setattr(cc, "gate_premortem", _boom)
    block = premortem_block(tmp_path, "NU")  # no code_root → composed, not gated
    assert "a grounded parallel" in block


# ---------------------------------------------------------------------------
# scorecard build + eval gate
# ---------------------------------------------------------------------------


def test_build_scorecard_thin_is_substrate_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cc, "gather_coach_inputs", lambda *_a, **_k: _inputs(graded=5))

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("must not call the LLM below the floor")

    monkeypatch.setattr(cc, "call_llm_structured", _boom)
    card = build_scorecard(PROJECT_ROOT, period="2026-06", generated_at="2026-06-14T00:00:00")
    assert card.can_coach is False
    assert card.biases == [] and card.experiment is None
    assert any("too thin to coach" in n for n in card.notes)
    # The substrate still rode through.
    assert card.improving is True and card.sizing_usd == pytest.approx(-3000.0)


def test_build_scorecard_synthesises_when_thick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cc, "gather_coach_inputs", lambda *_a, **_k: _inputs(graded=12))
    calls: list[str] = []

    def _structured(prompt: str, **_k: object) -> object:
        calls.append(prompt)
        return json.loads(_experiment_json() if "EXPERIMENT" in prompt else _bias_json(2))

    monkeypatch.setattr(cc, "call_llm_structured", _structured)
    card = build_scorecard(PROJECT_ROOT, period="2026-06", generated_at="2026-06-14T00:00:00")
    assert card.can_coach is True
    assert len(card.biases) == 2
    assert card.experiment is not None
    assert card.coach_quality_ok is None  # not gated yet (the CLI gates separately)


def _verdict(score: float) -> str:
    facets = (
        "grounded_in_own_history",
        "named_and_specific",
        "falsifiable_and_actionable",
        "honest_calibration",
    )
    return json.dumps({"facet_scores": {f: score for f in facets}, "rationale": "test verdict"})


def _coached_card() -> CalibrationScorecard:
    return CalibrationScorecard(
        period="2026-06",
        generated_at="2026-06-14T00:00:00",
        granularity="quarter",
        can_coach=True,
        n_graded=12,
        overall_hit_rate=0.45,
        improving=True,
        hit_rate_delta=0.3,
        latest_period="2026-Q2",
        latest_hit_rate=0.67,
        selection_usd=5000.0,
        sizing_usd=-3000.0,
        timing_usd=-500.0,
        biases=[NamedBias("oversizes adds", "you add too fast", ["sizing -$3000"], "5/5 adds")],
        experiment=BehavioralExperiment("cap adds at 1.2x median", "sizing leak", None),
        coach_quality_ok=None,
        coach_quality_score=None,
    )


def test_gate_scorecard_pass_keeps_prose() -> None:
    gated = gate_scorecard(
        _coached_card(), code_root=PROJECT_ROOT, judge_caller=lambda *_a, **_k: _verdict(1.0)
    )
    assert gated.coach_quality_ok is True
    assert gated.coach_quality_score == pytest.approx(1.0)
    assert gated.biases  # prose preserved


def test_gate_scorecard_fail_suppresses_prose() -> None:
    gated = gate_scorecard(
        _coached_card(), code_root=PROJECT_ROOT, judge_caller=lambda *_a, **_k: _verdict(0.0)
    )
    assert gated.coach_quality_ok is False
    assert gated.biases == [] and gated.experiment is None  # suppressed
    assert any("suppressed" in n.lower() for n in gated.notes)


def test_gate_scorecard_nothing_to_gate_is_noop() -> None:
    card = CalibrationScorecard(
        period="2026-06", generated_at="x", granularity="quarter", can_coach=False,
        n_graded=3, overall_hit_rate=None, improving=None, hit_rate_delta=None,
        latest_period=None, latest_hit_rate=None, selection_usd=None, sizing_usd=None,
        timing_usd=None, biases=[], experiment=None, coach_quality_ok=None,
        coach_quality_score=None,
    )  # fmt: skip

    def _boom(*_a: object, **_k: object) -> str:
        raise AssertionError("must not judge an empty scorecard")

    gated = gate_scorecard(card, code_root=PROJECT_ROOT, judge_caller=_boom)
    assert gated.coach_quality_ok is None  # no prose synthesised → no judge call


# ---------------------------------------------------------------------------
# render + persistence
# ---------------------------------------------------------------------------


def test_render_prose_includes_biases_and_experiment() -> None:
    prose = render_scorecard_prose(_coached_card())
    assert "oversizes adds" in prose
    assert "evidence:" in prose
    assert "experiment" in prose.lower()
    assert "cap adds at 1.2x median" in prose


def test_scorecard_round_trips_through_json(tmp_path: Path) -> None:
    card = _coached_card()
    path = save_scorecard(tmp_path, card)
    assert path.exists()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "prose" in raw  # denormalised for the eval corpus
    loaded = scorecard_from_dict(raw)
    assert loaded.period == card.period
    assert [b.name for b in loaded.biases] == ["oversizes adds"]
    assert loaded.experiment is not None


def test_load_latest_scorecard_picks_newest(tmp_path: Path) -> None:
    for period in ("2026-04", "2026-06", "2026-05"):
        card = scorecard_from_dict(dict(scorecard_to_dict(_coached_card()), period=period))
        save_scorecard(tmp_path, card)
    latest = load_latest_scorecard(tmp_path)
    assert latest is not None and latest.period == "2026-06"


def test_load_latest_scorecard_none_when_absent(tmp_path: Path) -> None:
    assert load_latest_scorecard(tmp_path) is None
