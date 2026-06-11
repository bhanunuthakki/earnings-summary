"""Pilot grader: golden-set eval of the NL -> ViewSpec compiler.

Why viewspec_compile first (directives/llm_evals_plan.md §4): deterministic
ground truth — a compiled spec either validates, executes against the DB, and
matches the golden intent, or it doesn't — on an interactive user-facing path,
while still exercising the judge layer on near-misses.

Grading ladder per case:
  1. compile  — ``compile_nl_to_viewspec`` (the PRODUCTION entry point, repair
                retry and model resolution included). Not ok -> score 0.
                ``budget_skipped`` aborts the whole run (config, not quality).
  2. execute  — ``execute_view`` must run without raising -> else score 0.
  3. compare  — normalized equality vs the golden spec (tickers/metrics as
                sets, transform+cadence exact, periods exact unless the case
                opts into ``periods_flexible``, cagr_years only under cagr).
                Match -> score 1.0, no judge spend.
  4. judge    — divergence goes to the ``eval_judge`` model: is the difference
                material to the question? Fail-closed on bad verdicts.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

from evals.harness import (
    CaseResult,
    EvalAbortError,
    EvalRunSummary,
    dumps_compact,
    now_naive_utc,
    resolve_git_sha,
    sha256_file,
)
from evals.judge import JUDGE_PURPOSE, JudgeOutcome, run_judge
from llm.cli import DEFAULT_MODEL, LLM_MODELS
from llm.prompt_versions import prompt_version_for
from viewspec.engine import execute_view
from viewspec.nl_compile import PURPOSE, compile_nl_to_viewspec
from viewspec.spec import ViewSpec

log = logging.getLogger(__name__)

DEFAULT_GOLDEN_RELPATH = Path("evals") / "golden" / "viewspec_compile.json"

JudgeFn = Callable[..., JudgeOutcome]


@dataclass(frozen=True, slots=True)
class GoldenCase:
    """One checked-in golden question + the spec it should compile to."""

    case_id: str
    question: str
    expected_spec: ViewSpec
    context_tickers: tuple[str, ...]
    periods_flexible: bool = False
    notes: str | None = None


def load_golden(path: Path) -> list[GoldenCase]:
    """Parse + validate the golden file. Raises ValueError listing every
    problem (mirrors ViewSpec.from_dict's all-problems style) so a broken
    golden set fails loudly before any LLM spend."""
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"golden file unreadable at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("golden file must be a JSON object")
    doc = cast("dict[str, object]", payload)
    if doc.get("purpose") != PURPOSE:
        raise ValueError(f"golden file purpose must be {PURPOSE!r}, got {doc.get('purpose')!r}")
    raw_cases = doc.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("golden file needs a non-empty `cases` list")

    errors: list[str] = []
    seen_ids: set[str] = set()
    cases: list[GoldenCase] = []
    for i, entry in enumerate(cast("list[object]", raw_cases)):
        label = f"cases[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{label}: must be an object")
            continue
        c = cast("dict[str, object]", entry)
        case_id = str(c.get("id") or "").strip()
        question = str(c.get("question") or "").strip()
        if not case_id:
            errors.append(f"{label}: missing id")
        elif case_id in seen_ids:
            errors.append(f"{label}: duplicate id {case_id!r}")
        seen_ids.add(case_id)
        if not question:
            errors.append(f"{label}: missing question")
        ctx_raw = c.get("context_tickers")
        ctx: tuple[str, ...] = ()
        if isinstance(ctx_raw, list):
            ctx = tuple(
                str(t).strip().upper() for t in cast("list[object]", ctx_raw) if str(t).strip()
            )
        if not ctx:
            errors.append(f"{label} ({case_id}): context_tickers must be a non-empty list")
        try:
            expected = ViewSpec.from_dict(c.get("expected_spec"))
        except ValueError as exc:
            errors.append(f"{label} ({case_id}): expected_spec invalid — {exc}")
            continue
        notes_raw = c.get("notes")
        cases.append(
            GoldenCase(
                case_id=case_id,
                question=question,
                expected_spec=expected,
                context_tickers=ctx,
                periods_flexible=bool(c.get("periods_flexible", False)),
                notes=str(notes_raw) if isinstance(notes_raw, str) else None,
            )
        )
    if errors:
        raise ValueError("golden file invalid: " + "; ".join(errors))
    return cases


def spec_diff(expected: ViewSpec, actual: ViewSpec, *, periods_flexible: bool) -> list[str]:
    """Human-readable field differences under the normalized comparison.
    Empty list == deterministic match (score 1.0, no judge)."""
    diffs: list[str] = []
    if frozenset(expected.tickers) != frozenset(actual.tickers):
        diffs.append(f"tickers: expected {sorted(expected.tickers)}, got {sorted(actual.tickers)}")
    exp_metrics = frozenset(m.token() for m in expected.metrics)
    act_metrics = frozenset(m.token() for m in actual.metrics)
    if exp_metrics != act_metrics:
        diffs.append(f"metrics: expected {sorted(exp_metrics)}, got {sorted(act_metrics)}")
    if expected.transform != actual.transform:
        diffs.append(f"transform: expected {expected.transform!r}, got {actual.transform!r}")
    if expected.cadence != actual.cadence:
        diffs.append(f"cadence: expected {expected.cadence!r}, got {actual.cadence!r}")
    if not periods_flexible and expected.periods != actual.periods:
        diffs.append(f"periods: expected {expected.periods}, got {actual.periods}")
    if expected.transform == "cagr" and expected.cagr_years != actual.cagr_years:
        diffs.append(f"cagr_years: expected {expected.cagr_years}, got {actual.cagr_years}")
    return diffs


def grade_case(
    case: GoldenCase,
    *,
    db_path: Path,
    run_id: str,
    judge: JudgeFn | None = run_judge,
) -> CaseResult:
    """Run one golden case down the grading ladder. ``judge=None`` disables
    judge spend (divergence then just fails — offline/dry mode)."""
    expected_json = dumps_compact(case.expected_spec.to_dict())
    t0 = time.monotonic()
    res = compile_nl_to_viewspec(
        case.question,
        db_path=db_path,
        context_tickers=list(case.context_tickers),
        run_id=run_id,
    )
    latency_ms = int((time.monotonic() - t0) * 1000)

    if res.status == "budget_skipped":
        raise EvalAbortError(
            f"{PURPOSE} is budget-skipped ({res.message}) — raise the cap or "
            "run with --force-budget-bypass; aborting instead of scoring 0s."
        )
    if res.status != "ok" or res.spec is None:
        return CaseResult(
            case_id=case.case_id,
            question=case.question,
            passed=False,
            score=0.0,
            expected_json=expected_json,
            actual_json=None,
            failure_stage="compile",
            judge_rationale=res.message,
            prompt_text=res.prompt_text,
            response_text=res.raw_response,
            latency_ms=latency_ms,
        )

    actual = res.spec
    actual_json = dumps_compact(actual.to_dict())
    try:
        execute_view(actual, db_path=db_path)
    except Exception as exc:
        return CaseResult(
            case_id=case.case_id,
            question=case.question,
            passed=False,
            score=0.0,
            expected_json=expected_json,
            actual_json=actual_json,
            failure_stage="execute",
            judge_rationale=f"{type(exc).__name__}: {exc}",
            prompt_text=res.prompt_text,
            response_text=res.raw_response,
            latency_ms=latency_ms,
        )

    diffs = spec_diff(case.expected_spec, actual, periods_flexible=case.periods_flexible)
    if not diffs:
        return CaseResult(
            case_id=case.case_id,
            question=case.question,
            passed=True,
            score=1.0,
            expected_json=expected_json,
            actual_json=actual_json,
            prompt_text=res.prompt_text,
            response_text=res.raw_response,
            latency_ms=latency_ms,
        )

    if judge is None:
        return CaseResult(
            case_id=case.case_id,
            question=case.question,
            passed=False,
            score=0.0,
            expected_json=expected_json,
            actual_json=actual_json,
            failure_stage="mismatch",
            judge_rationale="judge disabled; diffs: " + "; ".join(diffs),
            prompt_text=res.prompt_text,
            response_text=res.raw_response,
            latency_ms=latency_ms,
        )

    outcome = judge(case.question, expected_json, actual_json, "\n".join(diffs), run_id=run_id)
    if outcome.verdict is None:
        # Fail closed: an unjudgeable divergence is a failure, with the raw
        # judge text preserved so the judge itself stays auditable.
        return CaseResult(
            case_id=case.case_id,
            question=case.question,
            passed=False,
            score=0.0,
            expected_json=expected_json,
            actual_json=actual_json,
            failure_stage="mismatch",
            judge_verdict=outcome.raw or None,
            judge_rationale=f"judge failed: {outcome.error}",
            prompt_text=res.prompt_text,
            response_text=res.raw_response,
            latency_ms=latency_ms,
        )
    verdict = outcome.verdict
    return CaseResult(
        case_id=case.case_id,
        question=case.question,
        passed=verdict.equivalent,
        score=verdict.score,
        expected_json=expected_json,
        actual_json=actual_json,
        failure_stage=None if verdict.equivalent else "mismatch",
        judge_verdict=outcome.raw,
        judge_rationale=verdict.rationale,
        prompt_text=res.prompt_text,
        response_text=res.raw_response,
        latency_ms=latency_ms,
    )


def run_viewspec_eval(
    *,
    db_path: Path,
    golden_path: Path,
    code_root: Path,
    limit: int | None = None,
    judge: JudgeFn | None = run_judge,
    run_id: str | None = None,
) -> EvalRunSummary:
    """The full mode-A run: load golden, grade every case, summarize. Does
    NOT persist — the caller decides (execution/run_llm_evals.py).

    ``code_root`` is where the code under eval lives (this checkout) — its
    git sha stamps the run. Distinct from ``db_path``'s repo: an eval can
    grade THIS checkout's prompt against another repo's data."""
    cases = load_golden(golden_path)
    if limit is not None:
        cases = cases[: max(0, limit)]
    rid = run_id or uuid4().hex
    summary = EvalRunSummary(
        run_id=rid,
        purpose=PURPOSE,
        mode="live",
        prompt_version=prompt_version_for(PURPOSE),
        model=LLM_MODELS.get(PURPOSE, DEFAULT_MODEL),
        judge_model=LLM_MODELS.get(JUDGE_PURPOSE, DEFAULT_MODEL) if judge is not None else None,
        golden_set_sha=sha256_file(golden_path),
        started_at=now_naive_utc(),
        git_sha=resolve_git_sha(code_root),
    )
    for case in cases:
        result = grade_case(case, db_path=db_path, run_id=rid, judge=judge)
        summary.cases.append(result)
        log.info(
            {
                "event": "eval_case_graded",
                "purpose": PURPOSE,
                "case_id": case.case_id,
                "passed": result.passed,
                "score": result.score,
                "failure_stage": result.failure_stage,
            }
        )
    summary.finished_at = now_naive_utc()
    return summary
