"""Mode-A grader for the ``scenario_prior`` LLM purpose (dcf.scenario_prior).

The purpose sets per-name Bull/Base/Bear weights that move allocation, so the
golden set grades the two things that matter for a RISKY prior — deterministically,
no judge:

  * **directional skew** — given a pinned thesis/bear anchor block whose asymmetry
    is unambiguous, does the prior tilt the right way? A bear-skewed thesis must
    put more mass on bear than bull (and vice-versa); a balanced one must stay near
    symmetric. This is the headline ``score``.
  * **a real, grounded call** — the weights came back a valid non-degenerate simplex
    (``set_by == "llm"``, not the global fallback) WITH a non-empty rationale.

Per case ``score`` = 0.7*skew_ok + 0.3*grounded_ok; a case ``passes`` when both
hold. ``--min-score`` gates on the mean. Abort semantics mirror the other graders:
a ``StructuredParseError`` (unusable JSON twice) scores 0 at stage ``call``; a hard
stop (budget/setup) raises ``EvalAbortError`` rather than scoring 0 against the
prompt; the first case erroring twice aborts the run (transport down != prompt
quality).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import cast
from uuid import uuid4

from dcf.scenario_prior import ScenarioPrior, propose_scenario_prior
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
from llm.structured import StructuredParseError

log = logging.getLogger(__name__)

PURPOSE = "scenario_prior"
DEFAULT_GOLDEN_RELPATH = Path("evals") / "golden" / "scenario_prior.json"

# The tail-mass gap that counts as a real skew (|bull - bear|). Below it a prior
# reads as "balanced". Matches the intent of the prompt's "move OFF symmetric only
# where the anchors justify it".
_SKEW_MARGIN = 0.05
# A case passes when both the skew and the grounded-call checks hold.
_VALID_SKEWS = frozenset({"bull", "bear", "balanced"})
# Fixed as-of so the eval is reproducible (as_of doesn't affect scoring, but a
# banned date.today() in a deterministic harness would be a smell).
_EVAL_DATE = date(2026, 1, 1)

# generate_fn returns the ScenarioPrior for a case (decouples the grader from the
# production seam; the default wrapper runs the real proposer over pinned anchors).
GenerateFn = Callable[["ScenarioPriorCase"], ScenarioPrior]


@dataclass(frozen=True, slots=True)
class ScenarioPriorCase:
    case_id: str
    ticker: str
    anchor_block: str
    expected_skew: str  # "bull" | "bear" | "balanced"
    note: str = ""


def load_scenario_prior_golden(path: Path) -> list[ScenarioPriorCase]:
    import json

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
    cases: list[ScenarioPriorCase] = []
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
        ticker = str(c.get("ticker") or "").strip().upper()
        if not ticker:
            errors.append(f"{label} ({case_id}): missing/empty `ticker`")
        anchor_block = str(c.get("anchor_block") or "").strip()
        if not anchor_block:
            errors.append(f"{label} ({case_id}): missing/empty `anchor_block`")
        expected_skew = str(c.get("expected_skew") or "").strip().lower()
        if expected_skew not in _VALID_SKEWS:
            errors.append(
                f"{label} ({case_id}): `expected_skew` must be one of {sorted(_VALID_SKEWS)}"
            )
        note = str(c.get("note") or "").strip()
        cases.append(
            ScenarioPriorCase(
                case_id=case_id,
                ticker=ticker,
                anchor_block=anchor_block,
                expected_skew=expected_skew,
                note=note,
            )
        )
    if errors:
        raise ValueError(f"golden file invalid at {path}: " + "; ".join(errors))
    return cases


def _skew_ok(prior: ScenarioPrior, expected: str) -> bool:
    gap = prior.bull - prior.bear
    if expected == "bull":
        return gap >= _SKEW_MARGIN
    if expected == "bear":
        return -gap >= _SKEW_MARGIN
    return abs(gap) < _SKEW_MARGIN  # balanced


def grade_scenario_prior_case(case: ScenarioPriorCase, *, generate_fn: GenerateFn) -> CaseResult:
    t0 = time.monotonic()
    try:
        prior = generate_fn(case)
    except StructuredParseError as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return CaseResult(
            case_id=case.case_id,
            question=f"{PURPOSE}/{case.case_id} ({case.ticker})",
            passed=False,
            score=0.0,
            expected_json=dumps_compact({"expected_skew": case.expected_skew}),
            actual_json=None,
            failure_stage="call",
            judge_rationale=f"scenario_prior returned unusable JSON twice: {exc}",
            latency_ms=latency_ms,
        )
    except Exception as exc:
        if is_hard_stop(exc):
            raise EvalAbortError(
                f"{PURPOSE} hard stop on case {case.case_id}: {type(exc).__name__}: {exc} — "
                "aborting instead of scoring 0s."
            ) from exc
        raise
    latency_ms = int((time.monotonic() - t0) * 1000)

    skew_ok = _skew_ok(prior, case.expected_skew)
    grounded_ok = prior.set_by == "llm" and bool(prior.rationale.strip())
    score = 0.7 * (1.0 if skew_ok else 0.0) + 0.3 * (1.0 if grounded_ok else 0.0)
    passed = skew_ok and grounded_ok
    return CaseResult(
        case_id=case.case_id,
        question=f"{PURPOSE}/{case.case_id} ({case.ticker})",
        passed=passed,
        score=score,
        expected_json=dumps_compact({"expected_skew": case.expected_skew}),
        actual_json=dumps_compact(
            {
                "bull": round(prior.bull, 3),
                "base": round(prior.base, 3),
                "bear": round(prior.bear, 3),
                "set_by": prior.set_by,
            }
        ),
        failure_stage=None if passed else "mismatch",
        judge_rationale=(
            f"skew {'OK' if skew_ok else 'WRONG'} (want {case.expected_skew}, "
            f"got bull={prior.bull:.2f}/bear={prior.bear:.2f}); "
            f"grounded={'yes' if grounded_ok else f'no (set_by={prior.set_by})'}"
        ),
        latency_ms=latency_ms,
    )


def _production_generate(case: ScenarioPriorCase) -> ScenarioPrior:
    """The real proposer over the golden case's pinned anchor block — one live LLM
    call per case. Uses ``propose_scenario_prior`` (which RAISES StructuredParseError)
    so a transport/format failure is scored as a call failure, not silently
    degraded to the global prior the way the operational wrapper would."""
    return propose_scenario_prior(case.ticker, anchor_block=case.anchor_block, today=_EVAL_DATE)


def run_scenario_prior_eval(
    *,
    golden_path: Path,
    code_root: Path,
    limit: int | None = None,
    generate_fn: GenerateFn | None = None,
) -> EvalRunSummary:
    """The full mode-A run. Does NOT persist — the caller decides
    (execution/run_llm_evals.py). ``generate_fn`` injects a fake proposer for
    tests; None = the production ``propose_scenario_prior`` over pinned anchors."""
    cases = load_scenario_prior_golden(golden_path)
    if limit is not None:
        cases = cases[: max(0, limit)]
    target: GenerateFn = generate_fn if generate_fn is not None else _production_generate

    summary = EvalRunSummary(
        run_id=uuid4().hex,
        purpose=PURPOSE,
        mode="live",
        prompt_version=prompt_version_for(PURPOSE),
        model=LLM_MODELS.get(PURPOSE, DEFAULT_MODEL),
        judge_model=None,  # deterministic skew + grounded check, no judge
        golden_set_sha=sha256_file(golden_path),
        started_at=now_naive_utc(),
        git_sha=resolve_git_sha(code_root),
    )
    for i, case in enumerate(cases):
        result = grade_scenario_prior_case(case, generate_fn=target)
        if i == 0 and result.failure_stage == "call":
            retry = grade_scenario_prior_case(case, generate_fn=target)
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
