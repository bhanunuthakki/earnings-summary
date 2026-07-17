"""Mode-A golden wiring for the two #884 Ledger classifiers:
``ledger_reply_intent`` (onmymind.reply) and ``triage_route_suggest``
(user_state.triage_suggest).

All fully OFFLINE — every production fn is injected (fake ``fn``), so the
suite never spends an LLM token. Covers: the checked-in golden files load +
are balanced, the loaders reject malformed files, the graders score the
right stages, and run_classifier_eval drives both end-to-end with a fake fn.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar, cast

import pytest

from evals.golden_classifiers import (
    ClassifierCase,
    grade_ledger_reply_intent_case,
    grade_triage_route_suggest_case,
    load_ledger_reply_intent_golden,
    load_triage_route_suggest_golden,
    run_classifier_eval,
)
from onmymind.reply import REPLY_INTENTS
from user_state.notes import ROUTABLE_INTENTS

_T = TypeVar("_T")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = PROJECT_ROOT / "evals" / "golden"


def _returning(value: _T) -> Callable[..., _T]:
    def fn(*_a: object, **_k: object) -> _T:
        return value

    return fn


# ---------------------------------------------------------------------------
# checked-in golden files load + are balanced
# ---------------------------------------------------------------------------


def test_ledger_reply_golden_is_valid_and_covers_every_intent() -> None:
    cases = load_ledger_reply_intent_golden(GOLDEN_DIR / "ledger_reply_intent.json")
    assert len(cases) >= 18
    counts: dict[str, int] = {}
    for c in cases:
        intent = str(cast("dict[str, object]", c.expected)["intent"])
        counts[intent] = counts.get(intent, 0) + 1
        assert set(c.inputs) == {"card_text", "reply_text"}
    # Every enum value appears at least twice.
    for intent in REPLY_INTENTS:
        assert counts.get(intent, 0) >= 2, f"{intent} appears {counts.get(intent, 0)}x (< 2)"


def test_triage_golden_is_valid_covers_routes_and_has_park_cases() -> None:
    cases = load_triage_route_suggest_golden(GOLDEN_DIR / "triage_route_suggest.json")
    assert len(cases) >= 18
    counts: dict[str | None, int] = {}
    for c in cases:
        exp = cast("dict[str, object]", c.expected)
        intent = exp.get("intent")
        key = intent if isinstance(intent, str) else None
        counts[key] = counts.get(key, 0) + 1
        assert set(c.inputs) == {"comment_text", "context_line"}
    # Every routable intent appears at least twice.
    for intent in ROUTABLE_INTENTS:
        assert counts.get(intent, 0) >= 2, f"{intent} appears {counts.get(intent, 0)}x (< 2)"
    # Genuine park (null-intent) cases exist — the whole point of the second pass.
    assert counts.get(None, 0) >= 2


# ---------------------------------------------------------------------------
# loaders reject malformed files
# ---------------------------------------------------------------------------


def test_load_ledger_reply_rejects_bad_golden(tmp_path: Path) -> None:
    bad = tmp_path / "g.json"
    bad.write_text(
        json.dumps(
            {
                "purpose": "ledger_reply_intent",
                "cases": [
                    {
                        "id": "a",
                        "card_text": "c",
                        "reply_text": "r",
                        "expected": {"intent": "nope"},
                    },
                    {"id": "a", "card_text": "", "reply_text": "r", "expected": {"intent": "save"}},
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as exc:
        load_ledger_reply_intent_golden(bad)
    msg = str(exc.value)
    assert "duplicate id" in msg and "expected.intent must be one of" in msg
    assert "missing/empty `card_text`" in msg


def test_load_triage_rejects_bad_intent_but_allows_null(tmp_path: Path) -> None:
    # A null intent (park) is VALID; a non-routable string is not.
    good = tmp_path / "ok.json"
    good.write_text(
        json.dumps(
            {
                "purpose": "triage_route_suggest",
                "cases": [
                    {"id": "p1", "comment_text": "vague", "expected": {"intent": None}},
                    {"id": "r1", "comment_text": "fix it", "expected": {"intent": "fix_data"}},
                ],
            }
        ),
        encoding="utf-8",
    )
    cases = load_triage_route_suggest_golden(good)
    assert cases[0].expected == {"intent": None}
    assert cases[0].inputs["context_line"] == ""  # optional, defaults empty

    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "purpose": "triage_route_suggest",
                "cases": [
                    {"id": "x", "comment_text": "c", "expected": {"intent": "not_routable"}},
                    {"id": "y", "comment_text": "c2", "expected": {}},
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as exc:
        load_triage_route_suggest_golden(bad)
    msg = str(exc.value)
    assert "expected.intent must be null (park) or one of" in msg
    assert "expected.intent missing" in msg


# ---------------------------------------------------------------------------
# graders — exact-match reply, harm-weighted triage
# ---------------------------------------------------------------------------


def test_grade_ledger_reply_exact_match() -> None:
    case = ClassifierCase(
        "lr-x", {"card_text": "c", "reply_text": "dig in"}, {"intent": "research"}
    )
    ok = grade_ledger_reply_intent_case(case, fn=_returning({"intent": "research"}))
    assert ok.passed and ok.score == 1.0
    miss = grade_ledger_reply_intent_case(case, fn=_returning({"intent": "save"}))
    assert not miss.passed and miss.score == 0.0 and miss.failure_stage == "intent"
    assert miss.judge_rationale is not None and "research" in miss.judge_rationale


def test_grade_triage_harm_weighting() -> None:
    route_case = ClassifierCase(
        "tr-x", {"comment_text": "fix it", "context_line": ""}, {"intent": "fix_data"}
    )
    park_case = ClassifierCase(
        "tr-p", {"comment_text": "vague", "context_line": ""}, {"intent": None}
    )

    # Correct route → full pass.
    ok = grade_triage_route_suggest_case(
        route_case, fn=_returning({"intent": "fix_data", "confidence": "high"})
    )
    assert ok.passed and ok.score == 1.0

    # park == park → full pass (a valid outcome, not a failure).
    park_ok = grade_triage_route_suggest_case(
        park_case, fn=_returning({"intent": None, "confidence": "low"})
    )
    assert park_ok.passed and park_ok.score == 1.0

    # Wrong route at HIGH confidence — auto-routes wrongly — worst case.
    worst = grade_triage_route_suggest_case(
        route_case, fn=_returning({"intent": "curate_peers", "confidence": "high"})
    )
    assert not worst.passed and worst.score == 0.0 and worst.failure_stage == "auto_route"

    # Wrong route at LOW confidence — only a suggestion — partial credit.
    softer = grade_triage_route_suggest_case(
        route_case, fn=_returning({"intent": "curate_peers", "confidence": "low"})
    )
    assert not softer.passed and softer.score == 0.25 and softer.failure_stage == "route"

    # Model parked when a route was expected — safe recall miss.
    under = grade_triage_route_suggest_case(
        route_case, fn=_returning({"intent": None, "confidence": "low"})
    )
    assert not under.passed and under.score == 0.5 and under.failure_stage == "under_route"

    # Model routed something when park was expected, at high confidence — worst.
    over = grade_triage_route_suggest_case(
        park_case, fn=_returning({"intent": "fix_data", "confidence": "high"})
    )
    assert not over.passed and over.score == 0.0 and over.failure_stage == "auto_route"


# ---------------------------------------------------------------------------
# run_classifier_eval end-to-end with a fake fn (no LLM)
# ---------------------------------------------------------------------------


def test_run_reply_eval_end_to_end(tmp_path: Path) -> None:
    summary = run_classifier_eval(
        "ledger_reply_intent",
        golden_path=GOLDEN_DIR / "ledger_reply_intent.json",
        code_root=tmp_path,
        fn=_returning({"intent": "research"}),  # right for research cases, wrong elsewhere
    )
    assert summary.mode == "live" and summary.judge_model is None
    assert summary.n_cases >= 18
    assert 1 <= summary.n_pass < summary.n_cases  # some research cases pass, others miss
    assert summary.golden_set_sha is not None


def test_run_triage_eval_end_to_end(tmp_path: Path) -> None:
    summary = run_classifier_eval(
        "triage_route_suggest",
        golden_path=GOLDEN_DIR / "triage_route_suggest.json",
        code_root=tmp_path,
        fn=_returning({"intent": None, "confidence": "low"}),  # parks everything
    )
    assert summary.mode == "live" and summary.judge_model is None
    assert summary.n_cases >= 18
    # Parking everything: park cases pass (1.0), route cases score 0.5 (safe miss).
    assert summary.n_pass >= 2  # at least the genuine park cases pass
    assert summary.golden_set_sha is not None
