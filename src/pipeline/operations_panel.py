"""Pure typed projection and renderer for the read-only Operations screen."""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from log_redact import sanitize_operational_text
from operations.models import (
    JobReceiptObservation,
    ObservationEnvelope,
    OperationsRegistry,
    OperationsSnapshot,
    ScheduledTaskDefinition,
    ServiceObservation,
    ServiceRow,
    ServiceState,
)
from operations.readme_governance import ReadmeGovernanceStatus
from pipeline.operations_styles import OPERATIONS_STYLE
from pipeline.provenance_panel import PROVENANCE_SECTIONS

Tone = Literal["ok", "warn", "bad"]
_BAD_JOB_STATUSES = frozenset({"failed", "blocked_schema_drift"})


class _ViewModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SurfaceDisposition(_ViewModel):
    """UI ownership for one canonical operations field, never its data identity."""

    field: str
    destination: Literal[
        "overview", "jobs", "runtime_recovery", "governance", "linked_view", "internal"
    ]
    targets: tuple[str, ...] = ()
    rationale: str

    @model_validator(mode="after")
    def _linked_targets(self) -> SurfaceDisposition:
        if (self.destination == "linked_view") != bool(self.targets):
            raise ValueError("only linked-view dispositions require concrete targets")
        return self


class RelatedOperationsView(_ViewModel):
    key: str
    label: str
    endpoint: str
    section_ids: tuple[str, ...] = ()


OPERATIONS_RELATED_VIEWS = (
    RelatedOperationsView(
        key="settings",
        label="Settings",
        endpoint="/api/panel/data_policy_settings",
    ),
    RelatedOperationsView(
        key="provenance",
        label="Data provenance",
        endpoint="/api/panel/provenance",
        section_ids=tuple(panel_id for _anchor, _label, panel_id in PROVENANCE_SECTIONS),
    ),
)


OPERATIONS_REGISTRY_SURFACE_DISPOSITIONS = (
    SurfaceDisposition(
        field="registry_version",
        destination="internal",
        rationale="Binds snapshots to the registry contract without operator-facing content.",
    ),
    SurfaceDisposition(
        field="scheduled_tasks",
        destination="jobs",
        rationale="Declares every Scheduler-owned or service-owned task row.",
    ),
    SurfaceDisposition(
        field="job_steps",
        destination="jobs",
        rationale="Nests every canonical wrapper step and execution rail under its task.",
    ),
    SurfaceDisposition(
        field="services",
        destination="runtime_recovery",
        rationale="Declares managed-service identities and purposes for runtime reconciliation.",
    ),
    SurfaceDisposition(
        field="llm_model_pins",
        destination="linked_view",
        targets=("provenance:model_eval",),
        rationale="The governed Evals and model-optimizer views own detailed model-pin evidence.",
    ),
    SurfaceDisposition(
        field="llm_purposes",
        destination="linked_view",
        targets=("provenance:evals", "provenance:model_eval"),
        rationale="The governed Evals and model-optimizer views own purpose coverage.",
    ),
    SurfaceDisposition(
        field="eval_modes",
        destination="linked_view",
        targets=("provenance:evals",),
        rationale="The governed Evals view owns mode-specific evaluation coverage.",
    ),
    SurfaceDisposition(
        field="source_policy",
        destination="linked_view",
        targets=("settings", "provenance:section_coverage"),
        rationale="Settings and Data provenance own detailed collection-policy evidence.",
    ),
    SurfaceDisposition(
        field="queue_states",
        destination="runtime_recovery",
        rationale="Registered queue and circuit vocabularies validate runtime observations.",
    ),
    SurfaceDisposition(
        field="expected_alembic_head",
        destination="runtime_recovery",
        rationale="The expected revision is reconciled against the observed database revision.",
    ),
)

OPERATIONS_SNAPSHOT_SURFACE_DISPOSITIONS = (
    SurfaceDisposition(
        field="snapshot_version",
        destination="internal",
        rationale="Versions the evidence envelope without making a health claim.",
    ),
    SurfaceDisposition(
        field="observed_at",
        destination="overview",
        rationale="Anchors the visible snapshot time and cached-fragment truthfulness.",
    ),
    SurfaceDisposition(
        field="registry_version",
        destination="internal",
        rationale="Proves which declared registry contract the snapshot reconciled.",
    ),
    SurfaceDisposition(
        field="database_identity",
        destination="runtime_recovery",
        rationale="Shows which injected database supplied the bounded observations.",
    ),
    SurfaceDisposition(
        field="schema_revision",
        destination="runtime_recovery",
        rationale="Shows expected versus actual schema heads and mismatch state.",
    ),
    SurfaceDisposition(
        field="scheduler",
        destination="jobs",
        rationale="Reconciles cached Scheduler state against every declared task.",
    ),
    SurfaceDisposition(
        field="services",
        destination="runtime_recovery",
        rationale="Reconciles cached managed-service state and freshness.",
    ),
    SurfaceDisposition(
        field="job_receipts",
        destination="jobs",
        rationale="Shows per-step terminal status, evidence time, and receipt freshness.",
    ),
    SurfaceDisposition(
        field="database_runs",
        destination="linked_view",
        targets=("provenance:cron_health",),
        rationale="Data provenance and Cron Health own detailed ingestion-run history.",
    ),
    SurfaceDisposition(
        field="source_calls",
        destination="linked_view",
        targets=("provenance:source_calls",),
        rationale="Data provenance owns detailed source-call health and completeness.",
    ),
    SurfaceDisposition(
        field="llm_calls",
        destination="linked_view",
        targets=("provenance:evals", "provenance:model_eval"),
        rationale="Evals and model-optimizer views own detailed LLM-call governance.",
    ),
    SurfaceDisposition(
        field="fmp_backlog",
        destination="runtime_recovery",
        rationale="Shows bounded backlog counts and unregistered state drift.",
    ),
    SurfaceDisposition(
        field="fmp_circuit",
        destination="runtime_recovery",
        rationale="Shows provider circuit state, recency, and vocabulary drift.",
    ),
)

OPERATIONS_AUXILIARY_SURFACE_DISPOSITIONS = (
    SurfaceDisposition(
        field="readme_status",
        destination="governance",
        rationale="README stewardship status and its guarded preview/apply actions are visible.",
    ),
)


class EvidenceView(_ViewModel):
    state: str
    tone: Tone
    label: str
    observed_label: str
    recorded_label: str
    detail: str


class StepView(_ViewModel):
    ordinal: int
    job: str
    command_label: str
    execution_rails: tuple[str, ...]
    receipt: EvidenceView


class TaskView(_ViewModel):
    task_name: str
    wrapper: str
    schedule_label: str
    service_owned: bool
    declared_owner: str
    runtime_owner: str
    scheduler_state: str
    service_runtime_state: ServiceState | None
    service_runtime_tone: Tone | None
    runtime: EvidenceView
    steps: tuple[StepView, ...]
    attention: bool
    attention_rank: int


class RuntimeRowView(_ViewModel):
    label: str
    value: str
    evidence: EvidenceView


class OperationsPanelView(_ViewModel):
    observed_label: str
    attention_count: int
    runtime_summary_tone: Tone
    tasks: tuple[TaskView, ...]
    runtime_rows: tuple[RuntimeRowView, ...]
    readme_status: ReadmeGovernanceStatus | None = None


def _clock(value: datetime | None, *, prefix: str) -> str:
    if value is None:
        return f"{prefix} unavailable"
    stamp = value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return f"{prefix} {stamp}"


def _safe(value: object) -> str:
    """Redact absolute paths before values cross the presentation boundary."""

    return sanitize_operational_text(value, mode="presentation")


def _absolute_path(value: str) -> bool:
    return PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute()


def _html(value: object) -> str:
    return escape(_safe(value), quote=True)


def _tone(state: str) -> Tone:
    if state == "current":
        return "ok"
    if state in {"missing", "stale"}:
        return "warn"
    return "bad"


def _source_label(source: str) -> str:
    if source.startswith("sqlite:"):
        return "SQLite " + source.removeprefix("sqlite:").replace("_", " ")
    normalized = source.replace("\\", "/").casefold()
    if source.startswith("scheduler:") or normalized.endswith("scheduler.latest.json"):
        return "Scheduler cached receipt"
    if source.startswith("service:") or normalized.endswith("services.latest.json"):
        return "Managed-service cached receipt"
    if "job_health" in normalized or normalized.endswith("latest.json"):
        return "canonical job receipt"
    return "Canonical cached receipt"


def _detail(observation: ObservationEnvelope) -> str:
    if observation.state == "current":
        return "Evidence is within its declared freshness contract."
    if observation.state == "missing":
        return (
            "No runtime receipt supplied"
            if "receipt" in observation.evidence_source
            else "Evidence is unavailable."
        )
    if observation.state == "stale":
        return "Evidence exists but is outside its declared freshness contract."
    return "Evidence could not be validated against its declared contract."


def _evidence(observation: ObservationEnvelope) -> EvidenceView:
    return EvidenceView(
        state=observation.state.title(),
        tone=_tone(observation.state),
        label=_source_label(observation.evidence_source),
        observed_label=_clock(observation.observed_at, prefix="Observed"),
        recorded_label=_clock(observation.evidence_recorded_at, prefix="Evidence time"),
        detail=_detail(observation),
    )


def _job_evidence(observation: object) -> EvidenceView:
    receipt_observation = JobReceiptObservation.model_validate(observation)
    base = _evidence(receipt_observation)
    receipt = receipt_observation.receipt
    if receipt is None:
        return base
    terminal = f"Terminal {receipt.status} · exit {receipt.exit_code} · {receipt.severity}"
    if receipt.detail:
        terminal += f" · {_safe(receipt.detail)}"
    failed = _job_terminal_bad(receipt.status, receipt.exit_code, receipt.severity)
    tone: Tone = "bad" if failed else "warn" if receipt.status != "ok" else base.tone
    return base.model_copy(
        update={
            "state": f"{base.state} · {receipt.status}",
            "tone": tone,
            "detail": terminal,
        }
    )


def _unavailable(label: str, observed_at: datetime) -> EvidenceView:
    return EvidenceView(
        state="Unavailable",
        tone="warn",
        label=label,
        observed_label=_clock(observed_at, prefix="Observed"),
        recorded_label="Evidence time unavailable",
        detail=f"{label} unavailable",
    )


def _schedule_label(task: ScheduledTaskDefinition) -> str:
    schedule = task.schedule
    if schedule.repetition_interval:
        return f"{schedule.trigger} · every {schedule.repetition_interval}"
    if schedule.days_interval:
        return f"{schedule.trigger} · every {schedule.days_interval} day(s)"
    if schedule.days_of_week:
        return f"{schedule.trigger} · {', '.join(schedule.days_of_week)}"
    if schedule.days_of_month:
        days = ", ".join(str(day) for day in schedule.days_of_month)
        return f"{schedule.trigger} · day {days}"
    return schedule.trigger


def _command_label(command: tuple[str, ...]) -> str:
    if not command:
        return "No declared command"
    executable_path = command[0]
    if PureWindowsPath(executable_path).is_absolute():
        executable = PureWindowsPath(executable_path).name
    elif PurePosixPath(executable_path).is_absolute():
        executable = PurePosixPath(executable_path).name
    else:
        executable = executable_path
    arguments = tuple("[path]" if _absolute_path(arg) else _safe(arg) for arg in command[1:])
    return " ".join((_safe(executable), *arguments))


def _job_terminal_bad(status: str, exit_code: int, severity: str) -> bool:
    return status in _BAD_JOB_STATUSES or exit_code != 0 or severity == "error"


def _service_tone(state: ServiceState) -> Tone:
    if state == "Running":
        return "ok"
    if state == "Stopped":
        return "bad"
    return "warn"


def _runtime_rows(
    registry: OperationsRegistry, snapshot: OperationsSnapshot
) -> tuple[RuntimeRowView, ...]:
    schema = snapshot.schema_revision.value
    schema_value = "Unavailable"
    if schema is not None:
        schema_value = (
            schema.expected_head
            if schema.matches
            else f"Mismatch · expected {schema.expected_head}"
        )
    backlog = (
        f"{sum(row.count for row in snapshot.fmp_backlog.values)} queued item(s)"
        if snapshot.fmp_backlog.state == "current"
        else "Unavailable"
    )
    circuit = (
        (
            ", ".join(f"{row.provider}: {row.state}" for row in snapshot.fmp_circuit.values)
            or "No circuit rows"
        )
        if snapshot.fmp_circuit.state == "current"
        else "Unavailable"
    )
    service_rows = {row.name: row for row in snapshot.services.values}
    service_runtime_rows: tuple[RuntimeRowView, ...] = tuple(
        RuntimeRowView(
            label=f"Managed service · {service.name}",
            value=service.purpose,
            evidence=_service_evidence(snapshot.services, service_rows.get(service.name)),
        )
        for service in registry.services
    )
    return (
        RuntimeRowView(
            label="Database schema",
            value=schema_value,
            evidence=_evidence(snapshot.schema_revision),
        ),
        RuntimeRowView(
            label="Database identity",
            value=(
                f"{len(snapshot.database_identity.values)} attached schema(s)"
                if snapshot.database_identity.state == "current"
                else "Unavailable"
            ),
            evidence=_evidence(snapshot.database_identity),
        ),
        *service_runtime_rows,
        RuntimeRowView(
            label="FMP recovery backlog",
            value=backlog,
            evidence=_evidence(snapshot.fmp_backlog),
        ),
        RuntimeRowView(
            label="FMP provider circuit",
            value=circuit,
            evidence=_evidence(snapshot.fmp_circuit),
        ),
        RuntimeRowView(
            label="Backups",
            value="Not observed",
            evidence=_unavailable("Backup evidence", snapshot.observed_at),
        ),
        RuntimeRowView(
            label="Write locks",
            value="Not observed",
            evidence=_unavailable("Lock evidence", snapshot.observed_at),
        ),
    )


def _service_evidence(observation: ServiceObservation, row: ServiceRow | None) -> EvidenceView:
    evidence = _evidence(observation)
    if observation.state != "current":
        return evidence
    state: ServiceState = row.state if row is not None else "Missing"
    return evidence.model_copy(update={"state": state, "tone": _service_tone(state)})


def build_operations_panel_view(
    registry: OperationsRegistry,
    snapshot: OperationsSnapshot,
    *,
    readme_status: ReadmeGovernanceStatus | None = None,
) -> OperationsPanelView:
    """Join declared task ownership to bounded observations without doing I/O."""

    scheduler_rows = {row.task_name: row for row in snapshot.scheduler.values}
    service_rows = {row.name: row for row in snapshot.services.values}
    receipts = {row.job: row for row in snapshot.job_receipts}
    capture_service = next(
        (service for service in registry.services if service.role == "capture_poller"), None
    )
    tasks: list[TaskView] = []
    for task in registry.scheduled_tasks:
        steps = tuple(step for step in registry.job_steps if step.wrapper == task.wrapper)
        service_runtime_state: ServiceState | None
        service_runtime_tone: Tone | None
        if task.service_owned:
            declared_owner = "Managed service"
            runtime_owner = (
                f"Managed service · {capture_service.name}"
                if capture_service is not None
                else "Managed service · unavailable"
            )
            scheduler_state = "N/A - service-owned"
            service_row = service_rows.get(capture_service.name) if capture_service else None
            service_runtime_state = service_row.state if service_row is not None else "Missing"
            service_runtime_tone = _service_tone(service_runtime_state)
            runtime_attention = snapshot.services.state != "current" or (
                service_runtime_state != "Running"
            )
            runtime = _evidence(snapshot.services)
        else:
            declared_owner = "Windows Task Scheduler"
            runtime_owner = f"Scheduled task · {task.task_name}"
            scheduler_row = scheduler_rows.get(task.task_name)
            scheduler_state = scheduler_row.state if scheduler_row is not None else "Missing"
            runtime_attention = snapshot.scheduler.state != "current" or scheduler_state not in {
                "Ready",
                "Running",
            }
            service_runtime_state = None
            service_runtime_tone = None
            runtime = _evidence(snapshot.scheduler)
        step_views = tuple(
            StepView(
                ordinal=step.ordinal,
                job=step.job,
                command_label=_command_label(step.command),
                execution_rails=step.effective_lane,
                receipt=_job_evidence(receipts[step.job]),
            )
            for step in steps
        )
        receipt_attention = any(step.receipt.tone != "ok" for step in step_views)
        receipt_attention = receipt_attention or any(
            observation.receipt is None or observation.receipt.status != "ok"
            for observation in (receipts[step.job] for step in steps)
        )
        observation_states = [
            runtime.state.casefold(),
            *(step.receipt.state.casefold() for step in step_views),
        ]
        bad_terminal = any(
            observation.receipt is not None
            and _job_terminal_bad(
                observation.receipt.status,
                observation.receipt.exit_code,
                observation.receipt.severity,
            )
            for observation in (receipts[step.job] for step in steps)
        )
        stopped_service = service_runtime_state == "Stopped"
        if bad_terminal or scheduler_state in {"Disabled", "Unknown"} or stopped_service:
            attention_rank = 0
        elif any("invalid" in state for state in observation_states):
            attention_rank = 1
        elif any("stale" in state for state in observation_states):
            attention_rank = 2
        elif (
            any("missing" in state for state in observation_states)
            or any(step.receipt.tone == "warn" for step in step_views)
            or runtime_attention
            or receipt_attention
        ):
            attention_rank = 3
        else:
            attention_rank = 4
        tasks.append(
            TaskView(
                task_name=task.task_name,
                wrapper=task.wrapper,
                schedule_label=_schedule_label(task),
                service_owned=task.service_owned,
                declared_owner=declared_owner,
                runtime_owner=runtime_owner,
                scheduler_state=scheduler_state,
                service_runtime_state=service_runtime_state,
                service_runtime_tone=service_runtime_tone,
                runtime=runtime,
                steps=step_views,
                attention=runtime_attention or receipt_attention,
                attention_rank=attention_rank,
            )
        )
    runtime_rows = _runtime_rows(registry, snapshot)
    attention = sum(task.attention for task in tasks) + sum(
        row.evidence.tone != "ok" for row in runtime_rows
    )
    if readme_status is None or readme_status.tone != "ok":
        attention += 1
    return OperationsPanelView(
        observed_label=_clock(snapshot.observed_at, prefix="Observed"),
        attention_count=attention,
        runtime_summary_tone="ok" if attention == 0 else "warn",
        tasks=tuple(
            sorted(tasks, key=lambda task: (task.attention_rank, task.task_name.casefold()))
        ),
        runtime_rows=runtime_rows,
        readme_status=readme_status,
    )


def _pill(evidence: EvidenceView) -> str:
    tone = {"ok": " k-pill-ok", "warn": " k-pill-warn", "bad": " k-pill-bad"}[evidence.tone]
    return f'<span class="k-pill{tone}">{_html(evidence.state)}</span>'


def _state_pill(state: str, tone: Tone) -> str:
    tone_class = {"ok": "k-pill-ok", "warn": "k-pill-warn", "bad": "k-pill-bad"}[tone]
    return f'<span class="k-pill {tone_class}">{_html(state)}</span>'


def _evidence_html(evidence: EvidenceView) -> str:
    return (
        '<div class="k-card-meta">'
        f"{_html(evidence.label)} · {_html(evidence.observed_label)} · "
        f"{_html(evidence.recorded_label)}</div>"
        f'<div class="k-card-meta">{_html(evidence.detail)}</div>'
    )


def _overview(view: OperationsPanelView) -> str:
    current = sum(not task.attention for task in view.tasks)
    service_owned = sum(task.declared_owner == "Managed service" for task in view.tasks)
    return (
        '<div class="ops-summary-grid">'
        f'<article class="k-well"><div class="k-label">Declared tasks</div><div class="k-card-title">{len(view.tasks)}</div></article>'
        f'<article class="k-well"><div class="k-label">Execution steps</div><div class="k-card-title">{sum(len(task.steps) for task in view.tasks)}</div></article>'
        f'<article class="k-well"><div class="k-label">Without attention</div><div class="k-card-title">{current}</div></article>'
        f'<article class="k-well"><div class="k-label">Service-owned</div><div class="k-card-title">{service_owned}</div></article>'
        "</div>"
        '<div class="k-well k-well-warn"><div class="k-card-row-title">Attention is evidence-based</div>'
        f"<p>{view.attention_count} operational or governance observation(s) need attention. Missing, stale, or invalid evidence never becomes a healthy claim.</p></div>"
    )


def _jobs(view: OperationsPanelView) -> str:
    cards: list[str] = []
    for task in view.tasks:
        steps = "".join(
            "<li>"
            f'<div class="k-card-row-title">{_html(step.job)}</div>'
            f'<div class="k-card-meta">Step {step.ordinal} · {_html(step.command_label)}</div>'
            f'<div class="k-card-meta">Execution rails: {_html(", ".join(step.execution_rails))}</div>'
            f"{_pill(step.receipt)}{_evidence_html(step.receipt)}"
            "</li>"
            for step in task.steps
        )
        search = " ".join(
            (task.task_name, task.wrapper, task.declared_owner, *(step.job for step in task.steps))
        ).casefold()
        service_fact = ""
        if task.service_runtime_state is not None and task.service_runtime_tone is not None:
            service_fact = (
                '<div><span class="k-label">Service runtime</span><div>'
                f"{_state_pill(task.service_runtime_state, task.service_runtime_tone)}</div></div>"
            )
        cards.append(
            f'<article class="k-well ops-task-card" data-operations-task-card="true" '
            f'data-attention="{str(task.attention).lower()}" data-search="{_html(search)}">'
            '<div class="k-toolbar"><div>'
            f'<div class="k-card-row-title">{_html(task.task_name)}</div>'
            f'<div class="k-card-meta">{_html(task.wrapper)} · {_html(task.schedule_label)}</div></div>'
            f'<span class="k-pill {"k-pill-warn" if task.attention else "k-pill-ok"}">'
            f"{'Attention' if task.attention else 'Observed'}</span></div>"
            '<div class="ops-task-facts">'
            f'<div><span class="k-label">Declared owner</span><div>{_html(task.declared_owner)}</div></div>'
            f'<div><span class="k-label">Runtime owner</span><div>{_html(task.runtime_owner)}</div></div>'
            f'<div><span class="k-label">Scheduler</span><div>{_html(task.scheduler_state)}</div></div>'
            f"{service_fact}"
            "</div>"
            f"{_evidence_html(task.runtime)}"
            f'<details><summary class="k-btn k-btn-quiet k-btn-sm">{len(task.steps)} execution step(s)</summary><ol>{steps}</ol></details>'
            "</article>"
        )
    return (
        '<div class="ops-job-tools"><label class="k-label" for="operations-job-search">Find a task or job</label>'
        '<input id="operations-job-search" type="search" placeholder="Search declared tasks" autocomplete="off">'
        '<div class="operations-filter" aria-label="Job visibility">'
        '<button type="button" class="k-chip k-chip-btn is-on" data-operations-filter="attention" aria-pressed="true">Attention</button>'
        '<button type="button" class="k-chip k-chip-btn" data-operations-filter="all" aria-pressed="false">All</button></div></div>'
        f'<div class="ops-task-list">{"".join(cards)}</div>'
        '<div aria-live="polite" aria-atomic="true">'
        '<div class="k-card-meta" data-operations-results></div>'
        '<div class="k-well" data-operations-empty hidden>No tasks match this view.</div></div>'
    )


def _runtime(view: OperationsPanelView) -> str:
    return (
        '<div class="ops-runtime-grid">'
        + "".join(
            '<article class="k-well">'
            '<div class="k-toolbar"><div>'
            f'<div class="k-card-row-title">{_html(row.label)}</div>'
            f'<div class="k-card-meta">{_html(row.value)}</div></div>{_pill(row.evidence)}</div>'
            f"{_evidence_html(row.evidence)}</article>"
            for row in view.runtime_rows
        )
        + "</div>"
    )


def _governance(view: OperationsPanelView) -> str:
    status = view.readme_status
    labels = {
        "not_run": "Not run",
        "approved_preview": "Approved preview",
        "applied": "Current",
        "rejected": "Needs revision",
        "stale": "Stale",
        "invalid": "Invalid",
    }
    state = status.state if status is not None else "invalid"
    tone = status.tone if status is not None else "bad"
    state_label = labels[state]
    run_id = status.run_id if status is not None and status.run_id is not None else ""
    current_sha = (
        status.current_sha256[:12]
        if status is not None and status.current_sha256 is not None
        else "Unavailable"
    )
    run_label = run_id[:12] if run_id else "No judged run"
    apply_disabled = "" if status is not None and status.can_apply else " disabled"
    return (
        '<article class="k-well" data-readme-governance="true">'
        '<div class="k-toolbar"><div>'
        '<div class="k-card-row-title">README stewardship</div>'
        '<div class="k-card-meta">Project evidence, independent judgment, and guarded atomic apply</div>'
        f'</div><span class="k-pill k-pill-{tone}" data-readme-state>{_html(state_label)}</span></div>'
        "<p>Preview first. The generator collects bounded repository evidence and every candidate is independently judged. Apply accepts only that exact approved run and refuses a stale README.</p>"
        '<div class="ops-readme-facts">'
        f'<div><span class="k-label">README SHA</span><div data-readme-sha>{_html(current_sha)}</div></div>'
        f'<div><span class="k-label">Latest judged run</span><div data-readme-run>{_html(run_label)}</div></div>'
        "</div>"
        '<div class="ops-governance-actions">'
        '<button type="button" class="k-btn k-btn-primary" data-readme-action="preview">Preview &amp; judge</button>'
        f'<button type="button" class="k-btn k-btn-quiet" data-readme-action="apply" data-run-id="{_html(run_id)}"{apply_disabled}>Apply approved candidate</button>'
        '<span class="k-card-meta" role="status" aria-live="polite" data-readme-action-status></span>'
        "</div></article>"
    )


def render_operations_panel(view: OperationsPanelView) -> str:
    """Render only the supplied projection; no database, filesystem, or network access."""

    related_views = "".join(
        f'''<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-operations-related="{_html(item.key)}" onclick="window.workOsOpenRelatedView('{escape(item.endpoint, quote=True)}', '{_html(item.label)}')">{_html(item.label)}</button>'''
        for item in OPERATIONS_RELATED_VIEWS
    )
    return f"""
<section class="k-card k-card-stack operations-panel" aria-labelledby="operations-title">
  {OPERATIONS_STYLE}
  <div class="k-toolbar">
    <div><h1 class="k-card-title" id="operations-title">Operations</h1>
      <div class="k-card-meta">Read-only declared ownership, runtime receipts, and recovery evidence · {_html(view.observed_label)}</div></div>
    <span class="k-pill k-pill-warn">{view.attention_count} need attention</span>
  </div>
  <div class="operations-related" aria-label="Related Operations views">{related_views}</div>
  <div class="operations-tabs" role="tablist" aria-label="Operations views">
    <button type="button" class="k-chip k-chip-btn k-chip-tab is-on operations-tab" id="operations-tab-overview" role="tab" aria-selected="true" aria-controls="operations-pane-overview" tabindex="0">Overview</button>
    <button type="button" class="k-chip k-chip-btn k-chip-tab operations-tab" id="operations-tab-jobs" role="tab" aria-selected="false" aria-controls="operations-pane-jobs" tabindex="-1">Jobs</button>
    <button type="button" class="k-chip k-chip-btn k-chip-tab operations-tab" id="operations-tab-runtime" role="tab" aria-selected="false" aria-controls="operations-pane-runtime" tabindex="-1">Runtime &amp; Recovery</button>
    <button type="button" class="k-chip k-chip-btn k-chip-tab operations-tab" id="operations-tab-governance" role="tab" aria-selected="false" aria-controls="operations-pane-governance" tabindex="-1">Governance</button>
  </div>
  <div id="operations-pane-overview" role="tabpanel" aria-labelledby="operations-tab-overview">{_overview(view)}</div>
  <div id="operations-pane-jobs" role="tabpanel" aria-labelledby="operations-tab-jobs" hidden>{_jobs(view)}</div>
  <div id="operations-pane-runtime" role="tabpanel" aria-labelledby="operations-tab-runtime" hidden>{_runtime(view)}</div>
  <div id="operations-pane-governance" role="tabpanel" aria-labelledby="operations-tab-governance" hidden>{_governance(view)}</div>
</section>
<script>
(() => {{
  const root = document.currentScript.previousElementSibling;
  if (!root || !root.matches('.operations-panel')) return;
  const tabs = Array.from(root.querySelectorAll('[role="tab"]'));
  const activate = (index, focus) => {{
    tabs.forEach((tab, current) => {{
      const active = current === index;
      tab.classList.toggle('is-on', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
      tab.tabIndex = active ? 0 : -1;
      const pane = root.querySelector('#' + tab.getAttribute('aria-controls'));
      if (pane) pane.hidden = !active;
    }});
    if (focus) tabs[index].focus();
  }};
  tabs.forEach((tab, index) => {{
    tab.addEventListener('click', () => activate(index, false));
    tab.addEventListener('keydown', (event) => {{
      let next = index;
      if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
      else if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = tabs.length - 1;
      else return;
      event.preventDefault();
      activate(next, true);
    }});
  }});
  const cards = Array.from(root.querySelectorAll('[data-operations-task-card]'));
  const search = root.querySelector('#operations-job-search');
  let filter = 'attention';
  const applyJobFilter = () => {{
    const query = String(search && search.value || '').trim().toLowerCase();
    let visible = 0;
    cards.forEach((card) => {{
      const attentionMatch = filter === 'all' || card.dataset.attention === 'true';
      const searchMatch = !query || String(card.dataset.search || '').includes(query);
      card.hidden = !(attentionMatch && searchMatch);
      if (!card.hidden) visible += 1;
    }});
    const empty = root.querySelector('[data-operations-empty]');
    if (empty) empty.hidden = visible !== 0;
    const results = root.querySelector('[data-operations-results]');
    if (results) results.textContent = visible + (visible === 1 ? ' task shown' : ' tasks shown');
  }};
  root.querySelectorAll('[data-operations-filter]').forEach((button) => {{
    button.addEventListener('click', () => {{
      filter = button.dataset.operationsFilter || 'attention';
      root.querySelectorAll('[data-operations-filter]').forEach((candidate) => {{
        const active = candidate === button;
        candidate.classList.toggle('is-on', active);
        candidate.setAttribute('aria-pressed', active ? 'true' : 'false');
      }});
      applyJobFilter();
    }});
  }});
  if (search) search.addEventListener('input', applyJobFilter);
  applyJobFilter();

  const readmeButtons = Array.from(root.querySelectorAll('[data-readme-action]'));
  const readmeStatus = root.querySelector('[data-readme-action-status]');
  const applyReadme = root.querySelector('[data-readme-action="apply"]');
  let readmeCanApply = Boolean(applyReadme && !applyReadme.disabled);
  const setReadmeButtons = (busy) => readmeButtons.forEach((candidate) => {{
    candidate.disabled = busy || (candidate.dataset.readmeAction === 'apply' && !readmeCanApply);
  }});
  const refreshReadmeStatus = () => fetch('/api/readme-governance/status')
    .then((response) => response.json().then((body) => ({{ok: response.ok, body}})))
    .then((result) => {{
      if (!result.ok) throw new Error(result.body.error || 'Status refresh failed');
      const body = result.body;
      const labels = {{
        not_run: 'Not run', approved_preview: 'Approved preview', applied: 'Current',
        rejected: 'Needs revision', stale: 'Stale', invalid: 'Invalid'
      }};
      const pill = root.querySelector('[data-readme-state]');
      if (pill) {{
        pill.textContent = labels[body.state] || 'Invalid';
        pill.classList.remove('k-pill-ok', 'k-pill-warn', 'k-pill-bad');
        if (body.tone === 'ok') pill.classList.add('k-pill-ok');
        else if (body.tone === 'warn') pill.classList.add('k-pill-warn');
        else pill.classList.add('k-pill-bad');
      }}
      const sha = root.querySelector('[data-readme-sha]');
      if (sha) sha.textContent = body.current_sha256 ? body.current_sha256.slice(0, 12) : 'Unavailable';
      const run = root.querySelector('[data-readme-run]');
      if (run) run.textContent = body.run_id ? body.run_id.slice(0, 12) : 'No judged run';
      if (applyReadme) {{
        applyReadme.dataset.runId = body.run_id || '';
        readmeCanApply = Boolean(body.can_apply);
      }}
      setReadmeButtons(false);
    }});
  const runReadmeAction = (button) => {{
    const action = button.dataset.readmeAction || '';
    if (action === 'apply' && !window.confirm('Apply this exact judged README candidate?')) return;
    const payload = {{action}};
    if (action === 'apply') payload.run_id = button.dataset.runId || '';
    if (action === 'apply') {{
      readmeCanApply = false;
      button.dataset.runId = '';
    }}
    setReadmeButtons(true);
    if (readmeStatus) readmeStatus.textContent = action === 'preview' ? 'Generating and judging…' : 'Applying approved candidate…';
    fetch('/actions/readme-update', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify(payload)
    }}).then((response) => response.json().then((body) => ({{ok: response.ok, body}})))
      .then((result) => {{
        if (!result.ok) throw new Error(result.body.error || 'README action failed');
        const stream = new EventSource(result.body.stream_url);
        stream.onmessage = (event) => {{
          let message;
          try {{ message = JSON.parse(event.data); }} catch (_) {{ return; }}
          if (message.event !== 'done') return;
          stream.close();
          refreshReadmeStatus().then(() => {{
            if (readmeStatus) readmeStatus.textContent = message.exit_code === 0 ? 'Complete.' : 'Stopped without applying. Review the judged status.';
          }}).catch((error) => {{
            readmeCanApply = false;
            setReadmeButtons(false);
            if (readmeStatus) readmeStatus.textContent = error.message;
          }});
        }};
        stream.onerror = () => {{
          stream.close();
          readmeCanApply = false;
          setReadmeButtons(false);
          if (readmeStatus) readmeStatus.textContent = 'Status stream interrupted.';
        }};
      }}).catch((error) => {{
        readmeCanApply = false;
        setReadmeButtons(false);
        if (readmeStatus) readmeStatus.textContent = error.message;
      }});
  }};
  readmeButtons.forEach((button) => button.addEventListener('click', () => runReadmeAction(button)));
}})();
</script>
    """


def render_operations_shell() -> str:
    """Render the stable primary-screen mount before its cached fragment arrives."""

    return (
        '<section id="screen-execution-queue" class="screen-view" role="region" '
        'aria-label="Operations">'
        '<div class="k-grid-split-rail" data-layout-signature="k-grid-split-rail">'
        '<div id="workOsOperationsMount">'
        '<section class="k-card k-card-stack">'
        '<h1 class="k-card-title">Operations</h1>'
        '<div class="k-card-meta" role="status">Loading declared ownership and runtime '
        "evidence…</div>"
        "</section></div>"
        '<aside class="k-card k-card-stack" role="complementary" '
        'aria-labelledby="workOsOperationsRailHeading">'
        '<h2 class="k-card-title" id="workOsOperationsRailHeading">Governance context</h2>'
        '<div class="k-well" role="status">Live operation observations load from the governed '
        "Operations endpoint.</div>"
        "</aside></div></section>"
    )


__all__ = [
    "OperationsPanelView",
    "build_operations_panel_view",
    "render_operations_panel",
    "render_operations_shell",
]
