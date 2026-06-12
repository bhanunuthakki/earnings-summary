"""Mode-A grader for the ask pack router (fund-grade build S4).

Two case modes over one golden file (``evals/golden/ask_pack_router.json``),
both exercising PRODUCTION entry points with deterministic grading and no
judge layer (the ground truth is closed-form):

* ``router`` — ``ask.router.route_packs`` on the live DB: the selected pack
  set vs the pinned expectation. ``passed`` = exact set match; ``score`` =
  Jaccard similarity so a near-miss (one extra pack) is distinguishable
  from garbage. The selection is advisory — a wrong extra pack costs
  budget, a wrong missing pack costs context — so partial credit mirrors
  the real cost structure.
* ``grounded`` — ``route_packs`` → ``ask.packs.load_packs`` end-to-end:
  contract invariants instead of pinned values (live stores can't be
  pinned): every ``selected_includes`` pack was selected; every
  ``require_kinds`` kind produced an item (golden sets list only packs
  certain to materialize — holdings/performance always emit an item when
  selected, even tracker-offline); every loaded item's kind was selected;
  item text is non-empty and within its pack's char budget (+ small header
  slack). ``score`` = fraction of checks passing.

Abort semantics (mirrors evals.golden_classifiers): a ``gated`` route on
the eval DB means the DB is wrong for evaluating (no tracked companies),
``skipped`` means the purpose is over budget — both raise ``EvalAbortError``
instead of recording 0s against the prompt. ``route_packs`` swallows hard
stops into ``status="error"`` by design (its interactive fail-closed
contract), so transport health is judged at run level: the run aborts when
the FIRST case errors twice in a row (transport down ≠ prompt quality);
later sporadic errors score 0 at stage ``call`` — flakiness on an
interactive surface is a real quality signal.
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

from ask.packs import PACK_KEYS, PACKS, load_packs
from ask.router import PackRoute, route_packs
from evals.harness import (
    CaseResult,
    EvalAbortError,
    EvalRunSummary,
    dumps_compact,
    now_naive_utc,
    resolve_git_sha,
    sha256_file,
)
from llm.cli import DEFAULT_MODEL, LLM_MODELS
from llm.prompt_versions import prompt_version_for

log = logging.getLogger(__name__)

PURPOSE = "ask_pack_router"
DEFAULT_GOLDEN_RELPATH = Path("evals") / "golden" / "ask_pack_router.json"

# Loaded pack text may exceed its char budget only by the fixed header/
# truncation-marker overhead (see ask.packs._join_capped callers).
_BUDGET_SLACK_CHARS = 120

RouteFn = Callable[[str], PackRoute]
LoadFn = Callable[..., list[dict[str, object]]]


@dataclass(frozen=True, slots=True)
class RouterCase:
    """One golden case; mode decides which fields apply (see module doc)."""

    case_id: str
    mode: str  # "router" | "grounded"
    question: str
    expected_packs: frozenset[str] = frozenset()  # router mode
    focus_tickers: tuple[str, ...] = ()  # grounded mode
    selected_includes: frozenset[str] = frozenset()
    require_kinds: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# golden-file loading (all-problems validation, mirroring the other graders)
# ---------------------------------------------------------------------------


def _pack_set(c: dict[str, object], key: str, label: str, errors: list[str]) -> frozenset[str]:
    raw = c.get(key, [])
    if not isinstance(raw, list):
        errors.append(f"{label}: `{key}` must be a list")
        return frozenset()
    vals = {str(x) for x in cast("list[object]", raw)}
    unknown = vals - set(PACK_KEYS)
    if unknown:
        errors.append(f"{label}: `{key}` has unknown pack key(s): {sorted(unknown)}")
    return frozenset(vals & set(PACK_KEYS))


def load_ask_router_golden(path: Path) -> list[RouterCase]:
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
    seen: set[str] = set()
    cases: list[RouterCase] = []
    for i, entry in enumerate(cast("list[object]", raw_cases)):
        label = f"cases[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{label}: must be an object")
            continue
        c = cast("dict[str, object]", entry)
        case_id = str(c.get("id") or "")
        if not case_id:
            errors.append(f"{label}: missing/empty `id`")
        if case_id in seen:
            errors.append(f"{label}: duplicate id {case_id!r}")
        seen.add(case_id)
        mode = str(c.get("mode") or "")
        if mode not in ("router", "grounded"):
            errors.append(f"{label} ({case_id}): mode must be router|grounded, got {mode!r}")
        question = str(c.get("question") or "")
        if not question.strip():
            errors.append(f"{label} ({case_id}): missing/empty `question`")
        focus_raw = c.get("focus_tickers", [])
        focus = (
            tuple(str(t).upper() for t in cast("list[object]", focus_raw))
            if isinstance(focus_raw, list)
            else ()
        )
        if mode == "router" and "expected_packs" not in c:
            errors.append(f"{label} ({case_id}): router cases need `expected_packs`")
        if mode == "grounded" and "selected_includes" not in c:
            errors.append(f"{label} ({case_id}): grounded cases need `selected_includes`")
        cases.append(
            RouterCase(
                case_id=case_id,
                mode=mode,
                question=question,
                expected_packs=_pack_set(c, "expected_packs", f"{label} ({case_id})", errors),
                focus_tickers=focus,
                selected_includes=_pack_set(c, "selected_includes", f"{label} ({case_id})", errors),
                require_kinds=_pack_set(c, "require_kinds", f"{label} ({case_id})", errors),
            )
        )
    if errors:
        raise ValueError(f"golden file invalid at {path}: " + "; ".join(errors))
    return cases


# ---------------------------------------------------------------------------
# grading
# ---------------------------------------------------------------------------


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def _route_or_abort(case: RouterCase, route_fn: RouteFn) -> tuple[PackRoute, int]:
    """Run the router for one case; abort on configuration conditions that
    must not score as quality (wrong DB, budget cap). Returns (route, ms)."""
    t0 = time.monotonic()
    route = route_fn(case.question)
    latency_ms = int((time.monotonic() - t0) * 1000)
    if route.status == "gated":
        raise EvalAbortError(
            f"{PURPOSE} gated on case {case.case_id} ({route.note}) — the eval DB has no "
            "tracked companies; aborting instead of scoring 0s."
        )
    if route.status == "skipped":
        raise EvalAbortError(
            f"{PURPOSE} budget-skipped on case {case.case_id} ({route.note}) — "
            "aborting instead of scoring 0s."
        )
    return route, latency_ms


def grade_router_case(case: RouterCase, *, route_fn: RouteFn) -> CaseResult:
    route, latency_ms = _route_or_abort(case, route_fn)
    if route.status == "error":
        return CaseResult(
            case_id=case.case_id,
            question=f"{PURPOSE}/{case.case_id}",
            passed=False,
            score=0.0,
            expected_json=dumps_compact(sorted(case.expected_packs)),
            actual_json=None,
            failure_stage="call",
            judge_rationale=f"route_packs failed closed ({route.note})",
            latency_ms=latency_ms,
        )
    actual = frozenset(route.packs)
    passed = actual == case.expected_packs
    score = _jaccard(actual, case.expected_packs)
    return CaseResult(
        case_id=case.case_id,
        question=f"{PURPOSE}/{case.case_id}",
        passed=passed,
        score=score,
        expected_json=dumps_compact(sorted(case.expected_packs)),
        actual_json=dumps_compact(sorted(actual)),
        failure_stage=None if passed else "mismatch",
        judge_rationale=(
            None
            if passed
            else f"missing {sorted(case.expected_packs - actual)}, "
            f"extra {sorted(actual - case.expected_packs)}"
        ),
        latency_ms=latency_ms,
    )


def grade_grounded_case(
    case: RouterCase, *, route_fn: RouteFn, load_fn: LoadFn, db_path: Path
) -> CaseResult:
    route, latency_ms = _route_or_abort(case, route_fn)
    if route.status == "error":
        return CaseResult(
            case_id=case.case_id,
            question=f"{PURPOSE}/{case.case_id}",
            passed=False,
            score=0.0,
            expected_json=dumps_compact({"selected_includes": sorted(case.selected_includes)}),
            actual_json=None,
            failure_stage="call",
            judge_rationale=f"route_packs failed closed ({route.note})",
            latency_ms=latency_ms,
        )
    t0 = time.monotonic()
    items = load_fn(route.packs, db_path=db_path, focus_tickers=list(case.focus_tickers))
    latency_ms += int((time.monotonic() - t0) * 1000)
    kinds = {str(i.get("kind") or "") for i in items}
    budgets = {p.key: p.char_budget for p in PACKS}

    checks: list[tuple[str, bool]] = []
    checks.append(
        ("selected_includes ⊆ selection", case.selected_includes <= frozenset(route.packs))
    )
    checks.append(("require_kinds materialized", case.require_kinds <= kinds))
    checks.append(("every loaded kind was selected", kinds <= set(route.packs)))
    texts_ok = all(
        str(i.get("text") or "").strip()
        and len(str(i.get("text") or ""))
        <= budgets.get(str(i.get("kind") or ""), 0) + _BUDGET_SLACK_CHARS
        for i in items
    )
    checks.append(("item text non-empty and within budget", texts_ok))
    checks.append(("labels non-empty", all(str(i.get("label") or "").strip() for i in items)))

    failed = [name for name, ok in checks if not ok]
    score = (len(checks) - len(failed)) / len(checks)
    passed = not failed
    return CaseResult(
        case_id=case.case_id,
        question=f"{PURPOSE}/{case.case_id}",
        passed=passed,
        score=score,
        expected_json=dumps_compact(
            {
                "selected_includes": sorted(case.selected_includes),
                "require_kinds": sorted(case.require_kinds),
            }
        ),
        actual_json=dumps_compact({"selected": sorted(route.packs), "kinds": sorted(kinds)}),
        failure_stage=None if passed else "contract",
        judge_rationale=None if passed else "failed: " + "; ".join(failed),
        latency_ms=latency_ms,
    )


# ---------------------------------------------------------------------------
# run orchestration
# ---------------------------------------------------------------------------


def run_ask_router_eval(
    *,
    db_path: Path,
    golden_path: Path,
    code_root: Path,
    limit: int | None = None,
    route_fn: RouteFn | None = None,
    load_fn: LoadFn | None = None,
) -> EvalRunSummary:
    """The full mode-A run. Does NOT persist — the caller decides
    (execution/run_llm_evals.py). ``route_fn``/``load_fn`` inject fakes for
    tests; None = the production functions against ``db_path``."""
    cases = load_ask_router_golden(golden_path)
    if limit is not None:
        cases = cases[: max(0, limit)]

    def _prod_route(question: str) -> PackRoute:
        return route_packs(question, db_path=db_path)

    target_route: RouteFn = route_fn if route_fn is not None else _prod_route
    target_load: LoadFn = load_fn if load_fn is not None else load_packs

    summary = EvalRunSummary(
        run_id=uuid4().hex,
        purpose=PURPOSE,
        mode="live",
        prompt_version=prompt_version_for(PURPOSE),
        model=LLM_MODELS.get(PURPOSE, DEFAULT_MODEL),
        judge_model=None,  # closed-form ground truth — no judge layer
        golden_set_sha=sha256_file(golden_path),
        started_at=now_naive_utc(),
        git_sha=resolve_git_sha(code_root),
    )

    def _grade(case: RouterCase) -> CaseResult:
        if case.mode == "grounded":
            return grade_grounded_case(
                case, route_fn=target_route, load_fn=target_load, db_path=db_path
            )
        return grade_router_case(case, route_fn=target_route)

    for i, case in enumerate(cases):
        result = _grade(case)
        if i == 0 and result.failure_stage == "call":
            # First case errored — retry once to separate "transport down"
            # (abort, don't score the prompt) from a sporadic flake (score it).
            retry = _grade(case)
            if retry.failure_stage == "call":
                raise EvalAbortError(
                    f"{PURPOSE}: the first case errored twice ({result.judge_rationale}) — "
                    "transport looks down; aborting instead of scoring 0s."
                )
            result = retry
        summary.cases.append(result)
        log.info(
            {
                "event": "eval_case_graded",
                "purpose": PURPOSE,
                "case_id": result.case_id,
                "passed": result.passed,
                "score": result.score,
                "failure_stage": result.failure_stage,
            }
        )
    summary.finished_at = now_naive_utc()
    return summary
