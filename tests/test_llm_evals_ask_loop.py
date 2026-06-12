"""S7 loop eval grader (src/evals/ask_loop.py): golden-file validation, the
structural checks per case mode, abort semantics, and runner wiring. The
grader is driven through injected ``events_fn`` fakes — no LLM, no engine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ask.engine import ROUTE_NARRATIVE, route_turn
from ask.followup import PURPOSE
from evals.ask_loop import (
    DEFAULT_GOLDEN_RELPATH,
    LoopCase,
    grade_loop_case,
    load_ask_loop_golden,
    run_ask_loop_eval,
)
from evals.harness import EvalAbortError

_REPO = Path(__file__).resolve().parents[1]
_GOLDEN = _REPO / DEFAULT_GOLDEN_RELPATH


def _loop_events(
    *,
    rounds: int = 1,
    round1: int = 2,
    total: int = 3,
    final_text: str = "Coverage was thin in early 2024 [3].",
    cited: list[dict[str, object]] | None = None,
    route: str = ROUTE_NARRATIVE,
) -> list[dict[str, object]]:
    """A plausible armed-turn event stream, loop already resolved."""
    events: list[dict[str, object]] = [
        {"type": "stage", "stage": "answering", "route": "narrative"},
        {"type": "final", "text": final_text, "route": route},
        {
            "type": "grounding",
            "rounds": rounds,
            "evidence_total": total,
            "evidence_new": total - round1,
            "evidence_round1": round1,
        },
    ]
    if cited is None:
        cited = [{"n": 3, "kind": "transcript", "label": "TST Q1'24 call · L2"}]
    if cited:
        events.append({"type": "citations", "items": cited})
    return events


def _case(*, expect_loop: bool | None = True, kinds: frozenset[str] = frozenset()) -> LoopCase:
    return LoopCase(
        case_id="c1",
        question="what did management say in Q1 2024?",
        expect_loop=expect_loop,
        expect_new_kinds=kinds,
    )


# ----------------------------------------------------------------------------
# the checked-in golden file


def test_checked_in_golden_loads_and_routes_narrative() -> None:
    cases = load_ask_loop_golden(_GOLDEN)
    assert len(cases) >= 4
    # True (provably-insufficient) and None (graceful) modes both present.
    assert True in {c.expect_loop for c in cases}
    assert None in {c.expect_loop for c in cases}
    for c in cases:
        # Authoring guard: a question that routes to the data path can never
        # exercise the loop — catch it here, not in a paid eval run.
        assert route_turn(c.question) == ROUTE_NARRATIVE, c.case_id


def test_golden_loader_rejects_bad_shapes(tmp_path: Path) -> None:
    def write(payload: object) -> Path:
        p = tmp_path / "g.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        return p

    wrong_purpose: dict[str, object] = {"purpose": "other", "cases": [{}]}
    with pytest.raises(ValueError, match="purpose"):
        load_ask_loop_golden(write(wrong_purpose))
    bad_cases = {
        "purpose": PURPOSE,
        "cases": [
            {"id": "a", "question": "q", "expect_loop": "yes"},
            {"id": "a", "question": "", "expect_loop": True},
            {"id": "b", "question": "q", "expect_loop": False, "expect_new_kinds": ["nope"]},
        ],
    }
    with pytest.raises(ValueError) as exc:
        load_ask_loop_golden(write(bad_cases))
    msg = str(exc.value)
    assert "`expect_loop` must be a bool or null" in msg
    assert "duplicate id" in msg
    assert "unknown evidence kind" in msg
    assert "only applies to expect_loop=true cases" in msg


def test_graceful_mode_accepts_either_loop_outcome() -> None:
    # expect_loop=None → only universal checks; a turn that loops and one
    # that doesn't both pass, but a leaked need-request or cap breach fails.
    looped = _loop_events(rounds=1)
    quiet = _loop_events(rounds=0, round1=3, cited=[])
    assert grade_loop_case(_case(expect_loop=None), events_fn=lambda _c: looped).passed
    assert grade_loop_case(_case(expect_loop=None), events_fn=lambda _c: quiet).passed
    over_cap = _loop_events(rounds=3)
    assert not grade_loop_case(_case(expect_loop=None), events_fn=lambda _c: over_cap).passed


# ----------------------------------------------------------------------------
# grading — loop cases


def test_loop_case_passes_when_round2_cited() -> None:
    result = grade_loop_case(
        _case(kinds=frozenset({"transcript"})), events_fn=lambda _c: _loop_events()
    )
    assert result.passed and result.score == 1.0
    assert result.failure_stage is None


def test_loop_case_fails_when_loop_never_triggers() -> None:
    events = _loop_events(rounds=0, round1=3, total=3, cited=[])
    result = grade_loop_case(_case(), events_fn=lambda _c: events)
    assert not result.passed
    assert result.failure_stage == "contract"
    assert result.judge_rationale is not None
    assert "loop triggered" in result.judge_rationale
    assert "cites round-2" in result.judge_rationale
    assert 0.0 < result.score < 1.0  # partial credit: prose/cap checks still pass


def test_loop_case_fails_when_only_round1_cited() -> None:
    events = _loop_events(cited=[{"n": 1, "kind": "fact", "label": "TST · Revenue"}])
    result = grade_loop_case(_case(kinds=frozenset({"transcript"})), events_fn=lambda _c: events)
    assert not result.passed
    assert result.judge_rationale is not None
    assert "cites round-2" in result.judge_rationale


def test_loop_case_fails_on_leaked_need_request() -> None:
    leaked = json.dumps({"need": [{"kind": "transcript", "ticker": "TST"}]})
    result = grade_loop_case(_case(), events_fn=lambda _c: _loop_events(final_text=leaked))
    assert not result.passed
    assert result.judge_rationale is not None
    assert "prose" in result.judge_rationale


def test_no_loop_control_passes_and_fails() -> None:
    quiet = _loop_events(rounds=0, round1=3, total=3, cited=[])
    assert grade_loop_case(_case(expect_loop=False), events_fn=lambda _c: quiet).passed
    eager = _loop_events(rounds=1)
    result = grade_loop_case(_case(expect_loop=False), events_fn=lambda _c: eager)
    assert not result.passed
    assert result.judge_rationale is not None
    assert "did not trigger" in result.judge_rationale


def test_route_and_call_failures_are_distinct_stages() -> None:
    data_routed: list[dict[str, object]] = [{"type": "final", "text": "2 series", "route": "data"}]
    result = grade_loop_case(_case(), events_fn=lambda _c: data_routed)
    assert result.failure_stage == "route"

    errored: list[dict[str, object]] = [{"type": "error", "error": "transport down"}]
    result2 = grade_loop_case(_case(), events_fn=lambda _c: errored)
    assert result2.failure_stage == "call"
    assert result2.judge_rationale is not None
    assert "transport down" in result2.judge_rationale


# ----------------------------------------------------------------------------
# run orchestration


def test_run_builds_summary_with_injected_events(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    db.write_bytes(b"")

    def fake_events(c: LoopCase) -> list[dict[str, object]]:
        if not c.expect_loop:
            return _loop_events(rounds=0, round1=3, cited=[])
        kind = next(iter(c.expect_new_kinds), "transcript")
        return _loop_events(cited=[{"n": 3, "kind": kind, "label": "TST evidence"}])

    summary = run_ask_loop_eval(
        db_path=db,
        repo_root=tmp_path,
        golden_path=_GOLDEN,
        code_root=_REPO,
        events_fn=fake_events,
    )
    assert summary.purpose == PURPOSE
    assert summary.n_cases == len(load_ask_loop_golden(_GOLDEN))
    assert summary.n_pass == summary.n_cases
    assert summary.golden_set_sha is not None


def test_run_aborts_when_disarmed(tmp_path: Path) -> None:
    # No DB at all → followup_armed is False → configuration abort, not 0s.
    with pytest.raises(EvalAbortError, match="disarmed"):
        run_ask_loop_eval(
            db_path=tmp_path / "missing.db",
            repo_root=tmp_path,
            golden_path=_GOLDEN,
            code_root=_REPO,
        )


def test_run_aborts_when_first_case_errors_twice() -> None:
    calls: list[str] = []

    def always_error(_c: LoopCase) -> list[dict[str, object]]:
        calls.append("x")
        return [{"type": "error", "error": "down"}]

    with pytest.raises(EvalAbortError, match="errored twice"):
        run_ask_loop_eval(
            db_path=_REPO / "nonexistent.db",  # events_fn injected → no arming gate
            repo_root=_REPO,
            golden_path=_GOLDEN,
            code_root=_REPO,
            events_fn=always_error,
        )
    assert len(calls) == 2  # first case + its retry, then abort


def test_runner_purpose_lists_stay_in_sync() -> None:
    # Mirrors the ask_pack_router drift guard: the CLI runner, the dashboard
    # run bar, and coverage accounting must all know the new purpose.
    import sys

    sys.path.insert(0, str(_REPO / "execution"))
    try:
        import run_llm_evals
    finally:
        sys.path.pop(0)
    from evals.coverage import GOLDEN_PURPOSES as COVERAGE_GOLDEN
    from pipeline.evals_panel import RUNNABLE_PURPOSES

    assert PURPOSE in run_llm_evals.GOLDEN_PURPOSES
    assert PURPOSE in RUNNABLE_PURPOSES
    assert PURPOSE in COVERAGE_GOLDEN
    assert set(RUNNABLE_PURPOSES) == set(run_llm_evals.PURPOSES)
