from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

import pytest

from operations.models import (
    JobHealthRow,
    JobReceiptObservation,
    OperationsRegistry,
    OperationsSnapshot,
    SchedulerObservation,
    SchedulerTaskRow,
    ServiceObservation,
    ServiceRow,
    ServiceState,
)
from operations.readme_governance import ReadmeGovernanceStatus
from operations.registry import build_operations_registry
from operations.snapshot import collect_operations_snapshot
from pipeline.operations_panel import (
    OPERATIONS_AUXILIARY_SURFACE_DISPOSITIONS,
    OPERATIONS_REGISTRY_SURFACE_DISPOSITIONS,
    OPERATIONS_RELATED_VIEWS,
    OPERATIONS_SNAPSHOT_SURFACE_DISPOSITIONS,
    OperationsPanelView,
    build_operations_panel_view,
    render_operations_panel,
)
from pipeline.operations_styles import OPERATIONS_STYLE
from pipeline.provenance_panel import PROVENANCE_SECTIONS

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OBSERVED_AT = datetime(2026, 8, 13, 12, tzinfo=UTC)


def test_pair_receipt_uses_explicit_display_label() -> None:
    from pipeline import operations_panel

    source_label = cast(Callable[[str], str], getattr(operations_panel, "_source_label"))
    assert source_label("operations.runtime.pair.latest.json") == (
        "Scheduler/service runtime pair receipt"
    )


def _view(tmp_path: Path) -> OperationsPanelView:
    registry = build_operations_registry(PROJECT_ROOT)
    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT,
    )
    return build_operations_panel_view(registry, snapshot)


def _readme_status(
    state: Literal["not_run", "approved_preview", "applied", "rejected", "stale", "invalid"],
    tone: Literal["ok", "warn", "bad"],
) -> ReadmeGovernanceStatus:
    return ReadmeGovernanceStatus(
        state=state,
        tone=tone,
        run_id=None if state == "not_run" else "a" * 32,
        verdict=None if state == "not_run" else ("pass" if tone == "ok" else "revise"),
        attempts=0 if state == "not_run" else 1,
        current_sha256="b" * 64,
        candidate_sha256=None if state == "not_run" else "c" * 64,
        can_apply=state == "approved_preview",
        recorded_at=None if state == "not_run" else OBSERVED_AT,
    )


def test_operations_projection_groups_every_declared_task_wrapper_and_step(
    tmp_path: Path,
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    view = _view(tmp_path)

    assert len(view.tasks) == len(registry.scheduled_tasks)
    assert sum(len(task.steps) for task in view.tasks) == len(registry.job_steps)
    assert {task.wrapper for task in view.tasks} == {
        task.wrapper for task in registry.scheduled_tasks
    }
    assert all(task.steps for task in view.tasks)
    capture = next(task for task in view.tasks if task.service_owned)
    assert capture.declared_owner == "Managed service"
    assert capture.scheduler_state == "Unavailable"
    assert capture.runtime.state == "Unavailable"
    assert capture.runtime.recorded_label == "Evidence time unavailable"
    assert capture.attention is False
    assert capture.runtime_owner.startswith("Managed service")
    assert all(step.execution_rails for task in view.tasks for step in task.steps)


def test_missing_scheduler_receipt_is_unavailable_for_every_expectation_policy(
    tmp_path: Path,
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT,
    ).model_copy(
        update={
            "services": ServiceObservation(
                state="current",
                observed_at=OBSERVED_AT,
                evidence_source="service:cached",
                evidence_recorded_at=OBSERVED_AT,
                values=tuple(
                    ServiceRow(name=service.name, state="Running", registry_match="expected")
                    for service in registry.services
                ),
            )
        }
    )

    view = build_operations_panel_view(registry, snapshot)

    assert {task.scheduler_state for task in view.tasks} == {"Unavailable"}
    assert not any(task.attention for task in view.tasks)
    assert all(task.attention_rank == 4 for task in view.tasks)
    assert view.evidence_gap_count > 0
    assert all(task.runtime.state == "Unavailable" for task in view.tasks)
    assert all(task.runtime.recorded_label == "Evidence time unavailable" for task in view.tasks)
    assert {task.scheduler_expectation for task in registry.scheduled_tasks} == {
        "required_enabled",
        "required_disabled",
        "absent_service_owned",
    }


def test_noncurrent_service_observation_cannot_promote_retained_running_to_green(
    tmp_path: Path,
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    capture_service = next(
        service for service in registry.services if service.role == "capture_poller"
    )
    base = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT,
    )
    snapshot = base.model_copy(
        update={
            "scheduler": SchedulerObservation(
                state="current",
                observed_at=OBSERVED_AT,
                evidence_source="scheduler:cached",
                evidence_recorded_at=OBSERVED_AT,
                values=tuple(
                    SchedulerTaskRow(
                        task_name=task.task_name,
                        state="Missing" if task.service_owned else "Ready",
                        registry_match="expected",
                        scheduler_expectation=task.scheduler_expectation,
                        expectation_match=True,
                    )
                    for task in registry.scheduled_tasks
                ),
            ),
            "services": ServiceObservation(
                state="stale",
                observed_at=OBSERVED_AT,
                evidence_source="service:cached",
                evidence_recorded_at=OBSERVED_AT - timedelta(minutes=30),
                detail="retained service evidence is outside freshness",
                values=(
                    ServiceRow(
                        name=capture_service.name,
                        state="Running",
                        registry_match="expected",
                    ),
                ),
            ),
        }
    )

    task = next(
        task for task in build_operations_panel_view(registry, snapshot).tasks if task.service_owned
    )

    assert task.runtime.state == "Unavailable"
    assert task.runtime.tone == "warn"
    assert task.runtime.recorded_label == "Evidence time 2026-08-13 11:30 UTC"
    assert task.service_runtime_state is None
    assert task.service_runtime_tone is None
    assert task.attention is False


def test_invalid_runtime_domains_count_once_without_task_fanout(tmp_path: Path) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    base = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT,
    )
    missing = build_operations_panel_view(registry, base)
    invalid = build_operations_panel_view(
        registry,
        base.model_copy(
            update={
                "scheduler": SchedulerObservation(
                    state="invalid",
                    observed_at=OBSERVED_AT,
                    evidence_source="scheduler:cached",
                    detail="receipt contract invalid",
                ),
                "services": ServiceObservation(
                    state="invalid",
                    observed_at=OBSERVED_AT,
                    evidence_source="service:cached",
                    detail="receipt contract invalid",
                ),
            }
        ),
    )

    assert not any(task.attention for task in invalid.tasks)
    assert invalid.attention_count == missing.attention_count + 2


def test_operations_surface_dispositions_cover_every_registry_and_snapshot_field() -> None:
    registry_fields = [item.field for item in OPERATIONS_REGISTRY_SURFACE_DISPOSITIONS]
    snapshot_fields = [item.field for item in OPERATIONS_SNAPSHOT_SURFACE_DISPOSITIONS]

    assert len(registry_fields) == len(set(registry_fields))
    assert len(snapshot_fields) == len(set(snapshot_fields))
    assert set(registry_fields) == set(OperationsRegistry.model_fields)
    assert set(snapshot_fields) == set(OperationsSnapshot.model_fields)
    assert all(item.rationale.strip() for item in OPERATIONS_REGISTRY_SURFACE_DISPOSITIONS)
    assert all(item.rationale.strip() for item in OPERATIONS_SNAPSHOT_SURFACE_DISPOSITIONS)

    derived_panel_fields = {
        "observed_label",
        "attention_count",
        "runtime_summary_tone",
        "tasks",
        "runtime_rows",
    }
    auxiliary_fields = [item.field for item in OPERATIONS_AUXILIARY_SURFACE_DISPOSITIONS]
    assert set(auxiliary_fields) == set(OperationsPanelView.model_fields) - derived_panel_fields


def test_linked_dispositions_resolve_to_reachable_related_views_and_sections(
    tmp_path: Path,
) -> None:
    related = {item.key: item for item in OPERATIONS_RELATED_VIEWS}
    provenance_ids = {panel_id for _anchor, _label, panel_id in PROVENANCE_SECTIONS}
    dispositions = (
        *OPERATIONS_REGISTRY_SURFACE_DISPOSITIONS,
        *OPERATIONS_SNAPSHOT_SURFACE_DISPOSITIONS,
        *OPERATIONS_AUXILIARY_SURFACE_DISPOSITIONS,
    )

    for disposition in dispositions:
        assert (disposition.destination == "linked_view") == bool(disposition.targets)
        for target in disposition.targets:
            view_key, separator, section_id = target.partition(":")
            assert view_key in related
            if separator:
                assert view_key == "provenance"
                assert section_id in provenance_ids

    html = render_operations_panel(_view(tmp_path))
    for item in related.values():
        assert f'data-operations-related="{item.key}"' in html
        assert f"workOsOpenRelatedView('{item.endpoint}', '{item.label}')" in html


@pytest.mark.parametrize(
    ("state", "tone"),
    (
        ("not_run", "warn"),
        ("stale", "warn"),
        ("rejected", "warn"),
        ("invalid", "bad"),
    ),
)
def test_readme_governance_failures_contribute_to_headline_attention(
    tmp_path: Path,
    state: Literal["not_run", "stale", "rejected", "invalid"],
    tone: Literal["warn", "bad"],
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT,
    )
    baseline = build_operations_panel_view(
        registry, snapshot, readme_status=_readme_status("applied", "ok")
    )
    status = _readme_status(state, tone)

    view = build_operations_panel_view(registry, snapshot, readme_status=status)
    html = render_operations_panel(view)

    if state in {"not_run", "stale"}:
        assert view.attention_count == baseline.attention_count
        assert view.evidence_gap_count == baseline.evidence_gap_count + 1
    else:
        assert view.attention_count == baseline.attention_count + 1
        assert view.evidence_gap_count == baseline.evidence_gap_count
    assert view.runtime_summary_tone == "warn"
    assert "operational or governance observation(s) need attention" in html


def test_missing_readme_governance_status_is_invalid_and_contributes_attention(
    tmp_path: Path,
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT,
    )
    healthy = build_operations_panel_view(
        registry, snapshot, readme_status=_readme_status("applied", "ok")
    )

    missing = build_operations_panel_view(registry, snapshot)
    html = render_operations_panel(missing)

    assert missing.attention_count == healthy.attention_count + 1
    governance = html.split('id="operations-pane-governance"', 1)[1]
    assert "Invalid" in governance


def test_operations_renderer_has_governance_tab_and_related_views(
    tmp_path: Path,
) -> None:
    html = render_operations_panel(_view(tmp_path))
    tablist = html.split('role="tablist"', 1)[1].split("</div>", 1)[0]

    assert tablist.count('role="tab"') == 5
    assert ">Overview</button>" in tablist
    assert ">Attention</button>" in tablist
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
    assert "k-chip-tab" in tablist
    assert ".operations-tab{min-block-size:var(--touch-target-size)}" in OPERATIONS_STYLE
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


def test_operations_renderer_uses_card_title_for_the_card_heading(tmp_path: Path) -> None:
    html = render_operations_panel(_view(tmp_path))

    assert '<h1 class="k-card-title" id="operations-title">Operations</h1>' in html
    assert '<h1 class="k-toolbar-title" id="operations-title">Operations</h1>' not in html


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


@pytest.mark.parametrize("status", ("partial", "degraded_corpus", "skipped_locked"))
def test_current_non_ok_receipt_remains_actionable(tmp_path: Path, status: str) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT,
    )
    step = registry.job_steps[-1]
    observation = JobReceiptObservation(
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
            status=cast(Literal["partial", "degraded_corpus", "skipped_locked"], status),
            exit_code=0,
            severity="warning",
        ),
    )
    snapshot = snapshot.model_copy(
        update={
            "job_receipts": tuple(
                observation if item.job == step.job else item for item in snapshot.job_receipts
            )
        }
    )

    task = next(
        item
        for item in build_operations_panel_view(registry, snapshot).tasks
        if any(candidate.job == step.job for candidate in item.steps)
    )

    assert task.attention is True
    assert task.attention_rank == 3


def test_stale_failed_receipt_is_visible_as_gap_not_current_attention(tmp_path: Path) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT,
    )
    step = registry.job_steps[-1]
    stale_failed = JobReceiptObservation(
        state="stale",
        observed_at=OBSERVED_AT,
        evidence_source=f"job_health:{step.job}",
        evidence_recorded_at=OBSERVED_AT - timedelta(hours=2),
        job=step.job,
        receipt=JobHealthRow(
            schema_version="1",
            job=step.job,
            write_sets=step.effective_lane,
            started_at=OBSERVED_AT - timedelta(hours=2),
            ended_at=OBSERVED_AT - timedelta(hours=2),
            status="failed",
            exit_code=9,
            severity="error",
        ),
    )
    snapshot = snapshot.model_copy(
        update={
            "job_receipts": tuple(
                stale_failed if item.job == step.job else item for item in snapshot.job_receipts
            )
        }
    )

    view = build_operations_panel_view(registry, snapshot)
    task = next(
        item for item in view.tasks if any(candidate.job == step.job for candidate in item.steps)
    )
    card = (
        render_operations_panel(view)
        .split(f">{task.task_name}</div>", 1)[1]
        .split("</article>", 1)[0]
    )

    assert task.attention is False
    assert task.attention_rank == 4
    assert 'k-pill-warn">Evidence gap</span>' in card
    assert 'k-pill-ok">Observed</span>' not in card


def test_gap_count_tracks_distinct_job_receipts_not_shared_display_labels(tmp_path: Path) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT,
    )
    missing = build_operations_panel_view(registry, snapshot)
    step = registry.job_steps[-1]
    current = JobReceiptObservation(
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
    one_current = build_operations_panel_view(
        registry,
        snapshot.model_copy(
            update={
                "job_receipts": tuple(
                    current if item.job == step.job else item for item in snapshot.job_receipts
                )
            }
        ),
    )

    assert missing.evidence_gap_count >= len(registry.job_steps)
    assert missing.evidence_gap_count == one_current.evidence_gap_count + 1


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
            ),
            "scheduler": SchedulerObservation(
                state="current",
                observed_at=OBSERVED_AT,
                evidence_source="scheduler:cached",
                evidence_recorded_at=OBSERVED_AT,
                values=tuple(
                    SchedulerTaskRow(
                        task_name=task.task_name,
                        state="Missing" if task.service_owned else "Ready",
                        registry_match="expected",
                        scheduler_expectation=task.scheduler_expectation,
                        expectation_match=task.service_owned,
                    )
                    for task in registry.scheduled_tasks
                ),
            ),
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
    identity = html.split("Database identity", 1)[1].split("</article>", 1)[0]
    runtime = html.split('id="operations-pane-runtime"', 1)[1]

    for service in registry.services:
        service_card = runtime.split(f"Managed service · {service.name}", 1)[1].split(
            "</article>", 1
        )[0]
        assert service.purpose in service_card
        assert "Invalid" in service_card
    assert "Unavailable" in identity
    assert "Invalid" in identity
    assert "0 attached schema(s)" not in identity


def test_every_registered_service_is_visible_and_nonrunning_state_gets_attention(
    tmp_path: Path,
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT,
    )
    running_rows = tuple(
        ServiceRow(name=service.name, state="Running", registry_match="expected")
        for service in registry.services
    )
    running_snapshot = snapshot.model_copy(
        update={
            "scheduler": SchedulerObservation(
                state="current",
                observed_at=OBSERVED_AT,
                evidence_source="scheduler:receipt",
                evidence_recorded_at=OBSERVED_AT,
                values=tuple(
                    SchedulerTaskRow(
                        task_name=task.task_name,
                        state=(
                            "Missing"
                            if task.service_owned
                            else "Disabled"
                            if task.scheduler_expectation == "required_disabled"
                            else "Ready"
                        ),
                        registry_match="expected",
                        scheduler_expectation=task.scheduler_expectation,
                        expectation_match=True,
                    )
                    for task in registry.scheduled_tasks
                ),
            ),
            "services": ServiceObservation(
                state="current",
                observed_at=OBSERVED_AT,
                evidence_source="service:receipt",
                evidence_recorded_at=OBSERVED_AT,
                values=running_rows,
            ),
        }
    )
    running = build_operations_panel_view(
        registry, running_snapshot, readme_status=_readme_status("applied", "ok")
    )
    dashboard = next(service for service in registry.services if service.role == "dashboard")
    stopped_rows = tuple(
        row.model_copy(update={"state": "Stopped"}) if row.name == dashboard.name else row
        for row in running_rows
    )
    stopped_snapshot = running_snapshot.model_copy(
        update={"services": running_snapshot.services.model_copy(update={"values": stopped_rows})}
    )

    stopped = build_operations_panel_view(
        registry, stopped_snapshot, readme_status=_readme_status("applied", "ok")
    )
    html = render_operations_panel(stopped)
    runtime = html.split('id="operations-pane-runtime"', 1)[1]

    assert stopped.attention_count == running.attention_count + 1
    assert {task.task_name for task in stopped.tasks if task.attention} == {
        task.task_name for task in running.tasks if task.attention
    }
    for service in registry.services:
        card = runtime.split(f"Managed service · {service.name}", 1)[1].split("</article>", 1)[0]
        assert service.purpose in card
    dashboard_card = runtime.split(f"Managed service · {dashboard.name}", 1)[1].split(
        "</article>", 1
    )[0]
    assert "Stopped" in dashboard_card


def test_unexpected_current_service_is_explicitly_visible_with_attention(
    tmp_path: Path,
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    base = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT,
    )
    unexpected_name = "UnexpectedCaptureService"
    snapshot = base.model_copy(
        update={
            "services": ServiceObservation(
                state="current",
                observed_at=OBSERVED_AT,
                evidence_source="service:receipt",
                evidence_recorded_at=OBSERVED_AT,
                values=(
                    *(
                        ServiceRow(name=service.name, state="Running", registry_match="expected")
                        for service in registry.services
                    ),
                    ServiceRow(name=unexpected_name, state="Stopped", registry_match="unexpected"),
                ),
            )
        }
    )

    view = build_operations_panel_view(registry, snapshot)
    unexpected = next(task for task in view.tasks if task.task_name == unexpected_name)

    assert unexpected.runtime_owner == "Unexpected live managed service"
    assert unexpected.service_runtime_state == "Stopped"
    assert unexpected.service_runtime_tone == "bad"
    assert unexpected.attention is True
    assert unexpected_name in unexpected.runtime.detail
    assert "Stopped" in unexpected.runtime.detail


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
    # The manifest-owned semantic-review producer is a truthful primary
    # Operations card, bringing the declared Scheduler fleet to 46 tasks.
    assert html.count('data-operations-task-card="true"') == 46
    assert "monthly_p3_refresh" not in html
    assert "@media (max-width:" in html
    assert "min-block-size:var(--touch-target-size)" in html
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


def test_presentation_redacts_credentials_from_foreign_job_receipt(tmp_path: Path) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT,
    )
    step = registry.job_steps[-1]
    sentinel = "FOREIGN-RAW-CREDENTIAL-7319"
    foreign = JobReceiptObservation(
        state="current",
        observed_at=OBSERVED_AT,
        evidence_source="foreign:job-receipt",
        evidence_recorded_at=OBSERVED_AT,
        job=step.job,
        receipt=JobHealthRow(
            schema_version="1",
            job=step.job,
            write_sets=step.effective_lane,
            started_at=OBSERVED_AT,
            ended_at=OBSERVED_AT,
            status="failed",
            exit_code=1,
            severity="error",
            detail=(
                f"request failed: https://example.test/private?api_key={sentinel}; "
                r"C:\private\owner\job.py"
            ),
        ),
    )
    snapshot = snapshot.model_copy(
        update={
            "job_receipts": tuple(
                foreign if item.job == step.job else item for item in snapshot.job_receipts
            )
        }
    )

    view = build_operations_panel_view(registry, snapshot)
    receipt = next(
        item.receipt for task in view.tasks for item in task.steps if item.job == step.job
    )
    html = render_operations_panel(view)

    assert sentinel not in receipt.detail
    assert sentinel not in html
    assert "C:\\private" not in receipt.detail
    assert "[path]" in receipt.detail


def test_presentation_masks_complete_header_and_assignment_values(tmp_path: Path) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT,
    )
    step = registry.job_steps[-1]
    sentinel = "ALPHABETONLYCREDENTIAL"
    foreign = JobReceiptObservation(
        state="current",
        observed_at=OBSERVED_AT,
        evidence_source="foreign:job-receipt",
        evidence_recorded_at=OBSERVED_AT,
        job=step.job,
        receipt=JobHealthRow(
            schema_version="1",
            job=step.job,
            write_sets=step.effective_lane,
            started_at=OBSERVED_AT,
            ended_at=OBSERVED_AT,
            status="failed",
            exit_code=1,
            severity="error",
            detail=(
                "Proxy-Authorization: "
                + "Basic "
                + sentinel
                + f"; x-api-key: prefix {sentinel} suffix; retry=closed; "
                + f'{"api" + "_key"} = "prefix {sentinel} suffix"'
            ),
        ),
    )
    snapshot = snapshot.model_copy(
        update={
            "job_receipts": tuple(
                foreign if item.job == step.job else item for item in snapshot.job_receipts
            )
        }
    )

    view = build_operations_panel_view(registry, snapshot)
    receipt = next(
        item.receipt for task in view.tasks for item in task.steps if item.job == step.job
    )
    html = render_operations_panel(view)

    assert sentinel not in receipt.detail
    assert sentinel not in html
    assert "retry=closed" in receipt.detail


def test_presentation_masks_bearer_b64token_and_preserves_safe_suffix(tmp_path: Path) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT,
    )
    step = registry.job_steps[-1]
    credential = "ALPHABETONLY" + "~+TAILONLY/=="
    foreign = JobReceiptObservation(
        state="current",
        observed_at=OBSERVED_AT,
        evidence_source="foreign:job-receipt",
        evidence_recorded_at=OBSERVED_AT,
        job=step.job,
        receipt=JobHealthRow(
            schema_version="1",
            job=step.job,
            write_sets=step.effective_lane,
            started_at=OBSERVED_AT,
            ended_at=OBSERVED_AT,
            status="failed",
            exit_code=1,
            severity="error",
            detail=(
                "Proxy-Authorization: "
                + "Bearer "
                + credential
                + "\nrequest failed Bearer "
                + credential
                + "; retry=closed"
            ),
        ),
    )
    snapshot = snapshot.model_copy(
        update={
            "job_receipts": tuple(
                foreign if item.job == step.job else item for item in snapshot.job_receipts
            )
        }
    )

    view = build_operations_panel_view(registry, snapshot)
    receipt = next(
        item.receipt for task in view.tasks for item in task.steps if item.job == step.job
    )
    html = render_operations_panel(view)

    assert credential not in receipt.detail
    assert credential not in html
    assert "TAILONLY" not in receipt.detail
    assert "TAILONLY" not in html
    assert "retry=closed" in receipt.detail


@pytest.mark.parametrize("delimiter", [")", "]", "}", '"', "'", ":", ";"])
def test_presentation_preserves_non_b64token_bearer_delimiter(
    tmp_path: Path, delimiter: str
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT,
    )
    step = registry.job_steps[-1]
    credential = "ALPHABETONLY" + "~+TAILONLY/=="
    foreign = JobReceiptObservation(
        state="current",
        observed_at=OBSERVED_AT,
        evidence_source="foreign:job-receipt",
        evidence_recorded_at=OBSERVED_AT,
        job=step.job,
        receipt=JobHealthRow(
            schema_version="1",
            job=step.job,
            write_sets=step.effective_lane,
            started_at=OBSERVED_AT,
            ended_at=OBSERVED_AT,
            status="failed",
            exit_code=1,
            severity="error",
            detail="request failed Bearer " + credential + delimiter + " suffix=safe",
        ),
    )
    snapshot = snapshot.model_copy(
        update={
            "job_receipts": tuple(
                foreign if item.job == step.job else item for item in snapshot.job_receipts
            )
        }
    )

    view = build_operations_panel_view(registry, snapshot)
    receipt = next(
        item.receipt for task in view.tasks for item in task.steps if item.job == step.job
    )
    html = render_operations_panel(view)

    assert "TAILONLY" not in receipt.detail
    assert "TAILONLY" not in html
    assert delimiter + " suffix=safe" in receipt.detail
