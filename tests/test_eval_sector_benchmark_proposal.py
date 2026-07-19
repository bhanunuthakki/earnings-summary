"""Mode-A exact-match grader for the sector-benchmark-ETF proposal
(src/evals/sector_benchmark_proposal.py, docs/design/
comparable_sets_bottoms_up.md §4).

Deterministic — no live LLM call: the grader is driven by injected
suggest_fns, and the shipped golden set is validated structurally.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.sector_benchmark_proposal import (
    DEFAULT_GOLDEN_RELPATH,
    SectorBenchmarkCase,
    grade_sector_benchmark_case,
    load_sector_benchmark_golden,
    run_sector_benchmark_proposal_eval,
)
from llm.structured import StructuredParseError

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_GOLDEN: dict[str, object] = {
    "purpose": "sector_benchmark_proposal",
    "cases": [
        {
            "id": "g1",
            "industry": "Semiconductors",
            "expected_etf": "SMH",
            "also_valid_etf": ["SOXX"],
            "expected_sector_etf": "XLK",
        },
        {
            "id": "g2",
            "industry": "Utilities - Regulated Electric",
            "expected_etf": None,
            "expected_sector_etf": "XLU",
        },
    ],
}


def _golden_file(tmp_path: Path) -> Path:
    p = tmp_path / "sector_benchmark_proposal.json"
    p.write_text(json.dumps(_GOLDEN), encoding="utf-8")
    return p


def test_golden_loads_and_validates(tmp_path: Path) -> None:
    cases = load_sector_benchmark_golden(_golden_file(tmp_path))
    assert [c.case_id for c in cases] == ["g1", "g2"]
    assert cases[0].expected_etf == "SMH"
    assert cases[0].also_valid_etf == frozenset({"SOXX"})
    assert cases[1].expected_etf is None


def test_golden_rejects_missing_expected(tmp_path: Path) -> None:
    bad: dict[str, object] = {
        "purpose": "sector_benchmark_proposal",
        "cases": [{"id": "x", "industry": "Widgets"}],
    }
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="expected_etf"):
        load_sector_benchmark_golden(p)


def test_golden_rejects_wrong_purpose(tmp_path: Path) -> None:
    bad: dict[str, object] = {"purpose": "key_metrics", "cases": []}
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="purpose"):
        load_sector_benchmark_golden(p)


def _case() -> SectorBenchmarkCase:
    return SectorBenchmarkCase(
        case_id="g1",
        industry="Semiconductors",
        expected_etf="SMH",
        expected_sector_etf="XLK",
        also_valid_etf=frozenset({"SOXX"}),
    )


def test_grade_exact_match_full_score() -> None:
    res = grade_sector_benchmark_case(_case(), suggest_fn=lambda _c: ("SMH", "XLK"))
    assert res.score == pytest.approx(1.0)
    assert res.passed is True


def test_grade_also_valid_etf_counts_as_match() -> None:
    res = grade_sector_benchmark_case(_case(), suggest_fn=lambda _c: ("SOXX", "XLK"))
    assert res.score == pytest.approx(1.0)


def test_grade_sector_etf_only_partial_credit() -> None:
    # Wrong dedicated ETF, right sector ETF -> 0.5, still passes (>= 0.5).
    res = grade_sector_benchmark_case(_case(), suggest_fn=lambda _c: ("WRONG", "XLK"))
    assert res.score == pytest.approx(0.5)
    assert res.passed is True


def test_grade_both_wrong_fails() -> None:
    res = grade_sector_benchmark_case(_case(), suggest_fn=lambda _c: ("WRONG", "XLF"))
    assert res.score == pytest.approx(0.0)
    assert res.passed is False


def test_grade_none_none_expected_matches_none_returned() -> None:
    case = SectorBenchmarkCase(
        case_id="g2",
        industry="Utilities - Regulated Electric",
        expected_etf=None,
        expected_sector_etf="XLU",
    )
    res = grade_sector_benchmark_case(case, suggest_fn=lambda _c: (None, "XLU"))
    assert res.score == pytest.approx(1.0)


def test_grade_scores_parse_failure_zero() -> None:
    def _boom(_c: SectorBenchmarkCase) -> tuple[str | None, str | None]:
        raise StructuredParseError("nope", raw_head="x")

    res = grade_sector_benchmark_case(_case(), suggest_fn=_boom)
    assert res.score == 0.0
    assert res.failure_stage == "call"


def test_run_eval_averages_score(tmp_path: Path) -> None:
    answers = {"Semiconductors": ("SMH", "XLK"), "Utilities - Regulated Electric": (None, "XLU")}
    summary = run_sector_benchmark_proposal_eval(
        golden_path=_golden_file(tmp_path),
        code_root=PROJECT_ROOT,
        suggest_fn=lambda c: answers[c.industry],
    )
    assert summary.n_cases == 2
    assert summary.avg_score == pytest.approx(1.0)
    assert summary.purpose == "sector_benchmark_proposal"


def test_shipped_golden_set_is_valid() -> None:
    cases = load_sector_benchmark_golden(PROJECT_ROOT / DEFAULT_GOLDEN_RELPATH)
    assert len(cases) >= 5
    for c in cases:
        assert c.expected_etf is not None or c.expected_sector_etf is not None
