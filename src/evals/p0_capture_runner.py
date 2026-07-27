"""Budgeted orchestration for prospective P0 production-capture audits."""

from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from evals.capture_quality import (
    load_capture_quality_corpus,
    rubric_for_capture,
    run_capture_quality_eval,
)
from evals.capture_quality_specs import (
    CAPTURE_QUALITY_SPECS,
    P0_CAPTURE_QUALITY_PURPOSES,
)
from evals.harness import EvalAbortError, EvalRunSummary
from evals.rubric_judge import build_rubric_prompt
from llm.cli import call_llm
from llm.prompt_versions import prompt_version_for

DEFAULT_MAX_PLANNED_TOKENS = 1_200_000
DEFAULT_SINCE_DAYS = 8
PRIMARY_CAPTURE_BACKEND = "codex"
_CHARS_PER_ESTIMATED_INPUT_TOKEN = 2
_OUTPUT_TOKEN_RESERVE = 512

LlmCaller = Callable[..., str]


def plan_judge_call_tokens(prompt: str) -> int:
    """Conservative planning estimate; provider-reported usage remains authoritative."""
    return math.ceil(len(prompt) / _CHARS_PER_ESTIMATED_INPUT_TOKEN) + _OUTPUT_TOKEN_RESERVE


class PurposeCapturePlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    purpose: str
    prompt_version: str
    backend: str
    available_cases: int = Field(ge=0)
    selected_cases: int = Field(ge=0)
    planned_tokens: int = Field(ge=0)
    omitted_for_budget: int = Field(ge=0)


class P0CapturePlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_planned_tokens: int = Field(gt=0)
    planned_tokens: int = Field(ge=0)
    selected_cases: int = Field(ge=0)
    available_cases: int = Field(ge=0)
    since_days: int = Field(gt=0)
    purposes: tuple[PurposeCapturePlan, ...]


def build_p0_capture_plan(
    repo_root: Path,
    *,
    max_planned_tokens: int = DEFAULT_MAX_PLANNED_TOKENS,
    since_days: int = DEFAULT_SINCE_DAYS,
) -> P0CapturePlan:
    """Plan primary-backend cases under a conservative, non-billing estimate."""
    if max_planned_tokens <= 0:
        raise ValueError("max_planned_tokens must be positive")
    if since_days <= 0:
        raise ValueError("since_days must be positive")

    remaining = max_planned_tokens
    rows: list[PurposeCapturePlan] = []
    selected_total = 0
    available_total = 0
    planned_total = 0
    for purpose in P0_CAPTURE_QUALITY_PURPOSES:
        spec = CAPTURE_QUALITY_SPECS[purpose]
        prompt_version = prompt_version_for(purpose)
        items = load_capture_quality_corpus(
            repo_root,
            purpose,
            limit=spec.default_limit,
            since_days=since_days,
            required_prompt_version=prompt_version,
            required_backend=PRIMARY_CAPTURE_BACKEND,
        )
        rubric = rubric_for_capture(spec)
        selected = 0
        purpose_tokens = 0
        for item in items:
            estimate = plan_judge_call_tokens(build_rubric_prompt(rubric, item))
            if estimate > remaining:
                # Execution passes ``limit=selected`` back to the newest-first
                # loader, so the admitted sample must remain a prefix.
                break
            remaining -= estimate
            purpose_tokens += estimate
            planned_total += estimate
            selected += 1
            selected_total += 1
        available = len(items)
        available_total += available
        rows.append(
            PurposeCapturePlan(
                purpose=purpose,
                prompt_version=prompt_version,
                backend=PRIMARY_CAPTURE_BACKEND,
                available_cases=available,
                selected_cases=selected,
                planned_tokens=purpose_tokens,
                omitted_for_budget=available - selected,
            )
        )

    return P0CapturePlan(
        max_planned_tokens=max_planned_tokens,
        planned_tokens=planned_total,
        selected_cases=selected_total,
        available_cases=available_total,
        since_days=since_days,
        purposes=tuple(rows),
    )


class PlannedTokenBudgetCaller:
    """Bound the approved plan and force one no-fallback Codex attempt per case."""

    def __init__(
        self,
        *,
        max_planned_tokens: int,
        caller: LlmCaller = call_llm,
    ) -> None:
        if max_planned_tokens <= 0:
            raise ValueError("max_planned_tokens must be positive")
        self.max_planned_tokens = max_planned_tokens
        self.planned_tokens = 0
        self._caller = caller

    def __call__(self, prompt: str, **kwargs: object) -> str:
        estimate = plan_judge_call_tokens(prompt)
        if self.planned_tokens + estimate > self.max_planned_tokens:
            raise EvalAbortError(
                "P0 capture eval planned-token limit reached before judge call "
                f"(used={self.planned_tokens}, next={estimate}, "
                f"limit={self.max_planned_tokens})"
            )
        self.planned_tokens += estimate
        kwargs["backend"] = "codex"
        return self._caller(prompt, **kwargs)


def run_p0_capture_plan(
    plan: P0CapturePlan,
    *,
    repo_root: Path,
    code_root: Path,
    caller: LlmCaller = call_llm,
) -> list[EvalRunSummary]:
    """Execute only cases admitted by the manual, conservative plan."""
    budgeted_caller = PlannedTokenBudgetCaller(
        max_planned_tokens=plan.max_planned_tokens,
        caller=caller,
    )
    summaries: list[EvalRunSummary] = []
    for row in plan.purposes:
        if row.selected_cases == 0:
            continue
        summaries.append(
            run_capture_quality_eval(
                row.purpose,
                repo_root=repo_root,
                code_root=code_root,
                limit=row.selected_cases,
                since_days=plan.since_days,
                required_backend=PRIMARY_CAPTURE_BACKEND,
                caller=budgeted_caller,
            )
        )
    return summaries
