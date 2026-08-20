from __future__ import annotations

import re
from pathlib import Path

from evals.capture_quality_specs import CAPTURE_QUALITY_SPECS
from evals.coverage import GOLDEN_PURPOSES, META_PURPOSES, OUTCOME_PURPOSES
from evals.rubric_judge import AUDIT_SPECS
from llm.cli import LLM_MODELS
from llm.prompt_versions import registered_purposes
from operations.models import (
    EvalModeDefinition,
    IssuerPolicyDefinition,
    JobStepDefinition,
    LLMModelPinDefinition,
    OperationsRegistry,
    QueueStateDefinition,
    ScheduleDefinition,
    ScheduledTaskDefinition,
    ServiceDefinition,
    SourcePolicyDefinition,
)
from pipeline.fmp_recovery import CircuitState, ReceiptStatus, WorkState
from pipeline.source_policy import (
    DISPLAY_ROLE_ORDER,
    SOURCE_POLICY_CONFIG,
    CollectionMode,
    CollectionSource,
    ListType,
    issuer_policies,
)
from runtime.job_runtime import effective_scheduler_write_sets
from runtime.service_registry import managed_services
from scheduler_manifest import SERVICE_OWNED_TASKS, extract_xml_metadata, load_manifest
from schema_compat import expected_head

_RUN_PYTHON = re.compile(
    r'(?im)^\s*call\s+"%PROJECT_ROOT%\\cron\\run_python\.bat"\s+'
    r'"(?P<job>[^"]+)"\s+"(?P<lane>[^"]+)"\s+(?P<command>.*?)(?:\s+>>?.*)?$'
)
_WINDOWS_ARG = re.compile(r'"([^"]*)"|(\S+)')
_REPETITION = re.compile(r"^PT(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?$")


def _receipt_ttl_seconds(schedule: ScheduleDefinition | None) -> int:
    """Own receipt freshness by cadence; it never claims live process health."""

    day = 86_400
    if schedule is None:
        return 8 * day
    if schedule.repetition_interval:
        match = _REPETITION.fullmatch(schedule.repetition_interval)
        if match:
            interval = (
                int(match.group("hours") or 0) * 3_600 + int(match.group("minutes") or 0) * 60
            )
            return max(3_600, interval * 2)
    if schedule.days_interval:
        return (schedule.days_interval + 1) * day
    if schedule.weeks_interval or schedule.days_of_week:
        return 8 * day
    if schedule.days_of_month:
        return 35 * day
    if schedule.months:
        return 370 * day
    return 8 * day


def _wrapper_steps(
    repo_root: Path, schedules: dict[str, ScheduleDefinition]
) -> tuple[JobStepDefinition, ...]:
    steps: list[JobStepDefinition] = []
    for wrapper in sorted((repo_root / "cron").glob("run_*.bat")):
        text = wrapper.read_text(encoding="utf-8", errors="strict")
        text = re.sub(r"\^\s*\r?\n\s*", " ", text)
        for ordinal, match in enumerate(_RUN_PYTHON.finditer(text), start=1):
            raw_lane = match.group("lane")
            command = tuple(
                quoted if quoted else bare
                for quoted, bare in _WINDOWS_ARG.findall(match.group("command"))
            )
            steps.append(
                JobStepDefinition(
                    wrapper=wrapper.name,
                    ordinal=ordinal,
                    job=match.group("job"),
                    raw_lane=raw_lane,
                    effective_lane=effective_scheduler_write_sets(match.group("job"), [raw_lane]),
                    command=command,
                    receipt_ttl_seconds=_receipt_ttl_seconds(schedules.get(wrapper.name)),
                )
            )
    return tuple(steps)


def build_operations_registry(repo_root: Path) -> OperationsRegistry:
    root = repo_root.resolve()
    manifest = load_manifest(root / "cron" / "task_manifest.json")
    tasks = tuple(
        ScheduledTaskDefinition(
            task_name=task.task_name,
            xml=task.xml,
            wrapper=task.wrapper,
            schedule=ScheduleDefinition(
                trigger=task.schedule.trigger,
                start_boundary=task.schedule.start_boundary,
                repetition_interval=task.schedule.repetition_interval,
                days_interval=task.schedule.days_interval,
                weeks_interval=task.schedule.weeks_interval,
                days_of_week=task.schedule.days_of_week,
                days_of_month=task.schedule.days_of_month,
                months=task.schedule.months,
            ),
            service_owned=task.task_name.casefold() in SERVICE_OWNED_TASKS,
            scheduler_expectation=(
                "absent_service_owned"
                if task.task_name.casefold() in SERVICE_OWNED_TASKS
                else "required_enabled"
                if extract_xml_metadata(root / "cron" / task.xml).enabled
                else "required_disabled"
            ),
        )
        for task in manifest.tasks
    )
    schedules = {task.wrapper: task.schedule for task in tasks}
    policies = issuer_policies()
    source_policy = SourcePolicyDefinition(
        policy_version=SOURCE_POLICY_CONFIG.policy_version,
        roles=tuple(role.value for role in ListType),
        display_roles=tuple(role.value for role in DISPLAY_ROLE_ORDER),
        sources=tuple(source.value for source in CollectionSource),
        collection_modes=tuple(mode.value for mode in CollectionMode),
        issuers=tuple(
            IssuerPolicyDefinition(
                issuer_id=policy.issuer_id,
                ticker_aliases=policy.ticker_aliases,
                policy_sha256=policy.policy_sha256,
                adapter=policy.ir.adapter_key.value,
                canonical_json=policy.canonical_json(),
            )
            for policy in policies
        ),
    )
    purposes = tuple(sorted(set(LLM_MODELS) | set(registered_purposes())))
    return OperationsRegistry(
        scheduled_tasks=tasks,
        job_steps=_wrapper_steps(root, schedules),
        services=tuple(
            ServiceDefinition(
                role=service.role.value,
                name=service.name,
                purpose=service.purpose,
            )
            for service in managed_services()
        ),
        llm_model_pins=tuple(
            LLMModelPinDefinition(purpose=purpose, model=model)
            for purpose, model in sorted(LLM_MODELS.items())
        ),
        llm_purposes=purposes,
        eval_modes=(
            EvalModeDefinition(mode="golden", purposes=tuple(sorted(GOLDEN_PURPOSES))),
            EvalModeDefinition(mode="audit", purposes=tuple(sorted(AUDIT_SPECS))),
            EvalModeDefinition(mode="capture_audit", purposes=tuple(sorted(CAPTURE_QUALITY_SPECS))),
            EvalModeDefinition(mode="outcome", purposes=tuple(sorted(OUTCOME_PURPOSES))),
            EvalModeDefinition(mode="meta", purposes=tuple(sorted(META_PURPOSES))),
        ),
        source_policy=source_policy,
        queue_states=(
            QueueStateDefinition(
                queue="fmp_work", states=tuple(state.value for state in WorkState)
            ),
            QueueStateDefinition(
                queue="fmp_circuit", states=tuple(state.value for state in CircuitState)
            ),
            QueueStateDefinition(
                queue="fmp_receipt", states=tuple(state.value for state in ReceiptStatus)
            ),
        ),
        expected_alembic_head=expected_head(root),
    )
