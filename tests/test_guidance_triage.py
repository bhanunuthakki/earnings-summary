"""Tests for D2.1 Stage 2/3 LLM triage (``filings.guidance_triage``).

Mirrors ``filings.metric_triage``'s degrade-safe contract: a failed/unparseable
call must degrade to an EMPTY verdict map (never fabricate a verdict for any
candidate), and a hard-stop exception (budget cap / missing CLI) must
propagate untouched rather than degrade.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from filings.guidance_lifecycle import GuidanceCandidate  # noqa: E402
from filings.guidance_triage import (  # noqa: E402
    GuidanceRelevance,
    triage_guidance_candidates,
)
from filings.metric_triage import LifecyclePrior  # noqa: E402


def _candidate(subject_key: str = "NU") -> GuidanceCandidate:
    return GuidanceCandidate(
        ticker="NU",
        kind="guidance_withdrawn",
        lane="commitments",
        subject_key=subject_key,
        subject_label="NU management guidance practice",
        last_present_period="2025-09-30",
        last_present_rank=8102,
        as_of_period="2026-03-31",
        as_of_rank=8104,
        current_silence=2,
        historical_max_gap=1,
        n_known_periods=6,
    )


def test_no_candidates_short_circuits_without_calling_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("should not be called for an empty candidate list")

    monkeypatch.setattr("filings.guidance_triage.call_llm_structured", boom)
    outcome = triage_guidance_candidates("NU", [])
    assert outcome.verdicts == {}
    assert outcome.degraded is False


def test_successful_triage_parses_verdicts(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(*args: object, **kwargs: object) -> dict[str, object]:
        return {
            "NU": {
                "relevance": "forward_guidance",
                "prior": "concealment",
                "rationale": "Stopped after two weak quarters.",
            }
        }

    monkeypatch.setattr("filings.guidance_triage.call_llm_structured", fake)
    outcome = triage_guidance_candidates("NU", [_candidate("NU")])
    assert outcome.degraded is False
    v = outcome.verdicts["NU"]
    assert v.relevance is GuidanceRelevance.FORWARD_GUIDANCE
    assert v.prior is LifecyclePrior.CONCEALMENT


def test_parse_failure_degrades_to_empty_verdicts(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> object:
        raise ValueError("double parse failure")

    monkeypatch.setattr("filings.guidance_triage.call_llm_structured", boom)
    outcome = triage_guidance_candidates("NU", [_candidate("NU")])
    assert outcome.degraded is True
    assert outcome.verdicts == {}


def test_malformed_row_dropped_not_fabricated(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(*args: object, **kwargs: object) -> dict[str, object]:
        return {"NU": {"relevance": "not_a_real_value", "prior": "unclear", "rationale": "x"}}

    monkeypatch.setattr("filings.guidance_triage.call_llm_structured", fake)
    outcome = triage_guidance_candidates("NU", [_candidate("NU")])
    assert outcome.degraded is False
    assert outcome.verdicts == {}  # the one malformed row was dropped, not guessed


def test_hard_stop_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeHardStopError(Exception):
        pass

    def boom(*args: object, **kwargs: object) -> object:
        raise _FakeHardStopError("budget exceeded")

    def _always_hard_stop(exc: BaseException) -> bool:
        return True

    monkeypatch.setattr("filings.guidance_triage.call_llm_structured", boom)
    monkeypatch.setattr("filings.guidance_triage.is_hard_stop", _always_hard_stop)
    with pytest.raises(_FakeHardStopError):
        triage_guidance_candidates("NU", [_candidate("NU")])
