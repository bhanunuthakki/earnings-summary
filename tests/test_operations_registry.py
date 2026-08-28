from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from evals.capture_quality_specs import CAPTURE_QUALITY_SPECS
from evals.coverage import GOLDEN_PURPOSES, META_PURPOSES, OUTCOME_PURPOSES
from evals.rubric_judge import AUDIT_SPECS
from llm.cli import LLM_MODELS
from llm.prompt_versions import registered_purposes
from operations.registry import build_operations_registry
from pipeline.fmp_recovery import CircuitState, ReceiptStatus, WorkState
from pipeline.source_policy import (
    DISPLAY_ROLE_ORDER,
    SOURCE_POLICY_CONFIG,
    CollectionMode,
    CollectionSource,
    ListType,
    issuer_policies,
)
from runtime.service_registry import ServiceRole, managed_service_for_role, managed_service_names
from scheduler_manifest import load_manifest
from schema_compat import expected_head

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_registry_projects_every_manifest_task_and_wrapper_step() -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    manifest = load_manifest(PROJECT_ROOT / "cron" / "task_manifest.json")
    assert len(registry.scheduled_tasks) == len(manifest.tasks)
    assert tuple(
        (task.task_name, task.xml, task.wrapper) for task in registry.scheduled_tasks
    ) == tuple((task.task_name, task.xml, task.wrapper) for task in manifest.tasks)
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
    collector = next(
        task
        for task in registry.scheduled_tasks
        if task.task_name == r"\earnings-summary\collect_operations_runtime_observations"
    )
    assert collector.schedule.repetition_interval == "PT10M"
    collector_step = next(
        step for step in registry.job_steps if step.job == "collect-operations-runtime-observations"
    )
    assert collector_step.raw_lane == "operations-runtime-receipts"
    assert collector_step.effective_lane == ("operations-runtime-receipts",)
    assert collector_step.command == (
        r"execution\collect_operations_runtime_observations.py",
        "--repo-root",
        "%STATE_ROOT%",
        "--code-root",
        "%PROJECT_ROOT%",
        "--emit-receipts",
        "--json-out",
    )
    collector_xml = (
        PROJECT_ROOT / "cron" / "collect_operations_runtime_observations.task.xml"
    ).read_text(encoding="utf-8")
    assert r"runtime\earnings-summary\cron\run_collect_operations_runtime_observations.bat" in (
        collector_xml
    )
    assert r"scratch\earnings-summary" in collector_xml
    collector_wrapper = (
        PROJECT_ROOT / "cron" / "run_collect_operations_runtime_observations.bat"
    ).read_text(encoding="utf-8")
    shared_wrapper = (PROJECT_ROOT / "cron" / "run_python.bat").read_text(encoding="utf-8")
    assert 'set "ES_JOB_STATE_ROOT=%STATE_ROOT%"' in collector_wrapper
    assert 'if defined ES_JOB_STATE_ROOT set REPO_ROOT_ARG=--repo-root "%ES_JOB_STATE_ROOT%"' in (
        shared_wrapper
    )
    assert 'set "ES_JOB_STATE_ROOT="' in shared_wrapper
    tracker_api = next(
        task
        for task in registry.scheduled_tasks
        if task.task_name == r"\earnings-summary\portfolio_tracker_api"
    )
    assert tracker_api.schedule.trigger == "BootTrigger"
    assert tracker_api.service_owned is False
    assert tracker_api.scheduler_expectation == "required_enabled"
    tracker_api_step = next(
        step for step in registry.job_steps if step.job == "portfolio-tracker-api"
    )
    assert tracker_api_step.raw_lane == "portfolio-tracker-api"
    refresh_step = next(
        step for step in registry.job_steps if step.job == "refresh-portfolio-tracker"
    )
    assert refresh_step.raw_lane == "portfolio-tracker-refresh"


def test_registry_projects_canonical_services_llm_sources_and_schema() -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    assert tuple(service.name for service in registry.services) == managed_service_names()
    assert tuple(service.role for service in registry.services) == ("dashboard", "capture_poller")
    assert managed_service_for_role(ServiceRole.CAPTURE_POLLER).name == "es-poller"
    assert registry.llm_purposes == tuple(sorted(set(LLM_MODELS) | set(registered_purposes())))
    assert tuple((pin.purpose, pin.model) for pin in registry.llm_model_pins) == tuple(
        sorted(LLM_MODELS.items())
    )
    assert {mode.mode: mode.purposes for mode in registry.eval_modes} == {
        "golden": tuple(sorted(GOLDEN_PURPOSES)),
        "audit": tuple(sorted(AUDIT_SPECS)),
        "capture_audit": tuple(sorted(CAPTURE_QUALITY_SPECS)),
        "outcome": tuple(sorted(OUTCOME_PURPOSES)),
        "meta": tuple(sorted(META_PURPOSES)),
    }
    assert registry.source_policy.policy_version == SOURCE_POLICY_CONFIG.policy_version
    assert registry.source_policy.roles == tuple(role.value for role in ListType)
    assert registry.source_policy.display_roles == tuple(role.value for role in DISPLAY_ROLE_ORDER)
    assert registry.source_policy.sources == tuple(source.value for source in CollectionSource)
    assert registry.source_policy.collection_modes == tuple(mode.value for mode in CollectionMode)
    assert tuple(
        (
            item.issuer_id,
            item.ticker_aliases,
            item.policy_sha256,
            item.adapter,
            item.canonical_json,
        )
        for item in registry.source_policy.issuers
    ) == tuple(
        (
            policy.issuer_id,
            policy.ticker_aliases,
            policy.policy_sha256,
            policy.ir.adapter_key.value,
            policy.canonical_json(),
        )
        for policy in issuer_policies()
    )
    assert {item.queue: item.states for item in registry.queue_states} == {
        "fmp_work": tuple(state.value for state in WorkState),
        "fmp_circuit": tuple(state.value for state in CircuitState),
        "fmp_receipt": tuple(state.value for state in ReceiptStatus),
    }
    assert registry.expected_alembic_head == expected_head(PROJECT_ROOT)


def test_models_are_frozen_and_reject_extra_fields() -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    task = registry.scheduled_tasks[0]
    with pytest.raises(ValidationError):
        setattr(task, "task_name", "changed")

    payload = task.model_dump()
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        type(task).model_validate(payload)
