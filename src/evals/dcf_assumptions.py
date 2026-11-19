"""Mode-A grader for the ``dcf_assumptions`` purpose (prompt v3).

Each golden case pins one synthetic company context in the same format that
``execution/refresh_dcf_assumptions.py`` builds, and runs the production prompt
through :func:`dcf.assumption_prompt.call_dcf_assumptions`. Grading is
deterministic; no judge:

* **records** - the production guardrail passes: segments match and every
  deciding value has a complete assumption record and a consensus comparison;
* **bounds** - named values fall inside a range that the pinned evidence
  supports (for example, near-term growth not pulled up to a consensus figure
  that the history and KPIs do not support);
* **no false reported facts** - no ``reported_fact`` or ``calculation`` record
  cites a metric that the context lists only as a capture gap;
* **confidence ceilings** - a value with thin evidence is not given high
  confidence.

Per case ``score`` is the share of checks that pass; a case passes when all do.
A structured-parse failure scores 0 at stage ``call``; a hard stop aborts.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from uuid import uuid4

from dcf.assumption_prompt import PURPOSE, call_dcf_assumptions, segment_guard
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
from llm.contracts import DcfAssumptionsPayload
from llm.prompt_versions import prompt_version_for
from llm.structured import StructuredParseError

log = logging.getLogger(__name__)

DEFAULT_GOLDEN_RELPATH = Path("evals") / "golden" / "dcf_assumptions.json"
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
_FACT_CLASSES = frozenset({"reported_fact", "calculation"})


@dataclass(frozen=True, slots=True)
class DcfAssumptionsCase:
    case_id: str
    ticker: str
    seg_keys: tuple[str, ...]
    context: str
    bounds: dict[str, tuple[float, float]] = field(default_factory=dict[str, tuple[float, float]])
    gap_metrics: tuple[str, ...] = ()
    max_confidence: dict[str, str] = field(default_factory=dict[str, str])
    note: str = ""


GenerateFn = Callable[[DcfAssumptionsCase], DcfAssumptionsPayload]


def _strings(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item) for item in cast("list[object]", raw)]


def _case(raw: Mapping[str, object], label: str, errors: list[str]) -> DcfAssumptionsCase:
    case_id = str(raw.get("id") or "")
    ticker = str(raw.get("ticker") or "").strip().upper()
    seg_keys = tuple(_strings(raw.get("seg_keys")))
    context = str(raw.get("context") or "").strip()
    for name, ok in (
        ("id", case_id),
        ("ticker", ticker),
        ("seg_keys", seg_keys),
        ("context", context),
    ):
        if not ok:
            errors.append(f"{label}: missing/empty `{name}`")
    bounds: dict[str, tuple[float, float]] = {}
    raw_bounds = raw.get("bounds")
    if isinstance(raw_bounds, dict):
        for key, pair in cast("dict[str, object]", raw_bounds).items():
            values = cast("list[object]", pair) if isinstance(pair, list) else []
            if len(values) != 2 or not all(isinstance(v, (int, float)) for v in values):
                errors.append(f"{label}: bound {key!r} must be [low, high]")
                continue
            low, high = float(cast("float", values[0])), float(cast("float", values[1]))
            if low > high:
                errors.append(f"{label}: bound {key!r} has low > high")
            bounds[key] = (low, high)
    ceilings: dict[str, str] = {}
    raw_ceilings = raw.get("max_confidence")
    if isinstance(raw_ceilings, dict):
        for key, level in cast("dict[str, object]", raw_ceilings).items():
            if level not in _CONFIDENCE_RANK:
                errors.append(f"{label}: max_confidence {key!r} must be low|medium|high")
                continue
            ceilings[key] = str(level)
    if not (bounds or raw.get("gap_metrics") or ceilings):
        errors.append(f"{label}: needs at least one bound, gap metric or confidence ceiling")
    return DcfAssumptionsCase(
        case_id=case_id,
        ticker=ticker,
        seg_keys=seg_keys,
        context=context,
        bounds=bounds,
        gap_metrics=tuple(_strings(raw.get("gap_metrics"))),
        max_confidence=ceilings,
        note=str(raw.get("note") or ""),
    )


def load_dcf_assumptions_golden(path: Path) -> list[DcfAssumptionsCase]:
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
    cases: list[DcfAssumptionsCase] = []
    for i, entry in enumerate(cast("list[object]", raw_cases)):
        if not isinstance(entry, dict):
            errors.append(f"cases[{i}]: must be an object")
            continue
        cases.append(_case(cast("dict[str, object]", entry), f"cases[{i}]", errors))
    ids = [c.case_id for c in cases]
    errors += [f"duplicate id {i!r}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    if errors:
        raise ValueError(f"golden file invalid at {path}: " + "; ".join(errors))
    return cases


def _value(payload: DcfAssumptionsPayload, key: str) -> float | None:
    """The number at a dotted ``key`` such as ``segments.Cloud.near_term_growth``."""
    node: object = payload.model_dump()
    for part in key.split(".") if key.count(".") <= 2 else ():
        node = cast("dict[str, object]", node).get(part) if isinstance(node, dict) else None
    if isinstance(node, bool) or not isinstance(node, (int, float)):
        return None
    return float(node)


def check_payload(
    case: DcfAssumptionsCase, payload: DcfAssumptionsPayload
) -> list[tuple[str, bool, str]]:
    """Every check for one case as ``(name, passed, detail)``."""
    ok, reason = segment_guard(case.seg_keys)(payload)
    checks: list[tuple[str, bool, str]] = [("records", ok, reason or "complete")]
    for key, (low, high) in case.bounds.items():
        value = _value(payload, key)
        inside = value is not None and low <= value <= high
        checks.append((f"bound:{key}", inside, f"{value} not in [{low}, {high}]"))
    for metric in case.gap_metrics:
        cited = [
            r.field
            for r in payload.assumption_records
            if r.anchor_class in _FACT_CLASSES and metric.lower() in r.anchor.lower()
        ]
        checks.append((f"gap:{metric}", not cited, f"cited as fact in {cited}"))
    by_field = {r.field: r for r in payload.assumption_records}
    for key, ceiling in case.max_confidence.items():
        record = by_field.get(key)
        within = record is not None and (
            _CONFIDENCE_RANK[record.confidence] <= _CONFIDENCE_RANK[ceiling]
        )
        got = record.confidence if record is not None else "no record"
        checks.append((f"confidence:{key}", within, f"{got} above {ceiling}"))
    return checks


def grade_dcf_assumptions_case(case: DcfAssumptionsCase, *, generate_fn: GenerateFn) -> CaseResult:
    question = f"{PURPOSE}/{case.case_id} ({case.ticker})"
    expected = dumps_compact(
        {
            "bounds": {k: list(v) for k, v in case.bounds.items()},
            "gap_metrics": list(case.gap_metrics),
            "max_confidence": case.max_confidence,
        }
    )
    t0 = time.monotonic()
    try:
        payload = generate_fn(case)
    except StructuredParseError as exc:
        return CaseResult(
            case_id=case.case_id,
            question=question,
            passed=False,
            score=0.0,
            expected_json=expected,
            failure_stage="call",
            judge_rationale=f"no valid result after retries: {exc}",
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
    except Exception as exc:
        if is_hard_stop(exc):
            raise EvalAbortError(
                f"{PURPOSE} hard stop on case {case.case_id}: {type(exc).__name__}: {exc}"
            ) from exc
        raise
    latency_ms = int((time.monotonic() - t0) * 1000)
    checks = check_payload(case, payload)
    failed = [f"{name}: {detail}" for name, ok, detail in checks if not ok]
    return CaseResult(
        case_id=case.case_id,
        question=question,
        passed=not failed,
        score=sum(ok for _, ok, _ in checks) / len(checks),
        expected_json=expected,
        actual_json=payload.model_dump_json(),
        failure_stage=None if not failed else "mismatch",
        judge_rationale="; ".join(failed) or f"all {len(checks)} checks passed",
        latency_ms=latency_ms,
    )


def _production_generate(case: DcfAssumptionsCase) -> DcfAssumptionsPayload:
    """One live call per case through the production prompt and guardrail."""
    return call_dcf_assumptions(case.ticker, case.context, case.seg_keys)


def run_dcf_assumptions_eval(
    *,
    golden_path: Path,
    code_root: Path,
    limit: int | None = None,
    generate_fn: GenerateFn | None = None,
) -> EvalRunSummary:
    """The full mode-A run. Does not persist; the caller decides."""
    cases = load_dcf_assumptions_golden(golden_path)
    if limit is not None:
        cases = cases[: max(0, limit)]
    target = generate_fn if generate_fn is not None else _production_generate
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
        result = grade_dcf_assumptions_case(case, generate_fn=target)
        if i == 0 and result.failure_stage == "call":
            retry = grade_dcf_assumptions_case(case, generate_fn=target)
            if retry.failure_stage == "call":
                raise EvalAbortError(
                    f"{PURPOSE}: the first case errored twice ({result.judge_rationale}); "
                    "aborting instead of scoring 0s."
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
            }
        )
    summary.finished_at = now_naive_utc()
    return summary
