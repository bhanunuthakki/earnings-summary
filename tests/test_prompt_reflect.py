"""P2 reflective mutation (llm.prompt_reflect): rewrite validation, the
pre-spend rejection contract, and the Pareto frontier.

No LLM calls — the structured caller is dependency-injected.
"""

from __future__ import annotations

import json

import pytest

from llm.prompt_reflect import (
    Candidate,
    ParetoFrontier,
    reflect_and_rewrite,
)
from llm.prompt_registry import PromptTemplate

_BASE = PromptTemplate(
    template_id="t.reflect",
    body=(
        "You are analyzing {ticker}.\n"
        "Be specific and cite numbers.\n"
        'Return ONLY JSON: {{"verdict": "<call>"}}\n'
        "Data:\n{data}\n"
    ),
    variables=("ticker", "data"),
)


def _caller(payload: object):
    def fake(prompt: str, **_kw: object) -> object:
        return payload

    return fake


def _good_body(extra: str = "Name the mechanism explicitly.\n") -> str:
    return (
        "You are analyzing {ticker}.\n"
        "Be specific and cite numbers with their period.\n"
        + extra
        + 'Return ONLY JSON: {{"verdict": "<call>"}}\n'
        "Data:\n{data}\n"
    )


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_rewrite_produces_a_new_registry_version() -> None:
    out = reflect_and_rewrite(
        _BASE,
        purpose="bear_case",
        evidence="Judge: output stated 'revenue fell' with no period or figure.",
        struct=_caller(
            {
                "diagnosis": "The instruction says 'cite numbers' but never demands a period.",
                "revised_template": _good_body(),
                "expected_effect": "specificity facet",
            }
        ),
    )
    assert out is not None
    assert out.template.template_id == _BASE.template_id
    assert out.template.version != _BASE.version  # a real new version
    assert out.parent_version == _BASE.version
    assert set(out.template.variables) == {"ticker", "data"}
    # And it still renders — the property a body-level rewrite could break.
    rendered = out.template.render(ticker="NU", data="rev 1.2B")
    assert "NU" in rendered and '"verdict"' in rendered


# ---------------------------------------------------------------------------
# Pre-spend rejection — every one of these would otherwise cost judged cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "why"),
    [
        # Dropped a slot: the call site passes it, so render() would raise.
        ("You are analyzing {ticker}. Return JSON.\n", "dropped slot"),
        # Invented a slot: the call site has no such variable.
        (
            "Analyze {ticker} in {period}.\nData:\n{data}\n",
            "invented slot",
        ),
        # Collapsed to a stub — a model failure, not an insight.
        ("Analyze {ticker}. {data}", "too short"),
        # Ballooned.
        (
            "You are analyzing {ticker}.\n" + ("padding line\n" * 400) + "Data:\n{data}\n",
            "too long",
        ),
    ],
)
def test_invalid_rewrites_are_rejected_before_spend(body: str, why: str) -> None:
    out = reflect_and_rewrite(
        _BASE,
        purpose="bear_case",
        evidence="e",
        struct=_caller({"diagnosis": "d", "revised_template": body}),
    )
    assert out is None, f"should have rejected: {why}"


def test_noop_rewrite_is_rejected() -> None:
    """An identical body is not a candidate — running it would spend judged
    cases proving a prompt equals itself."""
    out = reflect_and_rewrite(
        _BASE,
        purpose="p",
        evidence="e",
        struct=_caller({"diagnosis": "d", "revised_template": _BASE.body}),
    )
    assert out is None


@pytest.mark.parametrize(
    "payload",
    [
        {"revised_template": "x {ticker} {data}"},  # no diagnosis
        {"diagnosis": "d"},  # no body
        {"diagnosis": "", "revised_template": "x"},  # empty diagnosis
        "not a dict",
        None,
    ],
)
def test_malformed_payloads_return_none(payload: object) -> None:
    assert reflect_and_rewrite(_BASE, purpose="p", evidence="e", struct=_caller(payload)) is None


def test_caller_exception_is_not_load_bearing() -> None:
    def boom(prompt: str, **_kw: object) -> object:
        raise RuntimeError("judge transport down")

    assert reflect_and_rewrite(_BASE, purpose="p", evidence="e", struct=boom) is None


def test_prompt_names_the_required_slots() -> None:
    """The rewrite prompt must TELL the model the exact slot contract — the
    single most common cause of an unusable rewrite."""
    seen: dict[str, str] = {}

    def capture(prompt: str, **_kw: object) -> object:
        seen["prompt"] = prompt
        return {"diagnosis": "d", "revised_template": _good_body()}

    reflect_and_rewrite(_BASE, purpose="p", evidence="ev", struct=capture)
    assert "data, ticker" in seen["prompt"]  # sorted, so the contract is stable
    assert "str.format" in seen["prompt"]  # the brace-doubling rule
    assert "ev" in seen["prompt"]


# ---------------------------------------------------------------------------
# Pareto frontier
# ---------------------------------------------------------------------------


def test_dominated_candidate_is_rejected() -> None:
    f = ParetoFrontier([Candidate("a", quality=0.8, cost=100)])
    assert f.add(Candidate("b", quality=0.7, cost=120)) is False  # worse on both
    assert [c.version for c in f.candidates] == ["a"]


def test_cheaper_at_equal_quality_stays_on_the_frontier() -> None:
    """The case argmax-on-quality would throw away — and the reason this is a
    frontier at all."""
    f = ParetoFrontier([Candidate("a", quality=0.8, cost=100)])
    assert f.add(Candidate("cheap", quality=0.8, cost=40)) is True
    versions = {c.version for c in f.candidates}
    assert versions == {"cheap"}  # 'a' is dominated (equal quality, pricier)


def test_better_and_pricier_coexists_with_cheaper_and_worse() -> None:
    f = ParetoFrontier(
        [
            Candidate("cheap", quality=0.6, cost=30),
            Candidate("rich", quality=0.9, cost=300),
        ]
    )
    assert {c.version for c in f.candidates} == {"cheap", "rich"}  # neither dominates
    assert f.best() is not None and f.best().version == "rich"  # promotion picks quality


def test_ties_keep_the_incumbent() -> None:
    f = ParetoFrontier([Candidate("incumbent", quality=0.8, cost=100)])
    assert f.add(Candidate("twin", quality=0.8, cost=100)) is False
    assert [c.version for c in f.candidates] == ["incumbent"]


def test_parent_draw_is_deterministic_and_quality_weighted() -> None:
    f = ParetoFrontier(
        [Candidate("hi", quality=0.9, cost=200), Candidate("lo", quality=0.1, cost=10)]
    )
    assert f.draw_parent(0.0) is not None
    # Same rand -> same parent (the seeded-replay contract).
    assert f.draw_parent(0.42).version == f.draw_parent(0.42).version
    picks = [f.draw_parent(i / 100).version for i in range(100)]
    assert picks.count("hi") > picks.count("lo")
    assert picks.count("lo") > 0  # never starves the frontier


def test_empty_frontier_is_honest() -> None:
    f = ParetoFrontier()
    assert f.best() is None
    assert f.draw_parent(0.5) is None


def test_frontier_serializes_for_persistence() -> None:
    """The cycle persists the frontier between runs; make sure the shape is
    trivially round-trippable."""
    from dataclasses import asdict

    f = ParetoFrontier([Candidate("a", 0.8, 100, n_cases=12)])
    # Candidate is a slots dataclass (no __dict__) — asdict is the round-trip
    # path the persistence layer must use.
    blob = json.dumps([asdict(c) for c in f.candidates])
    assert json.loads(blob)[0]["version"] == "a"
    assert json.loads(blob)[0]["n_cases"] == 12
