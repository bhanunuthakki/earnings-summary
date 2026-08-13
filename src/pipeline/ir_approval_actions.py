"""Server-owned owner actions for immutable IR approval candidates.

The browser supplies only the candidate identity, verb, and owner reason. This
module derives the actor, timestamp, evidence, current revision, and replay key
inside the trusted process. Exact selection remains unavailable until a future
capture seam persists a server-owned document-byte digest; the catalog
observation digest is deliberately never substituted for document bytes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pipeline.ir_approval_store import (
    DecisionAction,
    DecisionWriteResult,
    IrApprovalConflictError,
    IrAuthorizationError,
    IrCandidate,
    IrDecisionRequest,
    append_decision,
    authorize_current_candidate,
    get_candidate,
    get_current_decision,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


class IrApprovalUiAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    SELECT_EXACT = "select_exact"


class IrApprovalActionError(ValueError):
    """Base client-safe owner-action error."""


class IrApprovalActionConflictError(IrApprovalActionError):
    """The candidate changed or the immutable append lost its revision race."""


class IrApprovalActionUnauthorizedError(IrApprovalActionError):
    """Current source policy refuses the requested owner action."""


class IrExactSelectionUnavailableError(IrApprovalActionError):
    """No server-owned document-byte identity exists for exact selection."""


class IrApprovalActionInput(BaseModel):
    """The complete and intentionally narrow untrusted browser payload."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str
    action: IrApprovalUiAction
    reason: str = Field(min_length=1, max_length=4096)

    @field_validator("candidate_id")
    @classmethod
    def _candidate_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("candidate_id must be a lowercase SHA-256 hex digest")
        return value

    @field_validator("reason")
    @classmethod
    def _non_blank_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason is required")
        return normalized


class IrApprovalActionReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal["appended", "exact_replay"]
    candidate_id: str
    action: IrApprovalUiAction
    revision: int
    owner_actor: str
    decided_at: datetime
    candidate_url: str
    candidate_doc_type: str
    selected_content_sha256: None = None
    evidence_count: int = Field(ge=1)
    receipt: str


def _request_id(
    *,
    candidate_id: str,
    action: IrApprovalUiAction,
    current_revision: int,
    reason: str,
    owner_actor: str,
) -> str:
    payload = json.dumps(
        {
            "action": action.value,
            "candidate_id": candidate_id,
            "current_revision": current_revision,
            "owner_actor": owner_actor,
            "reason": reason,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"owner-ui:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _decision_action(action: IrApprovalUiAction) -> DecisionAction:
    if action is IrApprovalUiAction.APPROVE:
        return DecisionAction.APPROVE
    if action is IrApprovalUiAction.REJECT:
        return DecisionAction.REJECT
    raise IrExactSelectionUnavailableError(
        "Exact selection is unavailable until captured document bytes have a server-owned hash"
    )


def _receipt(
    result: DecisionWriteResult,
    candidate: IrCandidate,
    action: IrApprovalUiAction,
) -> IrApprovalActionReceipt:
    verb = "Approved" if action is IrApprovalUiAction.APPROVE else "Rejected"
    suffix = " (safe replay)" if result.outcome == "exact_replay" else ""
    return IrApprovalActionReceipt(
        outcome=result.outcome,
        candidate_id=candidate.candidate_id,
        action=action,
        revision=result.decision.revision,
        owner_actor=result.decision.owner_actor,
        decided_at=result.decision.decided_at,
        candidate_url=candidate.candidate_url,
        candidate_doc_type=candidate.doc_type.value,
        # There is no captured document-byte hash in schema 0009. The
        # observation_raw_sha256 field identifies the catalog observation and
        # is intentionally not exposed as selected content identity.
        selected_content_sha256=None,
        evidence_count=len(result.decision.evidence),
        receipt=f"{verb} {candidate.ticker} candidate at revision {result.decision.revision}{suffix}",
    )


def execute_ir_approval_action(
    db_path: Path,
    action_input: IrApprovalActionInput,
    *,
    owner_actor: str,
    now: Callable[[], datetime] | None = None,
) -> IrApprovalActionReceipt:
    """Append one policy-current owner decision in a serialized transaction."""

    validated = IrApprovalActionInput.model_validate(action_input.model_dump())
    actor = owner_actor.strip()
    if not actor:
        raise ValueError("owner_actor cannot be blank")
    decision_action = _decision_action(validated.action)
    clock = now or (lambda: datetime.now(UTC).replace(tzinfo=None))
    decided_at = clock()
    if decided_at.tzinfo is not None:
        decided_at = decided_at.astimezone(UTC).replace(tzinfo=None)

    connection = connect_sqlite(
        db_path,
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=True,
    )
    try:
        connection.execute("BEGIN IMMEDIATE")
        candidate = get_candidate(connection, validated.candidate_id)
        if candidate is None:
            raise IrApprovalActionUnauthorizedError("IR approval candidate does not exist")
        # Replay is a present-tense authorization claim too. Recheck policy
        # before both the no-write replay branch and the append branch.
        authorize_current_candidate(candidate)
        current = get_current_decision(connection, validated.candidate_id)
        if (
            current is not None
            and current.action is decision_action
            and current.owner_actor == actor
            and current.reason == validated.reason
            and current.selected_content_sha256 is None
        ):
            connection.commit()
            return _receipt(
                DecisionWriteResult(outcome="exact_replay", decision=current),
                candidate,
                validated.action,
            )
        current_revision = 0 if current is None else current.revision
        request = IrDecisionRequest(
            request_id=_request_id(
                candidate_id=candidate.candidate_id,
                action=validated.action,
                current_revision=current_revision,
                reason=validated.reason,
                owner_actor=actor,
            ),
            candidate_id=candidate.candidate_id,
            action=decision_action,
            expected_revision=current_revision,
            owner_actor=actor,
            decided_at=decided_at,
            reason=validated.reason,
            evidence=candidate.evidence,
        )
        result = append_decision(connection, request)
        connection.commit()
        return _receipt(result, candidate, validated.action)
    except IrApprovalConflictError as exc:
        connection.rollback()
        raise IrApprovalActionConflictError(str(exc)) from exc
    except IrAuthorizationError as exc:
        connection.rollback()
        raise IrApprovalActionUnauthorizedError(str(exc)) from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


__all__ = [
    "IrApprovalActionConflictError",
    "IrApprovalActionError",
    "IrApprovalActionInput",
    "IrApprovalActionReceipt",
    "IrApprovalActionUnauthorizedError",
    "IrApprovalUiAction",
    "IrExactSelectionUnavailableError",
    "execute_ir_approval_action",
]
