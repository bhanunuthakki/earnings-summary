"""Mode-A grader for the sector-benchmark-ETF proposal
(docs/design/comparable_sets_bottoms_up.md §4, Phase 3 ratification flow).

The generator (``compute.sector_benchmark_proposal.propose_benchmark``) is a
small, closed factual-lookup task -- "which published index ETF tracks FMP
industry X" -- so the ground truth is a hand-picked set of industries with a
well-known, unambiguous answer (mirrors ``evals.key_metrics``'s reasoning:
deterministic, no judge layer).

Per case:
  * ``etf_score`` = 1.0 when the returned ``etf`` matches ``expected_etf`` (or
    an ``also_valid_etf`` alternate), or when both are ``None`` (a genuinely
    ETF-less industry correctly identified as such).
  * ``sector_etf_score`` = same construction for ``sector_etf`` /
    ``expected_sector_etf`` / ``also_valid_sector_etf``.
  * ``score`` = mean of the two -- ``sector_etf`` alone getting it right still
    earns partial credit (it's the more load-bearing of the two: every
    industry rolls up to a sector, not every industry has a dedicated ETF).
  * ``passed`` = score >= _CASE_PASS_SCORE.

Abort semantics mirror the other graders: a hard stop (budget cap / missing
CLI) raises ``EvalAbortError``; a ``StructuredParseError`` (unusable JSON
twice) scores 0 at stage ``call``; the first case erroring twice aborts.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
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
from llm.structured import StructuredParseError

log = logging.getLogger(__name__)

PURPOSE = "sector_benchmark_proposal"
DEFAULT_GOLDEN_RELPATH = Path("evals") / "golden" / "sector_benchmark_proposal.json"

# A case "passes" at this mean etf/sector_etf score. The run-level regression
# gate uses the mean score across cases (--min-score), not this.
_CASE_PASS_SCORE = 0.5

# suggest_fn returns (etf, sector_etf) for a case -- decouples the grader from
# the production SectorBenchmarkSuggestion type (the production wrapper unpacks).
SuggestFn = Callable[["SectorBenchmarkCase"], tuple[str | None, str | None]]


@dataclass(frozen=True, slots=True)
class SectorBenchmarkCase:
    case_id: str
    industry: str
    expected_etf: str | None
    expected_sector_etf: str | None
    also_valid_etf: frozenset[str] = field(default_factory=frozenset[str])
    also_valid_sector_etf: frozenset[str] = field(default_factory=frozenset[str])


# ---------------------------------------------------------------------------
# golden-file loading
# ---------------------------------------------------------------------------


def _opt_ticker(c: dict[str, object], key: str) -> str | None:
    raw = c.get(key)
    if raw is None:
        return None
    s = str(raw).strip().upper()
    return s or None


def _ticker_set(c: dict[str, object], key: str) -> frozenset[str]:
    raw = c.get(key, [])
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(str(x).strip().upper() for x in cast("list[object]", raw) if str(x).strip())


def load_sector_benchmark_golden(path: Path) -> list[SectorBenchmarkCase]:
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
    cases: list[SectorBenchmarkCase] = []
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
        industry = str(c.get("industry") or "").strip()
        if not industry:
            errors.append(f"{label} ({case_id}): missing/empty `industry`")
        if "expected_etf" not in c and "expected_sector_etf" not in c:
            errors.append(
                f"{label} ({case_id}): needs at least one of `expected_etf`/`expected_sector_etf`"
            )
        cases.append(
            SectorBenchmarkCase(
                case_id=case_id,
                industry=industry,
                expected_etf=_opt_ticker(c, "expected_etf"),
                expected_sector_etf=_opt_ticker(c, "expected_sector_etf"),
                also_valid_etf=_ticker_set(c, "also_valid_etf"),
                also_valid_sector_etf=_ticker_set(c, "also_valid_sector_etf"),
            )
        )
    if errors:
        raise ValueError(f"golden file invalid at {path}: " + "; ".join(errors))
    return cases


# ---------------------------------------------------------------------------
# grading
# ---------------------------------------------------------------------------


def _component_score(
    returned: str | None, expected: str | None, also_valid: frozenset[str]
) -> float:
    if returned == expected:
        return 1.0
    if returned is not None and returned in also_valid:
        return 1.0
    return 0.0


def grade_sector_benchmark_case(case: SectorBenchmarkCase, *, suggest_fn: SuggestFn) -> CaseResult:
    t0 = time.monotonic()
    try:
        etf, sector_etf = suggest_fn(case)
    except StructuredParseError as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return CaseResult(
            case_id=case.case_id,
            question=f"{PURPOSE}/{case.case_id} ({case.industry})",
            passed=False,
            score=0.0,
            expected_json=dumps_compact(
                {"etf": case.expected_etf, "sector_etf": case.expected_sector_etf}
            ),
            actual_json=None,
            failure_stage="call",
            judge_rationale=f"propose_benchmark returned unusable JSON twice: {exc}",
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

    etf_score = _component_score(etf, case.expected_etf, case.also_valid_etf)
    sector_score = _component_score(
        sector_etf, case.expected_sector_etf, case.also_valid_sector_etf
    )
    score = (etf_score + sector_score) / 2.0
    passed = score >= _CASE_PASS_SCORE
    return CaseResult(
        case_id=case.case_id,
        question=f"{PURPOSE}/{case.case_id} ({case.industry})",
        passed=passed,
        score=score,
        expected_json=dumps_compact(
            {"etf": case.expected_etf, "sector_etf": case.expected_sector_etf}
        ),
        actual_json=dumps_compact({"etf": etf, "sector_etf": sector_etf}),
        failure_stage=None if passed else "mismatch",
        judge_rationale=(
            f"etf_score={etf_score:.1f} sector_etf_score={sector_score:.1f} "
            f"(returned etf={etf!r} sector_etf={sector_etf!r})"
        ),
        latency_ms=latency_ms,
    )


# ---------------------------------------------------------------------------
# run orchestration
# ---------------------------------------------------------------------------


def _production_suggest(case: SectorBenchmarkCase) -> tuple[str | None, str | None]:
    """The real generator over the golden case's industry -- one live LLM call
    per case."""
    from compute.sector_benchmark_proposal import propose_benchmark

    sug = propose_benchmark(case.industry)
    return sug.etf, sug.sector_etf


def run_sector_benchmark_proposal_eval(
    *,
    golden_path: Path,
    code_root: Path,
    limit: int | None = None,
    suggest_fn: SuggestFn | None = None,
) -> EvalRunSummary:
    """The full mode-A run. Does NOT persist — the caller decides
    (execution/run_llm_evals.py). ``suggest_fn`` injects a fake generator for
    tests; None = the production ``compute.sector_benchmark_proposal.propose_benchmark``.
    """
    cases = load_sector_benchmark_golden(golden_path)
    if limit is not None:
        cases = cases[: max(0, limit)]
    target: SuggestFn = suggest_fn if suggest_fn is not None else _production_suggest

    summary = EvalRunSummary(
        run_id=uuid4().hex,
        purpose=PURPOSE,
        mode="live",
        prompt_version=prompt_version_for(PURPOSE),
        model=LLM_MODELS.get(PURPOSE, DEFAULT_MODEL),
        judge_model=None,  # hand-picked ground truth — deterministic match, no judge
        golden_set_sha=sha256_file(golden_path),
        started_at=now_naive_utc(),
        git_sha=resolve_git_sha(code_root),
    )
    for i, case in enumerate(cases):
        result = grade_sector_benchmark_case(case, suggest_fn=target)
        if i == 0 and result.failure_stage == "call":
            retry = grade_sector_benchmark_case(case, suggest_fn=target)
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
