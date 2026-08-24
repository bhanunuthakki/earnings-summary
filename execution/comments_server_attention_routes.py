"""Closed HTTP boundary for governed Operations attention actions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from flask import Flask, abort, request

from operations.attention import AttentionReason, AttentionReasonCode
from operations.attention_store import (
    OPERATOR_ACTIONS,
    ActionFailureCode,
    ActionResultState,
    OperatorAction,
    OperatorActionReceipt,
    OperatorActionRequest,
    execute_operator_action,
)


@dataclass(frozen=True, slots=True)
class AttentionRouteContext:
    db_path: Path
    owner_actor: str
    invalidate_operations_panel_cache: Callable[[], None]
    client_error: Callable[[str, int], tuple[dict[str, str], int]]


def _parse_payload(raw_body: object, *, action: OperatorAction) -> OperatorActionRequest:
    if not isinstance(raw_body, dict):
        raise ValueError("JSON request body must be an object")
    body = cast("dict[str, object]", raw_body)
    common_fields = {
        "finding_id",
        "evidence_fingerprint_sha256",
        "idempotency_key",
        "occurred_at",
    }
    action_fields: set[str] = (
        {"reason", "acknowledge_until"}
        if action is OperatorAction.ACKNOWLEDGE
        else {"reason", "snooze_until"}
        if action is OperatorAction.SNOOZE
        else set()
    )
    if set(body) != common_fields | action_fields:
        raise ValueError("attention action payload has invalid fields")

    def required_text(name: str) -> str:
        value = body.get(name)
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        return value

    def required_datetime(name: str) -> datetime:
        try:
            value = datetime.fromisoformat(required_text(name))
        except ValueError as error:
            raise ValueError(f"{name} must be an ISO-8601 datetime") from error
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
        return value

    reason: AttentionReason | None = None
    acknowledge_until: datetime | None = None
    snooze_until: datetime | None = None
    if action is not OperatorAction.RESOLVE:
        raw_reason = body.get("reason")
        if not isinstance(raw_reason, dict):
            raise ValueError("reason must contain code and reference_sha256 only")
        reason_fields = cast("dict[str, object]", raw_reason)
        if set(reason_fields) != {"code", "reference_sha256"}:
            raise ValueError("reason must contain code and reference_sha256 only")
        code = reason_fields.get("code")
        reference_sha256 = reason_fields.get("reference_sha256")
        if not isinstance(code, str) or not isinstance(reference_sha256, str):
            raise ValueError("reason fields must be strings")
        reason = AttentionReason(code=AttentionReasonCode(code), reference_sha256=reference_sha256)
        if action is OperatorAction.ACKNOWLEDGE:
            acknowledge_until = required_datetime("acknowledge_until")
        else:
            snooze_until = required_datetime("snooze_until")

    return OperatorActionRequest(
        finding_id=required_text("finding_id"),
        action=action,
        evidence_fingerprint_sha256=required_text("evidence_fingerprint_sha256"),
        idempotency_key=required_text("idempotency_key"),
        occurred_at=required_datetime("occurred_at"),
        reason=reason,
        acknowledge_until=acknowledge_until,
        snooze_until=snooze_until,
    )


def _receipt_payload(receipt: OperatorActionReceipt) -> dict[str, object]:
    return {
        "schema_version": "operations_attention_action_receipt.v1",
        "receipt": {
            "receipt_id": receipt.receipt_id,
            "finding_id": receipt.finding_id,
            "action": receipt.action.value,
            "result_lifecycle": receipt.result_lifecycle.value,
            "result_state": receipt.result_state.value,
            "request_sha256": receipt.request_sha256,
            "idempotency_key_sha256": receipt.idempotency_key_sha256,
            "lifecycle_event_id": receipt.lifecycle_event_id,
            "failure_code": None if receipt.failure_code is None else receipt.failure_code.value,
            "durable": receipt.durable,
        },
    }


def register_attention_routes(app: Flask, context: AttentionRouteContext) -> None:
    """Register only the three durable, operator-owned attention actions."""

    @app.route("/api/operations/attention/<action_name>", methods=["POST", "OPTIONS"])
    def operations_attention_action(action_name: str):
        if request.method == "OPTIONS":
            return ("", 204)
        try:
            action = OperatorAction(action_name)
        except ValueError:
            abort(404)
        if action not in OPERATOR_ACTIONS:
            abort(404)
        try:
            action_request = _parse_payload(request.get_json(silent=True), action=action)
        except ValueError as error:
            return context.client_error(str(error), 400)

        receipt = execute_operator_action(
            action_request,
            actor=context.owner_actor,
            db_path=context.db_path,
        )
        if receipt.result_state is ActionResultState.APPLIED:
            context.invalidate_operations_panel_cache()
        status = (
            200
            if receipt.result_state in {ActionResultState.APPLIED, ActionResultState.REPLAYED}
            else 409
            if receipt.result_state is ActionResultState.CONFLICT
            or receipt.failure_code is ActionFailureCode.CONFLICT
            else 422
        )
        return (_receipt_payload(receipt), status)
