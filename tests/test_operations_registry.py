from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from operations.registry import build_operations_registry
from runtime.service_registry import ServiceRole, managed_service_for_role, managed_service_names
from scheduler_manifest import load_manifest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_registry_projects_every_manifest_task_and_wrapper_step() -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    manifest = load_manifest(PROJECT_ROOT / "cron" / "task_manifest.json")
    assert len(registry.scheduled_tasks) == len(manifest.tasks)
    task_names = {task.task_name for task in registry.scheduled_tasks}
    assert r"\earnings-summary\run_morning_pipeline" in task_names
    assert {step.job for step in registry.job_steps} >= {
        "backfill-earnings-surprises-fetch",
        "backfill-earnings-surprises-ingest",
        "track-comp-metrics-build-sets",
        "track-comp-metrics-record",
    }
    assert all(step.effective_lane for step in registry.job_steps)
    wrapper_invocations = sum(
        wrapper.read_text(encoding="utf-8").casefold().count("run_python.bat")
        for wrapper in (PROJECT_ROOT / "cron").glob("run_*.bat")
    )
    assert len(registry.job_steps) == wrapper_invocations
    morning = next(step for step in registry.job_steps if step.job == "morning_pipeline")
    assert morning.raw_lane == "portfolio-db"
    assert morning.effective_lane == ("morning-orchestration",)
    weekly = next(step for step in registry.job_steps if step.job == "weekly-model-eval")
    assert weekly.command == (
        r"execution\run_weekly_model_eval.py",
        "--repo-root",
        "%PROJECT_ROOT%",
    )


def test_registry_projects_canonical_services_llm_sources_and_schema() -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    assert tuple(service.name for service in registry.services) == managed_service_names()
    assert tuple(service.role for service in registry.services) == ("dashboard", "capture_poller")
    assert managed_service_for_role(ServiceRole.CAPTURE_POLLER).name == "es-poller"
    assert set(registry.llm_purposes) >= {pin.purpose for pin in registry.llm_model_pins}
    assert {mode.mode for mode in registry.eval_modes} == {
        "audit",
        "capture_audit",
        "golden",
        "meta",
        "outcome",
    }
    assert registry.source_policy.policy_version
    assert {issuer.ticker_aliases[0] for issuer in registry.source_policy.issuers} == {
        "RBRK",
        "WIX",
    }
    assert registry.expected_alembic_head


def test_models_are_frozen_and_reject_extra_fields() -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    task = registry.scheduled_tasks[0]
    with pytest.raises(ValidationError):
        setattr(task, "task_name", "changed")

    payload = task.model_dump()
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        type(task).model_validate(payload)
