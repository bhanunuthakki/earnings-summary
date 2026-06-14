"""Calibration scorecard panel (close_the_loops L8 PR2): the coach's-read
section rendered from a persisted scorecard, and its mount onto the decisions
page. Pure rendering — no LLM, no DB; the scorecard is built by hand.
"""

from __future__ import annotations

from calibration_coach import BehavioralExperiment, CalibrationScorecard, NamedBias
from decision_conditions import DecisionCondition
from integrations.portfolio_tracker_client import LivePortfolio
from pipeline.allocation_decisions_panel import compose_decisions_page
from pipeline.calibration_scorecard_panel import SCORECARD_CSS, render_scorecard_section


def _card(
    *,
    can_coach: bool = True,
    biases: list[NamedBias] | None = None,
    experiment: BehavioralExperiment | None = None,
    coach_quality_ok: bool | None = None,
    notes: list[str] | None = None,
) -> CalibrationScorecard:
    return CalibrationScorecard(
        period="2026-06",
        generated_at="2026-06-14T00:00:00",
        granularity="quarter",
        can_coach=can_coach,
        n_graded=12,
        overall_hit_rate=0.45,
        improving=True,
        hit_rate_delta=0.3,
        latest_period="2026-Q2",
        latest_hit_rate=0.67,
        selection_usd=5000.0,
        sizing_usd=-3000.0,
        timing_usd=-500.0,
        biases=biases or [],
        experiment=experiment,
        coach_quality_ok=coach_quality_ok,
        coach_quality_score=0.85 if coach_quality_ok else None,
        notes=notes or [],
    )


def test_render_none_is_empty() -> None:
    assert render_scorecard_section(None) == ""


def test_render_coached_card_shows_biases_and_experiment() -> None:
    bias = NamedBias("oversizes adds", "you add too fast", ["sizing -$3,000"], "5/5 conviction add")
    exp = BehavioralExperiment(
        "cap adds at 1.2x median",
        "sizing is the leak",
        DecisionCondition("sizing contribution", None, "gt", 0.0, "actual", 1, "turns positive"),
    )
    html = render_scorecard_section(_card(biases=[bias], experiment=exp, coach_quality_ok=True))
    assert "Coach" in html and "rsquo;s read" in html  # the section header
    assert "oversizes adds" in html
    assert "sizing -$3,000" in html  # the evidence chip
    assert "5/5 conviction add" in html  # the tell
    assert "cap adds at 1.2x median" in html  # the experiment hypothesis
    assert "turns positive" in html  # the falsifiable test
    assert "Passed the calibration_coach eval gate" in html


def test_render_suppressed_card_shows_gate_failure() -> None:
    html = render_scorecard_section(
        _card(coach_quality_ok=False, notes=["Coach prose suppressed — it scored 0.40"])
    )
    assert "suppressed" in html.lower()
    assert "oversizes" not in html  # no biases rendered


def test_render_thin_card_is_honest() -> None:
    html = render_scorecard_section(_card(can_coach=False, notes=["n=4, low confidence"]))
    assert "Not enough graded history to coach yet" in html
    assert "n=4, low confidence" in html


def test_mounts_onto_decisions_page() -> None:
    bias = NamedBias("oversizes adds", "you add too fast", ["sizing -$3,000"], "tell")
    section = render_scorecard_section(_card(biases=[bias], coach_quality_ok=True))
    scorecard_html = f"<style>{SCORECARD_CSS}</style>{section}"
    offline = LivePortfolio(available=False, api_url="http://x", error="down")
    page = compose_decisions_page([], [], offline, None, scorecard_html=scorecard_html)
    assert "Coach" in page and "oversizes adds" in page
    # Absent scorecard → the page omits the section entirely (byte-shape preserved).
    bare = compose_decisions_page([], [], offline, None)
    assert "oversizes adds" not in bare
