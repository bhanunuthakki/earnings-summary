"""Decision draft routes backed by the shared capture action cores."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from flask import Flask, request


def register_decision_draft_routes(app: Flask, db_path: Path) -> None:
    """Keep global access/cache guards and late-bound action dependencies on the app."""

    @app.route("/api/decision-drafts", methods=["GET"])
    def decision_drafts_api():
        """Pending Decision Drafts for the Inbox (PRD §11.6). Thin — the
        typed read lives in ``capture.decision_draft.list_pending_drafts``."""
        from capture.decision_draft import list_pending_drafts

        drafts = list_pending_drafts(db_path=db_path)
        return {
            "drafts": [
                {
                    "id": d.id,
                    "source_channel": d.source_channel,
                    "status": d.status,
                    "original_text": d.original_text,
                    "draft": d.draft.model_dump() if d.draft is not None else None,
                    "parse_confidence": d.parse_confidence,
                    "created_at": d.created_at,
                }
                for d in drafts
            ]
        }

    @app.route("/api/decision-drafts/<int:draft_id>/confirm", methods=["POST", "OPTIONS"])
    def decision_draft_confirm_api(draft_id: int):
        """Confirm a draft and create/link one Owner Decision idempotently —
        thin wrapper over ``capture.decision_draft_actions.confirm_draft``
        (the SAME action core Telegram callbacks and the mobile Inbox call)."""
        if request.method == "OPTIONS":
            return ("", 204)
        from capture.decision_draft_actions import DraftActionError, confirm_draft

        try:
            result = confirm_draft(draft_id, db_path=db_path)
        except DraftActionError as exc:
            return ({"error": str(exc)}, 400)
        return result

    @app.route("/api/decision-drafts/<int:draft_id>/correct", methods=["POST", "OPTIONS"])
    def decision_draft_correct_api(draft_id: int):
        """Validate owner-supplied corrected fields, then apply the same
        resolution ``confirm`` uses — ``capture.decision_draft_actions.
        correct_draft``."""
        if request.method == "OPTIONS":
            return ("", 204)
        from capture.decision_draft_actions import DraftActionError, correct_draft

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        try:
            result = correct_draft(draft_id, payload, db_path=db_path)
        except DraftActionError as exc:
            return ({"error": str(exc)}, 400)
        return result

    @app.route("/api/decision-drafts/<int:draft_id>/dismiss", methods=["POST", "OPTIONS"])
    def decision_draft_dismiss_api(draft_id: int):
        """Dismiss the draft without deleting the raw capture."""
        if request.method == "OPTIONS":
            return ("", 204)
        from capture.decision_draft_actions import DraftActionError, dismiss_draft

        try:
            result = dismiss_draft(draft_id, db_path=db_path)
        except DraftActionError as exc:
            return ({"error": str(exc)}, 400)
        return result

    @app.route("/api/decision-draft-groups/<int:draft_id>/confirm", methods=["POST", "OPTIONS"])
    def decision_draft_group_confirm_api(draft_id: int):
        """Confirm one tracker trade group as one aggregated Owner Decision."""
        if request.method == "OPTIONS":
            return ("", 204)
        from capture.decision_draft_actions import (
            DraftActionError,
            confirm_tracker_fill_group,
        )

        try:
            result = confirm_tracker_fill_group(draft_id, db_path=db_path)
        except DraftActionError as exc:
            return ({"error": str(exc)}, 400)
        return result

    @app.route("/api/decision-draft-groups/<int:draft_id>/dismiss", methods=["POST", "OPTIONS"])
    def decision_draft_group_dismiss_api(draft_id: int):
        """Dismiss every pending fill in one tracker group without deleting evidence."""
        if request.method == "OPTIONS":
            return ("", 204)
        from capture.decision_draft_actions import (
            DraftActionError,
            dismiss_tracker_fill_group,
        )

        try:
            result = dismiss_tracker_fill_group(draft_id, db_path=db_path)
        except DraftActionError as exc:
            return ({"error": str(exc)}, 400)
        return result

    @app.route("/api/decision-draft-groups/<int:draft_id>/correct", methods=["POST", "OPTIONS"])
    def decision_draft_group_correct_api(draft_id: int):
        """Correct one tracker group and its shared Owner Decision atomically."""
        if request.method == "OPTIONS":
            return ("", 204)
        from capture.decision_draft_actions import (
            DraftActionError,
            correct_tracker_fill_group,
        )

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        try:
            result = correct_tracker_fill_group(draft_id, payload, db_path=db_path)
        except DraftActionError as exc:
            return ({"error": str(exc)}, 400)
        return result
