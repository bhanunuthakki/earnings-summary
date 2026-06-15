"""Mode-A recall grader for the LLM key-metrics preselect
(src/evals/key_metrics.py, directives/key_metrics_picker.md).

Deterministic — no live LLM call: the grader is driven by injected suggest_fns,
and the shipped golden set is validated structurally (it must load cleanly and
every expected/also_valid token must live in its case's vocabulary).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.key_metrics import (
    DEFAULT_GOLDEN_RELPATH,
    KeyMetricCase,
    grade_key_metrics_case,
    load_key_metrics_golden,
    run_key_metrics_eval,
)
from llm.structured import StructuredParseError

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_GOLDEN: dict[str, object] = {
    "purpose": "key_metrics",
    "cases": [
        {
            "id": "g1",
            "ticker": "NU",
            "name": "Nu Holdings",
            "business_description": "A digital bank.",
            "vocabulary": [
                {"token": "kpi:NIM", "label": "NIM"},
                {"token": "kpi:NPL", "label": "NPL"},
                {"token": "kpi:Deposits", "label": "Deposits"},
                {"token": "fin:total assets", "label": "total assets"},
            ],
            "expected_metrics": ["kpi:NIM", "kpi:NPL"],
            "also_valid": ["kpi:Deposits"],
        },
        {
            "id": "g2",
            "ticker": "NOW",
            "name": "ServiceNow",
            "business_description": "Enterprise SaaS.",
            "vocabulary": [
                {"token": "kpi:NRR", "label": "NRR"},
                {"token": "kpi:Subscription revenue", "label": "Subscription revenue"},
                {"token": "fin:inventory", "label": "inventory"},
            ],
            "expected_metrics": ["kpi:NRR", "kpi:Subscription revenue"],
        },
    ],
}


def _golden_file(tmp_path: Path) -> Path:
    p = tmp_path / "key_metrics.json"
    p.write_text(json.dumps(_GOLDEN), encoding="utf-8")
    return p


def test_golden_loads_and_validates(tmp_path: Path) -> None:
    cases = load_key_metrics_golden(_golden_file(tmp_path))
    assert [c.case_id for c in cases] == ["g1", "g2"]
    assert cases[0].expected_metrics == frozenset({"kpi:NIM", "kpi:NPL"})
    assert cases[0].also_valid == frozenset({"kpi:Deposits"})
    assert ("kpi:NIM", "NIM") in cases[0].vocabulary


def test_golden_rejects_empty_expected(tmp_path: Path) -> None:
    bad: dict[str, object] = {
        "purpose": "key_metrics",
        "cases": [
            {
                "id": "x",
                "ticker": "NU",
                "business_description": "d",
                "vocabulary": [{"token": "kpi:NIM", "label": "NIM"}],
                "expected_metrics": [],
            }
        ],
    }
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="expected_metrics"):
        load_key_metrics_golden(p)


def test_golden_rejects_expected_outside_vocabulary(tmp_path: Path) -> None:
    """An expected token not in the case's vocabulary makes recall unwinnable —
    the loader rejects it loudly rather than ship an unscoreable case."""
    bad: dict[str, object] = {
        "purpose": "key_metrics",
        "cases": [
            {
                "id": "x",
                "ticker": "NU",
                "business_description": "d",
                "vocabulary": [{"token": "kpi:NIM", "label": "NIM"}],
                "expected_metrics": ["kpi:NOT_IN_VOCAB"],
            }
        ],
    }
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="not in vocabulary"):
        load_key_metrics_golden(p)


def _case(
    *,
    expected_metrics: frozenset[str] = frozenset({"kpi:NIM", "kpi:NPL"}),
    also_valid: frozenset[str] = frozenset({"kpi:Deposits"}),
) -> KeyMetricCase:
    return KeyMetricCase(
        case_id="g1",
        ticker="NU",
        name="Nu",
        business_description="d",
        vocabulary=(("kpi:NIM", "NIM"), ("kpi:NPL", "NPL"), ("kpi:Deposits", "Deposits")),
        expected_metrics=expected_metrics,
        also_valid=also_valid,
    )


def test_grade_recall_partial_credit() -> None:
    case = _case()
    # 1 of 2 expected found → recall 0.5; an also_valid extra doesn't hurt.
    res = grade_key_metrics_case(case, suggest_fn=lambda _c: ["kpi:NIM", "kpi:Deposits"])
    assert res.score == pytest.approx(0.5)
    assert res.passed is True  # >= 0.5


def test_grade_full_recall_passes() -> None:
    case = _case()
    res = grade_key_metrics_case(case, suggest_fn=lambda _c: ["kpi:NIM", "kpi:NPL"])
    assert res.score == pytest.approx(1.0)
    assert res.passed is True


def test_grade_scores_parse_failure_zero() -> None:
    case = _case(expected_metrics=frozenset({"kpi:NIM"}), also_valid=frozenset())

    def _boom(_c: KeyMetricCase) -> list[str]:
        raise StructuredParseError("nope", raw_head="x")

    res = grade_key_metrics_case(case, suggest_fn=_boom)
    assert res.score == 0.0
    assert res.failure_stage == "call"


def test_run_eval_averages_recall(tmp_path: Path) -> None:
    # NU → both expected (1.0); NOW → one of two (0.5).
    answers = {"NU": ["kpi:NIM", "kpi:NPL"], "NOW": ["kpi:NRR"]}
    summary = run_key_metrics_eval(
        golden_path=_golden_file(tmp_path),
        code_root=PROJECT_ROOT,
        suggest_fn=lambda c: answers[c.ticker],
    )
    assert summary.n_cases == 2
    assert summary.avg_score == pytest.approx((1.0 + 0.5) / 2)
    assert summary.purpose == "key_metrics"


def test_shipped_golden_set_is_valid() -> None:
    """The real evals/golden/key_metrics.json loads cleanly — every case has a
    vocabulary and its expected/also_valid tokens all live inside it."""
    cases = load_key_metrics_golden(PROJECT_ROOT / DEFAULT_GOLDEN_RELPATH)
    assert len(cases) >= 5
    for c in cases:
        vocab = {t for t, _ in c.vocabulary}
        assert c.expected_metrics, f"{c.case_id} has no expected_metrics"
        assert (c.expected_metrics | c.also_valid) <= vocab, f"{c.case_id} has stray tokens"
