"""Research proposal and governed-decision routes for the local comments server.

This module owns the proposal verb family plus the governed Ask detail and
decision endpoints. Task lifecycle routes live in
``comments_server_research_routes.py``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from comments_server_route_support import ActivationCounter, RedactedFailureLogger
from flask import Flask, request
from pydantic import ValidationError

from logging_config import get_correlation_id
from research.proposal_approval import (
    AskProposalDecisionV1,
    ProposalConflictError,
    StoredProposalError,
    TargetDriftError,
    decide_ask_proposal,
    get_ask_proposal_detail,
)
from run_lock import RunLockHeldError


@dataclass(frozen=True, slots=True)
class ResearchProposalRouteContext:
    """Explicit dependencies for the research proposal routes."""

    repo_root: Path
    db_path: Path
    bump_activation_count: ActivationCounter
    log_redacted_failure: RedactedFailureLogger


def _ask_proposal_error(
    code: str,
    message: str,
    *,
    proposal_id: int,
    status: int,
    **details: object,
) -> tuple[dict[str, object], int]:
    error: dict[str, object] = {
        "code": code,
        "message": message,
        "proposal_id": proposal_id,
    }
    error.update({key: value for key, value in details.items() if value is not None})
    return ({"schema_version": "ask_proposal_error.v1", "error": error}, status)


def register_research_proposal_routes(app: Flask, context: ResearchProposalRouteContext) -> None:
    """Register the research proposal verbs and governed Ask APIs on ``app``."""

    db_path = context.db_path
    repo_root = context.repo_root
    bump_activation_count = context.bump_activation_count
    log_redacted_failure = context.log_redacted_failure

    @app.route("/api/research/proposal/<int:proposal_id>/<verb>", methods=["POST", "OPTIONS"])
    def research_proposal_act(proposal_id: int, verb: str):
        """Approve, further, steer, or reject a research proposal."""
        if request.method == "OPTIONS":
            return ("", 204)
        from research.proposals import PROPOSAL_VERBS, act_on_proposal, get_proposal

        if verb not in PROPOSAL_VERBS:
            return ({"error": f"unknown verb {verb!r}"}, 400)
        proposal = get_proposal(proposal_id, db_path=db_path)
        if proposal is not None and proposal.canonical_content_json is not None:
            return (
                {
                    "error": "governed Ask proposals require the revisioned decision endpoint",
                    "detail_url": f"/api/research/proposals/{proposal_id}",
                    "decision_url": f"/api/research/proposals/{proposal_id}/decision",
                },
                409,
            )
        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        steer_text = str(payload.get("steer_text") or "").strip() or None
        bump_activation_count(f"act:proposal:{verb}")
        if verb == "approve" and proposal is not None and proposal.kind == "question":
            from research.question_artifact import approve_question_proposal
            from user_state.notes import NoteRevisionConflictError

            try:
                note = approve_question_proposal(proposal_id, db_path=db_path)
            except NoteRevisionConflictError as exc:
                return (
                    {"error": "revision_conflict", "current_revision": exc.current_revision},
                    409,
                )
            except LookupError as exc:
                return ({"error": str(exc)}, 404)
            except ValueError as exc:
                return ({"error": str(exc)}, 400)
            applied = f"open question #{note.id} persisted for {note.ticker or 'portfolio'}"
            return {
                "status": "approved",
                "applied": applied,
                "receipt": f"Approved — {applied}",
            }
        status = act_on_proposal(proposal_id, verb, steer_text=steer_text, db_path=db_path)
        applied = ""
        apply_failed = False
        if verb == "approve":
            from research.apply import apply_approved_proposal

            try:
                applied = apply_approved_proposal(proposal_id, db_path=db_path)
            except Exception as exc:  # a bad apply must not 500 the action
                apply_failed = True
                log_redacted_failure(
                    f"research proposal apply failed for proposal {proposal_id}",
                    exc,
                )
        receipts = {
            "approved": f"Approved — {applied}" if applied else "Approved — marked for follow-up",
            "researching": "Sent back for deeper research",
            "steered": "Steered — your direction was recorded",
            "rejected": "Rejected — this proposal won't be revisited",
        }
        receipt = receipts.get(status, "Saved")
        response: dict[str, object] = {"status": status, "applied": applied, "receipt": receipt}
        if apply_failed:
            response.update(
                {
                    "apply_error": "approved proposal could not be applied; retry the request",
                    "correlation_id": get_correlation_id(),
                }
            )
        return response

    @app.route("/api/research/proposals/<int:proposal_id>", methods=["GET"])
    def ask_proposal_detail(proposal_id: int):
        try:
            detail = get_ask_proposal_detail(proposal_id, db_path=db_path)
        except StoredProposalError as exc:
            log_redacted_failure("governed Ask proposal detail invalid", exc)
            return _ask_proposal_error(
                "stored_proposal_invalid",
                "proposal data is unavailable",
                proposal_id=proposal_id,
                status=500,
            )
        if detail is None:
            return _ask_proposal_error(
                "proposal_not_found",
                "governed proposal was not found",
                proposal_id=proposal_id,
                status=404,
            )
        return detail.model_dump(mode="json")

    @app.route(
        "/api/research/proposals/<int:proposal_id>/decision",
        methods=["POST", "OPTIONS"],
    )
    def ask_proposal_decision(proposal_id: int):
        if request.method == "OPTIONS":
            return ("", 204)
        if request.headers.get("Sec-Fetch-Site", "") == "cross-site":
            return _ask_proposal_error(
                "cross_site_rejected",
                "cross-site proposal decisions are not allowed",
                proposal_id=proposal_id,
                status=403,
            )
        try:
            decision = AskProposalDecisionV1.model_validate(request.get_json(silent=True))
        except ValidationError:
            return _ask_proposal_error(
                "invalid_request",
                "decision payload does not match ask_proposal_decision.v1",
                proposal_id=proposal_id,
                status=400,
            )
        if decision.proposal_id != proposal_id:
            return _ask_proposal_error(
                "proposal_id_mismatch",
                "path and payload proposal_id must match",
                proposal_id=proposal_id,
                status=400,
            )
        try:
            receipt = decide_ask_proposal(
                decision,
                repo_root=repo_root,
                db_path=db_path,
            )
        except ProposalConflictError as exc:
            return _ask_proposal_error(
                exc.code,
                str(exc),
                proposal_id=proposal_id,
                status=409,
                current_proposal_revision=exc.current_proposal_revision,
                current_status=exc.current_status,
            )
        except TargetDriftError as exc:
            return _ask_proposal_error(
                "target_drift",
                "proposal target changed after the proposal was created",
                proposal_id=proposal_id,
                status=412,
                expected_target_sha256=exc.expected_target_sha256,
                actual_target_sha256=exc.actual_target_sha256,
            )
        except RunLockHeldError:
            return _ask_proposal_error(
                "mutation_busy",
                "another portfolio mutation is in progress; retry the decision",
                proposal_id=proposal_id,
                status=409,
            )
        except (StoredProposalError, ValueError, OSError, sqlite3.Error) as exc:
            log_redacted_failure("governed Ask proposal decision failed", exc)
            return _ask_proposal_error(
                "decision_failed",
                "proposal decision could not be completed",
                proposal_id=proposal_id,
                status=500,
            )
        bump_activation_count(f"act:ask_proposal:{decision.decision}")
        return receipt.model_dump(mode="json")


__all__ = ["ResearchProposalRouteContext", "register_research_proposal_routes"]
