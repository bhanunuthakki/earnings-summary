"""B7 — research.triage: the routing triage behind a positive wondering verdict."""

from __future__ import annotations

from collections.abc import Callable

from research.triage import (
    ROUTES,
    TriageVerdict,
    build_session_prompt,
    classify_triage,
    estimate_cost_usd,
)


def _route(route: str) -> Callable[[str], dict[str, object]]:
    return lambda _prompt: {"route": route, "why": "test"}


def test_routes_are_the_closed_three() -> None:
    assert ROUTES == ("answer_now", "belief_candidate", "research_task")


def test_classify_triage_answer_now() -> None:
    verdict = classify_triage("what's my cost basis on NU?", ticker="NU", call=_route("answer_now"))
    assert verdict == TriageVerdict(route="answer_now", why="test", gate="llm")


def test_classify_triage_belief_candidate() -> None:
    verdict = classify_triage(
        "I think NU's credit discipline is the real moat here", call=_route("belief_candidate")
    )
    assert verdict.route == "belief_candidate"
    assert verdict.gate == "llm"


def test_classify_triage_research_task() -> None:
    verdict = classify_triage(
        "what's driving MELI's take rate this quarter?", call=_route("research_task")
    )
    assert verdict.route == "research_task"


def test_classify_triage_fails_open_on_exception() -> None:
    def boom(_prompt: str) -> dict[str, object]:
        raise RuntimeError("llm down")

    verdict = classify_triage("do margins hold?", call=boom)
    assert verdict.route == "research_task"
    assert verdict.gate == "fail_open"


def test_classify_triage_fails_open_on_unknown_route() -> None:
    verdict = classify_triage("do margins hold?", call=lambda _p: {"route": "bogus"})
    assert verdict.route == "research_task"
    assert verdict.gate == "fail_open"


def test_classify_triage_fails_open_on_missing_route_key() -> None:
    verdict = classify_triage("do margins hold?", call=lambda _p: {})
    assert verdict.route == "research_task"
    assert verdict.gate == "fail_open"


def test_classify_triage_route_is_case_and_whitespace_normalized() -> None:
    verdict = classify_triage("x", call=lambda _p: {"route": "  Answer_Now  "})
    assert verdict.route == "answer_now"


def test_estimate_cost_usd_ticker_vs_general() -> None:
    assert estimate_cost_usd("NU") > estimate_cost_usd(None)
    assert estimate_cost_usd(None) > 0


def test_estimate_cost_usd_is_deterministic_and_io_free() -> None:
    # Same input -> same output, called repeatedly, no mocking required (the
    # whole point: this must be safe to call from the fire-and-forget tap).
    assert estimate_cost_usd("NU") == estimate_cost_usd("NU") == estimate_cost_usd("nu")


def test_build_session_prompt_includes_musing_ticker_and_capabilities() -> None:
    prompt = build_session_prompt("does NU's NIM compress next quarter?", ticker="NU")
    assert "does NU's NIM compress next quarter?" in prompt
    assert "Ticker: NU" in prompt
    assert "cost basis" in prompt.lower()
    assert "Bring back" in prompt


def test_build_session_prompt_omits_ticker_line_when_none() -> None:
    prompt = build_session_prompt("how does the macro play out?", ticker=None)
    assert "Ticker:" not in prompt
