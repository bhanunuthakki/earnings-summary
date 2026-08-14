from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from operations.models import (
    JobHealthRow,
    JobReceiptObservation,
    OperationsSnapshot,
    SchedulerObservation,
    SchedulerTaskRow,
    ServiceObservation,
    ServiceRow,
    ServiceState,
)
from operations.registry import build_operations_registry
from operations.snapshot import collect_operations_snapshot
from pipeline.operations_panel import (
    OperationsPanelView,
    build_operations_panel_view,
    render_operations_panel,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OBSERVED_AT = datetime(2026, 8, 13, 12, tzinfo=UTC)


def _view(tmp_path: Path) -> OperationsPanelView:
    registry = build_operations_registry(PROJECT_ROOT)
    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT,
    )
    return build_operations_panel_view(registry, snapshot)


def test_operations_projection_groups_every_declared_task_wrapper_and_step(
    tmp_path: Path,
) -> None:
    view = _view(tmp_path)

    assert len(view.tasks) == 43
    assert sum(len(task.steps) for task in view.tasks) == 50
    assert len({task.wrapper for task in view.tasks}) == 43
    assert all(task.steps for task in view.tasks)
    capture = next(task for task in view.tasks if task.service_owned)
    assert capture.declared_owner == "Managed service"
    assert capture.scheduler_state == "N/A - service-owned"
    assert capture.runtime_owner.startswith("Managed service")
    assert all(step.execution_rails for task in view.tasks for step in task.steps)


def test_operations_renderer_has_governance_tab_and_related_views(
    tmp_path: Path,
) -> None:
    html = render_operations_panel(_view(tmp_path))
    tablist = html.split('role="tablist"', 1)[1].split("</div>", 1)[0]

    assert tablist.count('role="tab"') == 4
    assert ">Overview</button>" in tablist
    assert ">Jobs</button>" in tablist
    assert ">Runtime &amp; Recovery</button>" in tablist
    assert ">Governance</button>" in tablist
    assert ">Settings</a>" not in tablist
    assert ">Data provenance</a>" not in tablist
    assert ">Settings</button>" in html
    assert ">Data provenance</button>" in html
    assert "workOsOpenRelatedView('/api/panel/data_policy_settings', 'Settings')" in html
    assert "workOsOpenRelatedView('/api/panel/provenance', 'Data provenance')" in html
    assert "event.key === 'ArrowRight'" in html
    assert "event.key === 'ArrowLeft'" in html
    assert "event.key === 'Home'" in html
    assert "event.key === 'End'" in html
    assert 'aria-selected="true"' in tablist
    assert 'tabindex="0"' in tablist
    assert 'tabindex="-1"' in tablist
    assert "var(--touch-target-size)" in tablist
    assert 'data-operations-task-card="true"' in html
    assert 'id="operations-pane-governance"' in html
    assert "README stewardship" in html
    assert 'data-readme-action="preview"' in html
    assert 'data-readme-action="apply"' in html
    assert "Preview &amp; judge" in html
    assert "Apply approved candidate" in html
    assert "fetch('/actions/readme-update'" in html
    assert "readmeCanApply = false" in html
    assert "new EventSource" in html


def test_operations_health_views_remain_read_only_truthful_and_sanitize_evidence(
    tmp_path: Path,
) -> None:
    view = _view(tmp_path)
    html = render_operations_panel(view)

    assert "Backup evidence unavailable" in html
    assert "Lock evidence unavailable" in html
    assert "Observed 2026-08-13 12:00 UTC" in html
    assert "Evidence time unavailable" in html
    assert str(tmp_path) not in html
    assert "canonical job receipt" in html
    assert "No runtime receipt supplied" in html
    health_views = html.split('id="operations-pane-governance"', 1)[0]
    assert "<form" not in health_views
    assert 'method="post"' not in health_views.casefold()
    assert "/actions/" not in health_views
    assert "Run now" not in html


def test_attention_does_not_turn_missing_runtime_evidence_green(tmp_path: Path) -> None:
    view = _view(tmp_path)
    html = render_operations_panel(view)

    assert view.attention_count > 0
    assert view.runtime_summary_tone == "warn"
    assert '<span class="k-pill k-pill-ok">Healthy</span>' not in html


def test_failed_fresh_receipt_is_bad_first_and_exposes_terminal_evidence(
    tmp_path: Path,
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT,
    )
    failed_step = registry.job_steps[-1]
    failed = JobReceiptObservation(
        state="current",
        observed_at=OBSERVED_AT,
        evidence_source=str(tmp_path / ".tmp" / "job_health" / failed_step.job / "latest.json"),
        evidence_recorded_at=OBSERVED_AT,
        job=failed_step.job,
        receipt=JobHealthRow(
            schema_version="1",
            job=failed_step.job,
            write_sets=failed_step.effective_lane,
            started_at=OBSERVED_AT,
            ended_at=OBSERVED_AT,
            status="failed",
            exit_code=9,
            severity="error",
            detail="worker failed at C:\\private\\owner\\secret.py",
        ),
    )
    snapshot = snapshot.model_copy(
        update={
            "job_receipts": tuple(
                failed if item.job == failed_step.job else item for item in snapshot.job_receipts
            )
        }
    )

    view = build_operations_panel_view(registry, snapshot)
    first = view.tasks[0]
    failed_task = next(
        task for task in view.tasks if any(s.job == failed_step.job for s in task.steps)
    )
    html = render_operations_panel(view)

    assert first.task_name == failed_task.task_name
    receipt = next(step.receipt for step in failed_task.steps if step.job == failed_step.job)
    assert receipt.tone == "bad"
    assert "Terminal failed · exit 9 · error" in receipt.detail
    assert "worker failed" in receipt.detail
    assert "C:\\private" not in html
    assert "[path]" in html


def test_blocked_schema_drift_is_structured_bad_terminal_and_sorted_first(
    tmp_path: Path,
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT,
    )
    blocked_step = registry.job_steps[-1]
    blocked = JobReceiptObservation(
        state="current",
        observed_at=OBSERVED_AT,
        evidence_source=str(tmp_path / ".tmp" / "job_health" / blocked_step.job / "latest.json"),
        evidence_recorded_at=OBSERVED_AT,
        job=blocked_step.job,
        receipt=JobHealthRow(
            schema_version="1",
            job=blocked_step.job,
            write_sets=blocked_step.effective_lane,
            started_at=OBSERVED_AT,
            ended_at=OBSERVED_AT,
            status="blocked_schema_drift",
            exit_code=0,
            severity="warning",
        ),
    )
    snapshot = snapshot.model_copy(
        update={
            "job_receipts": tuple(
                blocked if item.job == blocked_step.job else item for item in snapshot.job_receipts
            )
        }
    )

    view = build_operations_panel_view(registry, snapshot)
    blocked_task = next(
        task for task in view.tasks if any(step.job == blocked_step.job for step in task.steps)
    )
    receipt = next(step.receipt for step in blocked_task.steps if step.job == blocked_step.job)

    assert view.tasks[0] == blocked_task
    assert blocked_task.attention_rank == 0
    assert receipt.tone == "bad"
    assert "blocked_schema_drift" in receipt.detail


@pytest.mark.parametrize(
    ("state", "tone"),
    (("Running", "ok"), ("Stopped", "bad"), ("Paused", "warn"), ("Unknown", "warn")),
)
def test_service_runtime_state_is_separate_from_receipt_freshness(
    tmp_path: Path, state: ServiceState, tone: Literal["ok", "warn", "bad"]
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT,
    )
    capture_service = next(
        service for service in registry.services if service.role == "capture_poller"
    )
    snapshot = snapshot.model_copy(
        update={
            "services": ServiceObservation(
                state="current",
                observed_at=OBSERVED_AT,
                evidence_source=str(tmp_path / ".tmp/operations/runtime/services.latest.json"),
                evidence_recorded_at=OBSERVED_AT,
                values=(
                    ServiceRow(name=capture_service.name, state=state, registry_match="expected"),
                ),
            )
        }
    )

    view = build_operations_panel_view(registry, snapshot)
    task = next(item for item in view.tasks if item.service_owned)
    html = render_operations_panel(view)

    assert task.runtime.state == "Current"
    assert task.service_runtime_state == state
    assert task.service_runtime_tone == tone
    assert f">{state}</span>" in html
    assert "Managed-service cached receipt" in html


@pytest.mark.parametrize("state", ("Paused", "Unknown", "Missing"))
def test_residual_service_attention_sorts_before_healthy_tasks(
    tmp_path: Path, state: ServiceState
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    base = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT,
    )
    capture_service = next(
        service for service in registry.services if service.role == "capture_poller"
    )
    healthy_receipts = tuple(
        JobReceiptObservation(
            state="current",
            observed_at=OBSERVED_AT,
            evidence_source=f"job_health:{step.job}",
            evidence_recorded_at=OBSERVED_AT,
            job=step.job,
            receipt=JobHealthRow(
                schema_version="1",
                job=step.job,
                write_sets=step.effective_lane,
                started_at=OBSERVED_AT,
                ended_at=OBSERVED_AT,
                status="ok",
                exit_code=0,
                severity="info",
            ),
        )
        for step in registry.job_steps
    )
    snapshot: OperationsSnapshot = base.model_copy(
        update={
            "scheduler": SchedulerObservation(
                state="current",
                observed_at=OBSERVED_AT,
                evidence_source="scheduler:cached",
                evidence_recorded_at=OBSERVED_AT,
                values=tuple(
                    SchedulerTaskRow(
                        task_name=task.task_name,
                        state="Ready",
                        registry_match="expected",
                    )
                    for task in registry.scheduled_tasks
                ),
            ),
            "services": ServiceObservation(
                state="current",
                observed_at=OBSERVED_AT,
                evidence_source="service:cached",
                evidence_recorded_at=OBSERVED_AT,
                values=(
                    ServiceRow(name=capture_service.name, state=state, registry_match="expected"),
                ),
            ),
            "job_receipts": healthy_receipts,
        }
    )

    view = build_operations_panel_view(registry, snapshot)
    service_task = next(task for task in view.tasks if task.service_owned)
    healthy_tasks = tuple(task for task in view.tasks if not task.attention)

    assert service_task.attention is True
    assert service_task.attention_rank == 3
    assert healthy_tasks
    assert all(service_task.attention_rank < task.attention_rank for task in healthy_tasks)
    assert view.tasks.index(service_task) < min(view.tasks.index(task) for task in healthy_tasks)


def test_runtime_receipt_labels_are_classified_before_generic_latest_json(tmp_path: Path) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT,
    )
    snapshot = snapshot.model_copy(
        update={
            "scheduler": snapshot.scheduler.model_copy(
                update={"evidence_source": str(tmp_path / "scheduler.latest.json")}
            ),
            "services": snapshot.services.model_copy(
                update={"evidence_source": str(tmp_path / "services.latest.json")}
            ),
        }
    )
    html = render_operations_panel(build_operations_panel_view(registry, snapshot))

    assert "Scheduler cached receipt" in html
    assert "Managed-service cached receipt" in html


def test_missing_fmp_evidence_never_renders_zero_or_no_rows(tmp_path: Path) -> None:
    html = render_operations_panel(_view(tmp_path))
    backlog = html.split("FMP recovery backlog", 1)[1].split("</article>", 1)[0]
    circuit = html.split("FMP provider circuit", 1)[1].split("</article>", 1)[0]

    assert "Unavailable" in backlog
    assert "0 queued" not in backlog
    assert "Unavailable" in circuit
    assert "No circuit rows" not in circuit


def test_invalid_runtime_receipts_do_not_turn_empty_observations_into_zero_claims(
    tmp_path: Path,
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT,
    )
    snapshot = snapshot.model_copy(
        update={
            "services": snapshot.services.model_copy(update={"state": "invalid", "values": ()}),
            "database_identity": snapshot.database_identity.model_copy(
                update={"state": "invalid", "values": ()}
            ),
        }
    )

    html = render_operations_panel(build_operations_panel_view(registry, snapshot))
    services = html.split("Managed services", 1)[1].split("</article>", 1)[0]
    identity = html.split("Database identity", 1)[1].split("</article>", 1)[0]

    assert f"{len(registry.services)} configured service(s)" in services
    assert "Invalid" in services
    assert "0 declared service(s)" not in services
    assert "Unavailable" in identity
    assert "Invalid" in identity
    assert "0 attached schema(s)" not in identity


def test_jobs_have_attention_filter_search_and_responsive_cards(tmp_path: Path) -> None:
    html = render_operations_panel(_view(tmp_path))

    assert 'id="operations-job-search"' in html
    assert 'data-operations-filter="attention"' in html
    assert 'data-operations-filter="all"' in html
    assert 'aria-pressed="true"' in html
    assert 'aria-pressed="false"' in html
    assert "data-operations-results" in html
    assert 'aria-live="polite"' in html
    assert ":focus-visible" in html
    assert html.count('data-operations-task-card="true"') == 43
    assert "@media (max-width:" in html
    assert "min-block-size: var(--touch-target-size)" in html
    assert 'data-operations-table-scroll="true"' not in html


def test_presentation_sanitizes_absolute_paths_in_every_untrusted_label(
    tmp_path: Path,
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    task = registry.scheduled_tasks[0].model_copy(
        update={"task_name": r"C:\Users\owner\task", "wrapper": "/home/owner/run_wrapper.bat"}
    )
    step = registry.job_steps[0].model_copy(
        update={"wrapper": task.wrapper, "command": (r"C:\private\run.py", "/srv/token.txt")}
    )
    registry = registry.model_copy(update={"scheduled_tasks": (task,), "job_steps": (step,)})
    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT,
    )
    html = render_operations_panel(build_operations_panel_view(registry, snapshot))

    assert "C:\\Users" not in html
    assert "/home/owner" not in html
    assert "/srv/token" not in html
    assert html.count("[path]") >= 3


def test_presentation_redaction_is_conservative_and_url_aware(tmp_path: Path) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    task = registry.scheduled_tasks[0]
    step = registry.job_steps[0].model_copy(
        update={
            "command": (
                r"C:\Program Files\Python\python.exe",
                r"\\server\private share\job.py",
                "/var",
                "https://example.test/public/path",
                "file:///C:/Users/owner/secret.txt",
            )
        }
    )
    registry = registry.model_copy(update={"scheduled_tasks": (task,), "job_steps": (step,)})
    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT,
    )
    receipt = snapshot.job_receipts[0].model_copy(
        update={
            "receipt": JobHealthRow(
                schema_version="1",
                job=step.job,
                write_sets=step.effective_lane,
                started_at=OBSERVED_AT,
                ended_at=OBSERVED_AT,
                status="failed",
                exit_code=1,
                severity="error",
                detail=(
                    r"anchors C:\Program Files\Owner\secret.py; "
                    r"\\server\share\token.txt; /opt/private dir/key.txt; /; "
                    "https://example.test/public/path; file:///var/private/key"
                ),
            )
        }
    )
    snapshot = snapshot.model_copy(update={"job_receipts": (receipt,)})
    html = render_operations_panel(build_operations_panel_view(registry, snapshot))

    for secret in ("Program Files", "server\\share", "/opt/private", "/var/private", "/var"):
        assert secret not in html
    assert "https://example.test/public/path" in html
    assert "file://[path]" in html
    assert html.count("[path]") >= 5
