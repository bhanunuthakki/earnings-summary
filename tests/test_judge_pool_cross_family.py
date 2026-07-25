"""P4 cross-family judge pool: DeepSeek (OpenRouter) + Codex (ChatGPT
membership) alongside Claude/Gemini.

The measurement that motivated this (2026-07-25, recorded so the reason
survives): the same candidate scored 100% (4-0-0) under Claude-judging-Claude
and 50% (4 wins / 1 loss / 3 ties) under DeepSeek+Codex. Same candidate, same
cases, same generation model — only the judge family changed. A same-family
pool would have promoted a candidate the independent judges call unproven.

No live calls: the transports are stubbed.
"""

from __future__ import annotations

import pytest

from llm.backend_judge import CLAUDE, CODEX, DEEPSEEK, GEMINI
from llm.model_ladder import DEEPSEEK_JUDGE_MODEL, JUDGE_POOL


def test_pool_spans_four_distinct_families() -> None:
    """A pool that is all one vendor measures its own preferences."""
    assert set(JUDGE_POOL) == {CLAUDE, GEMINI, DEEPSEEK, CODEX}
    assert JUDGE_POOL[DEEPSEEK] == DEEPSEEK_JUDGE_MODEL
    assert "deepseek" in JUDGE_POOL[DEEPSEEK]  # non-Anthropic, non-Google
    assert JUDGE_POOL[CODEX].startswith("gpt-")  # OpenAI family via membership


def test_deepseek_judge_is_on_the_openrouter_ladder() -> None:
    from llm.model_ladder import backend_for

    assert backend_for(DEEPSEEK_JUDGE_MODEL) == "openrouter"


def test_deepseek_judge_routes_through_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    """The judge must FORCE the OpenRouter backend + the pinned model; falling
    back to Claude would silently restore same-family judging."""
    import llm.backend_judge as bj

    seen: dict[str, object] = {}

    def fake_call_llm(prompt: str, **kw: object) -> str:
        seen.update(kw)
        return (
            '{"winner": "A", "margin": 0.5, "faithfulness": "A", "accuracy": "A", '
            '"format": "tie", "conciseness": "tie", "reason": "x"}'
        )

    monkeypatch.setattr(bj, "call_llm", fake_call_llm)
    bj._judge_once("p", "t", "a", "b", judge_backend=DEEPSEEK, run_id=None, max_prompt_chars=8000)
    assert seen.get("backend") == "openrouter"
    assert seen.get("model") == DEEPSEEK_JUDGE_MODEL


def test_codex_judge_uses_the_membership_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex must NOT go through call_llm (which would bill the Claude
    transport); it uses the membership wrapper and its own ledger row."""
    import llm.backend_judge as bj
    import llm.codex_backend as cb

    called: dict[str, object] = {}

    def fake_codex(prompt: str, **kw: object) -> str:
        called["used"] = True
        called.update(kw)
        return (
            '{"winner": "B", "margin": 0.7, "faithfulness": "B", "accuracy": "B", '
            '"format": "tie", "conciseness": "tie", "reason": "y"}'
        )

    def boom_call_llm(prompt: str, **kw: object) -> str:
        raise AssertionError("Codex judge must not route through call_llm")

    monkeypatch.setattr(cb, "call_codex_llm", fake_codex)
    monkeypatch.setattr(bj, "call_llm", boom_call_llm)
    bj._judge_once("p", "t", "a", "b", judge_backend=CODEX, run_id=None, max_prompt_chars=8000)
    assert called.get("used") is True
    assert called.get("scope") == "backend_judge"


def test_codex_backend_rejects_metered_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """The machine rulebook: membership billing, never the metered OpenAI SDK.
    The wrapper enforces it; this pins that we surface it as unavailable
    rather than silently falling back to a billed path."""
    from llm.codex_backend import codex_available

    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-be-used")
    assert codex_available() is False


def test_codex_failure_is_recorded_and_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """A judge that won't run must surface as a judge ERROR (infra), which the
    verdict layer treats as unmeasured — never as a score."""
    import llm.codex_backend as cb

    def boom() -> object:
        raise RuntimeError("Codex membership authentication could not be verified.")

    monkeypatch.setattr(cb, "_load_wrapper", boom)
    rows: list[dict[str, object]] = []
    monkeypatch.setattr("llm.ledger.record_llm_call", lambda **kw: rows.append(kw))
    with pytest.raises(RuntimeError, match="authentication"):
        cb.call_codex_llm("prompt", purpose="backend_compare_judge")
    assert rows and str(rows[0].get("error", "")).startswith("[codex]")
    assert str(rows[0].get("model", "")).startswith("codex:")


def test_judge_error_still_counts_as_infra_not_quality() -> None:
    """Cross-family judges do not change the honesty contract: a majority of
    errored judgments is JUDGE_DEGRADED, not a verdict about the candidate."""
    from llm.model_eval import JUDGE_DEGRADED, decide_switch

    verdict = decide_switch(
        purpose="p",
        incumbent="claude-opus-4-8",
        candidate="deepseek/deepseek-v4-flash",
        per_judge={DEEPSEEK: (1, 0, 1)},
        judge_agreement=0.0,
        min_n=4,
        parity_threshold=0.8,
        n_cases_attempted=8,
        n_judgments_attempted=16,
        n_judge_errors=12,
    )
    assert verdict.recommendation == JUDGE_DEGRADED
