from __future__ import annotations

import json
import multiprocessing
import sqlite3
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import BinaryIO, cast

import pytest
from pydantic import BaseModel
from pytest import MonkeyPatch

from execution.collect_operations_runtime_observations import (
    CANONICAL_TASK_NAMESPACE,
    MAX_SCHEDULER_OUTPUT_BYTES,
    SchedulerProbe,
    SchedulerTaskReceipt,
    ServiceProbe,
    ServiceReceipt,
    ServiceReceiptRow,
    build_runtime_summary,
    collect_scheduler_receipt,
    collect_scheduler_tasks_from_system,
    collect_service_receipt,
    write_atomic_receipt,
)
from operations.models import (
    RUNTIME_PAIR_RECEIPT_FILENAME,
    JobHealthRow,
    JobReceiptObservation,
    OperationsRegistry,
    RuntimeCollectionSummary,
    RuntimeProbeAttempt,
    RuntimeReceiptPair,
    SchedulerReceipt,
    SchedulerRuntimeReceipt,
    SchedulerTaskState,
    ServiceRuntimeReceipt,
    ServiceState,
)
from operations.paths import scheduler_receipt_path, service_receipt_path
from operations.registry import build_operations_registry
from operations.snapshot import collect_operations_snapshot
from pipeline.operations_panel import build_operations_panel_view

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OBSERVED_AT = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)


class _SlowStateMapping(Mapping[str, str]):
    def __init__(
        self, states: dict[str, str], delay_s: float, marker_path: str | None = None
    ) -> None:
        self._states = states
        self._delay_s = delay_s
        self._marker_path = marker_path

    def __getitem__(self, key: str) -> str:
        return self._states[key]

    def __iter__(self) -> Iterator[str]:
        if self._marker_path is not None:
            Path(self._marker_path).write_text("locked", encoding="utf-8")
        if self._delay_s:
            time.sleep(self._delay_s)
        return iter(self._states)

    def __len__(self) -> int:
        return len(self._states)


def _emit_runtime_worker(
    repo_root: str,
    registry_root: str,
    observed_at: str,
    delay_s: float,
    marker_path: str | None,
) -> None:
    from execution import collect_operations_runtime_observations as collector

    root = Path(repo_root)
    registry = build_operations_registry(Path(registry_root))
    scheduler_states = {task.task_name: "Ready" for task in registry.scheduled_tasks}
    service_states = {service.name.casefold(): "Running" for service in registry.services}
    collector.emit_runtime_receipts(
        registry,
        root,
        datetime.fromisoformat(observed_at),
        scheduler_probe=collector.SchedulerProbe(
            states=cast(
                Mapping[str, SchedulerTaskState],
                _SlowStateMapping(scheduler_states, delay_s, marker_path),
            )
        ),
        service_probe=collector.ServiceProbe(
            states=cast(Mapping[str, ServiceState], service_states)
        ),
        lock_wait_s=10.0,
    )


def test_unavailable_probe_is_persisted_separately_from_last_successful_evidence() -> None:
    registry = build_operations_registry(PROJECT_ROOT)

    scheduler = collect_scheduler_receipt(
        registry,
        OBSERVED_AT,
        probe=SchedulerProbe.unavailable("Task Scheduler probe unavailable"),
    )
    services = collect_service_receipt(
        registry,
        OBSERVED_AT,
        probe=ServiceProbe.unavailable("Service manager probe unavailable"),
    )

    assert scheduler.probe_attempt.availability == "unavailable"
    assert scheduler.probe_attempt.detail == "Task Scheduler probe unavailable"
    assert scheduler.last_successful is None
    assert services.probe_attempt.availability == "unavailable"
    assert services.last_successful is None


def test_v2_available_probe_cannot_claim_false_current_scheduler_or_service_evidence() -> None:
    scheduler_payload = SchedulerRuntimeReceipt.success(
        observed_at=OBSERVED_AT, tasks=()
    ).model_dump()
    scheduler_payload["probe_attempt"]["attempted_at"] = OBSERVED_AT + timedelta(seconds=1)
    with pytest.raises(ValueError, match="Scheduler probe evidence"):
        SchedulerRuntimeReceipt.model_validate(scheduler_payload)

    service_payload = ServiceRuntimeReceipt.success(
        observed_at=OBSERVED_AT, services=()
    ).model_dump()
    service_payload["probe_attempt"]["attempted_at"] = OBSERVED_AT + timedelta(seconds=1)
    with pytest.raises(ValueError, match="service probe evidence"):
        ServiceRuntimeReceipt.model_validate(service_payload)


def test_v2_unavailable_probe_rejects_future_retained_scheduler_or_service_evidence() -> None:
    scheduler_payload = SchedulerRuntimeReceipt.success(
        observed_at=OBSERVED_AT + timedelta(minutes=1), tasks=()
    ).model_dump()
    scheduler_payload["probe_attempt"] = {
        "attempted_at": OBSERVED_AT,
        "availability": "unavailable",
        "detail": "probe timed out",
    }
    with pytest.raises(ValueError, match="Scheduler probe evidence"):
        SchedulerRuntimeReceipt.model_validate(scheduler_payload)

    service_payload = ServiceRuntimeReceipt.success(
        observed_at=OBSERVED_AT + timedelta(minutes=1), services=()
    ).model_dump()
    service_payload["probe_attempt"] = {
        "attempted_at": OBSERVED_AT,
        "availability": "unavailable",
        "detail": "probe failed to launch",
    }
    with pytest.raises(ValueError, match="service probe evidence"):
        ServiceRuntimeReceipt.model_validate(service_payload)


def test_v2_probe_timestamps_allow_same_time_current_and_earlier_retained_evidence() -> None:
    assert SchedulerRuntimeReceipt.success(observed_at=OBSERVED_AT, tasks=()).last_successful
    assert ServiceRuntimeReceipt.success(observed_at=OBSERVED_AT, services=()).last_successful

    earlier = OBSERVED_AT - timedelta(minutes=1)
    scheduler_prior = SchedulerRuntimeReceipt.success(observed_at=earlier, tasks=())
    scheduler_unavailable = SchedulerRuntimeReceipt(
        probe_attempt=RuntimeProbeAttempt(
            attempted_at=OBSERVED_AT,
            availability="unavailable",
            detail="probe timed out",
        ),
        last_successful=scheduler_prior.last_successful,
    )
    assert SchedulerRuntimeReceipt.model_validate(scheduler_unavailable.model_dump())

    service_prior = ServiceRuntimeReceipt.success(observed_at=earlier, services=())
    service_unavailable = ServiceRuntimeReceipt(
        probe_attempt=RuntimeProbeAttempt(
            attempted_at=OBSERVED_AT,
            availability="unavailable",
            detail="probe unavailable",
        ),
        last_successful=service_prior.last_successful,
    )
    assert ServiceRuntimeReceipt.model_validate(service_unavailable.model_dump())


def test_collector_keeps_observed_registration_states_distinct_from_absent_observation() -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    first, second, third = registry.scheduled_tasks[:3]
    receipt = collect_scheduler_receipt(
        registry,
        OBSERVED_AT,
        probe=SchedulerProbe(
            states={
                first.task_name.casefold(): "Ready",
                second.task_name.casefold(): "Running",
                third.task_name.casefold(): "Disabled",
            }
        ),
    )

    assert receipt is not None
    assert receipt.last_successful is not None
    states = {row.task_name: row.state for row in receipt.last_successful.tasks}
    assert states[first.task_name] == "Ready"
    assert states[second.task_name] == "Running"
    assert states[third.task_name] == "Disabled"
    assert states[registry.scheduled_tasks[-1].task_name] == "Missing"


def test_current_receipts_drive_truthful_service_owned_and_expected_disabled_dispositions(
    tmp_path: Path,
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    expected_disabled = next(
        task
        for task in registry.scheduled_tasks
        if task.scheduler_expectation == "required_disabled"
    )
    capture_service = next(
        service for service in registry.services if service.role == "capture_poller"
    )
    scheduler_receipt = SchedulerRuntimeReceipt.success(
        observed_at=OBSERVED_AT,
        tasks=tuple(
            SchedulerTaskReceipt(
                task_name=task.task_name,
                state=(
                    "Missing"
                    if task.service_owned
                    else "Disabled"
                    if task.scheduler_expectation == "required_disabled"
                    else "Ready"
                ),
            )
            for task in registry.scheduled_tasks
        ),
    )
    service_receipt = ServiceReceipt(
        observed_at=OBSERVED_AT,
        services=tuple(
            ServiceReceiptRow(
                name=service.name,
                state="Running" if service == capture_service else "Unknown",
            )
            for service in registry.services
        ),
    )
    scheduler_path = scheduler_receipt_path(tmp_path)
    service_path = service_receipt_path(tmp_path)
    write_atomic_receipt(scheduler_path, scheduler_receipt.model_dump_json())
    write_atomic_receipt(service_path, service_receipt.model_dump_json())

    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT + timedelta(minutes=1),
        scheduler_receipt_path=scheduler_path,
        service_receipt_path=service_path,
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
    view = build_operations_panel_view(
        registry, snapshot.model_copy(update={"job_receipts": healthy_receipts})
    )
    disabled_task = next(
        item for item in view.tasks if item.task_name == expected_disabled.task_name
    )
    service_owned_task = next(item for item in view.tasks if item.service_owned)

    assert snapshot.scheduler.state == "current"
    assert snapshot.services.state == "current"
    assert disabled_task.scheduler_state == "Disabled (expected)"
    assert disabled_task.attention is False
    assert service_owned_task.scheduler_state == "Absent (service-owned)"
    assert service_owned_task.service_runtime_state == "Running"
    assert service_owned_task.attention is False


def test_runtime_receipt_freshness_is_distinct_from_current_state(tmp_path: Path) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    scheduler_path = scheduler_receipt_path(tmp_path)
    write_atomic_receipt(
        scheduler_path,
        SchedulerRuntimeReceipt.success(
            observed_at=OBSERVED_AT,
            tasks=tuple(
                SchedulerTaskReceipt(
                    task_name=task.task_name,
                    state=(
                        "Missing"
                        if task.service_owned
                        else "Disabled"
                        if task.scheduler_expectation == "required_disabled"
                        else "Ready"
                    ),
                )
                for task in registry.scheduled_tasks
            ),
        ).model_dump_json(),
    )

    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT + timedelta(minutes=16),
        scheduler_receipt_path=scheduler_path,
    )

    assert snapshot.scheduler.state == "stale"
    assert all(
        row.state
        == (
            "Missing"
            if task.service_owned
            else "Disabled"
            if task.scheduler_expectation == "required_disabled"
            else "Ready"
        )
        for row, task in zip(snapshot.scheduler.values, registry.scheduled_tasks, strict=True)
    )


def test_scheduler_policy_alerts_on_enabled_disabled_and_service_owned_conflicts(
    tmp_path: Path,
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    disabled_task = next(
        task
        for task in registry.scheduled_tasks
        if task.scheduler_expectation == "required_disabled"
    )
    service_owned_task = next(task for task in registry.scheduled_tasks if task.service_owned)
    capture_service = next(
        service for service in registry.services if service.role == "capture_poller"
    )
    scheduler_path = scheduler_receipt_path(tmp_path)
    service_path = service_receipt_path(tmp_path)
    write_atomic_receipt(
        scheduler_path,
        SchedulerRuntimeReceipt.success(
            observed_at=OBSERVED_AT,
            tasks=tuple(
                SchedulerTaskReceipt(
                    task_name=task.task_name,
                    state=(
                        "Ready"
                        if task in (disabled_task, service_owned_task)
                        else "Ready"
                        if task.scheduler_expectation == "required_enabled"
                        else "Disabled"
                    ),
                )
                for task in registry.scheduled_tasks
            ),
        ).model_dump_json(),
    )
    write_atomic_receipt(
        service_path,
        ServiceReceipt(
            observed_at=OBSERVED_AT,
            services=tuple(
                ServiceReceiptRow(
                    name=service.name,
                    state="Running" if service == capture_service else "Unknown",
                )
                for service in registry.services
            ),
        ).model_dump_json(),
    )

    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT + timedelta(minutes=1),
        scheduler_receipt_path=scheduler_path,
        service_receipt_path=service_path,
    )
    view = build_operations_panel_view(registry, snapshot)
    disabled_row = next(
        row for row in snapshot.scheduler.values if row.task_name == disabled_task.task_name
    )
    service_row = next(
        row for row in snapshot.scheduler.values if row.task_name == service_owned_task.task_name
    )
    service_task = next(
        task for task in view.tasks if task.task_name == service_owned_task.task_name
    )
    disabled_task_view = next(
        task for task in view.tasks if task.task_name == disabled_task.task_name
    )

    assert snapshot.scheduler.state == "current"
    assert disabled_row.state == "Ready"
    assert disabled_row.expectation_match is False
    assert service_row.state == "Ready"
    assert service_row.expectation_match is False
    assert service_task.scheduler_state == "Ready (forbidden for service-owned task)"
    assert service_task.service_runtime_state == "Running"
    assert service_task.attention is True
    assert disabled_task_view.scheduler_state == "Ready (forbidden for required_disabled)"
    assert disabled_task_view.attention is True


def test_unexpected_canonical_scheduler_task_is_explicitly_visible_in_panel(
    tmp_path: Path,
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    unexpected_name = f"{CANONICAL_TASK_NAMESPACE}unexpected_live"
    scheduler_path = scheduler_receipt_path(tmp_path)
    write_atomic_receipt(
        scheduler_path,
        SchedulerRuntimeReceipt.success(
            observed_at=OBSERVED_AT,
            tasks=(
                *(
                    SchedulerTaskReceipt(task_name=task.task_name, state="Ready")
                    for task in registry.scheduled_tasks
                ),
                SchedulerTaskReceipt(task_name=unexpected_name, state="Running"),
            ),
        ).model_dump_json(),
    )

    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT + timedelta(minutes=1),
        scheduler_receipt_path=scheduler_path,
    )
    view = build_operations_panel_view(registry, snapshot)
    unexpected = next(task for task in view.tasks if task.task_name == unexpected_name)

    assert snapshot.scheduler.state == "current"
    assert unexpected.scheduler_state == "Running (unexpected)"
    assert unexpected.runtime_owner == "Unexpected live Scheduler task"
    assert unexpected.attention is True
    assert unexpected.runtime.state == "Current"
    assert unexpected.runtime.recorded_label != "Evidence time unavailable"
    assert unexpected_name in unexpected.runtime.detail
    assert "outside the declared operations registry" in unexpected.runtime.detail


def test_unavailable_scheduler_with_retained_unexpected_task_is_historical_only(
    tmp_path: Path,
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    unexpected_name = f"{CANONICAL_TASK_NAMESPACE}unexpected_retained"
    retained = SchedulerReceipt(
        observed_at=OBSERVED_AT,
        tasks=(
            *(
                SchedulerTaskReceipt(task_name=task.task_name, state="Ready")
                for task in registry.scheduled_tasks
            ),
            SchedulerTaskReceipt(task_name=unexpected_name, state="Running"),
        ),
    )
    scheduler_path = scheduler_receipt_path(tmp_path)
    write_atomic_receipt(
        scheduler_path,
        SchedulerRuntimeReceipt(
            probe_attempt=RuntimeProbeAttempt(
                attempted_at=OBSERVED_AT,
                availability="unavailable",
                detail="Scheduler command timed out",
            ),
            last_successful=retained,
        ).model_dump_json(),
    )

    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT + timedelta(minutes=1),
        scheduler_receipt_path=scheduler_path,
    )
    unexpected = next(
        task
        for task in build_operations_panel_view(registry, snapshot).tasks
        if task.task_name == unexpected_name
    )

    assert snapshot.scheduler.state == "unavailable"
    assert unexpected.scheduler_state == "Running (historical)"
    assert unexpected.runtime_owner == "Historical Scheduler evidence"
    assert unexpected.attention is False
    assert "Unexpected live Scheduler task" not in unexpected.runtime_owner
    assert "Scheduler command timed out" in unexpected.runtime.detail
    assert "retained successful evidence at" in unexpected.runtime.detail


def test_stale_scheduler_with_retained_unexpected_task_is_historical_only(
    tmp_path: Path,
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    unexpected_name = f"{CANONICAL_TASK_NAMESPACE}unexpected_stale"
    scheduler_path = scheduler_receipt_path(tmp_path)
    write_atomic_receipt(
        scheduler_path,
        SchedulerRuntimeReceipt.success(
            observed_at=OBSERVED_AT - timedelta(minutes=30),
            tasks=(
                *(
                    SchedulerTaskReceipt(task_name=task.task_name, state="Ready")
                    for task in registry.scheduled_tasks
                ),
                SchedulerTaskReceipt(task_name=unexpected_name, state="Running"),
            ),
        ).model_dump_json(),
    )

    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT,
        scheduler_receipt_path=scheduler_path,
    )
    snapshot = snapshot.model_copy(
        update={"scheduler": snapshot.scheduler.model_copy(update={"state": "stale"})}
    )
    unexpected = next(
        task
        for task in build_operations_panel_view(registry, snapshot).tasks
        if task.task_name == unexpected_name
    )

    assert unexpected.scheduler_state == "Running (historical)"
    assert unexpected.runtime_owner == "Historical Scheduler evidence"
    assert unexpected.attention is False
    assert "retained successful evidence at" in unexpected.runtime.detail


def test_scheduler_overflow_is_an_unavailable_probe(monkeypatch: MonkeyPatch) -> None:
    from execution import collect_operations_runtime_observations as collector

    class Completed:
        returncode = 0

    def completed(*args: object, **kwargs: object) -> Completed:
        stdout = cast(BinaryIO, kwargs["stdout"])
        stdout.write(b"x" * (MAX_SCHEDULER_OUTPUT_BYTES + 1))
        return Completed()

    monkeypatch.setattr(collector, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(collector.subprocess, "run", completed)

    probe = collector.collect_scheduler_tasks_from_system()

    assert probe.states is None
    assert probe.detail == "Scheduler query exceeds bounded output"


def test_scheduler_probe_preserves_canonical_unexpected_tasks_and_excludes_other_namespaces(
    monkeypatch: MonkeyPatch,
) -> None:
    from execution import collect_operations_runtime_observations as collector

    registry = build_operations_registry(PROJECT_ROOT)
    expected = registry.scheduled_tasks[0].task_name
    unexpected = f"{CANONICAL_TASK_NAMESPACE}unexpected_live"

    def completed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        stdout = cast(BinaryIO, kwargs["stdout"])
        stdout.write(
            (
                '"TaskName","Next Run Time","Status"\n'
                f'"{expected}","N/A","Ready"\n'
                f'"{unexpected}","N/A","Running"\n'
                '"\\other-application\\task","N/A","Running"\n'
            ).encode()
        )
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(collector, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(collector.subprocess, "run", completed)

    probe = collect_scheduler_tasks_from_system()
    assert probe.states is not None
    assert expected in probe.states
    assert unexpected in probe.states
    assert "\\other-application\\task" not in probe.states

    receipt = collect_scheduler_receipt(registry, OBSERVED_AT, probe=probe)
    assert receipt.last_successful is not None
    rows = {row.task_name: row.state for row in receipt.last_successful.tasks}
    assert rows[unexpected] == "Running"


def test_scheduler_vnext_probe_hashes_registration_and_preserves_run_history(
    monkeypatch: MonkeyPatch,
) -> None:
    from execution import collect_operations_runtime_observations as collector

    registry = build_operations_registry(PROJECT_ROOT)
    task = registry.scheduled_tasks[0]
    calls = 0

    def completed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal calls
        calls += 1
        stdout = cast(BinaryIO, kwargs["stdout"])
        if calls == 1:
            stdout.write(
                f'"TaskName","Next Run Time","Status"\n"{task.task_name}","N/A","Ready"\n'.encode()
            )
        else:
            stdout.write(
                json.dumps(
                    [
                        {
                            "task_name": task.task_name,
                            "state": "Ready",
                            "execute": rf"C:\private\earnings-summary\cron\{task.wrapper}",
                            "arguments": "",
                            "working_directory": r"C:\private\earnings-summary\cron",
                            "last_run_time": "2026-08-20T07:55:00+00:00",
                            "next_run_time": "2026-08-21T07:55:00+00:00",
                            "last_task_result": 0,
                        }
                    ]
                ).encode()
            )
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(collector, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(collector.subprocess, "run", completed)

    probe = collect_scheduler_tasks_from_system()
    receipt = collect_scheduler_receipt(registry, OBSERVED_AT, probe=probe)
    assert receipt.schema_version == "3"
    assert receipt.last_successful is not None
    row = next(item for item in receipt.last_successful.tasks if item.task_name == task.task_name)
    assert row.registered_action_sha256 is not None
    assert row.registered_checkout_sha256 is not None
    assert row.registered_wrapper_sha256 is not None
    assert row.wrapper_match is True
    assert row.attempt_state == "succeeded"
    assert row.last_attempted_at == datetime(2026, 8, 20, 7, 55, tzinfo=UTC)
    assert row.last_successful_at == row.last_attempted_at
    assert row.next_expected_at == datetime(2026, 8, 21, 7, 55, tzinfo=UTC)


def test_scheduler_vnext_carries_success_only_for_same_registered_action(tmp_path: Path) -> None:
    from execution import collect_operations_runtime_observations as collector

    task_name = rf"{CANONICAL_TASK_NAMESPACE}morning"
    prior_success = OBSERVED_AT - timedelta(days=1)
    prior = SchedulerRuntimeReceipt.success(
        observed_at=OBSERVED_AT - timedelta(hours=1),
        tasks=(
            SchedulerTaskReceipt(
                task_name=task_name,
                state="Ready",
                registered_action_sha256="a" * 64,
                last_attempted_at=prior_success,
                last_successful_at=prior_success,
                attempt_state="succeeded",
            ),
        ),
    )
    receipt_path = tmp_path / "scheduler.latest.json"
    write_atomic_receipt(receipt_path, prior.model_dump_json())
    current = SchedulerRuntimeReceipt.success(
        observed_at=OBSERVED_AT,
        tasks=(
            SchedulerTaskReceipt(
                task_name=task_name,
                state="Ready",
                registered_action_sha256="a" * 64,
                last_attempted_at=OBSERVED_AT,
                last_result=1,
                attempt_state="failed",
            ),
        ),
    )

    merged = collector.retain_scheduler_task_successes(current, receipt_path)
    assert merged.last_successful is not None
    assert merged.last_successful.tasks[0].last_successful_at == prior_success

    assert current.last_successful is not None
    changed = current.model_copy(
        update={
            "last_successful": SchedulerReceipt(
                observed_at=OBSERVED_AT,
                tasks=(
                    current.last_successful.tasks[0].model_copy(
                        update={"registered_action_sha256": "b" * 64}
                    ),
                ),
            )
        }
    )
    not_merged = collector.retain_scheduler_task_successes(changed, receipt_path)
    assert not_merged.last_successful is not None
    assert not_merged.last_successful.tasks[0].last_successful_at is None


@pytest.mark.parametrize(
    "payload",
    [
        b'"TaskName","Next Run Time","Status"\n"\\earnings-summary\\broken","N/A"\n',
        b'"TaskName","Next Run Time","Status"\n"\\earnings-summary\\broken","N/A","Ready\n',
    ],
)
def test_scheduler_probe_rejects_malformed_csv_rows(
    monkeypatch: MonkeyPatch, payload: bytes
) -> None:
    from execution import collect_operations_runtime_observations as collector

    def completed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        cast(BinaryIO, kwargs["stdout"]).write(payload)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(collector, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(collector.subprocess, "run", completed)

    probe = collect_scheduler_tasks_from_system()
    assert probe.states is None
    assert "malformed CSV" in (probe.detail or "")


def test_scheduler_probe_rejects_duplicate_canonical_task_identity(
    monkeypatch: MonkeyPatch,
) -> None:
    from execution import collect_operations_runtime_observations as collector

    payload = b'"TaskName","Next Run Time","Status"\n"\\earnings-summary\\duplicate","N/A","Ready"\n"\\EARNINGS-SUMMARY\\DUPLICATE","N/A","Running"\n'

    def completed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        cast(BinaryIO, kwargs["stdout"]).write(payload)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(collector, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(collector.subprocess, "run", completed)

    probe = collect_scheduler_tasks_from_system()
    assert probe.states is None
    assert probe.detail == "Scheduler query contains duplicate task identity"


def test_service_missing_row_is_available_on_production_probe_path(
    monkeypatch: MonkeyPatch,
) -> None:
    from execution import collect_operations_runtime_observations as collector

    registry = build_operations_registry(PROJECT_ROOT)

    def missing(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        command = cast(list[str], args[0])
        if command[0].casefold() == "powershell.exe":
            cast(BinaryIO, kwargs["stdout"]).write(
                "\n".join(service.name for service in registry.services).encode()
            )
            return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            args=["sc.exe", "query", "missing"],
            returncode=1060,
            stdout="[SC] OpenService FAILED 1060: The specified service does not exist.\n",
            stderr="",
        )

    monkeypatch.setattr(collector, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(collector.subprocess, "run", missing)

    probe = collector.collect_services_from_system(registry)

    assert probe.detail is None
    assert probe.states == {service.name.casefold(): "Missing" for service in registry.services}


def test_service_namespace_nonzero_is_unavailable_without_declared_fallback(
    monkeypatch: MonkeyPatch,
) -> None:
    from execution import collect_operations_runtime_observations as collector

    registry = build_operations_registry(PROJECT_ROOT)

    def failed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        command = cast(list[str], args[0])
        cast(BinaryIO, kwargs["stderr"]).write(b"access denied\n")
        return subprocess.CompletedProcess(args=command, returncode=5, stdout="", stderr="")

    monkeypatch.setattr(collector, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(collector.subprocess, "run", failed)
    probe = collector.collect_services_from_system(registry)

    assert probe.states is None
    assert probe.detail == "Service namespace enumeration failed: PowerShell returned status 5"


def test_service_namespace_output_is_bounded_before_materialization(
    monkeypatch: MonkeyPatch,
) -> None:
    from execution import collect_operations_runtime_observations as collector

    registry = build_operations_registry(PROJECT_ROOT)

    def oversized(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        command = cast(list[str], args[0])
        cast(BinaryIO, kwargs["stdout"]).write(b"x" * (MAX_SCHEDULER_OUTPUT_BYTES + 1))
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(collector, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(collector.subprocess, "run", oversized)
    probe = collector.collect_services_from_system(registry)

    assert probe.states is None
    assert probe.detail == "Service namespace enumeration exceeds bounded output"


@pytest.mark.parametrize("output", ["garbage output\n", "SERVICE_NAME:\n"])
def test_service_namespace_garbage_or_malformed_records_are_unavailable(
    monkeypatch: MonkeyPatch, output: str
) -> None:
    from execution import collect_operations_runtime_observations as collector

    registry = build_operations_registry(PROJECT_ROOT)

    def garbage(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        command = cast(list[str], args[0])
        cast(BinaryIO, kwargs["stdout"]).write(output.encode())
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(collector, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(collector.subprocess, "run", garbage)
    probe = collector.collect_services_from_system(registry)

    assert probe.states is None
    assert probe.detail is not None
    assert "Service namespace enumeration" in probe.detail


def test_service_state_requires_anchored_state_token() -> None:
    from execution import collect_operations_runtime_observations as collector

    service_state = cast(Callable[[str, int], str], getattr(collector, "_service_state"))
    assert service_state("SERVICE_NAME: running-helper\n", 0) == "Unknown"
    assert service_state("SERVICE_NAME: es-dashboard\nSTATE : 4 RUNNING\n", 0) == "Running"


def test_service_receipt_preserves_injected_repo_managed_unexpected_service() -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    unexpected_name = "es-unexpected"
    receipt = collect_service_receipt(
        registry,
        OBSERVED_AT,
        probe=ServiceProbe(
            states={
                **{service.name: "Running" for service in registry.services},
                unexpected_name: "Stopped",
            }
        ),
    )
    assert receipt.last_successful is not None
    assert {
        row.name: row.state
        for row in receipt.last_successful.services
        if row.name == unexpected_name
    } == {unexpected_name: "Stopped"}


def test_production_service_probe_discovers_bounded_es_namespace_extra(
    monkeypatch: MonkeyPatch,
) -> None:
    from execution import collect_operations_runtime_observations as collector

    registry = build_operations_registry(PROJECT_ROOT)
    unexpected_name = "es-unexpected"

    def query(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        command = cast(list[str], args[0])
        if command[0].casefold() == "powershell.exe":
            assert "Get-Service -Name 'es-*'" in command[-1]
            assert "-ErrorAction Stop" in command[-1]
            assert "SilentlyContinue" not in command[-1]
            assert "state=" not in command
            cast(BinaryIO, kwargs["stdout"]).write(f"{unexpected_name}\n".encode())
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="",
                stderr="",
            )
        name = command[-1]
        state = "STOPPED" if name == unexpected_name else "RUNNING"
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=f"STATE : 4 {state}\n",
            stderr="",
        )

    monkeypatch.setattr(collector, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(collector.subprocess, "run", query)
    probe = collector.collect_services_from_system(registry)

    assert probe.states is not None
    assert probe.states[unexpected_name] == "Stopped"


@pytest.mark.parametrize(
    ("failure", "expected_detail"),
    [
        (
            subprocess.TimeoutExpired(cmd=["sc.exe", "query"], timeout=2),
            "Service probe failed: TimeoutExpired",
        ),
        (OSError("sc.exe unavailable"), "Service probe failed: OSError"),
    ],
)
def test_service_launch_or_timeout_failure_is_unavailable(
    monkeypatch: MonkeyPatch,
    failure: BaseException,
    expected_detail: str,
) -> None:
    from execution import collect_operations_runtime_observations as collector

    registry = build_operations_registry(PROJECT_ROOT)

    def failed(*args: object, **kwargs: object) -> object:
        raise failure

    monkeypatch.setattr(collector, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(collector.subprocess, "run", failed)

    probe = collector.collect_services_from_system(registry)

    assert probe.states is None
    assert probe.detail == expected_detail


def test_mixed_service_missing_and_launch_failure_is_fail_closed(
    monkeypatch: MonkeyPatch,
) -> None:
    from execution import collect_operations_runtime_observations as collector

    registry = build_operations_registry(PROJECT_ROOT)
    calls = 0

    def mixed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        command = cast(list[str], args[0])
        if command[0].casefold() == "powershell.exe":
            cast(BinaryIO, kwargs["stdout"]).write(
                "\n".join(service.name for service in registry.services).encode()
            )
            return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
        if calls == 2:
            return subprocess.CompletedProcess(
                args=["sc.exe", "query", "missing"],
                returncode=1060,
                stdout="[SC] OpenService FAILED 1060: The specified service does not exist.\n",
                stderr="",
            )
        raise subprocess.TimeoutExpired(cmd=["sc.exe", "query"], timeout=2)

    monkeypatch.setattr(collector, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(collector.subprocess, "run", mixed)

    probe = collector.collect_services_from_system(registry)

    assert calls == 3
    assert probe.states is None
    assert probe.detail == "Service probe failed: TimeoutExpired"


def test_service_unavailable_emit_retains_prior_success_in_v2_envelope(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from execution import collect_operations_runtime_observations as collector

    registry = build_operations_registry(PROJECT_ROOT)
    prior = ServiceRuntimeReceipt.success(
        observed_at=OBSERVED_AT,
        services=tuple(
            ServiceReceiptRow(name=service.name, state="Stopped") for service in registry.services
        ),
    )
    service_path = service_receipt_path(tmp_path)
    write_atomic_receipt(service_path, prior.model_dump_json())

    def failed(*args: object, **kwargs: object) -> object:
        raise OSError("sc.exe unavailable")

    def registry_for_root(_root: Path) -> OperationsRegistry:
        return registry

    monkeypatch.setattr(collector, "build_operations_registry", registry_for_root)
    monkeypatch.setattr(collector, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(collector.subprocess, "run", failed)

    assert collector.main(["--repo-root", str(tmp_path), "--emit-receipts"]) == 0

    emitted = ServiceRuntimeReceipt.model_validate_json(service_path.read_text(encoding="utf-8"))
    assert emitted.probe_attempt.availability == "unavailable"
    assert emitted.last_successful == prior.last_successful
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert {(event["event"], event["domain"]) for event in events} >= {
        ("retained_absent", "scheduler"),
        ("retained_revalidated", "service"),
    }
    assert all("last_successful_evidence_revalidated" not in event.values() for event in events)


def test_cli_reads_registry_from_code_root_and_writes_receipts_to_product_root(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from execution import collect_operations_runtime_observations as collector

    code_root = tmp_path / "runtime-checkout"
    product_root = tmp_path / "product-checkout"
    registry = build_operations_registry(PROJECT_ROOT)
    observed: dict[str, Path] = {}
    configured_database = product_root / "data" / "portfolio.db"

    def registry_for_root(root: Path) -> OperationsRegistry:
        observed["code_root"] = root
        return registry

    def emit_for_root(
        received_registry: OperationsRegistry,
        root: Path,
        observed_at: datetime,
    ) -> tuple[SchedulerRuntimeReceipt, ServiceRuntimeReceipt, bool]:
        assert received_registry == registry
        observed["product_root"] = root
        return (
            SchedulerRuntimeReceipt.success(observed_at=observed_at, tasks=()),
            ServiceRuntimeReceipt.success(observed_at=observed_at, services=()),
            True,
        )

    def env_is_absent(_root: Path) -> bool:
        return False

    monkeypatch.setattr(collector, "build_operations_registry", registry_for_root)
    monkeypatch.setattr(collector, "emit_runtime_receipts", emit_for_root)
    monkeypatch.setattr(collector, "load_project_env", env_is_absent)
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(configured_database))

    assert (
        collector.main(
            [
                "--code-root",
                str(code_root),
                "--emit-receipts",
            ]
        )
        == 0
    )
    assert observed == {
        "code_root": code_root.resolve(),
        "product_root": product_root.resolve(),
    }


def test_collector_wrapper_routes_receipts_to_configured_product_root() -> None:
    wrapper = PROJECT_ROOT / "cron" / "run_collect_operations_runtime_observations.bat"
    text = wrapper.read_text(encoding="utf-8")

    assert '--code-root "%PROJECT_ROOT%"' in text
    assert "--repo-root" not in text


def test_configured_product_root_rejects_noncanonical_database_layout(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from execution import collect_operations_runtime_observations as collector

    def env_is_absent(_root: Path) -> bool:
        return False

    monkeypatch.setattr(collector, "load_project_env", env_is_absent)
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(tmp_path / "portfolio.db"))

    with pytest.raises(RuntimeError, match=r"data/portfolio\.db"):
        collector.configured_product_root(tmp_path / "runtime-checkout")


def test_collector_clock_rollback_drops_future_retained_v2_evidence(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from execution import collect_operations_runtime_observations as collector

    registry = build_operations_registry(PROJECT_ROOT)
    future = OBSERVED_AT + timedelta(minutes=1)
    write_atomic_receipt(
        scheduler_receipt_path(tmp_path),
        SchedulerRuntimeReceipt.success(observed_at=future, tasks=()).model_dump_json(),
    )
    write_atomic_receipt(
        service_receipt_path(tmp_path),
        ServiceRuntimeReceipt.success(observed_at=future, services=()).model_dump_json(),
    )

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return OBSERVED_AT

    def registry_for_root(_root: Path) -> OperationsRegistry:
        return registry

    def failed(*args: object, **kwargs: object) -> object:
        raise OSError("status command unavailable")

    monkeypatch.setattr(collector, "build_operations_registry", registry_for_root)
    monkeypatch.setattr(collector, "datetime", FrozenDateTime)
    monkeypatch.setattr(collector, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(collector.subprocess, "run", failed)

    assert collector.main(["--repo-root", str(tmp_path), "--emit-receipts"]) == 0

    emitted_scheduler = SchedulerRuntimeReceipt.model_validate_json(
        scheduler_receipt_path(tmp_path).read_text(encoding="utf-8")
    )
    emitted_services = ServiceRuntimeReceipt.model_validate_json(
        service_receipt_path(tmp_path).read_text(encoding="utf-8")
    )
    assert emitted_scheduler.probe_attempt.availability == "available"
    assert emitted_scheduler.probe_attempt.attempted_at == future
    assert emitted_services.probe_attempt.availability == "available"
    assert emitted_services.probe_attempt.attempted_at == future
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert {(event["event"], event["domain"]) for event in events} == {
        ("retained_rejected", "scheduler"),
        ("retained_rejected", "service"),
        ("skipped_older", "scheduler"),
        ("skipped_older", "service"),
    }
    assert not any(event["event"] == "retained_revalidated" for event in events)
    assert all(
        event["probe_reason"] == "Service probe failed: OSError"
        for event in events
        if event["domain"] == "service"
    )


def test_cross_process_receipt_lock_keeps_older_slower_emitter_from_overwriting_newer(
    tmp_path: Path,
) -> None:
    marker_path = tmp_path / "slow-emitter-locked"
    context = multiprocessing.get_context("spawn")
    older = context.Process(
        target=_emit_runtime_worker,
        args=(
            str(tmp_path),
            str(PROJECT_ROOT),
            OBSERVED_AT.isoformat(),
            0.8,
            str(marker_path),
        ),
    )
    newer = context.Process(
        target=_emit_runtime_worker,
        args=(
            str(tmp_path),
            str(PROJECT_ROOT),
            (OBSERVED_AT + timedelta(minutes=1)).isoformat(),
            0.0,
            None,
        ),
    )
    older.start()
    deadline = time.monotonic() + 10.0
    while not marker_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker_path.exists()
    newer.start()
    older.join(15.0)
    newer.join(15.0)
    assert older.exitcode == 0
    assert newer.exitcode == 0

    scheduler = SchedulerRuntimeReceipt.model_validate_json(
        scheduler_receipt_path(tmp_path).read_text(encoding="utf-8")
    )
    services = ServiceRuntimeReceipt.model_validate_json(
        service_receipt_path(tmp_path).read_text(encoding="utf-8")
    )
    assert scheduler.probe_attempt.attempted_at == OBSERVED_AT + timedelta(minutes=1)
    assert services.probe_attempt.attempted_at == OBSERVED_AT + timedelta(minutes=1)


def test_reverse_order_newer_receipts_block_later_older_emitter(tmp_path: Path) -> None:
    marker_path = tmp_path / "newer-emitter-locked"
    context = multiprocessing.get_context("spawn")
    newer = context.Process(
        target=_emit_runtime_worker,
        args=(
            str(tmp_path),
            str(PROJECT_ROOT),
            (OBSERVED_AT + timedelta(minutes=1)).isoformat(),
            0.0,
            str(marker_path),
        ),
    )
    newer.start()
    newer.join(15.0)
    assert marker_path.exists()
    assert newer.exitcode == 0

    older = context.Process(
        target=_emit_runtime_worker,
        args=(str(tmp_path), str(PROJECT_ROOT), OBSERVED_AT.isoformat(), 0.0, None),
    )
    older.start()
    older.join(15.0)
    assert older.exitcode == 0

    scheduler = SchedulerRuntimeReceipt.model_validate_json(
        scheduler_receipt_path(tmp_path).read_text(encoding="utf-8")
    )
    services = ServiceRuntimeReceipt.model_validate_json(
        service_receipt_path(tmp_path).read_text(encoding="utf-8")
    )
    assert scheduler.probe_attempt.attempted_at == OBSERVED_AT + timedelta(minutes=1)
    assert services.probe_attempt.attempted_at == OBSERVED_AT + timedelta(minutes=1)


def test_v1_receipt_timestamp_blocks_older_v2_emitter(tmp_path: Path) -> None:
    from execution import collect_operations_runtime_observations as collector

    registry = build_operations_registry(PROJECT_ROOT)
    newer = OBSERVED_AT + timedelta(minutes=1)
    scheduler_path = scheduler_receipt_path(tmp_path)
    service_path = service_receipt_path(tmp_path)
    write_atomic_receipt(
        scheduler_path,
        collector.SchedulerReceipt(
            schema_version="1",
            observed_at=newer,
            tasks=tuple(
                SchedulerTaskReceipt(task_name=task.task_name, state="Ready")
                for task in registry.scheduled_tasks
            ),
        ).model_dump_json(),
    )
    write_atomic_receipt(
        service_path,
        collector.ServiceReceipt(
            schema_version="1",
            observed_at=newer,
            services=tuple(
                ServiceReceiptRow(name=service.name, state="Running")
                for service in registry.services
            ),
        ).model_dump_json(),
    )
    before_scheduler = scheduler_path.read_bytes()
    before_service = service_path.read_bytes()
    scheduler, services, ok = collector.emit_runtime_receipts(
        registry,
        tmp_path,
        OBSERVED_AT,
        scheduler_probe=collector.SchedulerProbe(
            states={task.task_name: "Ready" for task in registry.scheduled_tasks}
        ),
        service_probe=collector.ServiceProbe(
            states={service.name.casefold(): "Running" for service in registry.services}
        ),
    )
    assert ok is True
    assert scheduler.probe_attempt.attempted_at == OBSERVED_AT
    assert services.probe_attempt.attempted_at == OBSERVED_AT
    assert scheduler_path.read_bytes() == before_scheduler
    assert service_path.read_bytes() == before_service


def test_pair_publication_rolls_back_both_receipts_on_second_domain_failure(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from execution import collect_operations_runtime_observations as collector

    registry = build_operations_registry(PROJECT_ROOT)
    scheduler_path = scheduler_receipt_path(tmp_path)
    service_path = service_receipt_path(tmp_path)
    old_scheduler = collector.SchedulerRuntimeReceipt.success(
        observed_at=OBSERVED_AT,
        tasks=tuple(
            SchedulerTaskReceipt(task_name=task.task_name, state="Ready")
            for task in registry.scheduled_tasks
        ),
    )
    old_services = collector.ServiceRuntimeReceipt.success(
        observed_at=OBSERVED_AT,
        services=tuple(
            ServiceReceiptRow(name=service.name, state="Running") for service in registry.services
        ),
    )
    write_atomic_receipt(scheduler_path, old_scheduler.model_dump_json())
    write_atomic_receipt(service_path, old_services.model_dump_json())
    before_scheduler = scheduler_path.read_bytes()
    before_service = service_path.read_bytes()
    original_commit = collector.commit_staged_receipt

    def fail_service(staged: Path, target: Path) -> None:
        if target == service_path:
            raise OSError("service receipt disk full")
        original_commit(staged, target)

    monkeypatch.setattr(collector, "commit_staged_receipt", fail_service)
    _scheduler, _services, ok = collector.emit_runtime_receipts(
        registry,
        tmp_path,
        OBSERVED_AT + timedelta(minutes=1),
        scheduler_probe=collector.SchedulerProbe(
            states={task.task_name: "Running" for task in registry.scheduled_tasks}
        ),
        service_probe=collector.ServiceProbe(
            states={service.name.casefold(): "Stopped" for service in registry.services}
        ),
    )

    assert ok is False
    assert scheduler_path.read_bytes() == before_scheduler
    assert service_path.read_bytes() == before_service
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert events[-1] == {
        "detail": "OSError",
        "domain": "service",
        "event": "runtime_receipt_write_failed",
    }
    assert events[-1]["event"] != "runtime_receipt_lock_unavailable"


def test_snapshot_prioritizes_one_valid_pair_generation_over_legacy_projections(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from operations import snapshot as snapshot_module

    registry = build_operations_registry(PROJECT_ROOT)
    scheduler_path = scheduler_receipt_path(tmp_path)
    service_path = service_receipt_path(tmp_path)
    pair_path = scheduler_path.with_name(RUNTIME_PAIR_RECEIPT_FILENAME)
    pair = RuntimeReceiptPair(
        generation="generation-pair",
        scheduler=SchedulerRuntimeReceipt.success(
            observed_at=OBSERVED_AT,
            tasks=tuple(
                SchedulerTaskReceipt(task_name=task.task_name, state="Ready")
                for task in registry.scheduled_tasks
            ),
        ),
        services=ServiceRuntimeReceipt.success(
            observed_at=OBSERVED_AT,
            services=tuple(
                ServiceReceiptRow(name=service.name, state="Running")
                for service in registry.services
            ),
        ),
    )
    write_atomic_receipt(pair_path, pair.model_dump_json())
    write_atomic_receipt(
        scheduler_path,
        SchedulerRuntimeReceipt.success(
            observed_at=OBSERVED_AT,
            tasks=tuple(
                SchedulerTaskReceipt(task_name=task.task_name, state="Disabled")
                for task in registry.scheduled_tasks
            ),
        ).model_dump_json(),
    )
    write_atomic_receipt(
        service_path,
        ServiceRuntimeReceipt.success(
            observed_at=OBSERVED_AT,
            services=tuple(
                ServiceReceiptRow(name=service.name, state="Stopped")
                for service in registry.services
            ),
        ).model_dump_json(),
    )

    original_read = cast(
        Callable[[Path, type[BaseModel]], BaseModel], getattr(snapshot_module, "_read_receipt")
    )
    pair_reads: list[Path] = []

    def counted_read(path: Path, model: type[BaseModel]) -> BaseModel:
        if path.name == RUNTIME_PAIR_RECEIPT_FILENAME:
            pair_reads.append(path)
        return original_read(path, model)

    monkeypatch.setattr(snapshot_module, "_read_receipt", counted_read)

    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=OBSERVED_AT + timedelta(minutes=1),
        scheduler_receipt_path=scheduler_path,
        service_receipt_path=service_path,
    )

    assert {row.state for row in snapshot.scheduler.values if row.registry_match == "expected"} == {
        "Ready"
    }
    assert {row.state for row in snapshot.services.values if row.registry_match == "expected"} == {
        "Running"
    }
    assert pair_reads == [pair_path]


def test_crafted_pair_journal_is_rejected_without_touching_outside_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from execution import collect_operations_runtime_observations as collector

    registry = build_operations_registry(PROJECT_ROOT)
    scheduler_path = scheduler_receipt_path(tmp_path)
    journal = scheduler_path.with_name(".operations-runtime-receipts.pair.journal.json")
    outside = tmp_path / "outside-sensitive.txt"
    outside.write_text("sentinel", encoding="utf-8")
    receipt_dir = scheduler_path.parent
    token = "a" * 32
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(
        json.dumps(
            {
                "token": token,
                "scheduler_target": str(scheduler_path),
                "service_target": str(service_receipt_path(tmp_path)),
                "pair_target": str(scheduler_path.with_name(RUNTIME_PAIR_RECEIPT_FILENAME)),
                "scheduler_stage": str(outside),
                "service_stage": str(receipt_dir / f".services.latest.json.pair-{token}.tmp"),
                "pair_stage": str(
                    receipt_dir / f".{RUNTIME_PAIR_RECEIPT_FILENAME}.pair-{token}.tmp"
                ),
                "scheduler_backup": str(
                    receipt_dir / f".scheduler.latest.json.pair-backup-{token}"
                ),
                "service_backup": str(receipt_dir / f".services.latest.json.pair-backup-{token}"),
                "pair_backup": str(
                    receipt_dir / f".{RUNTIME_PAIR_RECEIPT_FILENAME}.pair-backup-{token}"
                ),
                "scheduler_had_target": False,
                "service_had_target": False,
                "pair_had_target": False,
            }
        ),
        encoding="utf-8",
    )

    _scheduler, _services, ok = collector.emit_runtime_receipts(
        registry,
        tmp_path,
        OBSERVED_AT,
        scheduler_probe=collector.SchedulerProbe(
            states={task.task_name: "Ready" for task in registry.scheduled_tasks}
        ),
        service_probe=collector.ServiceProbe(
            states={service.name.casefold(): "Running" for service in registry.services}
        ),
    )

    assert ok is False
    assert outside.read_text(encoding="utf-8") == "sentinel"
    assert journal.exists()
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert events[-1]["event"] == "runtime_receipt_write_failed"
    assert events[-1]["domain"] == "pair"


def test_pair_rollback_failure_is_classified_and_journal_preserved(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from execution import collect_operations_runtime_observations as collector

    registry = build_operations_registry(PROJECT_ROOT)
    scheduler_path = scheduler_receipt_path(tmp_path)
    service_path = service_receipt_path(tmp_path)
    write_atomic_receipt(
        scheduler_path,
        SchedulerRuntimeReceipt.success(
            observed_at=OBSERVED_AT,
            tasks=tuple(
                SchedulerTaskReceipt(task_name=task.task_name, state="Ready")
                for task in registry.scheduled_tasks
            ),
        ).model_dump_json(),
    )
    write_atomic_receipt(
        service_path,
        ServiceRuntimeReceipt.success(
            observed_at=OBSERVED_AT,
            services=tuple(
                ServiceReceiptRow(name=service.name, state="Running")
                for service in registry.services
            ),
        ).model_dump_json(),
    )
    original_commit = collector.commit_staged_receipt
    original_copyfile = collector.shutil.copyfile

    def fail_service_commit(staged: Path, target: Path) -> None:
        if target == service_path:
            raise OSError("service commit failed")
        original_commit(staged, target)

    def fail_scheduler_restore(source: str | Path, target: str | Path) -> None:
        if Path(target) == scheduler_path and "pair-backup" in Path(source).name:
            raise OSError("scheduler rollback failed")
        original_copyfile(source, target)

    monkeypatch.setattr(collector, "commit_staged_receipt", fail_service_commit)
    monkeypatch.setattr(collector.shutil, "copyfile", fail_scheduler_restore)
    _scheduler, _services, ok = collector.emit_runtime_receipts(
        registry,
        tmp_path,
        OBSERVED_AT + timedelta(minutes=1),
        scheduler_probe=collector.SchedulerProbe(
            states={task.task_name: "Running" for task in registry.scheduled_tasks}
        ),
        service_probe=collector.ServiceProbe(
            states={service.name.casefold(): "Stopped" for service in registry.services}
        ),
    )

    assert ok is False
    journal = scheduler_path.with_name(".operations-runtime-receipts.pair.journal.json")
    assert journal.exists()
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert events[-1]["event"] == "runtime_receipt_write_failed"
    assert events[-1]["domain"] == "rollback"


def test_pair_rollback_failure_recovery_restores_all_prior_artifacts(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from execution import collect_operations_runtime_observations as collector

    registry = build_operations_registry(PROJECT_ROOT)
    initial_scheduler = collector.SchedulerProbe(
        states={task.task_name: "Ready" for task in registry.scheduled_tasks}
    )
    initial_services = collector.ServiceProbe(
        states={service.name.casefold(): "Running" for service in registry.services}
    )
    collector.emit_runtime_receipts(
        registry,
        tmp_path,
        OBSERVED_AT,
        scheduler_probe=initial_scheduler,
        service_probe=initial_services,
    )
    scheduler_path = scheduler_receipt_path(tmp_path)
    service_path = service_receipt_path(tmp_path)
    pair_path = scheduler_path.with_name(RUNTIME_PAIR_RECEIPT_FILENAME)
    before = (scheduler_path.read_bytes(), service_path.read_bytes(), pair_path.read_bytes())
    original_commit = collector.commit_staged_receipt
    original_copyfile = collector.shutil.copyfile

    def fail_service_commit(staged: Path, target: Path) -> None:
        if target == service_path:
            raise OSError("service commit failed")
        original_commit(staged, target)

    def fail_scheduler_restore(source: str | Path, destination: str | Path) -> str | Path:
        if Path(destination) == scheduler_path and "pair-backup" in Path(source).name:
            raise OSError("scheduler rollback failed")
        return original_copyfile(source, destination)

    monkeypatch.setattr(collector, "commit_staged_receipt", fail_service_commit)
    monkeypatch.setattr(collector.shutil, "copyfile", fail_scheduler_restore)
    _scheduler, _services, ok = collector.emit_runtime_receipts(
        registry,
        tmp_path,
        OBSERVED_AT + timedelta(minutes=1),
        scheduler_probe=collector.SchedulerProbe(
            states={task.task_name: "Running" for task in registry.scheduled_tasks}
        ),
        service_probe=collector.ServiceProbe(
            states={service.name.casefold(): "Stopped" for service in registry.services}
        ),
    )
    assert ok is False
    journal = scheduler_path.with_name(".operations-runtime-receipts.pair.journal.json")
    assert journal.exists()

    monkeypatch.undo()
    recover = cast(Callable[[Path], None], getattr(collector, "_recover_pair_journal"))
    journal_payload = json.loads(journal.read_text(encoding="utf-8"))
    missing_backup = Path(cast(str, journal_payload["scheduler_backup"]))
    missing_backup.unlink()
    with pytest.raises(collector.PairRecoveryError, match="required pair rollback backup"):
        recover(journal)
    assert journal.exists()
    missing_backup.write_bytes(before[0])
    original_unlink = Path.unlink
    failed_backup_cleanup = False

    def fail_second_backup_cleanup(self: Path, missing_ok: bool = False) -> None:
        nonlocal failed_backup_cleanup
        if ".services.latest.json.pair-backup-" in self.name and not failed_backup_cleanup:
            failed_backup_cleanup = True
            raise OSError("second backup cleanup failed")
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(collector.Path, "unlink", fail_second_backup_cleanup)
    with pytest.raises(collector.PairRecoveryError, match="OSError"):
        recover(journal)
    marker = scheduler_path.with_name(collector.PAIR_COMMITTED_MARKER_FILENAME)
    assert not journal.exists()
    assert marker.exists()
    monkeypatch.undo()
    recover(journal)
    assert (
        scheduler_path.read_bytes(),
        service_path.read_bytes(),
        pair_path.read_bytes(),
    ) == before
    assert not journal.exists()
    assert not marker.exists()


def test_pair_backup_copy_failure_preserves_all_prior_receipts(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from execution import collect_operations_runtime_observations as collector

    registry = build_operations_registry(PROJECT_ROOT)
    scheduler_path = scheduler_receipt_path(tmp_path)
    service_path = service_receipt_path(tmp_path)
    write_atomic_receipt(
        scheduler_path,
        SchedulerRuntimeReceipt.success(
            observed_at=OBSERVED_AT,
            tasks=tuple(
                SchedulerTaskReceipt(task_name=task.task_name, state="Ready")
                for task in registry.scheduled_tasks
            ),
        ).model_dump_json(),
    )
    write_atomic_receipt(
        service_path,
        ServiceRuntimeReceipt.success(
            observed_at=OBSERVED_AT,
            services=tuple(
                ServiceReceiptRow(name=service.name, state="Running")
                for service in registry.services
            ),
        ).model_dump_json(),
    )
    before = (scheduler_path.read_bytes(), service_path.read_bytes())
    original_copy = collector.shutil.copyfile

    def fail_service_backup(source: str | Path, destination: str | Path) -> str | Path:
        if Path(source) == service_path:
            raise OSError("service backup copy failed")
        return original_copy(source, destination)

    monkeypatch.setattr(collector.shutil, "copyfile", fail_service_backup)
    _scheduler, _services, ok = collector.emit_runtime_receipts(
        registry,
        tmp_path,
        OBSERVED_AT + timedelta(minutes=1),
        scheduler_probe=collector.SchedulerProbe(
            states={task.task_name: "Running" for task in registry.scheduled_tasks}
        ),
        service_probe=collector.ServiceProbe(
            states={service.name.casefold(): "Stopped" for service in registry.services}
        ),
    )

    assert ok is False
    assert (scheduler_path.read_bytes(), service_path.read_bytes()) == before
    assert not scheduler_path.with_name(RUNTIME_PAIR_RECEIPT_FILENAME).exists()
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert events[-1]["event"] == "runtime_receipt_write_failed"
    assert events[-1]["domain"] == "pair"


def test_pair_backup_cleanup_failure_leaves_safe_marker_and_recovery_cleans_it(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from execution import collect_operations_runtime_observations as collector

    registry = build_operations_registry(PROJECT_ROOT)
    collector.emit_runtime_receipts(
        registry,
        tmp_path,
        OBSERVED_AT,
        scheduler_probe=collector.SchedulerProbe(
            states={task.task_name: "Ready" for task in registry.scheduled_tasks}
        ),
        service_probe=collector.ServiceProbe(
            states={service.name.casefold(): "Running" for service in registry.services}
        ),
    )
    scheduler_path = scheduler_receipt_path(tmp_path)
    service_path = service_receipt_path(tmp_path)
    pair_path = scheduler_path.with_name(RUNTIME_PAIR_RECEIPT_FILENAME)
    original_unlink = Path.unlink
    failed = False

    def fail_backup_cleanup(self: Path, missing_ok: bool = False) -> None:
        nonlocal failed
        if "pair-backup" in self.name and not failed:
            failed = True
            raise OSError("backup cleanup failed")
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(collector.Path, "unlink", fail_backup_cleanup)
    _scheduler, _services, ok = collector.emit_runtime_receipts(
        registry,
        tmp_path,
        OBSERVED_AT + timedelta(minutes=1),
        scheduler_probe=collector.SchedulerProbe(
            states={task.task_name: "Running" for task in registry.scheduled_tasks}
        ),
        service_probe=collector.ServiceProbe(
            states={service.name.casefold(): "Stopped" for service in registry.services}
        ),
    )
    assert ok is True
    assert SchedulerRuntimeReceipt.model_validate_json(scheduler_path.read_bytes())
    assert ServiceRuntimeReceipt.model_validate_json(service_path.read_bytes())
    assert RuntimeReceiptPair.model_validate_json(pair_path.read_bytes())
    journal = scheduler_path.with_name(".operations-runtime-receipts.pair.journal.json")
    marker = scheduler_path.with_name(collector.PAIR_COMMITTED_MARKER_FILENAME)
    assert not journal.exists()
    assert marker.exists()

    monkeypatch.undo()
    recover = cast(Callable[[Path], None], getattr(collector, "_recover_pair_journal"))
    recover(journal)
    assert not marker.exists()
    assert SchedulerRuntimeReceipt.model_validate_json(scheduler_path.read_bytes())
    assert ServiceRuntimeReceipt.model_validate_json(service_path.read_bytes())
    assert RuntimeReceiptPair.model_validate_json(pair_path.read_bytes())
    assert "runtime_receipt_cleanup_pending" in capsys.readouterr().err


def test_rollback_success_stage_cleanup_failure_uses_cleanup_marker(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from execution import collect_operations_runtime_observations as collector

    registry = build_operations_registry(PROJECT_ROOT)
    collector.emit_runtime_receipts(
        registry,
        tmp_path,
        OBSERVED_AT,
        scheduler_probe=collector.SchedulerProbe(
            states={task.task_name: "Ready" for task in registry.scheduled_tasks}
        ),
        service_probe=collector.ServiceProbe(
            states={service.name.casefold(): "Running" for service in registry.services}
        ),
    )
    scheduler_path = scheduler_receipt_path(tmp_path)
    service_path = service_receipt_path(tmp_path)
    pair_path = scheduler_path.with_name(RUNTIME_PAIR_RECEIPT_FILENAME)
    original_commit = collector.commit_staged_receipt
    original_unlink = Path.unlink
    failed_cleanup = False

    def fail_service_commit(staged: Path, target: Path) -> None:
        if target == service_path:
            raise OSError("service commit failed")
        original_commit(staged, target)

    def fail_service_stage_cleanup(self: Path, missing_ok: bool = False) -> None:
        nonlocal failed_cleanup
        if ".services.latest.json.pair-" in self.name and not failed_cleanup:
            failed_cleanup = True
            raise OSError("staged cleanup failed")
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(collector, "commit_staged_receipt", fail_service_commit)
    monkeypatch.setattr(collector.Path, "unlink", fail_service_stage_cleanup)
    _scheduler, _services, ok = collector.emit_runtime_receipts(
        registry,
        tmp_path,
        OBSERVED_AT + timedelta(minutes=1),
        scheduler_probe=collector.SchedulerProbe(
            states={task.task_name: "Running" for task in registry.scheduled_tasks}
        ),
        service_probe=collector.ServiceProbe(
            states={service.name.casefold(): "Stopped" for service in registry.services}
        ),
    )
    assert ok is False
    assert SchedulerRuntimeReceipt.model_validate_json(scheduler_path.read_bytes()).last_successful
    assert ServiceRuntimeReceipt.model_validate_json(service_path.read_bytes()).last_successful
    assert RuntimeReceiptPair.model_validate_json(pair_path.read_bytes()).generation
    journal = scheduler_path.with_name(".operations-runtime-receipts.pair.journal.json")
    marker = scheduler_path.with_name(collector.PAIR_COMMITTED_MARKER_FILENAME)
    assert not journal.exists()
    assert marker.exists()

    monkeypatch.undo()
    recover = cast(Callable[[Path], None], getattr(collector, "_recover_pair_journal"))
    recover(journal)
    assert not marker.exists()
    assert SchedulerRuntimeReceipt.model_validate_json(scheduler_path.read_bytes()).last_successful
    assert ServiceRuntimeReceipt.model_validate_json(service_path.read_bytes()).last_successful
    assert RuntimeReceiptPair.model_validate_json(pair_path.read_bytes()).generation


def test_receipt_lock_failure_is_typed_unavailable_and_loud(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from execution import collect_operations_runtime_observations as collector
    from runtime.job_runtime import JobAlreadyRunningError

    class BusyLock:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def __enter__(self) -> BusyLock:
            raise JobAlreadyRunningError("write set busy: operations-runtime-receipts")

        def __exit__(self, *args: object) -> None:
            del args

    monkeypatch.setattr(collector, "JobLock", BusyLock)
    registry = build_operations_registry(PROJECT_ROOT)

    scheduler, services, lock_ok = collector.emit_runtime_receipts(
        registry, tmp_path, OBSERVED_AT, lock_wait_s=0.0
    )

    assert lock_ok is False
    assert scheduler.probe_attempt.availability == "unavailable"
    assert services.probe_attempt.availability == "unavailable"
    assert not scheduler_receipt_path(tmp_path).exists()
    assert not service_receipt_path(tmp_path).exists()
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert events == [
        {
            "detail": "JobAlreadyRunningError",
            "event": "runtime_receipt_lock_unavailable",
        }
    ]


def test_receipt_emission_borrows_only_a_cryptographically_valid_wrapper_lock(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from execution import collect_operations_runtime_observations as collector

    class UnexpectedLock:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("valid inherited lock must not be re-acquired")

    def valid_inherited_lock(*_args: object) -> bool:
        return True

    monkeypatch.setattr(collector, "inherited_lock_is_valid", valid_inherited_lock)
    monkeypatch.setattr(collector, "JobLock", UnexpectedLock)
    registry = build_operations_registry(PROJECT_ROOT)

    _scheduler, _services, lock_ok = collector.emit_runtime_receipts(
        registry,
        tmp_path,
        OBSERVED_AT,
        scheduler_probe=SchedulerProbe(
            states={task.task_name: "Ready" for task in registry.scheduled_tasks}
        ),
        service_probe=ServiceProbe(
            states={service.name.casefold(): "Running" for service in registry.services}
        ),
    )

    assert lock_ok is True


def test_cli_summary_schema_counts_every_declared_runtime_state() -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    summary = build_runtime_summary(
        SchedulerRuntimeReceipt.success(
            observed_at=OBSERVED_AT,
            tasks=(
                SchedulerTaskReceipt(task_name="one", state="Ready"),
                SchedulerTaskReceipt(task_name="two", state="Ready"),
                SchedulerTaskReceipt(task_name="three", state="Missing"),
            ),
        ),
        collect_service_receipt(
            registry,
            OBSERVED_AT,
            probe=ServiceProbe.unavailable("Service manager probe unavailable"),
        ),
        OBSERVED_AT,
        registry,
    )

    assert isinstance(summary, RuntimeCollectionSummary)
    assert summary.scheduler.counts["Ready"] == 2
    assert summary.scheduler.counts["Missing"] == 1
    assert summary.services.counts == {}
    assert summary.recurring_collection.state == "activation_required"


def test_runtime_summary_activates_only_for_current_ready_or_running_collector_task() -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    task_name = r"\earnings-summary\collect_operations_runtime_observations"
    summary = build_runtime_summary(
        SchedulerRuntimeReceipt.success(
            observed_at=OBSERVED_AT,
            tasks=(SchedulerTaskReceipt(task_name=task_name, state="Running"),),
        ),
        ServiceRuntimeReceipt.success(observed_at=OBSERVED_AT, services=()),
        OBSERVED_AT,
        registry,
    )

    assert summary.recurring_collection.task_name == task_name
    assert summary.recurring_collection.configuration_state == "declared_enabled"
    assert summary.recurring_collection.scheduler_observation == "current"
    assert summary.recurring_collection.scheduler_state == "Running"
    assert summary.recurring_collection.state == "activated"


@pytest.mark.parametrize("scheduler_state", ["Disabled", "Unknown", "Missing"])
def test_runtime_summary_requires_activation_for_nonrunning_collector_state(
    scheduler_state: SchedulerTaskState,
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    summary = build_runtime_summary(
        SchedulerRuntimeReceipt.success(
            observed_at=OBSERVED_AT,
            tasks=(
                SchedulerTaskReceipt(
                    task_name=r"\earnings-summary\collect_operations_runtime_observations",
                    state=scheduler_state,
                ),
            ),
        ),
        ServiceRuntimeReceipt.success(observed_at=OBSERVED_AT, services=()),
        OBSERVED_AT,
        registry,
    )

    assert summary.recurring_collection.state == "activation_required"
    payload = summary.model_dump()
    payload["recurring_collection"]["state"] = "activated"
    with pytest.raises(ValueError, match="activated recurring collection"):
        RuntimeCollectionSummary.model_validate(payload)


def test_runtime_summary_never_activates_from_retained_or_mismatched_scheduler_evidence() -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    summary = build_runtime_summary(
        SchedulerRuntimeReceipt(
            probe_attempt=RuntimeProbeAttempt(
                attempted_at=OBSERVED_AT,
                availability="unavailable",
                detail="probe unavailable",
            ),
            last_successful=SchedulerReceipt(
                observed_at=OBSERVED_AT - timedelta(minutes=1),
                tasks=(
                    SchedulerTaskReceipt(
                        task_name=r"\earnings-summary\collect_operations_runtime_observations",
                        state="Ready",
                    ),
                ),
            ),
        ),
        ServiceRuntimeReceipt.success(observed_at=OBSERVED_AT, services=()),
        OBSERVED_AT,
        registry,
    )

    assert summary.recurring_collection.scheduler_observation == "unavailable"
    assert summary.recurring_collection.state == "activation_required"

    mismatched = build_runtime_summary(
        SchedulerRuntimeReceipt.success(
            observed_at=OBSERVED_AT,
            tasks=(SchedulerTaskReceipt(task_name=r"\earnings-summary\other", state="Ready"),),
        ),
        ServiceRuntimeReceipt.success(observed_at=OBSERVED_AT, services=()),
        OBSERVED_AT,
        registry,
    )
    assert mismatched.recurring_collection.scheduler_observation == "current"
    assert mismatched.recurring_collection.scheduler_state is None
    assert mismatched.recurring_collection.state == "activation_required"


def test_runtime_summary_rejects_cross_domain_count_keys() -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    summary = build_runtime_summary(
        SchedulerRuntimeReceipt.success(observed_at=OBSERVED_AT, tasks=()),
        collect_service_receipt(
            registry, OBSERVED_AT, probe=ServiceProbe.unavailable("service unavailable")
        ),
        OBSERVED_AT,
        registry,
    )

    scheduler_payload = summary.model_dump()
    scheduler_payload["scheduler"]["counts"] = {"Stopped": 1}
    with pytest.raises(ValueError):
        RuntimeCollectionSummary.model_validate(scheduler_payload)

    service_payload = summary.model_dump()
    service_payload["services"]["counts"] = {"Ready": 1}
    with pytest.raises(ValueError):
        RuntimeCollectionSummary.model_validate(service_payload)


def test_runtime_summary_rejects_negative_counts() -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    summary = build_runtime_summary(
        SchedulerRuntimeReceipt.success(observed_at=OBSERVED_AT, tasks=()),
        collect_service_receipt(
            registry, OBSERVED_AT, probe=ServiceProbe.unavailable("service unavailable")
        ),
        OBSERVED_AT,
        registry,
    )
    payload = summary.model_dump()
    payload["scheduler"]["counts"] = {"Ready": -1}

    with pytest.raises(ValueError):
        RuntimeCollectionSummary.model_validate(payload)
