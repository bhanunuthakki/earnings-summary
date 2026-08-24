"""One transactional writer for governed Operations attention actions.

This deliberately has no HTTP, Scheduler, or presentation dependency.  It
accepts only the small operator verb set, proves the request still refers to
the finding's current evidence, and persists the projection, lifecycle event,
and immutable action receipt as one SQLite transaction.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from operations.attention import (
    AttentionAction,
    AttentionHealth,
    AttentionLifecycle,
    AttentionReason,
    AttentionSeverity,
    EvidenceIdentity,
    EvidenceKind,
    FindingKind,
    OperationsAttentionFinding,
    apply_attention_action,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FINDING_ID = re.compile(r"operations-attention:[0-9a-f]{64}\Z")
_LABEL = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z")


class OperatorAction(StrEnum):
    """The entire interactive action registry for this writer."""

    ACKNOWLEDGE = "acknowledge"
    SNOOZE = "snooze"
    RESOLVE = "resolve"


OPERATOR_ACTIONS: Final[frozenset[OperatorAction]] = frozenset(OperatorAction)


class ActionResultState(StrEnum):
    APPLIED = "applied"
    REJECTED = "rejected"
    REPLAYED = "replayed"
    CONFLICT = "conflict"


class ActionFailureCode(StrEnum):
    PROHIBITED_SUPPRESSION = "prohibited_suppression"
    INVALID_TRANSITION = "invalid_transition"
    INVALID_EXPIRY = "invalid_expiry"
    VALIDATION_FAILED = "validation_failed"
    CONFLICT = "conflict"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_sha256(value: dict[str, str | None]) -> str:
    return _sha256(json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True))


def _require_sha256(value: str, *, field: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return value


def _require_aware(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


def _require_actor(value: str) -> str:
    if _LABEL.fullmatch(value.strip()) is None:
        raise ValueError("actor must be a bounded canonical label")
    return value.strip()


def _require_idempotency_key(value: str) -> str:
    if _IDEMPOTENCY_KEY.fullmatch(value.strip()) is None:
        raise ValueError("idempotency_key must be a bounded canonical key")
    return value.strip()


@dataclass(frozen=True, slots=True)
class OperatorActionRequest:
    """Validated request with no free-form note, command, or URL field."""

    finding_id: str
    action: OperatorAction
    evidence_fingerprint_sha256: str
    idempotency_key: str = field(repr=False)
    occurred_at: datetime
    reason: AttentionReason | None = None
    acknowledge_until: datetime | None = None
    snooze_until: datetime | None = None

    def __post_init__(self) -> None:
        if _FINDING_ID.fullmatch(self.finding_id) is None:
            raise ValueError("finding_id must be canonical")
        object.__setattr__(self, "action", OperatorAction(self.action))
        _require_sha256(self.evidence_fingerprint_sha256, field="evidence_fingerprint_sha256")
        object.__setattr__(self, "idempotency_key", _require_idempotency_key(self.idempotency_key))
        _require_aware(self.occurred_at, field="occurred_at")
        if self.acknowledge_until is not None:
            _require_aware(self.acknowledge_until, field="acknowledge_until")
        if self.snooze_until is not None:
            _require_aware(self.snooze_until, field="snooze_until")


@dataclass(frozen=True, slots=True)
class OperatorActionReceipt:
    """Safe action outcome: all identifiers are canonical hashes or labels."""

    receipt_id: str | None
    finding_id: str
    action: OperatorAction
    result_lifecycle: AttentionLifecycle
    result_state: ActionResultState
    request_sha256: str
    idempotency_key_sha256: str
    lifecycle_event_id: str | None
    failure_code: ActionFailureCode | None
    durable: bool


def canonical_request_sha256(request: OperatorActionRequest, *, actor: str) -> str:
    """Hash the full semantic request, including the server-supplied actor."""

    actor_label = _require_actor(actor)
    return _canonical_sha256(
        {
            "acknowledge_until": _iso_or_none(request.acknowledge_until),
            "action": request.action.value,
            "actor": actor_label,
            "evidence_fingerprint_sha256": request.evidence_fingerprint_sha256,
            "finding_id": request.finding_id,
            "occurred_at": _iso_or_none(request.occurred_at),
            "reason_code": request.reason.code.value if request.reason is not None else None,
            "reason_reference_sha256": (
                request.reason.reference_sha256 if request.reason is not None else None
            ),
            "snooze_until": _iso_or_none(request.snooze_until),
        }
    )


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _result_lifecycle(action: OperatorAction) -> AttentionLifecycle:
    return AttentionLifecycle(
        {
            OperatorAction.ACKNOWLEDGE: AttentionLifecycle.ACKNOWLEDGED,
            OperatorAction.SNOOZE: AttentionLifecycle.SNOOZED,
            OperatorAction.RESOLVE: AttentionLifecycle.RESOLVED,
        }[action]
    )


def _failure_for(error: ValueError) -> ActionFailureCode:
    message = str(error)
    if "cannot be acknowledged or snoozed" in message:
        return ActionFailureCode.PROHIBITED_SUPPRESSION
    if "must be after" in message or "has not expired" in message:
        return ActionFailureCode.INVALID_EXPIRY
    if "not allowed from" in message or "require healthy evidence" in message:
        return ActionFailureCode.INVALID_TRANSITION
    return ActionFailureCode.VALIDATION_FAILED


def _validate_request_shape(request: OperatorActionRequest) -> None:
    """Keep action-specific fields closed before any projection mutation."""

    if request.action is OperatorAction.ACKNOWLEDGE:
        if (
            request.reason is None
            or request.acknowledge_until is None
            or request.snooze_until is not None
        ):
            raise ValueError("acknowledge requires only reason and acknowledge_until")
        return
    if request.action is OperatorAction.SNOOZE:
        if (
            request.reason is None
            or request.snooze_until is None
            or request.acknowledge_until is not None
        ):
            raise ValueError("snooze requires only reason and snooze_until")
        return
    if (
        request.reason is not None
        or request.acknowledge_until is not None
        or request.snooze_until is not None
    ):
        raise ValueError("resolve accepts no suppression fields")


def _finding_from_row(row: sqlite3.Row) -> OperationsAttentionFinding:
    def parsed(name: str) -> datetime | None:
        value = row[name]
        return None if value is None else datetime.fromisoformat(str(value))

    return OperationsAttentionFinding(
        finding_id=str(row["finding_id"]),
        owner=str(row["owner"]),
        kind=FindingKind(str(row["kind"])),
        severity=AttentionSeverity(str(row["severity"])),
        health=AttentionHealth(str(row["health"])),
        evidence=EvidenceIdentity(
            kind=EvidenceKind(str(row["evidence_kind"])),
            fingerprint_sha256=str(row["evidence_fingerprint_sha256"]),
            version=str(row["evidence_version"]),
            reference=str(row["evidence_reference"]),
            reference_sha256=str(row["evidence_reference_sha256"]),
        ),
        lifecycle=AttentionLifecycle(str(row["lifecycle"])),
        opened_at=_required_datetime(parsed("opened_at"), "opened_at"),
        acknowledged_at=parsed("acknowledged_at"),
        acknowledged_until=parsed("acknowledged_until"),
        snoozed_until=parsed("snoozed_until"),
        resolved_at=parsed("resolved_at"),
        superseded_by_finding_id=(
            None
            if row["superseded_by_finding_id"] is None
            else str(row["superseded_by_finding_id"])
        ),
    )


def _required_datetime(value: datetime | None, field: str) -> datetime:
    if value is None:
        raise ValueError(f"stored {field} is missing")
    return _require_aware(value, field=field)


def _stored_updated_at(row: sqlite3.Row) -> datetime:
    """Treat malformed projection chronology as a failed closed write boundary."""

    raw = row["updated_at"]
    if raw is None:
        raise ValueError("stored updated_at is missing")
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError as error:
        raise ValueError("stored updated_at is invalid") from error
    return _require_aware(parsed, field="stored updated_at")


def _receipt_from_row(row: sqlite3.Row, *, state: ActionResultState) -> OperatorActionReceipt:
    failure = row["failure_code"]
    return OperatorActionReceipt(
        receipt_id=str(row["receipt_id"]),
        finding_id=str(row["finding_id"]),
        action=OperatorAction(str(row["action"])),
        result_lifecycle=AttentionLifecycle(str(row["result_lifecycle"])),
        result_state=state,
        request_sha256=str(row["request_sha256"]),
        idempotency_key_sha256=str(row["idempotency_key_sha256"]),
        lifecycle_event_id=(
            None if row["lifecycle_event_id"] is None else str(row["lifecycle_event_id"])
        ),
        failure_code=None if failure is None else ActionFailureCode(str(failure)),
        durable=True,
    )


def _receipt_ids(*, idempotency_key_sha256: str, request_sha256: str) -> tuple[str, str]:
    event_id = "operations-attention-event:" + _canonical_sha256(
        {"idempotency_key_sha256": idempotency_key_sha256, "request_sha256": request_sha256}
    )
    receipt_id = "operations-attention-action:" + _canonical_sha256(
        {"idempotency_key_sha256": idempotency_key_sha256, "request_sha256": request_sha256}
    )
    return event_id, receipt_id


def _update_projection(
    conn: sqlite3.Connection, finding: OperationsAttentionFinding, *, updated_at: datetime
) -> None:
    conn.execute(
        """
        UPDATE operations_attention_findings
        SET lifecycle=?, acknowledged_at=?, acknowledged_until=?, snoozed_until=?, resolved_at=?,
            superseded_by_finding_id=?, updated_at=?
        WHERE finding_id=?
        """,
        (
            finding.lifecycle.value,
            _iso_or_none(finding.acknowledged_at),
            _iso_or_none(finding.acknowledged_until),
            _iso_or_none(finding.snoozed_until),
            _iso_or_none(finding.resolved_at),
            finding.superseded_by_finding_id,
            updated_at.isoformat(),
            finding.finding_id,
        ),
    )


def _insert_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    original: OperationsAttentionFinding,
    updated: OperationsAttentionFinding,
    request: OperatorActionRequest,
    request_sha256: str,
) -> None:
    conn.execute(
        """
        INSERT INTO operations_attention_lifecycle_events(
            event_id,finding_id,event_kind,from_lifecycle,to_lifecycle,
            evidence_fingerprint_sha256,occurred_at,receipt_sha256
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            original.finding_id,
            request.action.value,
            original.lifecycle.value,
            updated.lifecycle.value,
            original.evidence.fingerprint_sha256,
            request.occurred_at.isoformat(),
            request_sha256,
        ),
    )


def _insert_receipt(
    conn: sqlite3.Connection,
    *,
    receipt_id: str,
    idempotency_key_sha256: str,
    finding_id: str,
    action: OperatorAction,
    result_lifecycle: AttentionLifecycle,
    actor: str,
    occurred_at: datetime,
    request_sha256: str,
    request: OperatorActionRequest,
    lifecycle_event_id: str | None,
    failure_code: ActionFailureCode | None,
) -> None:
    if lifecycle_event_id is None:
        if failure_code is None:
            raise ValueError("rejected receipts require a failure code")
        persisted_result_state = "rejected"
        persisted_failure_code = failure_code.value
    else:
        persisted_result_state = "applied"
        persisted_failure_code = None
    failure_sha256 = None if failure_code is None else _sha256(failure_code.value)
    conn.execute(
        """
        INSERT INTO operations_attention_action_receipts(
            receipt_id,idempotency_key_sha256,finding_id,lifecycle_event_id,actor,action,
            result_lifecycle,occurred_at,request_sha256,reason_code,reason_reference_sha256,
            acknowledged_until,snoozed_until,result_state,failure_code,failure_sha256
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            receipt_id,
            idempotency_key_sha256,
            finding_id,
            lifecycle_event_id,
            actor,
            action.value,
            result_lifecycle.value,
            occurred_at.isoformat(),
            request_sha256,
            request.reason.code.value
            if lifecycle_event_id is not None and request.reason
            else None,
            request.reason.reference_sha256
            if lifecycle_event_id is not None and request.reason
            else None,
            _iso_or_none(request.acknowledge_until) if lifecycle_event_id is not None else None,
            _iso_or_none(request.snooze_until) if lifecycle_event_id is not None else None,
            persisted_result_state,
            persisted_failure_code,
            failure_sha256,
        ),
    )


def _durable_rejection(
    conn: sqlite3.Connection,
    *,
    request: OperatorActionRequest,
    actor: str,
    idempotency_key_sha256: str,
    request_sha256: str,
    failure_code: ActionFailureCode,
) -> OperatorActionReceipt:
    _, receipt_id = _receipt_ids(
        idempotency_key_sha256=idempotency_key_sha256, request_sha256=request_sha256
    )
    _insert_receipt(
        conn,
        receipt_id=receipt_id,
        idempotency_key_sha256=idempotency_key_sha256,
        finding_id=request.finding_id,
        action=request.action,
        result_lifecycle=_result_lifecycle(request.action),
        actor=actor,
        occurred_at=request.occurred_at,
        request_sha256=request_sha256,
        request=request,
        lifecycle_event_id=None,
        failure_code=failure_code,
    )
    return OperatorActionReceipt(
        receipt_id=receipt_id,
        finding_id=request.finding_id,
        action=request.action,
        result_lifecycle=_result_lifecycle(request.action),
        result_state=ActionResultState.REJECTED,
        request_sha256=request_sha256,
        idempotency_key_sha256=idempotency_key_sha256,
        lifecycle_event_id=None,
        failure_code=failure_code,
        durable=True,
    )


def execute_operator_action(
    request: OperatorActionRequest,
    *,
    actor: str,
    db_path: Path | str,
) -> OperatorActionReceipt:
    """Apply one governed action under a fail-closed, serialized transaction.

    ``actor`` is deliberately supplied by the trusted server boundary rather
    than being a client-owned field in ``OperatorActionRequest``.  A replay of
    the same semantic request returns the original durable receipt; a reused
    key with different semantics returns a non-mutating conflict result.
    """

    actor_label = _require_actor(actor)
    idempotency_key_sha256 = _sha256(request.idempotency_key)
    request_sha256 = canonical_request_sha256(request, actor=actor_label)
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("SAVEPOINT operations_attention_action")
        existing = conn.execute(
            "SELECT * FROM operations_attention_action_receipts WHERE idempotency_key_sha256=?",
            (idempotency_key_sha256,),
        ).fetchone()
        if existing is not None:
            conn.execute("RELEASE SAVEPOINT operations_attention_action")
            conn.commit()
            existing_request_sha256 = str(existing["request_sha256"])
            if existing_request_sha256 == request_sha256:
                return _receipt_from_row(existing, state=ActionResultState.REPLAYED)
            return OperatorActionReceipt(
                receipt_id=str(existing["receipt_id"]),
                finding_id=request.finding_id,
                action=request.action,
                result_lifecycle=_result_lifecycle(request.action),
                result_state=ActionResultState.CONFLICT,
                request_sha256=request_sha256,
                idempotency_key_sha256=idempotency_key_sha256,
                lifecycle_event_id=None,
                failure_code=ActionFailureCode.CONFLICT,
                durable=False,
            )

        row = conn.execute(
            "SELECT * FROM operations_attention_findings WHERE finding_id=?", (request.finding_id,)
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK TO SAVEPOINT operations_attention_action")
            conn.execute("RELEASE SAVEPOINT operations_attention_action")
            conn.commit()
            return OperatorActionReceipt(
                receipt_id=None,
                finding_id=request.finding_id,
                action=request.action,
                result_lifecycle=_result_lifecycle(request.action),
                result_state=ActionResultState.REJECTED,
                request_sha256=request_sha256,
                idempotency_key_sha256=idempotency_key_sha256,
                lifecycle_event_id=None,
                failure_code=ActionFailureCode.VALIDATION_FAILED,
                durable=False,
            )
        try:
            stored_updated_at = _stored_updated_at(row)
        except ValueError:
            receipt = _durable_rejection(
                conn,
                request=request,
                actor=actor_label,
                idempotency_key_sha256=idempotency_key_sha256,
                request_sha256=request_sha256,
                failure_code=ActionFailureCode.VALIDATION_FAILED,
            )
            conn.execute("RELEASE SAVEPOINT operations_attention_action")
            conn.commit()
            return receipt
        if request.occurred_at < stored_updated_at:
            receipt = _durable_rejection(
                conn,
                request=request,
                actor=actor_label,
                idempotency_key_sha256=idempotency_key_sha256,
                request_sha256=request_sha256,
                failure_code=ActionFailureCode.CONFLICT,
            )
            conn.execute("RELEASE SAVEPOINT operations_attention_action")
            conn.commit()
            return receipt
        finding = _finding_from_row(row)
        if finding.evidence.fingerprint_sha256 != request.evidence_fingerprint_sha256:
            receipt = _durable_rejection(
                conn,
                request=request,
                actor=actor_label,
                idempotency_key_sha256=idempotency_key_sha256,
                request_sha256=request_sha256,
                failure_code=ActionFailureCode.CONFLICT,
            )
            conn.execute("RELEASE SAVEPOINT operations_attention_action")
            conn.commit()
            return receipt
        try:
            _validate_request_shape(request)
            updated = apply_attention_action(
                finding,
                AttentionAction(request.action.value),
                at=request.occurred_at,
                reason=request.reason,
                acknowledge_until=request.acknowledge_until,
                snooze_until=request.snooze_until,
            )
        except ValueError as error:
            receipt = _durable_rejection(
                conn,
                request=request,
                actor=actor_label,
                idempotency_key_sha256=idempotency_key_sha256,
                request_sha256=request_sha256,
                failure_code=_failure_for(error),
            )
            conn.execute("RELEASE SAVEPOINT operations_attention_action")
            conn.commit()
            return receipt

        event_id, receipt_id = _receipt_ids(
            idempotency_key_sha256=idempotency_key_sha256, request_sha256=request_sha256
        )
        _insert_event(
            conn,
            event_id=event_id,
            original=finding,
            updated=updated,
            request=request,
            request_sha256=request_sha256,
        )
        _update_projection(conn, updated, updated_at=request.occurred_at)
        _insert_receipt(
            conn,
            receipt_id=receipt_id,
            idempotency_key_sha256=idempotency_key_sha256,
            finding_id=request.finding_id,
            action=request.action,
            result_lifecycle=updated.lifecycle,
            actor=actor_label,
            occurred_at=request.occurred_at,
            request_sha256=request_sha256,
            request=request,
            lifecycle_event_id=event_id,
            failure_code=None,
        )
        conn.execute("RELEASE SAVEPOINT operations_attention_action")
        conn.commit()
        return OperatorActionReceipt(
            receipt_id=receipt_id,
            finding_id=request.finding_id,
            action=request.action,
            result_lifecycle=updated.lifecycle,
            result_state=ActionResultState.APPLIED,
            request_sha256=request_sha256,
            idempotency_key_sha256=idempotency_key_sha256,
            lifecycle_event_id=event_id,
            failure_code=None,
            durable=True,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


__all__ = [
    "OPERATOR_ACTIONS",
    "ActionFailureCode",
    "ActionResultState",
    "OperatorAction",
    "OperatorActionReceipt",
    "OperatorActionRequest",
    "canonical_request_sha256",
    "execute_operator_action",
]
