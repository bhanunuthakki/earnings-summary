"""Local-only, CSRF-guarded owner routes for the IR approval review queue."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from flask import Flask, request
from pydantic import ValidationError

from pipeline.ir_approval_actions import (
    IrApprovalActionConflictError,
    IrApprovalActionInput,
    IrApprovalActionUnauthorizedError,
    IrExactSelectionUnavailableError,
    execute_ir_approval_action,
)
from pipeline.ir_approval_panel import read_ir_approval_review, render_ir_approval_panel


@dataclass(frozen=True)
class IrApprovalRouteContext:
    db_path: Path
    owner_actor: str


def register_ir_approval_routes(app: Flask, context: IrApprovalRouteContext) -> None:
    """Register owner-action APIs; the app's global guards enforce local/CSRF scope."""

    @app.route(
        "/api/ir-approval/candidates/<candidate_id>/<action>",
        methods=["POST", "OPTIONS"],
    )
    def ir_approval_action(candidate_id: str, action: str):
        if request.method == "OPTIONS":
            return ("", 204)
        raw = request.get_json(silent=True)
        if not isinstance(raw, dict):
            return ({"ok": False, "code": "invalid_request", "error": "JSON body required"}, 400)
        body = cast("dict[str, object]", raw)
        unexpected = sorted(str(key) for key in body if key != "reason")
        if unexpected:
            return (
                {
                    "ok": False,
                    "code": "server_owned_fields",
                    "error": "Only reason may be supplied by the browser",
                },
                400,
            )
        try:
            action_input = IrApprovalActionInput.model_validate(
                {
                    "candidate_id": candidate_id,
                    "action": action,
                    "reason": body.get("reason"),
                }
            )
        except ValidationError:
            return (
                {
                    "ok": False,
                    "code": "invalid_request",
                    "error": "Candidate, action, and non-blank reason are required",
                },
                400,
            )
        try:
            receipt = execute_ir_approval_action(
                context.db_path,
                action_input,
                owner_actor=context.owner_actor,
            )
        except IrExactSelectionUnavailableError as exc:
            return (
                {
                    "ok": False,
                    "code": "selection_bytes_unavailable",
                    "error": str(exc),
                },
                409,
            )
        except IrApprovalActionConflictError as exc:
            return (
                {"ok": False, "code": "revision_conflict", "error": str(exc)},
                409,
            )
        except IrApprovalActionUnauthorizedError as exc:
            return (
                {"ok": False, "code": "policy_refused", "error": str(exc)},
                409,
            )
        except (OSError, sqlite3.Error):
            app.logger.error("IR approval action failed at the local database boundary")
            return (
                {
                    "ok": False,
                    "code": "store_unavailable",
                    "error": "Approval store unavailable; no decision was recorded",
                },
                503,
            )

        panel_html = render_ir_approval_panel(read_ir_approval_review(context.db_path))
        return (
            {
                "ok": True,
                "outcome": receipt.outcome,
                "revision": receipt.revision,
                "receipt": receipt.receipt,
                "panel_html": panel_html,
            },
            200,
        )


__all__ = ["IrApprovalRouteContext", "register_ir_approval_routes"]
