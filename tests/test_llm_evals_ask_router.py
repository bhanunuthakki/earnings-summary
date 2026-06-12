"""Ask pack-router eval grader (src/evals/ask_router.py, S4 PR2): golden-file
validation (including the checked-in set), Jaccard/exact router grading,
grounded contract checks, and the abort semantics. All route/load functions
are injected fakes — no LLM, no tracker, no prod DB."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ask.packs import PACK_KEYS
from ask.router import PackRoute
from evals.ask_router import (
    DEFAULT_GOLDEN_RELPATH,
    RouterCase,
    grade_grounded_case,
    grade_router_case,
    load_ask_router_golden,
    run_ask_router_eval,
)
from evals.harness import EvalAbortError

REPO_ROOT = Path(__file__).resolve().parents[1]


def _golden(tmp_path: Path, cases: list[dict[str, object]]) -> Path:
    path = tmp_path / "golden.json"
    path.write_text(
        json.dumps({"purpose": "ask_pack_router", "version": 1, "cases": cases}),
        encoding="utf-8",
    )
    return path


def _routes(*results: PackRoute):
    """A route_fn yielding the given results in order (last one repeats)."""
    queue = list(results)

    def _fn(_question: str) -> PackRoute:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return _fn


def _items(*kinds: str) -> list[dict[str, object]]:
    return [
        {"kind": k, "label": f"{k} label", "text": f"{k}: content", "doc_id": None} for k in kinds
    ]


def _load_nothing(
    keys: object, *, db_path: Path, focus_tickers: list[str]
) -> list[dict[str, object]]:
    return []


# ---------------------------------------------------------------------------
# golden loading
# ---------------------------------------------------------------------------


def test_checked_in_golden_set_loads_and_is_well_formed() -> None:
    cases = load_ask_router_golden(REPO_ROOT / DEFAULT_GOLDEN_RELPATH)
    assert len(cases) >= 16
    assert any(c.mode == "grounded" for c in cases)
    for c in cases:
        assert c.question.strip()
        assert c.expected_packs <= set(PACK_KEYS)
        assert c.selected_includes <= set(PACK_KEYS)
        assert c.require_kinds <= set(PACK_KEYS)


def test_golden_validation_collects_all_problems(tmp_path: Path) -> None:
    path = _golden(
        tmp_path,
        [
            {"id": "a", "mode": "router", "question": "q", "expected_packs": ["weather"]},
            {"id": "a", "mode": "nope", "question": "", "expected_packs": []},
            {"id": "b", "mode": "grounded", "question": "q"},
        ],
    )
    with pytest.raises(ValueError) as exc:
        load_ask_router_golden(path)
    message = str(exc.value)
    assert "unknown pack key" in message
    assert "duplicate id" in message
    assert "mode must be" in message
    assert "selected_includes" in message


# ---------------------------------------------------------------------------
# router grading
# ---------------------------------------------------------------------------


def _router_case(expected: frozenset[str]) -> RouterCase:
    return RouterCase(case_id="rt-x", mode="router", question="q", expected_packs=expected)


def test_router_exact_match_passes() -> None:
    case = _router_case(frozenset({"holdings", "dcf"}))
    result = grade_router_case(case, route_fn=_routes(PackRoute(("holdings", "dcf"), "ok")))
    assert result.passed
    assert result.score == 1.0


def test_router_near_miss_gets_partial_credit() -> None:
    case = _router_case(frozenset({"holdings", "dcf"}))
    result = grade_router_case(
        case, route_fn=_routes(PackRoute(("holdings", "dcf", "conviction"), "ok"))
    )
    assert not result.passed
    assert result.failure_stage == "mismatch"
    assert result.score == pytest.approx(2 / 3)
    assert result.judge_rationale is not None
    assert "conviction" in result.judge_rationale


def test_router_both_empty_is_perfect() -> None:
    case = _router_case(frozenset())
    result = grade_router_case(case, route_fn=_routes(PackRoute((), "ok")))
    assert result.passed
    assert result.score == 1.0


def test_router_gated_and_skipped_abort_instead_of_scoring() -> None:
    case = _router_case(frozenset())
    with pytest.raises(EvalAbortError):
        grade_router_case(case, route_fn=_routes(PackRoute((), "gated", "no tracked")))
    with pytest.raises(EvalAbortError):
        grade_router_case(case, route_fn=_routes(PackRoute((), "skipped", "over budget")))


# ---------------------------------------------------------------------------
# grounded grading
# ---------------------------------------------------------------------------


def _grounded_case(selected: frozenset[str], require: frozenset[str] = frozenset()) -> RouterCase:
    return RouterCase(
        case_id="gr-x",
        mode="grounded",
        question="q",
        focus_tickers=("NU",),
        selected_includes=selected,
        require_kinds=require,
    )


def test_grounded_contract_passes_when_everything_lines_up(tmp_path: Path) -> None:
    case = _grounded_case(frozenset({"dcf"}), frozenset({"dcf"}))

    def _load(keys: object, *, db_path: Path, focus_tickers: list[str]) -> list[dict[str, object]]:
        assert focus_tickers == ["NU"]
        return _items("dcf", "holdings")

    result = grade_grounded_case(
        case,
        route_fn=_routes(PackRoute(("holdings", "dcf"), "ok")),
        load_fn=_load,
        db_path=tmp_path / "x.db",
    )
    assert result.passed
    assert result.score == 1.0


def test_grounded_contract_fails_fractionally(tmp_path: Path) -> None:
    # The router selected journal only — the required dcf kind can't appear,
    # and selected_includes isn't satisfied either: 2 of 5 checks fail.
    case = _grounded_case(frozenset({"dcf"}), frozenset({"dcf"}))

    def _load(keys: object, *, db_path: Path, focus_tickers: list[str]) -> list[dict[str, object]]:
        return _items("journal")

    result = grade_grounded_case(
        case,
        route_fn=_routes(PackRoute(("journal",), "ok")),
        load_fn=_load,
        db_path=tmp_path / "x.db",
    )
    assert not result.passed
    assert result.failure_stage == "contract"
    assert result.score == pytest.approx(3 / 5)


def test_grounded_flags_unselected_kinds_and_blown_budgets(tmp_path: Path) -> None:
    case = _grounded_case(frozenset({"holdings"}))

    def _load(keys: object, *, db_path: Path, focus_tickers: list[str]) -> list[dict[str, object]]:
        return [
            {"kind": "holdings", "label": "ok", "text": "x" * 5000},  # blows the 900 budget
            {"kind": "dcf", "label": "ok", "text": "fine"},  # never selected
        ]

    result = grade_grounded_case(
        case,
        route_fn=_routes(PackRoute(("holdings",), "ok")),
        load_fn=_load,
        db_path=tmp_path / "x.db",
    )
    assert not result.passed
    assert result.judge_rationale is not None
    assert "selected" in result.judge_rationale
    assert "budget" in result.judge_rationale


# ---------------------------------------------------------------------------
# run orchestration
# ---------------------------------------------------------------------------


def test_run_summary_fields_and_mixed_modes(tmp_path: Path) -> None:
    golden = _golden(
        tmp_path,
        [
            {"id": "r1", "mode": "router", "question": "q1", "expected_packs": ["holdings"]},
            {
                "id": "g1",
                "mode": "grounded",
                "question": "q2",
                "focus_tickers": ["NU"],
                "selected_includes": ["holdings"],
                "require_kinds": ["holdings"],
            },
        ],
    )

    def _load(keys: object, *, db_path: Path, focus_tickers: list[str]) -> list[dict[str, object]]:
        return _items("holdings")

    summary = run_ask_router_eval(
        db_path=tmp_path / "x.db",
        golden_path=golden,
        code_root=REPO_ROOT,
        route_fn=_routes(PackRoute(("holdings",), "ok")),
        load_fn=_load,
    )
    assert summary.purpose == "ask_pack_router"
    assert summary.judge_model is None
    assert summary.n_cases == 2
    assert summary.n_pass == 2
    assert summary.avg_score == 1.0
    assert summary.golden_set_sha


def test_run_aborts_when_first_case_errors_twice(tmp_path: Path) -> None:
    golden = _golden(
        tmp_path,
        [{"id": "r1", "mode": "router", "question": "q1", "expected_packs": []}],
    )
    with pytest.raises(EvalAbortError, match="transport"):
        run_ask_router_eval(
            db_path=tmp_path / "x.db",
            golden_path=golden,
            code_root=REPO_ROOT,
            route_fn=_routes(PackRoute((), "error", "RuntimeError")),
            load_fn=_load_nothing,
        )


def test_run_scores_a_later_sporadic_error(tmp_path: Path) -> None:
    golden = _golden(
        tmp_path,
        [
            {"id": "r1", "mode": "router", "question": "q1", "expected_packs": []},
            {"id": "r2", "mode": "router", "question": "q2", "expected_packs": ["holdings"]},
        ],
    )
    summary = run_ask_router_eval(
        db_path=tmp_path / "x.db",
        golden_path=golden,
        code_root=REPO_ROOT,
        route_fn=_routes(PackRoute((), "ok"), PackRoute((), "error", "RuntimeError")),
        load_fn=_load_nothing,
    )
    assert summary.n_cases == 2
    assert summary.cases[0].passed
    assert summary.cases[1].failure_stage == "call"
    assert summary.cases[1].score == 0.0
