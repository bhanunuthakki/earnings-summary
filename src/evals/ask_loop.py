"""Mode-A grader for the S7 agentic evidence loop (``ask.followup``).

Each golden case (``evals/golden/ask_evidence_followup.json``) drives the
PRODUCTION ask path — ``ask.engine.respond_turn`` with the portfolio pack —
and grades the loop's behavior deterministically from the event stream the
engine already emits (no judge; the ground truth is structural):

* ``expect_loop`` true cases are authored so one-shot retrieval is PROVABLY
  insufficient (transcript quarters older than the latest two, filings older
  than the newest on disk). Checks: the loop triggered (``grounding.rounds
  >= 1``), terminated within ``MAX_ROUNDS``, the final answer is prose (not
  a leaked need-request), and it CITES round-2 evidence — a used [n] marker
  above ``grounding.evidence_round1`` (``expect_new_kinds`` narrows which
  channel must have delivered it).
* ``expect_loop`` false (strict no-loop) cases assert the loop must NOT
  trigger. Supported and unit-tested, but no live golden case uses it: with
  a noisy keyword scorer no live question reliably avoids the loop.
* ``expect_loop`` null (graceful) cases — "latest call/filing" questions
  where one-shot retrieval is borderline — assert only the universal bounds
  (prose answer, within cap, telemetry present). The model legitimately
  fetches more on some runs and not others; both are correct as long as the
  turn stays bounded.

``score`` = fraction of checks passing, so an answer that loops correctly
but fails to cite the new evidence is distinguishable from one that never
looped at all.

Abort semantics (mirrors ``evals.ask_router``): the loop disarmed —
DB missing or the purpose budget-skipped — is a configuration condition,
not prompt quality, so the run raises ``EvalAbortError`` instead of
recording 0s. A first case with no final frame is retried once to separate
"transport down" (abort) from a sporadic flake (scored at stage ``call``).

Cost note: every case is one streamed pass-1 call plus 0-2 follow-up calls
(purpose ``ask_evidence_followup``, ledger-attributed) — run on demand /
prompt change, not in CI.
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

from ask.followup import MAX_ROUNDS, PURPOSE, followup_armed, parse_need_request
from ask.grounding import NEED_KINDS
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

DEFAULT_GOLDEN_RELPATH = Path("evals") / "golden" / "ask_evidence_followup.json"


@dataclass(frozen=True, slots=True)
class LoopCase:
    """One golden case (see module doc for the check semantics).

    ``expect_loop``: True (loop MUST fire and cite round-2 evidence), False
    (loop must NOT fire — a clean one-shot-sufficient question), or None
    ("graceful": assert only the universal bounds — prose answer, within
    the round cap, telemetry present — without pinning whether the loop
    fired). None is the honest expectation for questions where one-shot
    keyword retrieval is borderline: the model legitimately fetches more on
    some and not others, and both are correct as long as the turn stays
    bounded."""

    case_id: str
    question: str
    expect_loop: bool | None
    tickers: tuple[str, ...] = ()
    expect_new_kinds: frozenset[str] = frozenset()


EventsFn = Callable[[LoopCase], list[dict[str, object]]]


# ---------------------------------------------------------------------------
# golden-file loading (all-problems validation, mirroring the other graders)
# ---------------------------------------------------------------------------


def load_ask_loop_golden(path: Path) -> list[LoopCase]:
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
    cases: list[LoopCase] = []
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
        question = str(c.get("question") or "")
        if not question.strip():
            errors.append(f"{label} ({case_id}): missing/empty `question`")
        expect_loop_raw = c.get("expect_loop", None)
        if expect_loop_raw is not None and not isinstance(expect_loop_raw, bool):
            errors.append(f"{label} ({case_id}): `expect_loop` must be a bool or null")
        tickers_raw = c.get("tickers", [])
        tickers = (
            tuple(str(t).upper() for t in cast("list[object]", tickers_raw))
            if isinstance(tickers_raw, list)
            else ()
        )
        kinds_raw = c.get("expect_new_kinds", [])
        if not isinstance(kinds_raw, list):
            errors.append(f"{label} ({case_id}): `expect_new_kinds` must be a list")
            kinds_raw = []
        kinds = {str(k) for k in cast("list[object]", kinds_raw)}
        unknown = kinds - NEED_KINDS
        if unknown:
            errors.append(f"{label} ({case_id}): unknown evidence kind(s): {sorted(unknown)}")
        if kinds and expect_loop_raw is not True:
            errors.append(
                f"{label} ({case_id}): `expect_new_kinds` only applies to expect_loop=true cases"
            )
        cases.append(
            LoopCase(
                case_id=case_id,
                question=question,
                expect_loop=expect_loop_raw if isinstance(expect_loop_raw, bool) else None,
                tickers=tickers,
                expect_new_kinds=frozenset(kinds & NEED_KINDS),
            )
        )
    if errors:
        raise ValueError(f"golden file invalid at {path}: " + "; ".join(errors))
    return cases


# ---------------------------------------------------------------------------
# grading
# ---------------------------------------------------------------------------


def _first(events: list[dict[str, object]], kind: str) -> dict[str, object] | None:
    return next((e for e in events if e.get("type") == kind), None)


def grade_loop_case(case: LoopCase, *, events_fn: EventsFn) -> CaseResult:
    t0 = time.monotonic()
    try:
        events = events_fn(case)
    except Exception as exc:
        return CaseResult(
            case_id=case.case_id,
            question=f"{PURPOSE}/{case.case_id}",
            passed=False,
            score=0.0,
            expected_json=_expected_json(case),
            actual_json=None,
            failure_stage="call",
            judge_rationale=f"engine raised {type(exc).__name__}: {str(exc)[:200]}",
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
    latency_ms = int((time.monotonic() - t0) * 1000)

    final = _first(events, "final")
    if final is None:
        error = _first(events, "error")
        return CaseResult(
            case_id=case.case_id,
            question=f"{PURPOSE}/{case.case_id}",
            passed=False,
            score=0.0,
            expected_json=_expected_json(case),
            actual_json=None,
            failure_stage="call",
            judge_rationale=f"no final frame ({(error or {}).get('error', 'no error frame')})",
            latency_ms=latency_ms,
        )
    if final.get("route") != "narrative" or _first(events, "fragment") is not None:
        # A golden question that routes to the data path is an authoring bug,
        # not loop quality — surface it distinctly so the case gets rewritten.
        return CaseResult(
            case_id=case.case_id,
            question=f"{PURPOSE}/{case.case_id}",
            passed=False,
            score=0.0,
            expected_json=_expected_json(case),
            actual_json=dumps_compact({"route": final.get("route")}),
            failure_stage="route",
            judge_rationale="question did not route narrative — rewrite the golden case",
            latency_ms=latency_ms,
        )

    grounding = _first(events, "grounding") or {}
    rounds = int(cast("int", grounding.get("rounds") or 0))
    round1 = int(cast("int", grounding.get("evidence_round1") or 0))
    citations = _first(events, "citations")
    cited_raw: object = citations.get("items") if citations is not None else None
    cited = (
        [cast("dict[str, object]", it) for it in cast("list[object]", cited_raw)]
        if isinstance(cited_raw, list)
        else []
    )
    final_text = str(final.get("text") or "")

    checks: list[tuple[str, bool]] = [
        (
            "final answer is prose",
            bool(final_text.strip()) and parse_need_request(final_text) is None,
        ),
        ("loop telemetry present", _first(events, "grounding") is not None),
        ("terminates within the round cap", rounds <= MAX_ROUNDS),
    ]
    cited_new = [it for it in cited if int(cast("int", it.get("n") or 0)) > round1]
    if case.expect_loop is True:
        checks.append(("loop triggered", rounds >= 1))
        checks.append(("final answer cites round-2 evidence", bool(cited_new)))
        if case.expect_new_kinds:
            checks.append(
                (
                    f"round-2 citation from {sorted(case.expect_new_kinds)}",
                    any(str(it.get("kind")) in case.expect_new_kinds for it in cited_new),
                )
            )
    elif case.expect_loop is False:
        checks.append(("loop did not trigger", rounds == 0))
    # expect_loop is None → "graceful": universal checks only (the loop may
    # legitimately fire or not on borderline one-shot retrieval; both are
    # correct as long as the turn stays prose + bounded).

    failed = [name for name, ok in checks if not ok]
    return CaseResult(
        case_id=case.case_id,
        question=f"{PURPOSE}/{case.case_id}",
        passed=not failed,
        score=(len(checks) - len(failed)) / len(checks),
        expected_json=_expected_json(case),
        actual_json=dumps_compact(
            {
                "rounds": rounds,
                "evidence_round1": round1,
                "evidence_total": int(cast("int", grounding.get("evidence_total") or 0)),
                "cited": [
                    {"n": it.get("n"), "kind": it.get("kind"), "label": it.get("label")}
                    for it in cited
                ],
            }
        ),
        failure_stage=None if not failed else "contract",
        judge_rationale=None if not failed else "failed: " + "; ".join(failed),
        response_text=final_text,
        latency_ms=latency_ms,
    )


def _expected_json(case: LoopCase) -> str:
    return dumps_compact(
        {
            "expect_loop": case.expect_loop,
            "expect_new_kinds": sorted(case.expect_new_kinds),
            "max_rounds": MAX_ROUNDS,
        }
    )


# ---------------------------------------------------------------------------
# run orchestration
# ---------------------------------------------------------------------------


def run_ask_loop_eval(
    *,
    db_path: Path,
    repo_root: Path,
    golden_path: Path,
    code_root: Path,
    limit: int | None = None,
    events_fn: EventsFn | None = None,
) -> EvalRunSummary:
    """The full mode-A run. Does NOT persist — the caller decides
    (execution/run_llm_evals.py). ``events_fn`` injects fakes for tests;
    None = the production engine against ``repo_root``/``db_path``."""
    cases = load_ask_loop_golden(golden_path)
    if limit is not None:
        cases = cases[: max(0, limit)]

    if events_fn is None and not followup_armed(db_path):
        raise EvalAbortError(
            f"{PURPOSE}: the follow-up loop is disarmed on this DB (missing DB or "
            "budget-skipped) — aborting instead of scoring 0s against the prompt."
        )

    def _prod_events(case: LoopCase) -> list[dict[str, object]]:
        from ask.context import build_portfolio_pack
        from ask.engine import AskTurn, respond_turn

        pack = build_portfolio_pack(repo_root, db_path)
        turn = AskTurn(text=case.question, tickers=list(case.tickers))
        return list(respond_turn(turn, pack, db_path=db_path, repo_root=repo_root))

    target: EventsFn = events_fn if events_fn is not None else _prod_events

    summary = EvalRunSummary(
        run_id=uuid4().hex,
        purpose=PURPOSE,
        mode="live",
        prompt_version=prompt_version_for(PURPOSE),
        model=LLM_MODELS.get(PURPOSE, DEFAULT_MODEL),
        judge_model=None,  # structural ground truth — no judge layer
        golden_set_sha=sha256_file(golden_path),
        started_at=now_naive_utc(),
        git_sha=resolve_git_sha(code_root),
    )
    for i, case in enumerate(cases):
        result = grade_loop_case(case, events_fn=target)
        if i == 0 and result.failure_stage == "call":
            # First case produced no answer — retry once to separate
            # "transport down" (abort) from a sporadic flake (score it).
            retry = grade_loop_case(case, events_fn=target)
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


__all__ = [
    "DEFAULT_GOLDEN_RELPATH",
    "PURPOSE",
    "EventsFn",
    "LoopCase",
    "grade_loop_case",
    "load_ask_loop_golden",
    "run_ask_loop_eval",
]
