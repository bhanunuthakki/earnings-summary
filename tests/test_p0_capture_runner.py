from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evals.capture_quality_specs import P0_CAPTURE_QUALITY_PURPOSES
from evals.harness import EvalAbortError
from evals.p0_capture_runner import (
    PlannedTokenBudgetCaller,
    build_p0_capture_plan,
    run_p0_capture_plan,
)
from llm.prompt_versions import prompt_version_for


def _write_capture(repo: Path, purpose: str, prompt: str, *, version: str | None = None) -> None:
    directory = repo / "data" / "llm_capture"
    directory.mkdir(parents=True, exist_ok=True)
    row = {
        "captured_at": datetime.now(UTC).isoformat(),
        "purpose": purpose,
        "prompt_version": version or prompt_version_for(purpose),
        "ticker": "NU",
        "model": "test-model",
        "backend": "codex",
        "prompt": prompt,
        "response": '{"result":"grounded"}',
        "prompt_sha256": f"{purpose}-{version}-{prompt}",
    }
    path = directory / "capture_2026-07-27.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def test_plan_is_p0_only_current_version_and_zero_spend(tmp_path: Path) -> None:
    purpose = P0_CAPTURE_QUALITY_PURPOSES[0]
    _write_capture(tmp_path, purpose, "current")
    _write_capture(tmp_path, purpose, "stale", version="v999")

    plan = build_p0_capture_plan(tmp_path)

    assert {row.purpose for row in plan.purposes} == set(P0_CAPTURE_QUALITY_PURPOSES)
    selected = next(row for row in plan.purposes if row.purpose == purpose)
    assert selected.prompt_version == prompt_version_for(purpose)
    assert selected.backend == "codex"
    assert selected.available_cases == 1
    assert selected.selected_cases == 1


def test_plan_ignores_newer_fallback_backend_capture(tmp_path: Path) -> None:
    purpose = P0_CAPTURE_QUALITY_PURPOSES[0]
    _write_capture(tmp_path, purpose, "primary")
    path = tmp_path / "data" / "llm_capture" / "capture_2026-07-27.jsonl"
    fallback = {
        "captured_at": datetime.now(UTC).isoformat(),
        "purpose": purpose,
        "prompt_version": prompt_version_for(purpose),
        "ticker": "NU",
        "model": "claude-haiku",
        "backend": "claude",
        "prompt": "fallback",
        "response": '{"result":"fallback"}',
        "prompt_sha256": "fallback",
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(fallback) + "\n")

    plan = build_p0_capture_plan(tmp_path)

    selected = next(row for row in plan.purposes if row.purpose == purpose)
    assert selected.available_cases == 1
    assert selected.selected_cases == 1


def test_plan_never_exceeds_ceiling(tmp_path: Path) -> None:
    purpose = P0_CAPTURE_QUALITY_PURPOSES[0]
    _write_capture(tmp_path, purpose, "x" * 20_000)

    plan = build_p0_capture_plan(tmp_path, max_planned_tokens=10)

    assert plan.planned_tokens <= plan.max_planned_tokens
    assert plan.selected_cases == 0
    assert plan.available_cases == 1


def test_budgeted_caller_refuses_before_plan_limit() -> None:
    calls = 0

    def fake(prompt: str, **_kwargs: object) -> str:
        nonlocal calls
        calls += 1
        return prompt

    caller = PlannedTokenBudgetCaller(max_planned_tokens=600, caller=fake)
    with pytest.raises(EvalAbortError, match="limit reached before judge call"):
        caller("x" * 1_000)
    assert calls == 0


def test_execute_uses_judge_only_for_selected_cases(tmp_path: Path) -> None:
    purpose = P0_CAPTURE_QUALITY_PURPOSES[0]
    _write_capture(tmp_path, purpose, "source")
    plan = build_p0_capture_plan(tmp_path)
    calls: list[dict[str, object]] = []

    def fake(_prompt: str, **kwargs: object) -> str:
        calls.append(kwargs)
        return json.dumps(
            {
                "facet_scores": {
                    "source_fidelity": 1.0,
                    "reasoning_quality": 1.0,
                    "countercase": 1.0,
                    "calibration": 1.0,
                },
                "rationale": "Grounded.",
            }
        )

    summaries = run_p0_capture_plan(
        plan,
        repo_root=tmp_path,
        code_root=tmp_path,
        caller=fake,
    )

    assert len(summaries) == 1
    assert summaries[0].purpose == purpose
    assert summaries[0].n_cases == 1
    assert calls and all(call["purpose"] == "eval_judge" for call in calls)
    assert all(call["backend"] == "codex" for call in calls)
