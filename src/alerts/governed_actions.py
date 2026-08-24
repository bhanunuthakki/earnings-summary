"""Evidence-bound, append-only lifecycle actions for persisted alerts.

This is intentionally a core adapter, not an HTTP endpoint.  A caller supplies
one already-open SQLite transaction and a stable alert identity; this module
then verifies the alert's immutable evidence reference before either recording
a review or delegating a thesis-episode transition to its authoritative core.
It never places, sizes, or transmits a broker trade.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from compute.thesis_episode_attention import (
    AttentionError,
    AttentionState,
    acknowledge_episode,
    act_on_episode,
    get_attention,
    supersede_prior,
)

_ACTION_LABEL_MAX_LENGTH = 128
_MAX_NOTE_LENGTH = 1000
_SHA256_LENGTH = 64
_RECEIPT_PREFIX = "governed-alert-action:"
_THESIS_TRIGGER_KIND = "thesis_drift"


class GovernedAlertActionError(ValueError):
    """Raised when a governed alert action is stale, invalid, or disallowed."""


class GovernedAlertActionType(StrEnum):
    """Closed action vocabulary for the unexposed governed alert core."""

    REVIEW = "review"
    ACKNOWLEDGE = "acknowledge"
    DISMISS = "dismiss"
    DEFER = "defer"
    SNOOZE = "defer"
    COMPLETE = "complete"
    SUPERSEDE = "supersede"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GovernedAlertAction(_FrozenModel):
    """One fully specified, idempotent request against one persisted alert."""

    idempotency_key: str = Field(min_length=1, max_length=_ACTION_LABEL_MAX_LENGTH)
    actor: str = Field(min_length=1, max_length=_ACTION_LABEL_MAX_LENGTH)
    alert_id: int = Field(gt=0)
    source_ref: str = Field(min_length=1, max_length=_ACTION_LABEL_MAX_LENGTH)
    evidence_ref: str = Field(min_length=_SHA256_LENGTH, max_length=_SHA256_LENGTH)
    action_type: GovernedAlertActionType
    occurred_at: datetime
    note: str | None = Field(default=None, max_length=_MAX_NOTE_LENGTH)
    dismiss_reason: str | None = Field(default=None, max_length=_MAX_NOTE_LENGTH)
    defer_until: datetime | None = None
    decision_id: int | None = Field(default=None, gt=0)
    replacement_episode_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("idempotency_key", "actor", "source_ref", "replacement_episode_id")
    @classmethod
    def _nonempty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("evidence_ref")
    @classmethod
    def _hex_evidence_ref(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != _SHA256_LENGTH or any(
            char not in "0123456789abcdef" for char in normalized
        ):
            raise ValueError("must be a lowercase SHA-256 digest")
        return normalized

    @field_validator("occurred_at", "defer_until")
    @classmethod
    def _aware_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("note", "dismiss_reason")
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def _validate_action_shape(self) -> GovernedAlertAction:
        if self.source_ref != f"alert:{self.alert_id}":
            raise ValueError("source_ref must exactly identify alert_id")
        if self.action_type is GovernedAlertActionType.DISMISS:
            if self.dismiss_reason is None:
                raise ValueError("dismiss_reason is required for dismiss")
        elif self.dismiss_reason is not None:
            raise ValueError("dismiss_reason is allowed only for dismiss")
        if self.action_type is GovernedAlertActionType.DEFER:
            if self.defer_until is None:
                raise ValueError("defer_until is required for defer")
            if self.defer_until <= self.occurred_at:
                raise ValueError("defer_until must be after occurred_at")
        elif self.defer_until is not None:
            raise ValueError("defer_until is allowed only for defer")
        if self.action_type is GovernedAlertActionType.COMPLETE:
            if self.decision_id is None:
                raise ValueError("decision_id is required for complete")
        elif self.decision_id is not None:
            raise ValueError("decision_id is allowed only for complete")
        if self.action_type is GovernedAlertActionType.SUPERSEDE:
            if self.replacement_episode_id is None:
                raise ValueError("replacement_episode_id is required for supersede")
        elif self.replacement_episode_id is not None:
            raise ValueError("replacement_episode_id is allowed only for supersede")
        if (
            self.action_type
            not in {
                GovernedAlertActionType.ACKNOWLEDGE,
                GovernedAlertActionType.DEFER,
            }
            and self.note is not None
        ):
            raise ValueError("note is allowed only for acknowledge or defer")
        return self


class GovernedAlertActionReceipt(_FrozenModel):
    """One immutable audit receipt for a successful governed alert action."""

    receipt_id: str
    idempotency_key: str
    request_sha256: str
    actor: str
    alert_id: int
    source_ref: str
    evidence_ref: str
    action_type: GovernedAlertActionType
    occurred_at: datetime
    note_sha256: str | None
    dismiss_reason_sha256: str | None
    defer_until: datetime | None
    decision_id: int | None
    replacement_episode_id: str | None
    result_state: str


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _optional_digest(value: str | None) -> str | None:
    return None if value is None else _sha256(value)


def _request_sha256(action: GovernedAlertAction) -> str:
    payload = {
        "action_type": action.action_type.value,
        "actor": action.actor,
        "alert_id": action.alert_id,
        "decision_id": action.decision_id,
        "defer_until": None if action.defer_until is None else action.defer_until.isoformat(),
        "dismiss_reason_sha256": _optional_digest(action.dismiss_reason),
        "evidence_ref": action.evidence_ref,
        "idempotency_key": action.idempotency_key,
        "note_sha256": _optional_digest(action.note),
        "occurred_at": action.occurred_at.isoformat(),
        "replacement_episode_id": action.replacement_episode_id,
        "source_ref": action.source_ref,
    }
    return _sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _receipt_id(idempotency_key: str) -> str:
    return _RECEIPT_PREFIX + _sha256(idempotency_key)


def _parse_timestamp(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GovernedAlertActionError("persisted receipt timestamp is not timezone-aware")
    return parsed.astimezone(UTC)


def _receipt_from_row(row: sqlite3.Row) -> GovernedAlertActionReceipt:
    return GovernedAlertActionReceipt(
        receipt_id=str(row["receipt_id"]),
        idempotency_key=str(row["idempotency_key"]),
        request_sha256=str(row["request_sha256"]),
        actor=str(row["actor"]),
        alert_id=int(row["alert_id"]),
        source_ref=str(row["source_ref"]),
        evidence_ref=str(row["evidence_ref"]),
        action_type=GovernedAlertActionType(str(row["action_type"])),
        occurred_at=_parse_timestamp(row["occurred_at"]),
        note_sha256=None if row["note_sha256"] is None else str(row["note_sha256"]),
        dismiss_reason_sha256=(
            None if row["dismiss_reason_sha256"] is None else str(row["dismiss_reason_sha256"])
        ),
        defer_until=None if row["defer_until"] is None else _parse_timestamp(row["defer_until"]),
        decision_id=None if row["decision_id"] is None else int(row["decision_id"]),
        replacement_episode_id=(
            None if row["replacement_episode_id"] is None else str(row["replacement_episode_id"])
        ),
        result_state=str(row["result_state"]),
    )


def _existing_receipt(
    connection: sqlite3.Connection,
    *,
    idempotency_key: str,
    request_sha256: str,
) -> GovernedAlertActionReceipt | None:
    row = connection.execute(
        "SELECT * FROM governed_alert_action_receipts WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    receipt = _receipt_from_row(row)
    if receipt.request_sha256 != request_sha256:
        raise GovernedAlertActionError("idempotency key conflicts with a different action request")
    return receipt


def _linked_episode_id(alert: sqlite3.Row) -> str:
    if str(alert["trigger_kind"]) != _THESIS_TRIGGER_KIND:
        raise GovernedAlertActionError("action is allowed only for a thesis_drift alert")
    episode_id = alert["thesis_evaluation_episode_id"]
    if episode_id is None:
        raise GovernedAlertActionError("action requires a linked thesis episode")
    return str(episode_id)


def _require_pending_alert(
    connection: sqlite3.Connection, action: GovernedAlertAction
) -> sqlite3.Row:
    alert = connection.execute("SELECT * FROM alerts WHERE id=?", (action.alert_id,)).fetchone()
    if alert is None:
        raise GovernedAlertActionError(f"unknown alert: {action.alert_id}")
    if str(alert["signature_sha"]) != action.evidence_ref:
        raise GovernedAlertActionError("evidence_ref does not match the persisted alert")
    if str(alert["status"]) != "pending":
        raise GovernedAlertActionError("alert is no longer pending")
    return alert


def _dismiss_unlinked_alert(
    connection: sqlite3.Connection,
    action: GovernedAlertAction,
) -> None:
    updated = connection.execute(
        "UPDATE alerts SET status='dismissed',dismissed_at=?,dismiss_reason=? "
        "WHERE id=? AND status='pending'",
        (action.occurred_at.isoformat(), action.dismiss_reason, action.alert_id),
    )
    if updated.rowcount != 1:
        raise GovernedAlertActionError("alert changed during dismissal")


def _supersede_episode(
    connection: sqlite3.Connection,
    *,
    old_episode_id: str,
    replacement_episode_id: str,
    occurred_at: datetime,
) -> None:
    if old_episode_id == replacement_episode_id:
        raise GovernedAlertActionError("replacement episode must differ from the linked episode")
    old = connection.execute(
        "SELECT ticker,first_evaluated_at FROM thesis_evaluation_episodes WHERE episode_id=?",
        (old_episode_id,),
    ).fetchone()
    replacement = connection.execute(
        "SELECT ticker,first_evaluated_at FROM thesis_evaluation_episodes WHERE episode_id=?",
        (replacement_episode_id,),
    ).fetchone()
    if old is None or replacement is None:
        raise GovernedAlertActionError("supersession requires existing thesis episodes")
    if str(old["ticker"]) != str(replacement["ticker"]):
        raise GovernedAlertActionError("replacement episode must have the same ticker")
    # The authoritative core accepts ``first_evaluated_at <= replacement`` so
    # separate material episodes evaluated in the same batch may supersede.
    if _parse_timestamp(replacement["first_evaluated_at"]) < _parse_timestamp(
        old["first_evaluated_at"]
    ):
        raise GovernedAlertActionError(
            "replacement episode must be evaluated at the same time or later"
        )
    old_attention = get_attention(connection, old_episode_id, now=occurred_at)
    if old_attention.state not in {AttentionState.UNREVIEWED, AttentionState.ACKNOWLEDGED}:
        raise GovernedAlertActionError("only unresolved thesis episodes may be superseded")
    supersede_prior(
        connection,
        new_episode_id=replacement_episode_id,
        superseded_at=occurred_at,
    )
    settled = get_attention(connection, old_episode_id, now=occurred_at)
    if (
        settled.state is not AttentionState.SUPERSEDED
        or settled.superseded_by_episode_id != replacement_episode_id
    ):
        raise GovernedAlertActionError(
            "linked thesis episode was not superseded by the replacement"
        )


def _apply_transition(
    connection: sqlite3.Connection,
    action: GovernedAlertAction,
    alert: sqlite3.Row,
) -> str:
    if action.action_type is GovernedAlertActionType.REVIEW:
        return "reviewed"
    if action.action_type is GovernedAlertActionType.DISMISS:
        episode_id = alert["thesis_evaluation_episode_id"]
        if episode_id is None:
            _dismiss_unlinked_alert(connection, action)
        else:
            _linked_episode_id(alert)
            acknowledge_episode(
                connection,
                str(episode_id),
                acknowledged_at=action.occurred_at,
                note=action.dismiss_reason,
            )
            connection.execute(
                "UPDATE alerts SET dismiss_reason=? WHERE id=? AND status='dismissed'",
                (action.dismiss_reason, action.alert_id),
            )
        return "dismissed"

    episode_id = _linked_episode_id(alert)
    try:
        if action.action_type is GovernedAlertActionType.ACKNOWLEDGE:
            acknowledge_episode(
                connection,
                episode_id,
                acknowledged_at=action.occurred_at,
                note=action.note,
            )
            return "acknowledged"
        if action.action_type is GovernedAlertActionType.DEFER:
            if action.defer_until is None:  # Pydantic is the primary guard.
                raise GovernedAlertActionError("defer_until is required for defer")
            acknowledge_episode(
                connection,
                episode_id,
                acknowledged_at=action.occurred_at,
                note=action.note,
                next_review_at=action.defer_until,
            )
            return "deferred"
        if action.action_type is GovernedAlertActionType.COMPLETE:
            if action.decision_id is None:  # Pydantic is the primary guard.
                raise GovernedAlertActionError("decision_id is required for complete")
            act_on_episode(
                connection,
                episode_id,
                decision_id=action.decision_id,
                acted_at=action.occurred_at,
            )
            return "completed"
        if action.action_type is GovernedAlertActionType.SUPERSEDE:
            if action.replacement_episode_id is None:  # Pydantic is the primary guard.
                raise GovernedAlertActionError("replacement_episode_id is required for supersede")
            _supersede_episode(
                connection,
                old_episode_id=episode_id,
                replacement_episode_id=action.replacement_episode_id,
                occurred_at=action.occurred_at,
            )
            return "superseded"
    except AttentionError as error:
        raise GovernedAlertActionError(str(error)) from error
    raise GovernedAlertActionError(f"unsupported action type: {action.action_type.value}")


def _append_receipt(
    connection: sqlite3.Connection,
    action: GovernedAlertAction,
    *,
    request_sha256: str,
    result_state: str,
) -> GovernedAlertActionReceipt:
    try:
        connection.execute(
            "INSERT INTO governed_alert_action_receipts "
            "(receipt_id,idempotency_key,request_sha256,actor,alert_id,source_ref,evidence_ref,"
            "action_type,occurred_at,note_sha256,dismiss_reason_sha256,defer_until,decision_id,"
            "replacement_episode_id,result_state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _receipt_id(action.idempotency_key),
                action.idempotency_key,
                request_sha256,
                action.actor,
                action.alert_id,
                action.source_ref,
                action.evidence_ref,
                action.action_type.value,
                action.occurred_at.isoformat(),
                _optional_digest(action.note),
                _optional_digest(action.dismiss_reason),
                None if action.defer_until is None else action.defer_until.isoformat(),
                action.decision_id,
                action.replacement_episode_id,
                result_state,
            ),
        )
    except sqlite3.IntegrityError as error:
        existing = _existing_receipt(
            connection,
            idempotency_key=action.idempotency_key,
            request_sha256=request_sha256,
        )
        if existing is not None:
            return existing
        raise GovernedAlertActionError("could not append governed alert action receipt") from error
    receipt = _existing_receipt(
        connection,
        idempotency_key=action.idempotency_key,
        request_sha256=request_sha256,
    )
    if receipt is None:  # pragma: no cover - defensive database invariant.
        raise GovernedAlertActionError("appended receipt could not be read")
    return receipt


def execute_governed_alert_action(
    connection: sqlite3.Connection,
    action: GovernedAlertAction,
) -> GovernedAlertActionReceipt:
    """Perform one policy-allowed action and append its immutable receipt.

    Replaying an identical idempotency key returns the original receipt without
    reapplying a transition.  A reused key with different action content, a
    changed source/evidence binding, or a stale alert raises before any new
    receipt is written.  The caller must provide an active transaction and
    serialize concurrent writers for the same alert.  This function never
    commits or rolls back that transaction, but does atomically roll back its
    own work if receipt persistence fails.
    """

    if not connection.in_transaction:
        raise GovernedAlertActionError("governed alert actions require an active transaction")
    request_sha256 = _request_sha256(action)
    connection.execute("SAVEPOINT governed_alert_action")
    try:
        existing = _existing_receipt(
            connection,
            idempotency_key=action.idempotency_key,
            request_sha256=request_sha256,
        )
        if existing is not None:
            receipt = existing
        else:
            alert = _require_pending_alert(connection, action)
            result_state = _apply_transition(connection, action, alert)
            receipt = _append_receipt(
                connection,
                action,
                request_sha256=request_sha256,
                result_state=result_state,
            )
    except BaseException:
        connection.execute("ROLLBACK TO SAVEPOINT governed_alert_action")
        connection.execute("RELEASE SAVEPOINT governed_alert_action")
        raise
    connection.execute("RELEASE SAVEPOINT governed_alert_action")
    return receipt


__all__ = [
    "GovernedAlertAction",
    "GovernedAlertActionError",
    "GovernedAlertActionReceipt",
    "GovernedAlertActionType",
    "execute_governed_alert_action",
]
