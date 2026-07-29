"""Mode-A exact-map eval for the governed sealed Ask claim auditor."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

from ask.engine import CLAIM_AUDIT_ADAPTER, CLAIM_AUDIT_TEMPLATE
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
from llm.structured import call_llm_structured_with_raw

PURPOSE = "ask_claim_audit"
DEFAULT_GOLDEN_RELPATH = Path("evals") / "golden" / "ask_claim_audit.json"


@dataclass(frozen=True, slots=True)
class ClaimAuditCase:
    case_id: str
    answer: str
    evidence: str
    expected: dict[str, object]


def load_claim_audit_golden(path: Path) -> list[ClaimAuditCase]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("golden file must be a JSON object")
    payload = cast(dict[str, object], decoded)
    if payload.get("purpose") != PURPOSE:
        raise ValueError(f"golden file purpose must be {PURPOSE!r}")
    raw_cases = payload.get("eval_cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("claim-audit golden requires non-empty eval_cases")
    cases: list[ClaimAuditCase] = []
    seen: set[str] = set()
    for index, raw in enumerate(cast(list[object], raw_cases)):
        if not isinstance(raw, dict):
            raise ValueError(f"eval_cases[{index}] must be an object")
        item = cast(dict[str, object], raw)
        case_id = str(item.get("id") or "")
        answer = str(item.get("answer") or "")
        evidence = str(item.get("evidence") or "")
        expected = item.get("expected")
        if (
            not case_id
            or case_id in seen
            or not answer
            or not evidence
            or not isinstance(expected, dict)
        ):
            raise ValueError(f"eval_cases[{index}] is incomplete or duplicated")
        seen.add(case_id)
        validated = CLAIM_AUDIT_ADAPTER.validate_python(expected)
        cases.append(
            ClaimAuditCase(
                case_id=case_id,
                answer=answer,
                evidence=evidence,
                expected=validated.model_dump(mode="json"),
            )
        )
    return cases


def run_claim_audit_eval(
    *,
    db_path: Path,
    golden_path: Path,
    code_root: Path,
    limit: int | None = None,
) -> EvalRunSummary:
    cases = load_claim_audit_golden(golden_path)
    if limit is not None:
        cases = cases[: max(0, limit)]
    run_id = uuid4().hex
    summary = EvalRunSummary(
        run_id=run_id,
        purpose=PURPOSE,
        mode="live",
        prompt_version=prompt_version_for(PURPOSE),
        model=LLM_MODELS.get(PURPOSE, DEFAULT_MODEL),
        judge_model=None,
        golden_set_sha=sha256_file(golden_path),
        started_at=now_naive_utc(),
        git_sha=resolve_git_sha(code_root),
    )
    for case in cases:
        prompt = CLAIM_AUDIT_TEMPLATE.render(
            repair_feedback="",
            answer=case.answer,
            evidence=case.evidence,
        )
        started = time.monotonic()
        try:
            result = call_llm_structured_with_raw(
                prompt,
                purpose=PURPOSE,
                scope="eval",
                run_id=run_id,
                db_path=db_path,
                schema=CLAIM_AUDIT_ADAPTER,
                repair_prompt=lambda error, case=case: CLAIM_AUDIT_TEMPLATE.render(
                    repair_feedback=f"Schema error: {error}. Return corrected JSON.",
                    answer=case.answer,
                    evidence=case.evidence,
                ),
            )
            actual = result.value.model_dump(mode="json")
            passed = actual == case.expected
            failure_stage = None if passed else "mismatch"
            response_text = result.raw_response
        except Exception as exc:
            if is_hard_stop(exc):
                raise EvalAbortError(f"{PURPOSE}/{case.case_id} could not run: {exc}") from exc
            actual = {"error": str(exc)}
            passed = False
            failure_stage = "call"
            response_text = None
        summary.cases.append(
            CaseResult(
                case_id=case.case_id,
                question=f"{PURPOSE}/{case.case_id}",
                passed=passed,
                score=1.0 if passed else 0.0,
                expected_json=dumps_compact(case.expected),
                actual_json=dumps_compact(actual),
                failure_stage=failure_stage,
                prompt_text=str(prompt),
                response_text=response_text,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        )
    summary.finished_at = now_naive_utc()
    return summary
