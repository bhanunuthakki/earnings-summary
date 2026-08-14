"""Typed append-only operation requests and lifecycle events.

The durable schema stores only closed labels, canonical bounded JSON, hashes,
and redacted terminal detail. It has no columns for commands, argv,
environments, outputs, prompts, responses, URLs, or arbitrary payloads.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TypeAlias, cast

from log_redact import redact

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_LABEL = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_OPERATION_ID = re.compile(r"operation:[0-9a-f]{64}\Z")
_EVENT_ID = re.compile(r"operation-event:[0-9a-f]{64}\Z")
_MAX_PAGE_SIZE = 500
_MAX_SCOPE_KEYS = 32
_MAX_DETAIL_REASON = 240
_UNSAFE_LABEL_FRAGMENTS = (
    "argv",
    "env",
    "prompt",
    "response",
    "stdout",
    "stderr",
    "payload",
    "secret",
    "token",
    "credential",
    "apikey",
    "api_key",
)

ScopeScalar: TypeAlias = str | int | bool | None


class TriggerKind(StrEnum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"
    SERVICE = "service"


class JobHealthStatus(StrEnum):
    OK = "ok"
    DEGRADED_CORPUS = "degraded_corpus"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED_LOCKED = "skipped_locked"
    BLOCKED_SCHEMA_DRIFT = "blocked_schema_drift"


class OperationConflictError(RuntimeError):
    """An idempotency identity was reused for a different canonical request."""


@dataclass(frozen=True, slots=True)
class OperationRequestInput:
    idempotency_key: str
    actor: str
    job_name: str
    trigger_kind: TriggerKind | str
    trace_id: str
    stage: str
    scope: Mapping[str, ScopeScalar]
    command_sha256: str
    write_sets: tuple[str, ...]
    requested_at: datetime


@dataclass(frozen=True, slots=True)
class OperationRequest:
    operation_id: str
    request_sha256: str
    actor: str
    job_name: str
    trigger_kind: TriggerKind
    trace_id: str
    stage: str
    scope_json: str
    command_sha256: str
    write_sets: tuple[str, ...]
    requested_at: datetime


@dataclass(frozen=True, slots=True)
class OperationEvent:
    event_id: str
    operation_id: str
    event_kind: str
    event_sha256: str
    occurred_at: datetime
    status: JobHealthStatus | None
    exit_code: int | None
    severity: str | None
    detail_code: str | None
    detail_reason: str | None


@dataclass(frozen=True, slots=True)
class OperationListRow:
    operation_id: str
    actor: str
    job_name: str
    trigger_kind: TriggerKind
    trace_id: str
    stage: str
    write_sets: tuple[str, ...]
    requested_at: datetime
    terminal_status: JobHealthStatus | None
    terminal_exit_code: int | None
    terminal_severity: str | None
    terminal_at: datetime | None


@dataclass(frozen=True, slots=True)
class OperationCursor:
    requested_at: datetime
    operation_id: str

    def __post_init__(self) -> None:
        _require_aware(self.requested_at, "requested_at")
        _require_operation_id(self.operation_id)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def make_operation_id(idempotency_key: str) -> str:
    """Derive an opaque stable ID without persisting the caller's raw key."""

    normalized = idempotency_key.strip()
    if not normalized or len(normalized) > 2048:
        raise ValueError("idempotency_key must contain 1 to 2048 characters")
    return f"operation:{_digest(normalized)}"


def make_command_sha256(command: tuple[str, ...] | list[str]) -> str:
    """Hash the exact argv vector without persisting any argument text."""

    if not command or any(not arg for arg in command):
        raise ValueError("command must contain non-empty string arguments")
    canonical = json.dumps(command, ensure_ascii=False, separators=(",", ":"))
    return _digest(canonical)


def _require_sha256(value: str, name: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def _require_label(value: str, name: str) -> str:
    normalized = value.strip()
    if _LABEL.fullmatch(normalized) is None:
        raise ValueError(f"{name} contains unsupported characters")
    lowered = normalized.casefold()
    if any(fragment in lowered for fragment in _UNSAFE_LABEL_FRAGMENTS):
        raise ValueError(f"{name} contains an unsafe label fragment")
    return normalized


def _require_operation_id(value: str) -> str:
    if _OPERATION_ID.fullmatch(value) is None:
        raise ValueError("operation_id must be a canonical operation identifier")
    return value


def _require_aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _to_iso(value: datetime) -> str:
    return _require_aware(value, "occurred_at").astimezone(UTC).isoformat(timespec="microseconds")


def _from_iso(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    return _require_aware(parsed, "persisted timestamp")


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("persisted exit_code must be an integer or null")
    return value


def _canonical_scope(scope: Mapping[str, ScopeScalar]) -> str:
    if len(scope) > _MAX_SCOPE_KEYS:
        raise ValueError(f"scope cannot exceed {_MAX_SCOPE_KEYS} keys")
    canonical: dict[str, ScopeScalar] = {}
    for raw_key, value in scope.items():
        key = _require_label(raw_key, "scope key")
        raw_value = cast(object, value)
        if isinstance(raw_value, str):
            canonical[key] = _require_label(raw_value, f"scope value {key}")
        elif raw_value is None or isinstance(raw_value, (int, bool)):
            canonical[key] = raw_value
        else:
            raise ValueError(f"scope value {key} has unsupported type")
    return json.dumps(canonical, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _canonical_write_sets(write_sets: tuple[str, ...]) -> tuple[tuple[str, ...], str]:
    normalized = tuple(sorted({_require_label(value, "write_set") for value in write_sets}))
    if not normalized:
        raise ValueError("write_sets must not be empty")
    return normalized, json.dumps(normalized, separators=(",", ":"))


def _canonical_request(
    request: OperationRequestInput,
) -> tuple[str, str, str, str, TriggerKind, str, str, str, tuple[str, ...], str]:
    actor = _require_label(request.actor, "actor")
    job = _require_label(request.job_name, "job_name")
    trigger = TriggerKind(request.trigger_kind)
    trace = _require_label(request.trace_id, "trace_id")
    stage = _require_label(request.stage, "stage")
    scope_json = _canonical_scope(request.scope)
    command_sha = _require_sha256(request.command_sha256, "command_sha256")
    write_sets, write_sets_json = _canonical_write_sets(request.write_sets)
    canonical = json.dumps(
        {
            "actor": actor,
            "command_sha256": command_sha,
            "job_name": job,
            "scope": json.loads(scope_json),
            "stage": stage,
            "trace_id": trace,
            "trigger_kind": trigger.value,
            "write_sets": write_sets,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        _digest(canonical),
        actor,
        job,
        trace,
        trigger,
        stage,
        scope_json,
        command_sha,
        write_sets,
        write_sets_json,
    )


def _event_identity(operation_id: str, event_kind: str) -> str:
    return f"operation-event:{_digest(chr(10).join((operation_id, event_kind)))}"


def _event_hash(
    operation_id: str,
    event_kind: str,
    status: JobHealthStatus | None,
    exit_code: int | None,
    severity: str | None,
    detail_code: str | None,
    detail_reason: str | None,
) -> str:
    return _digest(
        "\n".join(
            (
                "operation-event/v2",
                operation_id,
                event_kind,
                status.value if status is not None else "",
                "" if exit_code is None else str(exit_code),
                severity or "",
                detail_code or "",
                detail_reason or "",
            )
        )
    )


def _scope_from_json(value: str) -> dict[str, ScopeScalar]:
    raw: object = json.loads(value)
    if not isinstance(raw, dict):
        raise ValueError("persisted scope_json is invalid")
    output: dict[str, ScopeScalar] = {}
    for key, item in cast("dict[object, object]", raw).items():
        if not isinstance(key, str) or not (item is None or isinstance(item, (str, int, bool))):
            raise ValueError("persisted scope_json is invalid")
        output[key] = item
    return output


def _write_sets_from_json(value: str) -> tuple[str, ...]:
    raw: object = json.loads(value)
    if not isinstance(raw, list):
        raise ValueError("persisted write_sets_json is invalid")
    output: list[str] = []
    for item in cast("list[object]", raw):
        if not isinstance(item, str):
            raise ValueError("persisted write_sets_json is invalid")
        output.append(item)
    return tuple(output)


def _request_from_row(row: sqlite3.Row | tuple[object, ...]) -> OperationRequest:
    operation_id = _require_operation_id(str(row[0]))
    _require_sha256(str(row[1]), "persisted idempotency_key_sha256")
    request_sha = _require_sha256(str(row[2]), "persisted request_sha256")
    actor = _require_label(str(row[3]), "persisted actor")
    job_name = _require_label(str(row[4]), "persisted job_name")
    trigger = TriggerKind(str(row[5]))
    trace_id = _require_label(str(row[6]), "persisted trace_id")
    stage = _require_label(str(row[7]), "persisted stage")
    scope_json = str(row[8])
    if _canonical_scope(_scope_from_json(scope_json)) != scope_json:
        raise ValueError("persisted scope_json is not canonical")
    command_sha = _require_sha256(str(row[9]), "persisted command_sha256")
    write_sets, canonical_sets = _canonical_write_sets(_write_sets_from_json(str(row[10])))
    if canonical_sets != str(row[10]):
        raise ValueError("persisted write_sets_json is not canonical")
    return OperationRequest(
        operation_id=operation_id,
        request_sha256=request_sha,
        actor=actor,
        job_name=job_name,
        trigger_kind=trigger,
        trace_id=trace_id,
        stage=stage,
        scope_json=scope_json,
        command_sha256=command_sha,
        write_sets=write_sets,
        requested_at=_from_iso(row[11]),
    )


_REQUEST_SELECT = (
    "operation_id,idempotency_key_sha256,request_sha256,actor,job_name,trigger_kind,"
    "trace_id,stage,scope_json,command_sha256,write_sets_json,requested_at"
)


def accept_operation_request(
    conn: sqlite3.Connection, request: OperationRequestInput
) -> OperationRequest:
    """Accept one canonical request, or return an exact idempotent replay."""

    operation_id = make_operation_id(request.idempotency_key)
    idempotency_key_sha256 = _digest(request.idempotency_key.strip())
    (
        request_sha,
        actor,
        job,
        trace,
        trigger,
        stage,
        scope_json,
        command_sha,
        _write_sets,
        write_sets_json,
    ) = _canonical_request(request)
    stamp = _to_iso(request.requested_at)
    try:
        with conn:
            conn.execute(
                "INSERT INTO operation_requests "
                "(operation_id,idempotency_key_sha256,request_sha256,actor,job_name,"
                "trigger_kind,trace_id,stage,scope_json,command_sha256,write_sets_json,"
                "requested_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    operation_id,
                    idempotency_key_sha256,
                    request_sha,
                    actor,
                    job,
                    trigger.value,
                    trace,
                    stage,
                    scope_json,
                    command_sha,
                    write_sets_json,
                    stamp,
                ),
            )
    except sqlite3.IntegrityError:
        row = conn.execute(
            f"SELECT {_REQUEST_SELECT} FROM operation_requests WHERE operation_id=?",  # nosec B608 -- internal constant projection
            (operation_id,),
        ).fetchone()
        if row is None:
            raise
        existing = _request_from_row(row)
        if existing.request_sha256 != request_sha:
            raise OperationConflictError("idempotency canonical request hash conflict") from None
        return existing
    row = conn.execute(
        f"SELECT {_REQUEST_SELECT} FROM operation_requests WHERE operation_id=?",  # nosec B608 -- internal constant projection
        (operation_id,),
    ).fetchone()
    if row is None:  # pragma: no cover
        raise RuntimeError("operation request disappeared after insert")
    return _request_from_row(row)


def mark_operation_started(
    conn: sqlite3.Connection, *, operation_id: str, occurred_at: datetime
) -> OperationEvent:
    """Append the one started event after mutable-lane ownership is acquired."""

    return _append_event(
        conn,
        operation_id=_require_operation_id(operation_id),
        event_kind="started",
        occurred_at=occurred_at,
        status=None,
        exit_code=None,
        severity=None,
        detail_reason=None,
    )


def finish_operation(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    status: JobHealthStatus | str,
    exit_code: int,
    severity: str,
    occurred_at: datetime,
    detail_reason: str | None = None,
) -> OperationEvent:
    """Append the canonical terminal event, or return an exact replay."""

    terminal_status = JobHealthStatus(status)
    terminal_severity = _require_label(severity, "severity")
    if terminal_severity not in {"info", "warning", "error"}:
        raise ValueError("severity must be info, warning, or error")
    raw_exit_code = cast(object, exit_code)
    if isinstance(raw_exit_code, bool) or not isinstance(raw_exit_code, int):
        raise ValueError("exit_code must be an integer")
    return _append_event(
        conn,
        operation_id=_require_operation_id(operation_id),
        event_kind="terminal",
        occurred_at=occurred_at,
        status=terminal_status,
        exit_code=exit_code,
        severity=terminal_severity,
        detail_reason=detail_reason,
    )


def _append_event(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    event_kind: str,
    occurred_at: datetime,
    status: JobHealthStatus | None,
    exit_code: int | None,
    severity: str | None,
    detail_reason: str | None,
) -> OperationEvent:
    detail_code = f"job_{status.value}" if status is not None else None
    safe_reason = redact(detail_reason)[:_MAX_DETAIL_REASON] if detail_reason else None
    stamp = _to_iso(occurred_at)
    event_id = _event_identity(operation_id, event_kind)
    event_hash = _event_hash(
        operation_id,
        event_kind,
        status,
        exit_code,
        severity,
        detail_code,
        safe_reason,
    )
    try:
        with conn:
            conn.execute(
                "INSERT INTO operation_events "
                "(event_id,operation_id,event_kind,event_sha256,occurred_at,status,exit_code,"
                "severity,detail_code,detail_reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    operation_id,
                    event_kind,
                    event_hash,
                    stamp,
                    status.value if status is not None else None,
                    exit_code,
                    severity,
                    detail_code,
                    safe_reason,
                ),
            )
    except sqlite3.IntegrityError:
        row = conn.execute(
            "SELECT event_id,operation_id,event_kind,event_sha256,occurred_at,status,exit_code,"
            "severity,detail_code,detail_reason FROM operation_events "
            "WHERE operation_id=? AND event_kind=?",
            (operation_id, event_kind),
        ).fetchone()
        if row is None:
            raise
        if str(row[3]) != event_hash:
            raise OperationConflictError(f"{event_kind} event hash conflict") from None
        return _event_from_row(row)
    row = conn.execute(
        "SELECT event_id,operation_id,event_kind,event_sha256,occurred_at,status,exit_code,"
        "severity,detail_code,detail_reason FROM operation_events "
        "WHERE operation_id=? AND event_kind=?",
        (operation_id, event_kind),
    ).fetchone()
    if row is None:  # pragma: no cover
        raise RuntimeError("operation event disappeared after insert")
    return _event_from_row(row)


def _event_from_row(row: sqlite3.Row | tuple[object, ...]) -> OperationEvent:
    event_id = str(row[0])
    if _EVENT_ID.fullmatch(event_id) is None:
        raise ValueError("persisted event_id is not canonical")
    operation_id = _require_operation_id(str(row[1]))
    event_kind = str(row[2])
    if event_kind not in {"started", "terminal"}:
        raise ValueError("persisted event_kind is invalid")
    event_sha = _require_sha256(str(row[3]), "persisted event_sha256")
    status = None if row[5] is None else JobHealthStatus(str(row[5]))
    exit_code = _optional_int(row[6])
    severity = None if row[7] is None else _require_label(str(row[7]), "persisted severity")
    detail_code = None if row[8] is None else _require_label(str(row[8]), "persisted detail_code")
    detail_reason = None if row[9] is None else str(row[9])
    if event_kind == "started" and any(
        value is not None for value in (status, exit_code, severity, detail_code, detail_reason)
    ):
        raise ValueError("persisted started event has terminal fields")
    if event_kind == "terminal":
        if status is None or exit_code is None or severity not in {"info", "warning", "error"}:
            raise ValueError("persisted terminal event is incomplete")
        if detail_code != f"job_{status.value}":
            raise ValueError("persisted terminal detail_code is invalid")
        if detail_reason is not None and not 1 <= len(detail_reason) <= _MAX_DETAIL_REASON:
            raise ValueError("persisted terminal detail_reason is invalid")
    return OperationEvent(
        event_id=event_id,
        operation_id=operation_id,
        event_kind=event_kind,
        event_sha256=event_sha,
        occurred_at=_from_iso(row[4]),
        status=status,
        exit_code=exit_code,
        severity=severity,
        detail_code=detail_code,
        detail_reason=detail_reason,
    )


def list_operations(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
    before: OperationCursor | None = None,
) -> tuple[OperationListRow, ...]:
    """Read a bounded stable newest-first page using the compound index."""

    if isinstance(limit, bool) or not 1 <= limit <= _MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {_MAX_PAGE_SIZE}")
    projection = (
        "r.operation_id,r.actor,r.job_name,r.trigger_kind,r.trace_id,r.stage,"
        "r.write_sets_json,r.requested_at,e.status,e.exit_code,e.severity,e.occurred_at"
    )
    base = (
        f"SELECT {projection} FROM operation_requests r LEFT JOIN operation_events e "  # nosec B608 -- internal constant projection
        "ON e.operation_id=r.operation_id AND e.event_kind='terminal' "
    )
    if before is not None:
        cursor_stamp = before.requested_at.astimezone(UTC).isoformat(timespec="microseconds")
        rows = conn.execute(
            base + "WHERE (r.requested_at < ? OR (r.requested_at = ? AND r.operation_id < ?)) "
            "ORDER BY r.requested_at DESC,r.operation_id DESC LIMIT ?",  # nosec B608 -- internal fixed clause
            (cursor_stamp, cursor_stamp, before.operation_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            base + "ORDER BY r.requested_at DESC,r.operation_id DESC LIMIT ?",  # nosec B608 -- internal fixed clause
            (limit,),
        ).fetchall()
    output: list[OperationListRow] = []
    for row in rows:
        write_sets = _write_sets_from_json(str(row[6]))
        output.append(
            OperationListRow(
                operation_id=str(row[0]),
                actor=str(row[1]),
                job_name=str(row[2]),
                trigger_kind=TriggerKind(str(row[3])),
                trace_id=str(row[4]),
                stage=str(row[5]),
                write_sets=write_sets,
                requested_at=_from_iso(row[7]),
                terminal_status=None if row[8] is None else JobHealthStatus(str(row[8])),
                terminal_exit_code=_optional_int(row[9]),
                terminal_severity=None if row[10] is None else str(row[10]),
                terminal_at=None if row[11] is None else _from_iso(row[11]),
            )
        )
    return tuple(output)


__all__ = [
    "JobHealthStatus",
    "OperationConflictError",
    "OperationCursor",
    "OperationEvent",
    "OperationListRow",
    "OperationRequest",
    "OperationRequestInput",
    "ScopeScalar",
    "TriggerKind",
    "accept_operation_request",
    "finish_operation",
    "list_operations",
    "make_command_sha256",
    "make_operation_id",
    "mark_operation_started",
]
