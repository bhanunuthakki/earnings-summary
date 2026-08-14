"""Pure typed projection and renderer for the read-only Operations screen."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html import escape
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict

from operations.models import (
    JobReceiptObservation,
    ObservationEnvelope,
    OperationsRegistry,
    OperationsSnapshot,
    ScheduledTaskDefinition,
    ServiceState,
)

Tone = Literal["ok", "warn", "bad"]
_URL = re.compile(r"(?i)\b(?:https?|file)://[^\s<>\"'\u00b7;]+")
_WINDOWS_PATH = re.compile(r"(?i)(?<![\w])(?:[a-z]:[\\/]|\\\\)[^<>\r\n\"'\u00b7;]*")
_POSIX_PATH = re.compile(r"(?<![\w:])/(?!/)[^<>\r\n\"'\u00b7;]*")
_BAD_JOB_STATUSES = frozenset({"failed", "blocked_schema_drift"})


class _ViewModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


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


def _clock(value: datetime | None, *, prefix: str) -> str:
    if value is None:
        return f"{prefix} unavailable"
    stamp = value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return f"{prefix} {stamp}"


def _safe(value: object) -> str:
    """Redact absolute paths before values cross the presentation boundary."""

    protected: list[str] = []

    def protect_url(match: re.Match[str]) -> str:
        url = match.group(0)
        protected.append("file://[path]" if url.casefold().startswith("file://") else url)
        return f"\x00URL{len(protected) - 1}\x00"

    text = _URL.sub(protect_url, str(value))
    text = _WINDOWS_PATH.sub("[path]", text)
    text = _POSIX_PATH.sub("[path]", text)
    for index, url in enumerate(protected):
        text = text.replace(f"\x00URL{index}\x00", url)
    return text


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
        RuntimeRowView(
            label="Managed services",
            value=f"{len(registry.services)} configured service(s)",
            evidence=_evidence(snapshot.services),
        ),
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


def build_operations_panel_view(
    registry: OperationsRegistry, snapshot: OperationsSnapshot
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
    return OperationsPanelView(
        observed_label=_clock(snapshot.observed_at, prefix="Observed"),
        attention_count=attention,
        runtime_summary_tone="ok" if attention == 0 else "warn",
        tasks=tuple(
            sorted(tasks, key=lambda task: (task.attention_rank, task.task_name.casefold()))
        ),
        runtime_rows=runtime_rows,
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
        f"<p>{view.attention_count} task or runtime observation(s) need attention. Missing, stale, or invalid evidence never becomes a healthy claim.</p></div>"
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


def render_operations_panel(view: OperationsPanelView) -> str:
    """Render only the supplied projection; no database, filesystem, or network access."""

    return f"""
<section class="k-card k-card-stack operations-panel" aria-labelledby="operations-title">
  <style>
    .operations-panel {{ gap: var(--sp-4); }}
    .operations-related, .operations-tabs, .operations-filter {{ display: flex; gap: var(--sp-2); flex-wrap: wrap; }}
    .operations-tabs [role="tab"], .operations-related .k-btn, .operations-filter .k-chip, .ops-task-card summary {{ min-block-size: var(--touch-target-size); }}
    .operations-filter .k-chip:focus-visible {{ outline: var(--bw-thin) solid var(--accent); outline-offset: var(--sp-1); }}
    .ops-summary-grid, .ops-runtime-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(var(--grid-card-sm), 1fr)); gap: var(--sp-3); }}
    .ops-job-tools {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: end; gap: var(--sp-3); }}
    .ops-job-tools > .k-label {{ grid-column: 1 / -1; }}
    .ops-job-tools input {{ min-block-size: var(--touch-target-size); }}
    .ops-task-list {{ display: grid; gap: var(--sp-3); }}
    .ops-task-card, .ops-task-card ol {{ display: grid; gap: var(--sp-3); }}
    .ops-task-facts {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--sp-3); }}
    @media (max-width: 48rem) {{
      .ops-job-tools, .ops-task-facts {{ grid-template-columns: 1fr; }}
      .operations-related .k-btn, .operations-filter .k-chip {{ flex: 1 1 auto; }}
    }}
    .operations-panel [role="tabpanel"] {{ display: grid; gap: var(--sp-3); }}
    .operations-panel [role="tabpanel"][hidden] {{ display: none; }}
  </style>
  <div class="k-toolbar">
    <div><h1 class="k-toolbar-title" id="operations-title">Operations</h1>
      <div class="k-card-meta">Read-only declared ownership, runtime receipts, and recovery evidence · {_html(view.observed_label)}</div></div>
    <span class="k-pill k-pill-warn">{view.attention_count} need attention</span>
  </div>
  <div class="operations-related" aria-label="Related Operations views">
    <button type="button" class="k-btn k-btn-quiet k-btn-sm" onclick="window.workOsOpenRelatedView('/api/panel/data_policy_settings', 'Settings')">Settings</button>
    <button type="button" class="k-btn k-btn-quiet k-btn-sm" onclick="window.workOsOpenRelatedView('/api/panel/provenance', 'Data provenance')">Data provenance</button>
  </div>
  <div class="operations-tabs" role="tablist" aria-label="Operations views">
    <button type="button" class="k-chip k-chip-btn k-chip-tab is-on" style="min-block-size:var(--touch-target-size);" id="operations-tab-overview" role="tab" aria-selected="true" aria-controls="operations-pane-overview" tabindex="0">Overview</button>
    <button type="button" class="k-chip k-chip-btn k-chip-tab" style="min-block-size:var(--touch-target-size);" id="operations-tab-jobs" role="tab" aria-selected="false" aria-controls="operations-pane-jobs" tabindex="-1">Jobs</button>
    <button type="button" class="k-chip k-chip-btn k-chip-tab" style="min-block-size:var(--touch-target-size);" id="operations-tab-runtime" role="tab" aria-selected="false" aria-controls="operations-pane-runtime" tabindex="-1">Runtime &amp; Recovery</button>
  </div>
  <div id="operations-pane-overview" role="tabpanel" aria-labelledby="operations-tab-overview">{_overview(view)}</div>
  <div id="operations-pane-jobs" role="tabpanel" aria-labelledby="operations-tab-jobs" hidden>{_jobs(view)}</div>
  <div id="operations-pane-runtime" role="tabpanel" aria-labelledby="operations-tab-runtime" hidden>{_runtime(view)}</div>
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
}})();
</script>
    """


def render_operations_shell() -> str:
    """Render the stable primary-screen mount before its cached fragment arrives."""

    return (
        '<section id="screen-execution-queue" class="screen-view">'
        '<div class="k-card k-card-stack" id="workOsOperationsMount" role="status">'
        '<h1 class="k-toolbar-title">Operations</h1>'
        '<div class="k-card-meta">Loading declared ownership and runtime evidence…</div>'
        "</div></section>"
    )


__all__ = [
    "OperationsPanelView",
    "build_operations_panel_view",
    "render_operations_panel",
    "render_operations_shell",
]
