"""Mode-A contract grader for ``podcast_takeaway_summary`` (S11).

The grader checks three deterministic invariants — no judge layer needed:
  1. Non-empty output (score 0 if blank after strip).
  2. Hard char cap: ≤ 400 chars (the prompt's stated constraint).
  3. No disallowed phrase from the golden case (RSS marketing language the
     prompt explicitly bans).

It also checks keyword recall: does the output contain at least one of the
``required_keywords``? A response that names no company/metric/catalyst from
the source episode has almost certainly hallucinated or repeated generic framing.

Score semantics:
  * ``score = 1.0`` — all invariants hold AND ≥ 1 required keyword appears.
  * ``score = 0.5`` — invariants hold but no required keyword (generic framing).
  * ``score = 0.0`` — any hard invariant violated (empty / over-length / banned
    phrase). Scored at ``failure_stage="constraints"``.

A case ``passed`` when score ≥ 0.5 (invariant-clean). The run-level
regression gate (``--min-score``) should be set to 0.8 so a run that writes
mostly keyword-clean outputs still flags.

Abort semantics mirror the other graders: a hard LLM stop raises
``EvalAbortError``; a ``StructuredParseError`` (two bad JSON parses) scores
0 at stage ``call``.
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
from llm.cli import DEFAULT_MODEL, LLM_MODELS, is_hard_stop
from llm.prompt_versions import prompt_version_for

log = logging.getLogger(__name__)

PURPOSE = "podcast_takeaway_summary"
DEFAULT_GOLDEN_RELPATH = Path("evals") / "golden" / "podcast_takeaway_summary.json"

_CASE_PASS_SCORE = 0.5  # score >= this → passed (invariant-clean)
_MAX_CHARS = 400


@dataclass(frozen=True, slots=True)
class TakeawayCase:
    case_id: str
    show_name: str
    title: str
    raw_body: str | None
    required_keywords: tuple[str, ...]
    disallowed_phrases: tuple[str, ...]


# ---------------------------------------------------------------------------
# golden-file loading
# ---------------------------------------------------------------------------


def load_takeaway_golden(path: Path) -> list[TakeawayCase]:
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
    cases: list[TakeawayCase] = []
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
        show = str(c.get("show_name") or "").strip()
        if not show:
            errors.append(f"{label} ({case_id}): missing/empty `show_name`")
        title = str(c.get("title") or "").strip()
        if not title:
            errors.append(f"{label} ({case_id}): missing/empty `title`")
        raw_body_v = c.get("raw_body")
        raw_body = str(raw_body_v).strip() if raw_body_v else None
        kws_raw = c.get("required_keywords", [])
        kws: tuple[str, ...] = (
            tuple(str(k).strip() for k in cast("list[object]", kws_raw) if str(k).strip())
            if isinstance(kws_raw, list)
            else ()
        )
        bans_raw = c.get("disallowed_phrases", [])
        bans: tuple[str, ...] = (
            tuple(str(b).strip() for b in cast("list[object]", bans_raw) if str(b).strip())
            if isinstance(bans_raw, list)
            else ()
        )
        cases.append(
            TakeawayCase(
                case_id=case_id,
                show_name=show,
                title=title,
                raw_body=raw_body,
                required_keywords=kws,
                disallowed_phrases=bans,
            )
        )
    if errors:
        raise ValueError(f"golden file invalid at {path}: " + "; ".join(errors))
    return cases


# ---------------------------------------------------------------------------
# grading
# ---------------------------------------------------------------------------

TakeawayFn = Callable[["TakeawayCase"], str]


def _run_takeaway(case: TakeawayCase) -> str:
    from llm.cli import call_llm
    from signals.takeaway import build_prompt

    prompt = build_prompt(case.show_name, case.title, case.raw_body)
    return call_llm(prompt, purpose=PURPOSE).strip()


def grade_takeaway_case(
    case: TakeawayCase,
    *,
    takeaway_fn: Callable[[TakeawayCase], str] | None = None,
) -> CaseResult:
    runner: Callable[[TakeawayCase], str] = (
        takeaway_fn if takeaway_fn is not None else _run_takeaway
    )
    t0 = time.monotonic()
    try:
        output = runner(case)
    except Exception as exc:
        if is_hard_stop(exc):
            raise EvalAbortError(
                f"{PURPOSE} hard stop on case {case.case_id}: {type(exc).__name__}: {exc}"
            ) from exc
        latency_ms = int((time.monotonic() - t0) * 1000)
        return CaseResult(
            case_id=case.case_id,
            question=f"{PURPOSE}/{case.case_id}",
            passed=False,
            score=0.0,
            expected_json=dumps_compact({"required_keywords": list(case.required_keywords)}),
            actual_json=None,
            failure_stage="call",
            judge_rationale=f"runner raised: {type(exc).__name__}: {exc}",
            latency_ms=latency_ms,
        )
    latency_ms = int((time.monotonic() - t0) * 1000)

    # Hard constraint checks
    if not output:
        return CaseResult(
            case_id=case.case_id,
            question=f"{PURPOSE}/{case.case_id}",
            passed=False,
            score=0.0,
            expected_json=dumps_compact({"required_keywords": list(case.required_keywords)}),
            actual_json=dumps_compact(output),
            failure_stage="constraints",
            judge_rationale="output is empty",
            latency_ms=latency_ms,
        )
    if len(output) > _MAX_CHARS:
        return CaseResult(
            case_id=case.case_id,
            question=f"{PURPOSE}/{case.case_id}",
            passed=False,
            score=0.0,
            expected_json=dumps_compact({"required_keywords": list(case.required_keywords)}),
            actual_json=dumps_compact(output[:60] + "…"),
            failure_stage="constraints",
            judge_rationale=f"output exceeds {_MAX_CHARS} chars: {len(output)}",
            latency_ms=latency_ms,
        )
    output_lower = output.lower()
    for phrase in case.disallowed_phrases:
        if phrase.lower() in output_lower:
            return CaseResult(
                case_id=case.case_id,
                question=f"{PURPOSE}/{case.case_id}",
                passed=False,
                score=0.0,
                expected_json=dumps_compact({"required_keywords": list(case.required_keywords)}),
                actual_json=dumps_compact(output),
                failure_stage="constraints",
                judge_rationale=f"disallowed phrase present: {phrase!r}",
                latency_ms=latency_ms,
            )

    # Keyword recall
    hits = [kw for kw in case.required_keywords if kw.lower() in output_lower]
    keyword_recall = len(hits) / len(case.required_keywords) if case.required_keywords else 1.0
    score = 1.0 if keyword_recall > 0 else 0.5
    missed = [kw for kw in case.required_keywords if kw not in hits]

    return CaseResult(
        case_id=case.case_id,
        question=f"{PURPOSE}/{case.case_id}",
        passed=score >= _CASE_PASS_SCORE,
        score=score,
        expected_json=dumps_compact({"required_keywords": list(case.required_keywords)}),
        actual_json=dumps_compact(output),
        failure_stage=None if score >= _CASE_PASS_SCORE else "keywords",
        judge_rationale=(
            f"keyword_recall={keyword_recall:.2f}"
            + (f"; missed {missed}" if missed else "")
            + f"; chars={len(output)}"
        ),
        latency_ms=latency_ms,
    )


# ---------------------------------------------------------------------------
# run orchestration
# ---------------------------------------------------------------------------


def run_podcast_takeaway_eval(
    *,
    golden_path: Path,
    code_root: Path,
    limit: int | None = None,
    takeaway_fn: Callable[[TakeawayCase], str] | None = None,
) -> EvalRunSummary:
    cases = load_takeaway_golden(golden_path)
    if limit is not None:
        cases = cases[: max(0, limit)]

    summary = EvalRunSummary(
        run_id=uuid4().hex,
        purpose=PURPOSE,
        mode="live",
        prompt_version=prompt_version_for(PURPOSE),
        model=LLM_MODELS.get(PURPOSE, DEFAULT_MODEL),
        judge_model=None,
        golden_set_sha=sha256_file(golden_path),
        started_at=now_naive_utc(),
        git_sha=resolve_git_sha(code_root),
    )
    for i, case in enumerate(cases):
        result = grade_takeaway_case(case, takeaway_fn=takeaway_fn)
        if i == 0 and result.failure_stage == "call":
            retry = grade_takeaway_case(case, takeaway_fn=takeaway_fn)
            if retry.failure_stage == "call":
                raise EvalAbortError(
                    f"{PURPOSE}: first case errored twice ({result.judge_rationale}) — "
                    "transport looks down; aborting."
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
