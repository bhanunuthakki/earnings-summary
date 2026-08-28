#!/usr/bin/env python3
"""Collect bounded, typed Scheduler and managed-service observations.

This is a Layer-3 evidence producer.  It never runs a task or service: it only
queries the platform's read-only status interfaces and, when evidence is
available, atomically replaces the two cached receipts consumed by Operations.
An unavailable probe deliberately does not overwrite a prior receipt; absence
of a receipt remains an unavailable observation, not a failed job.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, TypedDict, cast
from uuid import uuid4

from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from log_redact import sanitize_operational_text  # noqa: E402
from operations.models import (  # noqa: E402
    RUNTIME_PAIR_RECEIPT_FILENAME,
    OperationsRegistry,
    RuntimeCollectionSummary,
    RuntimeProbeAttempt,
    RuntimeReceiptPair,
    SchedulerAttemptState,
    SchedulerReceipt,
    SchedulerRuntimeReceipt,
    SchedulerTaskReceipt,
    SchedulerTaskState,
    ServiceReceipt,
    ServiceReceiptRow,
    ServiceRuntimeReceipt,
    ServiceState,
)
from operations.paths import scheduler_receipt_path, service_receipt_path  # noqa: E402
from operations.registry import build_operations_registry  # noqa: E402
from runtime.job_runtime import (  # noqa: E402
    JobAlreadyRunningError,
    JobLock,
    inherited_lock_is_valid,
)

SchedulerState = SchedulerTaskState
ServiceRuntimeState = ServiceState
MAX_SCHEDULER_OUTPUT_BYTES = 64 * 1024
MAX_SCHEDULER_ROWS = 256
MAX_SERVICE_ROWS = 256
CANONICAL_TASK_NAMESPACE = "\\earnings-summary\\"
RUNTIME_RECEIPT_WRITE_SET = "operations-runtime-receipts"
RUNTIME_RECEIPT_LOCK_WAIT_S = 30.0
PAIR_COMMITTED_MARKER_FILENAME = ".operations-runtime-receipts.pair.committed.json"
RECURRING_COLLECTOR_TASK_NAME = r"\earnings-summary\collect_operations_runtime_observations"


RetentionStatus = Literal["available", "absent", "rejected"]


def _is_windows_platform() -> bool:
    """Return whether the host provides the Windows runtime probes.

    Keep this decision behind a narrow seam so tests can exercise the Windows
    command paths without mutating the process-global ``os.name`` value.  The
    latter also controls pathlib's concrete path class and makes a Linux test
    process attempt to instantiate ``WindowsPath``.
    """

    return os.name == "nt"


@dataclass(frozen=True, slots=True)
class _RetainedSchedulerRead:
    receipt: SchedulerReceipt | None
    status: RetentionStatus
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _RetainedServiceRead:
    receipt: ServiceReceipt | None
    status: RetentionStatus
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class SchedulerProbe:
    """The result of one bounded Scheduler query, without inferred health."""

    states: Mapping[str, SchedulerState] | None
    details: Mapping[str, SchedulerTaskDetail] | None = None
    detail: str | None = None

    @classmethod
    def unavailable(cls, detail: str) -> SchedulerProbe:
        return cls(states=None, detail=detail)


@dataclass(frozen=True, slots=True)
class SchedulerTaskDetail:
    state: SchedulerState
    registered_action_sha256: str
    registered_checkout_sha256: str | None
    registered_wrapper_sha256: str | None
    registered_wrapper_name: str | None
    last_attempted_at: datetime | None
    next_expected_at: datetime | None
    last_result: int | None


class _SchedulerReceiptDetails(TypedDict, total=False):
    registered_action_sha256: str | None
    registered_checkout_sha256: str | None
    registered_wrapper_sha256: str | None
    wrapper_match: bool | None
    last_attempted_at: datetime | None
    last_successful_at: datetime | None
    next_expected_at: datetime | None
    last_result: int | None
    attempt_state: SchedulerAttemptState


@dataclass(frozen=True, slots=True)
class ServiceProbe:
    """The result of bounded per-service status queries, without inferred health."""

    states: Mapping[str, ServiceRuntimeState] | None
    detail: str | None = None

    @classmethod
    def unavailable(cls, detail: str) -> ServiceProbe:
        return cls(states=None, detail=detail)


class PairRecoveryError(OSError):
    """Typed, durable failure while validating or recovering pair publication."""


def log_event(event_type: str, **payload: Any) -> None:
    """Keep machine-readable events on stderr and omit raw command output."""

    sys.stderr.write(json.dumps({"event": event_type, **payload}, sort_keys=True) + "\n")
    sys.stderr.flush()


def _safe_reason(reason: str | None, fallback: str) -> str:
    value = reason or fallback
    return sanitize_operational_text(" ".join(value.split()), mode="persisted")


def _scheduler_state(value: str) -> SchedulerState:
    normalized = value.strip().casefold()
    if normalized == "ready":
        return "Ready"
    if normalized == "running":
        return "Running"
    if normalized == "disabled":
        return "Disabled"
    return "Unknown"


def _identity_sha256(value: str) -> str:
    normalized = value.strip().replace("/", "\\").casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _optional_scheduler_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Scheduler timestamp is not timezone-aware")
    return parsed


def _registered_wrapper_name(execute: str, arguments: str) -> str | None:
    candidates = re.findall(r'(?i)([^"\s]+\.bat)|"([^"]+\.bat)"', f"{execute} {arguments}")
    if not candidates:
        return None
    value = next(part for part in candidates[-1] if part)
    return PureWindowsPath(value).name


def _checkout_identity(execute: str, working_directory: str) -> str | None:
    candidate = working_directory.strip()
    if not candidate:
        command_path = PureWindowsPath(execute.strip().strip('"'))
        candidate = str(command_path.parent)
    if not candidate or candidate == ".":
        return None
    path = PureWindowsPath(candidate)
    if path.name.casefold() == "cron":
        path = path.parent
    return _identity_sha256(str(path))


def _collect_scheduler_details_from_system(
    timeout: float,
) -> Mapping[str, SchedulerTaskDetail] | None:
    """Collect typed task history while ensuring raw action paths are never persisted."""

    script = (
        "$ErrorActionPreference='Stop';"
        "$rows=Get-ScheduledTask -TaskPath '\\earnings-summary\\' | ForEach-Object {"
        "$task=$_;$info=$task | Get-ScheduledTaskInfo;$action=@($task.Actions)[0];"
        "[pscustomobject]@{task_name=($task.TaskPath+$task.TaskName);"
        "state=[string]$task.State;execute=[string]$action.Execute;"
        "arguments=[string]$action.Arguments;working_directory=[string]$action.WorkingDirectory;"
        "last_run_time=if($info.LastRunTime.Year -le 1900){$null}else{"
        "$info.LastRunTime.ToUniversalTime().ToString('o')};"
        "next_run_time=if($info.NextRunTime.Year -le 1900){$null}else{"
        "$info.NextRunTime.ToUniversalTime().ToString('o')};"
        "last_task_result=[int64]$info.LastTaskResult}};"
        "$json=ConvertTo-Json -InputObject @($rows) -Compress -Depth 3;"
        "[Console]::Out.Write($json)"
    )
    with tempfile.TemporaryFile() as stdout_file:
        try:
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                stdout=stdout_file,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        stdout_file.seek(0, os.SEEK_END)
        if stdout_file.tell() > MAX_SCHEDULER_OUTPUT_BYTES:
            return None
        stdout_file.seek(0)
        try:
            payload = json.loads(
                stdout_file.read(MAX_SCHEDULER_OUTPUT_BYTES + 1).decode("utf-8-sig")
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
    if not isinstance(payload, list):
        return None
    payload_items = cast(list[object], payload)
    if len(payload_items) > MAX_SCHEDULER_ROWS:
        return None
    details: dict[str, SchedulerTaskDetail] = {}
    try:
        for raw in payload_items:
            if not isinstance(raw, dict):
                return None
            raw = cast(dict[str, object], raw)
            task_name = raw.get("task_name")
            execute = raw.get("execute")
            arguments = raw.get("arguments")
            working_directory = raw.get("working_directory")
            if not (
                isinstance(task_name, str)
                and isinstance(execute, str)
                and isinstance(arguments, str)
                and isinstance(working_directory, str)
            ):
                return None
            if not task_name.casefold().startswith(CANONICAL_TASK_NAMESPACE):
                continue
            wrapper_name = _registered_wrapper_name(execute, arguments)
            last_result_raw = raw.get("last_task_result")
            last_result = last_result_raw if isinstance(last_result_raw, int) else None
            detail = SchedulerTaskDetail(
                state=_scheduler_state(str(raw.get("state", ""))),
                registered_action_sha256=_identity_sha256(
                    "\0".join((execute, arguments, working_directory))
                ),
                registered_checkout_sha256=_checkout_identity(execute, working_directory),
                registered_wrapper_sha256=(
                    _identity_sha256(wrapper_name) if wrapper_name is not None else None
                ),
                registered_wrapper_name=wrapper_name,
                last_attempted_at=_optional_scheduler_timestamp(raw.get("last_run_time")),
                next_expected_at=_optional_scheduler_timestamp(raw.get("next_run_time")),
                last_result=last_result,
            )
            key = task_name.casefold()
            if key in details:
                return None
            details[key] = detail
    except (TypeError, ValueError):
        return None
    return details


def _service_state(output: str, returncode: int) -> ServiceRuntimeState:
    normalized = output.casefold()
    if "1060" in normalized and "does not exist" in normalized:
        return "Missing"
    if returncode != 0:
        return "Unknown"
    state_match = re.search(r"(?im)^\s*state\s*:\s*\d+\s+([a-z_]+)\b", output)
    if state_match is not None:
        state_token = state_match.group(1).casefold()
        if state_token == "running":
            return "Running"
        if state_token == "paused":
            return "Paused"
        if state_token == "stopped":
            return "Stopped"
    return "Unknown"


def collect_scheduler_tasks_from_system(timeout: float = 4.0) -> SchedulerProbe:
    """Read all Scheduler task states once, with no Scheduler mutation or task run."""

    if not _is_windows_platform():
        return SchedulerProbe.unavailable("Windows Task Scheduler is unavailable on this platform")

    # Redirect stdout to a temporary file so a malformed or unexpectedly large
    # Scheduler response cannot be materialized without a bound in this process.
    # The file is read only after the child exits and is cleaned up by the context
    # manager; it is never an operational receipt or runtime state.
    with tempfile.TemporaryFile() as stdout_file:
        try:
            completed = subprocess.run(
                ["schtasks.exe", "/Query", "/FO", "CSV"],
                stdout=stdout_file,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return SchedulerProbe.unavailable(f"Scheduler probe failed: {type(exc).__name__}")
        if completed.returncode != 0:
            return SchedulerProbe.unavailable("Scheduler query returned a nonzero status")
        stdout_file.seek(0, os.SEEK_END)
        if stdout_file.tell() > MAX_SCHEDULER_OUTPUT_BYTES:
            return SchedulerProbe.unavailable("Scheduler query exceeds bounded output")
        stdout_file.seek(0)
        try:
            scheduler_output = stdout_file.read(MAX_SCHEDULER_OUTPUT_BYTES + 1).decode("utf-8-sig")
        except UnicodeDecodeError:
            return SchedulerProbe.unavailable("Scheduler query is not valid UTF-8")
        if len(scheduler_output.encode("utf-8")) > MAX_SCHEDULER_OUTPUT_BYTES:
            return SchedulerProbe.unavailable("Scheduler query exceeds bounded output")

    states: dict[str, SchedulerState] = {}
    try:
        for row in csv.reader(io.StringIO(scheduler_output), strict=True):
            if not any(cell.strip() for cell in row):
                continue
            if row[0].strip().casefold() == "taskname":
                if len(row) != 3:
                    return SchedulerProbe.unavailable("Scheduler query contains malformed CSV row")
                continue
            if len(row) != 3 or not row[0].strip() or not row[2].strip():
                return SchedulerProbe.unavailable("Scheduler query contains malformed CSV row")
            task_name = row[0].strip()
            if not task_name.casefold().startswith(CANONICAL_TASK_NAMESPACE):
                continue
            task_key = task_name.casefold()
            if task_key in {name.casefold() for name in states}:
                return SchedulerProbe.unavailable(
                    "Scheduler query contains duplicate task identity"
                )
            states[task_name] = _scheduler_state(row[2])
            if len(states) > MAX_SCHEDULER_ROWS:
                return SchedulerProbe.unavailable("Scheduler query exceeds bounded task rows")
    except csv.Error:
        return SchedulerProbe.unavailable("Scheduler query contains malformed CSV")
    return SchedulerProbe(states=states, details=_collect_scheduler_details_from_system(timeout))


def _collect_windows_service_state(name: str, timeout: float) -> ServiceRuntimeState:
    """Read one Windows service state; a missing service is a state, not a probe outage."""

    completed = subprocess.run(
        ["sc.exe", "query", name],
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
    )
    return _service_state(completed.stdout + completed.stderr, completed.returncode)


def _enumerate_repo_service_names(
    registry: OperationsRegistry, timeout: float
) -> tuple[str, ...] | str:
    """Bound discovery to declared names plus the repo-owned ``es-`` namespace."""

    declared = {service.name.casefold(): service.name for service in registry.services}
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        (
            "$ErrorActionPreference='Stop'; "
            "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
            "Get-Service -Name 'es-*' -ErrorAction Stop | "
            "ForEach-Object { $_.Name }"
        ),
    ]
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            completed = subprocess.run(
                command,
                stdout=stdout_file,
                stderr=stderr_file,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"Service probe failed: {type(exc).__name__}"
        stdout_file.seek(0, os.SEEK_END)
        stderr_file.seek(0, os.SEEK_END)
        if (
            stdout_file.tell() > MAX_SCHEDULER_OUTPUT_BYTES
            or stderr_file.tell() > MAX_SCHEDULER_OUTPUT_BYTES
        ):
            return "Service namespace enumeration exceeds bounded output"
        stdout_file.seek(0)
        try:
            enumeration_output = stdout_file.read(MAX_SCHEDULER_OUTPUT_BYTES + 1).decode(
                "utf-8-sig"
            )
        except UnicodeDecodeError:
            return "Service namespace enumeration is not valid UTF-8"
    if completed.returncode != 0:
        return (
            "Service namespace enumeration failed: "
            f"PowerShell returned status {completed.returncode}"
        )
    for line in enumeration_output.splitlines():
        name = line.strip()
        if not name:
            continue
        if re.fullmatch(r"[A-Za-z0-9_.-]+", name) is None or not name.casefold().startswith("es-"):
            return "Service namespace enumeration contains malformed service name"
        declared.setdefault(name.casefold(), name)
    if len(declared) > MAX_SERVICE_ROWS:
        return "Service namespace enumeration exceeds bounded service rows"
    return tuple(declared.values())


def collect_services_from_system(
    registry: OperationsRegistry, timeout: float = 2.0
) -> ServiceProbe:
    """Read declared Windows service states; no port, HTTP, or process inference occurs."""

    if not _is_windows_platform():
        return ServiceProbe.unavailable("Windows service manager is unavailable on this platform")

    discovered_names = _enumerate_repo_service_names(registry, timeout)
    if isinstance(discovered_names, str):
        return ServiceProbe.unavailable(discovered_names)
    states: dict[str, ServiceRuntimeState] = {}
    for name in discovered_names:
        try:
            states[name.casefold()] = _collect_windows_service_state(name, timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            # Fail closed for the whole domain: a launch or timeout failure means
            # we cannot establish a coherent service-manager snapshot. A normal
            # sc.exe nonzero response (including ERROR_SERVICE_DOES_NOT_EXIST)
            # remains an available Missing/Unknown row via _service_state.
            return ServiceProbe.unavailable(f"Service probe failed: {type(exc).__name__}")
    return ServiceProbe(states=states)


def _scheduler_probe(value: SchedulerProbe | Mapping[str, SchedulerState]) -> SchedulerProbe:
    """Accept a mapping for narrow test injection while keeping production typed."""

    return value if isinstance(value, SchedulerProbe) else SchedulerProbe(states=value)


def collect_scheduler_receipt(
    registry: OperationsRegistry,
    observed_at: datetime,
    *,
    probe: SchedulerProbe | Mapping[str, SchedulerState] | None = None,
) -> SchedulerRuntimeReceipt:
    """Bind every declared task identity to one observed Scheduler state."""

    current_probe = (
        _scheduler_probe(probe) if probe is not None else collect_scheduler_tasks_from_system()
    )
    if current_probe.states is None:
        return SchedulerRuntimeReceipt(
            probe_attempt=RuntimeProbeAttempt(
                attempted_at=observed_at,
                availability="unavailable",
                detail=current_probe.detail or "Scheduler probe unavailable",
            )
        )
    states: dict[str, tuple[str, SchedulerState]] = {
        key.casefold(): (key, state)
        for key, state in current_probe.states.items()
        if key.casefold().startswith(CANONICAL_TASK_NAMESPACE)
    }
    missing: SchedulerState = "Missing"
    declared_keys = {task.task_name.casefold() for task in registry.scheduled_tasks}
    return SchedulerRuntimeReceipt.success(
        observed_at=observed_at,
        tasks=tuple(
            SchedulerTaskReceipt(
                task_name=task.task_name,
                state=states.get(task.task_name.casefold(), (task.task_name, missing))[1],
                **_scheduler_receipt_detail(
                    current_probe.details,
                    task.task_name,
                    expected_wrapper=task.wrapper,
                ),
            )
            for task in registry.scheduled_tasks
        )
        + tuple(
            SchedulerTaskReceipt(
                task_name=task_name,
                state=state,
                **_scheduler_receipt_detail(current_probe.details, task_name),
            )
            for task_key, (task_name, state) in sorted(states.items())
            if task_key not in declared_keys
        ),
    )


def _scheduler_receipt_detail(
    details: Mapping[str, SchedulerTaskDetail] | None,
    task_name: str,
    *,
    expected_wrapper: str | None = None,
) -> _SchedulerReceiptDetails:
    if details is None or (detail := details.get(task_name.casefold())) is None:
        return {}
    if detail.last_attempted_at is None:
        attempt_state = "never_attempted"
    elif detail.state == "Running":
        attempt_state = "running"
    elif detail.last_result == 0:
        attempt_state = "succeeded"
    elif detail.last_result is None:
        attempt_state = "unknown"
    else:
        attempt_state = "failed"
    return {
        "registered_action_sha256": detail.registered_action_sha256,
        "registered_checkout_sha256": detail.registered_checkout_sha256,
        "registered_wrapper_sha256": detail.registered_wrapper_sha256,
        "wrapper_match": (
            None
            if expected_wrapper is None or detail.registered_wrapper_name is None
            else detail.registered_wrapper_name.casefold() == expected_wrapper.casefold()
        ),
        "last_attempted_at": detail.last_attempted_at,
        "last_successful_at": (detail.last_attempted_at if attempt_state == "succeeded" else None),
        "next_expected_at": detail.next_expected_at,
        "last_result": detail.last_result,
        "attempt_state": attempt_state,
    }


def collect_service_receipt(
    registry: OperationsRegistry,
    observed_at: datetime,
    *,
    probe: ServiceProbe | None = None,
) -> ServiceRuntimeReceipt:
    """Bind every declared service identity to its direct service-manager observation."""

    current_probe = probe if probe is not None else collect_services_from_system(registry)
    if current_probe.states is None:
        return ServiceRuntimeReceipt(
            probe_attempt=RuntimeProbeAttempt(
                attempted_at=observed_at,
                availability="unavailable",
                detail=current_probe.detail or "Managed-service probe unavailable",
            )
        )
    states: dict[str, tuple[str, ServiceRuntimeState]] = {
        key.casefold(): (key, state) for key, state in current_probe.states.items()
    }
    missing: ServiceRuntimeState = "Missing"
    declared_keys = {service.name.casefold() for service in registry.services}
    rows = tuple(
        ServiceReceiptRow(
            name=service.name,
            state=states.get(service.name.casefold(), (service.name, missing))[1],
        )
        for service in registry.services
    ) + tuple(
        ServiceReceiptRow(name=name, state=state)
        for key, (name, state) in sorted(states.items())
        if key not in declared_keys
    )
    return ServiceRuntimeReceipt.success(observed_at=observed_at, services=rows)


def write_atomic_receipt(target_path: Path, payload_json: str) -> None:
    """Replace one receipt atomically, retaining the old evidence on a failed write."""

    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = target_path.with_name(f".{target_path.name}.{os.getpid()}.tmp")
    try:
        temporary_path.write_text(payload_json, encoding="utf-8")
        temporary_path.replace(target_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _bounded_runtime_payload(path: Path) -> bytes:
    with path.open("rb") as handle:
        payload = handle.read(MAX_SCHEDULER_OUTPUT_BYTES + 1)
    if len(payload) > MAX_SCHEDULER_OUTPUT_BYTES:
        raise ValueError("runtime receipt exceeds bounded output")
    return payload


def _retained_scheduler_success(path: Path) -> _RetainedSchedulerRead:
    try:
        payload = _bounded_runtime_payload(path)
    except FileNotFoundError:
        return _RetainedSchedulerRead(None, "absent", "receipt file is absent")
    except OSError as exc:
        return _RetainedSchedulerRead(None, "rejected", type(exc).__name__)
    try:
        try:
            receipt = SchedulerRuntimeReceipt.model_validate_json(payload).last_successful
        except ValueError:
            receipt = SchedulerReceipt.model_validate_json(payload)
    except ValueError as exc:
        return _RetainedSchedulerRead(None, "rejected", type(exc).__name__)
    if receipt is None:
        return _RetainedSchedulerRead(None, "absent", "no last successful evidence")
    return _RetainedSchedulerRead(receipt, "available")


def retain_scheduler_task_successes(
    current: SchedulerRuntimeReceipt, path: Path
) -> SchedulerRuntimeReceipt:
    """Carry forward a task's last success only for the same registered action."""

    if current.last_successful is None:
        return current
    retained = _retained_scheduler_success(path).receipt
    if retained is None:
        return current
    prior_by_name = {row.task_name.casefold(): row for row in retained.tasks}
    tasks: list[SchedulerTaskReceipt] = []
    for row in current.last_successful.tasks:
        prior = prior_by_name.get(row.task_name.casefold())
        can_retain = (
            row.last_successful_at is None
            and row.registered_action_sha256 is not None
            and prior is not None
            and prior.registered_action_sha256 == row.registered_action_sha256
            and prior.last_successful_at is not None
        )
        tasks.append(
            row.model_copy(update={"last_successful_at": prior.last_successful_at})
            if can_retain and prior is not None
            else row
        )
    return SchedulerRuntimeReceipt(
        probe_attempt=current.probe_attempt,
        last_successful=SchedulerReceipt(
            observed_at=current.last_successful.observed_at,
            tasks=tuple(tasks),
        ),
    )


def _retained_service_success(path: Path) -> _RetainedServiceRead:
    try:
        payload = _bounded_runtime_payload(path)
    except FileNotFoundError:
        return _RetainedServiceRead(None, "absent", "receipt file is absent")
    except OSError as exc:
        return _RetainedServiceRead(None, "rejected", type(exc).__name__)
    try:
        try:
            receipt = ServiceRuntimeReceipt.model_validate_json(payload).last_successful
        except ValueError:
            receipt = ServiceReceipt.model_validate_json(payload)
    except ValueError as exc:
        return _RetainedServiceRead(None, "rejected", type(exc).__name__)
    if receipt is None:
        return _RetainedServiceRead(None, "absent", "no last successful evidence")
    return _RetainedServiceRead(receipt, "available")


def _retain_scheduler_success(
    probe: SchedulerRuntimeReceipt, path: Path
) -> SchedulerRuntimeReceipt:
    retained_read = _retained_scheduler_success(path)
    probe_reason = _safe_reason(probe.probe_attempt.detail, "Scheduler probe unavailable")
    if retained_read.status == "absent":
        log_event("retained_absent", domain="scheduler", probe_reason=probe_reason)
        return probe
    if retained_read.status == "rejected" or retained_read.receipt is None:
        log_event(
            "retained_rejected",
            domain="scheduler",
            probe_reason=probe_reason,
            retained_reason=_safe_reason(retained_read.reason, "receipt rejected"),
        )
        return probe
    try:
        result = SchedulerRuntimeReceipt(
            probe_attempt=probe.probe_attempt,
            last_successful=retained_read.receipt,
        )
    except ValidationError:
        log_event(
            "retained_rejected",
            domain="scheduler",
            probe_reason=probe_reason,
            retained_reason="retained evidence failed temporal validation",
        )
        return probe
    log_event(
        "retained_revalidated",
        domain="scheduler",
        probe_reason=probe_reason,
        retained_observed_at=retained_read.receipt.observed_at.isoformat(),
    )
    return result


def _retain_service_success(probe: ServiceRuntimeReceipt, path: Path) -> ServiceRuntimeReceipt:
    retained_read = _retained_service_success(path)
    probe_reason = _safe_reason(probe.probe_attempt.detail, "Managed-service probe unavailable")
    if retained_read.status == "absent":
        log_event("retained_absent", domain="service", probe_reason=probe_reason)
        return probe
    if retained_read.status == "rejected" or retained_read.receipt is None:
        log_event(
            "retained_rejected",
            domain="service",
            probe_reason=probe_reason,
            retained_reason=_safe_reason(retained_read.reason, "receipt rejected"),
        )
        return probe
    try:
        result = ServiceRuntimeReceipt(
            probe_attempt=probe.probe_attempt,
            last_successful=retained_read.receipt,
        )
    except ValidationError:
        log_event(
            "retained_rejected",
            domain="service",
            probe_reason=probe_reason,
            retained_reason="retained evidence failed temporal validation",
        )
        return probe
    log_event(
        "retained_revalidated",
        domain="service",
        probe_reason=probe_reason,
        retained_observed_at=retained_read.receipt.observed_at.isoformat(),
    )
    return result


def _existing_probe_attempt(
    path: Path, domain: Literal["scheduler", "service"]
) -> RuntimeProbeAttempt | None:
    """Read a v2 attempt or synthesize one from a legacy v1 receipt timestamp."""

    try:
        payload = _bounded_runtime_payload(path)
        if path.name == RUNTIME_PAIR_RECEIPT_FILENAME:
            pair = RuntimeReceiptPair.model_validate_json(payload)
            return (
                pair.scheduler.probe_attempt
                if domain == "scheduler"
                else pair.services.probe_attempt
            )
        if domain == "scheduler":
            try:
                return SchedulerRuntimeReceipt.model_validate_json(payload).probe_attempt
            except ValueError:
                observed_at = SchedulerReceipt.model_validate_json(payload).observed_at
        else:
            try:
                return ServiceRuntimeReceipt.model_validate_json(payload).probe_attempt
            except ValueError:
                observed_at = ServiceReceipt.model_validate_json(payload).observed_at
        return RuntimeProbeAttempt(attempted_at=observed_at, availability="available")
    except (FileNotFoundError, OSError, ValueError):
        return None


def _receipt_write_allowed(
    target_path: Path,
    *,
    domain: Literal["scheduler", "service"],
    probe: SchedulerRuntimeReceipt | ServiceRuntimeReceipt,
    canonical_path: Path | None = None,
) -> bool:
    """Allow only a strictly newer probe attempt to enter pair publication."""

    current_attempts = tuple(
        attempt
        for path in (target_path, canonical_path)
        if path is not None
        for attempt in (_existing_probe_attempt(path, domain),)
        if attempt is not None
    )
    current_attempt = max(
        current_attempts,
        key=lambda attempt: attempt.attempted_at,
        default=None,
    )
    if (
        current_attempt is not None
        and current_attempt.attempted_at >= probe.probe_attempt.attempted_at
    ):
        log_event(
            "skipped_older",
            domain=domain,
            probe_reason=_safe_reason(
                probe.probe_attempt.detail,
                f"{domain} probe result is not newer than the retained receipt",
            ),
            retained_attempted_at=current_attempt.attempted_at.isoformat(),
        )
        return False
    return True


def _unavailable_scheduler_receipt(observed_at: datetime, detail: str) -> SchedulerRuntimeReceipt:
    return SchedulerRuntimeReceipt(
        probe_attempt=RuntimeProbeAttempt(
            attempted_at=observed_at,
            availability="unavailable",
            detail=detail,
        )
    )


def _unavailable_service_receipt(observed_at: datetime, detail: str) -> ServiceRuntimeReceipt:
    return ServiceRuntimeReceipt(
        probe_attempt=RuntimeProbeAttempt(
            attempted_at=observed_at,
            availability="unavailable",
            detail=detail,
        )
    )


def _pair_journal_path(scheduler_path: Path) -> Path:
    return scheduler_path.with_name(".operations-runtime-receipts.pair.journal.json")


def _pair_committed_marker_path(journal_path: Path) -> Path:
    return journal_path.with_name(PAIR_COMMITTED_MARKER_FILENAME)


def _pair_receipt_path(scheduler_path: Path) -> Path:
    return scheduler_path.with_name(RUNTIME_PAIR_RECEIPT_FILENAME)


def _stage_payload(target_path: Path, payload_json: str, token: str) -> Path:
    staged = target_path.with_name(f".{target_path.name}.pair-{token}.tmp")
    descriptor = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload_json)
            handle.flush()
            os.fsync(handle.fileno())
        return staged
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def _validate_staged_payload(
    payload_json: str, domain: Literal["scheduler", "service", "pair"]
) -> None:
    if domain == "scheduler":
        SchedulerRuntimeReceipt.model_validate_json(payload_json)
    elif domain == "service":
        ServiceRuntimeReceipt.model_validate_json(payload_json)
    else:
        RuntimeReceiptPair.model_validate_json(payload_json)


def commit_staged_receipt(staged: Path, target: Path) -> None:
    os.replace(staged, target)


def _write_pair_journal(path: Path, payload: Mapping[str, object]) -> None:
    write_atomic_receipt(path, json.dumps(payload, sort_keys=True))


def _pair_entries(path: Path) -> list[tuple[Path, Path, Path, bool]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("pair journal is not an object")
    journal = cast(dict[str, object], payload)
    token = journal.get("token")
    if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{32}", token) is None:
        raise ValueError("pair journal token is invalid")
    receipt_dir = path.parent.resolve()
    entries: list[tuple[Path, Path, Path, bool]] = []
    for stem, target_key, stage_key, backup_key, had_key in (
        (
            "scheduler.latest.json",
            "scheduler_target",
            "scheduler_stage",
            "scheduler_backup",
            "scheduler_had_target",
        ),
        (
            "services.latest.json",
            "service_target",
            "service_stage",
            "service_backup",
            "service_had_target",
        ),
        (
            RUNTIME_PAIR_RECEIPT_FILENAME,
            "pair_target",
            "pair_stage",
            "pair_backup",
            "pair_had_target",
        ),
    ):
        target_name = journal.get(target_key)
        stage_name = journal.get(stage_key)
        backup_name = journal.get(backup_key)
        had_target = journal.get(had_key)
        stage_expected = receipt_dir / f".{stem}.pair-{token}.tmp"
        backup_expected = receipt_dir / f".{stem}.pair-backup-{token}"
        if (
            not isinstance(target_name, str)
            or not isinstance(stage_name, str)
            or not isinstance(backup_name, str)
            or not isinstance(had_target, bool)
            or Path(target_name).resolve() != (receipt_dir / stem).resolve()
            or Path(stage_name).resolve() != stage_expected.resolve()
            or Path(backup_name).resolve() != backup_expected.resolve()
        ):
            raise ValueError("pair journal paths are not canonical")
        entries.append((receipt_dir / stem, stage_expected, backup_expected, had_target))
    return entries


def _cleanup_committed_marker(marker: Path) -> None:
    entries = _pair_entries(marker)
    for _target, stage, _backup, _had_target in entries:
        stage.unlink(missing_ok=True)
    for _target, _stage, backup, _had_target in entries:
        backup.unlink(missing_ok=True)
    marker.unlink(missing_ok=True)


def _recover_pair_journal(path: Path) -> None:
    """Roll back an interrupted pair commit before admitting a new one."""

    marker = _pair_committed_marker_path(path)
    try:
        if marker.exists():
            _cleanup_committed_marker(marker)
        if not path.exists():
            return
        entries = _pair_entries(path)
        # Validate every required backup before mutating any target.  A partial
        # rollback must remain recoverable by a later invocation.
        for _target, _stage, backup, had_target in entries:
            if had_target and (not backup.exists() or not backup.is_file()):
                raise PairRecoveryError("required pair rollback backup is missing")
        for target, _stage, backup, had_target in entries:
            if had_target:
                shutil.copyfile(backup, target)
                if not target.is_file() or target.read_bytes() != backup.read_bytes():
                    raise PairRecoveryError("pair rollback target verification failed")
            else:
                target.unlink(missing_ok=True)
                if target.exists():
                    raise PairRecoveryError("pair rollback target removal failed")
        # All canonical targets are restored and verified.  Move the journal
        # before cleanup so a later failure cannot leave rollback metadata that
        # references a backup already removed by an earlier cleanup step.
        os.replace(path, marker)
        for _target, stage, _backup, _had_target in entries:
            stage.unlink(missing_ok=True)
        for _target, _stage, backup, _had_target in entries:
            backup.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
    except PairRecoveryError:
        raise
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise PairRecoveryError(
            f"runtime receipt pair recovery failed: {type(exc).__name__}"
        ) from exc


def _publish_receipt_pair(
    scheduler_path: Path,
    scheduler_payload: str,
    service_path: Path,
    service_payload: str,
) -> tuple[bool, str | None, Literal["scheduler", "service", "pair", "rollback"]]:
    """Stage, validate, and commit both receipts with rollback on either failure."""

    journal = _pair_journal_path(scheduler_path)
    pair_path = _pair_receipt_path(scheduler_path)
    staged_paths: list[Path] = []
    backups: list[tuple[Path, Path, bool, bool]] = []
    token = uuid4().hex
    failed_domain: Literal["scheduler", "service", "pair"] = "pair"
    journal_written = False
    commits_started = False
    try:
        _recover_pair_journal(journal)
        _validate_staged_payload(scheduler_payload, "scheduler")
        _validate_staged_payload(service_payload, "service")
        pair_payload = RuntimeReceiptPair(
            generation=token,
            scheduler=SchedulerRuntimeReceipt.model_validate_json(scheduler_payload),
            services=ServiceRuntimeReceipt.model_validate_json(service_payload),
        ).model_dump_json(indent=2)
        _validate_staged_payload(pair_payload, "pair")
        scheduler_path.parent.mkdir(parents=True, exist_ok=True)
        service_path.parent.mkdir(parents=True, exist_ok=True)
        scheduler_stage = _stage_payload(scheduler_path, scheduler_payload, token)
        staged_paths.append(scheduler_stage)
        service_stage = _stage_payload(service_path, service_payload, token)
        staged_paths.append(service_stage)
        pair_stage = _stage_payload(pair_path, pair_payload, token)
        staged_paths.append(pair_stage)
        targets = (scheduler_path, service_path, pair_path)
        for target in targets:
            backup = target.with_name(f".{target.name}.pair-backup-{token}")
            existed = target.exists()
            backup_created = False
            if existed:
                shutil.copyfile(target, backup)
                backup_created = True
            backups.append((target, backup, existed, backup_created))
        journal_payload: dict[str, object] = {
            "token": token,
            "scheduler_target": str(scheduler_path),
            "service_target": str(service_path),
            "pair_target": str(pair_path),
            "scheduler_stage": str(scheduler_stage),
            "service_stage": str(service_stage),
            "pair_stage": str(pair_stage),
            "scheduler_backup": str(backups[0][1]),
            "service_backup": str(backups[1][1]),
            "pair_backup": str(backups[2][1]),
            "scheduler_had_target": backups[0][2],
            "service_had_target": backups[1][2],
            "pair_had_target": backups[2][2],
        }
        _write_pair_journal(journal, journal_payload)
        journal_written = True
        commits_started = True
        failed_domain = "pair"
        commit_staged_receipt(pair_stage, pair_path)
        failed_domain = "scheduler"
        commit_staged_receipt(scheduler_stage, scheduler_path)
        failed_domain = "service"
        commit_staged_receipt(service_stage, service_path)
        # The commit is now observable and canonical.  Move the rollback
        # journal out of the recovery namespace before deleting any backups;
        # a cleanup failure must never leave a journal that asks recovery to
        # restore a backup already deleted by an earlier cleanup step.
        committed_marker = _pair_committed_marker_path(journal)
        os.replace(journal, committed_marker)
        journal_written = False
        cleanup_errors: list[OSError] = []
        for stage in staged_paths:
            try:
                stage.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                cleanup_errors.append(cleanup_exc)
        for _target, backup, _existed, _created in backups:
            try:
                backup.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                cleanup_errors.append(cleanup_exc)
        if cleanup_errors:
            log_event(
                "runtime_receipt_cleanup_pending",
                domain="pair",
                detail=_safe_reason(type(cleanup_errors[0]).__name__, "cleanup pending"),
            )
            # The committed marker is safe to replay: it only removes stale
            # stage/backup artifacts and never rolls back canonical receipts.
            return True, None, "pair"
        try:
            committed_marker.unlink(missing_ok=True)
        except OSError as cleanup_exc:
            log_event(
                "runtime_receipt_cleanup_pending",
                domain="pair",
                detail=_safe_reason(type(cleanup_exc).__name__, "cleanup pending"),
            )
        return True, None, "pair"
    except (OSError, TypeError, ValueError) as exc:
        rollback_error: OSError | None = None
        if commits_started and len(backups) == 3:
            try:
                for _target, backup, existed, _created in backups:
                    if existed and (not backup.exists() or not backup.is_file()):
                        raise OSError("required pair rollback backup is missing")
                for target, backup, existed, _created in backups:
                    if existed:
                        shutil.copyfile(backup, target)
                        if not target.is_file() or target.read_bytes() != backup.read_bytes():
                            raise OSError("pair rollback target verification failed")
                    else:
                        target.unlink(missing_ok=True)
                        if target.exists():
                            raise OSError("pair rollback target removal failed")
            except OSError as rollback_exc:
                rollback_error = rollback_exc
        cleanup_error: OSError | None = None
        cleanup_marker: Path | None = None
        if rollback_error is None:
            if journal_written:
                try:
                    cleanup_marker = _pair_committed_marker_path(journal)
                    os.replace(journal, cleanup_marker)
                    journal_written = False
                except OSError as marker_exc:
                    cleanup_error = marker_exc
            for stage in staged_paths:
                if cleanup_error is None:
                    try:
                        stage.unlink(missing_ok=True)
                    except OSError as cleanup_exc:
                        cleanup_error = cleanup_error or cleanup_exc
            for _target, backup, _existed, backup_created in backups:
                if cleanup_error is None and backup_created:
                    try:
                        backup.unlink(missing_ok=True)
                    except OSError as cleanup_exc:
                        cleanup_error = cleanup_error or cleanup_exc
        if rollback_error is not None or cleanup_error is not None:
            reason = rollback_error or cleanup_error
            assert reason is not None
            return (
                False,
                f"{type(exc).__name__}; rollback {type(reason).__name__}",
                "rollback",
            )
        if cleanup_marker is not None:
            try:
                cleanup_marker.unlink(missing_ok=True)
            except OSError as marker_exc:
                return (
                    False,
                    f"{type(exc).__name__}; rollback {type(marker_exc).__name__}",
                    "rollback",
                )
        if journal_written:
            try:
                journal.unlink(missing_ok=True)
            except OSError as journal_exc:
                return (
                    False,
                    f"{type(exc).__name__}; rollback {type(journal_exc).__name__}",
                    "rollback",
                )
        failure_detail = str(exc) if isinstance(exc, PairRecoveryError) else type(exc).__name__
        return False, failure_detail, failed_domain


def emit_runtime_receipts(
    registry: OperationsRegistry,
    repo_root: Path,
    observed_at: datetime,
    *,
    scheduler_probe: SchedulerProbe | Mapping[str, SchedulerState] | None = None,
    service_probe: ServiceProbe | None = None,
    lock_wait_s: float | None = None,
) -> tuple[SchedulerRuntimeReceipt, ServiceRuntimeReceipt, bool]:
    """Probe and publish both receipts as one cross-process single-writer transaction."""

    try:
        lock = (
            nullcontext()
            if inherited_lock_is_valid(repo_root, RUNTIME_RECEIPT_WRITE_SET)
            else JobLock(
                repo_root,
                "operations-runtime-receipts",
                [RUNTIME_RECEIPT_WRITE_SET],
                wait_s=lock_wait_s,
            )
        )
        with lock:
            scheduler = collect_scheduler_receipt(registry, observed_at, probe=scheduler_probe)
            services = collect_service_receipt(registry, observed_at, probe=service_probe)
            scheduler_path = scheduler_receipt_path(repo_root)
            service_path = service_receipt_path(repo_root)
            if scheduler.probe_attempt.availability == "unavailable":
                scheduler = _retain_scheduler_success(scheduler, scheduler_path)
            else:
                scheduler = retain_scheduler_task_successes(scheduler, scheduler_path)
            if services.probe_attempt.availability == "unavailable":
                services = _retain_service_success(services, service_path)
            pair_path = _pair_receipt_path(scheduler_path)
            scheduler_allowed = _receipt_write_allowed(
                scheduler_path,
                domain="scheduler",
                probe=scheduler,
                canonical_path=pair_path,
            )
            service_allowed = _receipt_write_allowed(
                service_path,
                domain="service",
                probe=services,
                canonical_path=pair_path,
            )
            if scheduler_allowed and service_allowed:
                published, failure, failure_domain = _publish_receipt_pair(
                    scheduler_path,
                    scheduler.model_dump_json(indent=2),
                    service_path,
                    services.model_dump_json(indent=2),
                )
                if not published:
                    detail = _safe_reason(failure, "runtime receipt pair publication failed")
                    log_event(
                        "runtime_receipt_write_failed",
                        domain=failure_domain,
                        detail=detail,
                    )
                    return (
                        _unavailable_scheduler_receipt(observed_at, detail),
                        _unavailable_service_receipt(observed_at, detail),
                        False,
                    )
            return scheduler, services, True
    except (JobAlreadyRunningError, OSError) as exc:
        detail = _safe_reason(type(exc).__name__, "runtime receipt lock unavailable")
        log_event("runtime_receipt_lock_unavailable", detail=detail)
        return (
            _unavailable_scheduler_receipt(observed_at, detail),
            _unavailable_service_receipt(observed_at, detail),
            False,
        )


def build_runtime_summary(
    scheduler: SchedulerRuntimeReceipt,
    services: ServiceRuntimeReceipt,
    observed_at: datetime,
    registry: OperationsRegistry,
) -> RuntimeCollectionSummary:
    scheduler_evidence = scheduler.last_successful
    service_evidence = services.last_successful
    configured_task = next(
        (
            task
            for task in registry.scheduled_tasks
            if task.task_name == RECURRING_COLLECTOR_TASK_NAME
        ),
        None,
    )
    declared_enabled = (
        configured_task is not None and configured_task.scheduler_expectation == "required_enabled"
    )
    current_scheduler = scheduler.probe_attempt.availability == "available"
    current_receipt = scheduler.last_successful if current_scheduler else None
    observed_task = (
        next(
            (
                task
                for task in current_receipt.tasks
                if configured_task is not None and task.task_name == configured_task.task_name
            ),
            None,
        )
        if current_receipt is not None
        else None
    )
    scheduler_state = observed_task.state if observed_task is not None else None
    activated = declared_enabled and current_scheduler and scheduler_state in {"Ready", "Running"}
    if configured_task is None:
        recurring_detail = "Recurring collector task is not declared in the canonical registry."
    elif not declared_enabled:
        recurring_detail = "Recurring collector task is not declared enabled."
    elif not current_scheduler:
        recurring_detail = "Current Scheduler probe is unavailable."
    elif scheduler_state is None:
        recurring_detail = "Current Scheduler evidence has no matching collector task."
    elif activated:
        recurring_detail = "Current Scheduler evidence reports the declared collector task active."
    else:
        recurring_detail = f"Current Scheduler evidence reports collector state {scheduler_state}."
    return RuntimeCollectionSummary.model_validate(
        {
            "status": "observed",
            "observed_at": observed_at,
            "scheduler": {
                "state": (
                    "current"
                    if scheduler.probe_attempt.availability == "available"
                    else "unavailable"
                ),
                "counts": (
                    {
                        label: sum(row.state == label for row in scheduler_evidence.tasks)
                        for label in ("Ready", "Running", "Disabled", "Unknown", "Missing")
                    }
                    if scheduler.probe_attempt.availability == "available"
                    and scheduler_evidence is not None
                    else {}
                ),
            },
            "services": {
                "state": (
                    "current"
                    if services.probe_attempt.availability == "available"
                    else "unavailable"
                ),
                "counts": (
                    {
                        label: sum(row.state == label for row in service_evidence.services)
                        for label in ("Running", "Stopped", "Paused", "Unknown", "Missing")
                    }
                    if services.probe_attempt.availability == "available"
                    and service_evidence is not None
                    else {}
                ),
            },
            "recurring_collection": {
                "task_name": (
                    configured_task.task_name
                    if configured_task is not None
                    else RECURRING_COLLECTOR_TASK_NAME
                ),
                "state": "activated" if activated else "activation_required",
                "configuration_state": (
                    "declared_enabled" if declared_enabled else "not_declared_enabled"
                ),
                "scheduler_observation": "current" if current_scheduler else "unavailable",
                "scheduler_state": scheduler_state,
                "detail": recurring_detail,
            },
        }
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--emit-receipts",
        action="store_true",
        help="atomically replace only receipts backed by an available probe",
    )
    parser.add_argument("--json-out", action="store_true", help="emit a structured summary")
    arguments = parser.parse_args(argv)

    root = arguments.repo_root.resolve()
    registry = build_operations_registry(root)
    observed_at = datetime.now(UTC)
    lock_ok = True
    if arguments.emit_receipts:
        scheduler, services, lock_ok = emit_runtime_receipts(registry, root, observed_at)
    else:
        scheduler = collect_scheduler_receipt(registry, observed_at)
        services = collect_service_receipt(registry, observed_at)

    summary = build_runtime_summary(scheduler, services, observed_at, registry)
    if arguments.json_out or not arguments.emit_receipts:
        sys.stdout.write(summary.model_dump_json() + "\n")
    return 0 if lock_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
