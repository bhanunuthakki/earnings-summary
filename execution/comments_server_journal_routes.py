"""Analyst-journal note routes for the local comments server."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, cast

from flask import Flask, Response, request

import comments


@dataclass(frozen=True)
class JournalRouteContext:
    repo_root: Path
    db_path: Path
    default_user_id: str
    note_to_json: Callable[[object], dict[str, object]]
    optional_int: Callable[[object], int | None]
    bump_activation_count: Callable[[str], None]


def register_journal_routes(app: Flask, context: JournalRouteContext) -> None:
    """Register analyst-note capture and lifecycle routes directly on ``app``."""
    db_path = context.db_path

    @app.route("/api/work-os/question-proposals", methods=["POST", "OPTIONS"])
    def question_proposals_api():
        """Draft one inert thesis/engagement-derived open question."""

        if request.method == "OPTIONS":
            return ("", 204)
        from research.question_artifact import draft_question_proposal
        from ticker_validation import safe_ticker

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        try:
            ticker = safe_ticker(str(payload.get("ticker") or ""))
        except ValueError as exc:
            return ({"error": str(exc)}, 400)
        body = str(payload.get("body") or "").strip()
        raw_origin = str(payload.get("origin") or "engagement")
        if raw_origin not in {"thesis", "engagement", "model"}:
            return ({"error": "origin must be thesis, engagement, or model"}, 400)
        origin = cast("Literal['thesis', 'engagement', 'model']", raw_origin)
        try:
            proposal_id = draft_question_proposal(
                ticker=ticker,
                body=body,
                origin=origin,
                evidence_ref=(
                    str(payload["evidence_ref"]) if payload.get("evidence_ref") else None
                ),
                supersedes_note_id=context.optional_int(payload.get("supersedes_note_id")),
                expected_revision=(
                    str(payload["expected_revision"])
                    if payload.get("expected_revision") is not None
                    else None
                ),
                db_path=db_path,
            )
        except ValueError as exc:
            return ({"error": str(exc)}, 400)
        return ({"proposal_id": proposal_id, "status": "pending", "note": None}, 201)

    @app.route(
        "/api/work-os/question-proposals/<int:proposal_id>/approve",
        methods=["POST", "OPTIONS"],
    )
    def approve_question_proposal_api(proposal_id: int):
        """Apply an explicit owner approval to the durable question store."""

        if request.method == "OPTIONS":
            return ("", 204)
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
        return {
            "proposal_id": proposal_id,
            "status": "approved",
            "note": context.note_to_json(note),
        }

    @app.route("/api/notes", methods=["GET", "POST", "OPTIONS"])
    def notes_api():
        if request.method == "OPTIONS":
            return ("", 204)
        from user_state import notes as notes_store

        if request.method == "GET":
            query_ticker = (request.args.get("ticker") or "").strip().upper() or None
            query_kind = (request.args.get("kind") or "").strip() or None
            query_status = (request.args.get("status") or "").strip() or None
            try:
                rows = notes_store.list_notes(
                    user_id=context.default_user_id,
                    ticker=query_ticker,
                    kind=query_kind,
                    status=query_status,
                    db_path=db_path,
                )
            except ValueError as exc:
                return ({"error": str(exc)}, 400)
            return {
                "notes": [context.note_to_json(note) for note in rows],
            }

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        note_body = str(payload.get("body") or "").strip()
        if not note_body:
            return ({"error": "body required"}, 400)
        kind = str(payload.get("kind") or "observation")
        ticker_raw = payload.get("ticker")
        note_ticker = str(ticker_raw).strip().upper() or None if ticker_raw is not None else None
        anchor_type_raw = payload.get("anchor_type")
        anchor_key_raw = payload.get("anchor_key")
        fact_ref_raw = payload.get("fact_ref")
        context_raw = payload.get("context")
        link_decision_id = context.optional_int(payload.get("decision_id"))
        link_position_id = context.optional_int(payload.get("position_entry_id"))
        if link_decision_id is not None or link_position_id is not None:
            from journal_links import get_target

            for target_kind, target_id in (
                ("decision", link_decision_id),
                ("position", link_position_id),
            ):
                if target_id is not None and (
                    get_target(
                        kind=target_kind,
                        target_id=target_id,
                        db_path=db_path,
                    )
                    is None
                ):
                    return (
                        {"error": f"{target_kind} id={target_id} not found"},
                        404,
                    )
        try:
            created = notes_store.create_note(
                user_id=context.default_user_id,
                ticker=note_ticker,
                kind=kind,
                body=note_body,
                anchor_type=(str(anchor_type_raw) if anchor_type_raw is not None else None),
                anchor_key=(str(anchor_key_raw) if anchor_key_raw is not None else None),
                fact_ref=(str(fact_ref_raw) if fact_ref_raw is not None else None),
                source="manual",
                context=(
                    cast("dict[str, object]", context_raw)
                    if isinstance(context_raw, dict)
                    else None
                ),
                decision_id=link_decision_id,
                position_entry_id=link_position_id,
                link_auto_resolve=bool(payload.get("auto_resolve")),
                db_path=db_path,
            )
        except ValueError as exc:
            return ({"error": str(exc)}, 400)
        return ({"note": context.note_to_json(created)}, 201)

    @app.route("/api/notes/<int:note_id>/<action>", methods=["POST", "OPTIONS"])
    def notes_action_api(note_id: int, action: str):
        if request.method == "OPTIONS":
            return ("", 204)
        from user_state import notes as notes_store

        context.bump_activation_count(f"act:note:{action}")
        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        route_intent = ""
        try:
            if action == "resolve":
                resolution_raw = payload.get("resolution_note")
                updated = notes_store.resolve_note(
                    note_id,
                    resolution_note=(
                        str(resolution_raw).strip() or None if resolution_raw is not None else None
                    ),
                    db_path=db_path,
                )
            elif action == "archive":
                updated = notes_store.archive_note(note_id, db_path=db_path)
            elif action == "unarchive":
                updated = notes_store.unarchive_note(note_id, db_path=db_path)
            elif action == "set_ticker":
                ticker = str(payload.get("ticker") or "").strip()
                if not ticker:
                    return ({"error": "ticker required"}, 400)
                updated = notes_store.set_ticker(
                    note_id,
                    ticker=ticker,
                    db_path=db_path,
                )
            elif action == "reclassify":
                updated = notes_store.reclassify_note(
                    note_id,
                    kind=str(payload.get("kind") or ""),
                    db_path=db_path,
                )
            elif action == "supersede":
                new_body = str(payload.get("body") or "").strip()
                if not new_body:
                    return ({"error": "body required for supersede"}, 400)
                kind_raw = payload.get("kind")
                updated = notes_store.supersede_note(
                    note_id,
                    body=new_body,
                    kind=str(kind_raw) if kind_raw is not None else None,
                    expected_revision=(
                        str(payload["expected_revision"])
                        if payload.get("expected_revision") is not None
                        else None
                    ),
                    db_path=db_path,
                )
            elif action == "link":
                from journal_links import link_note

                updated = link_note(
                    note_id,
                    decision_id=context.optional_int(
                        payload.get("decision_id"),
                    ),
                    position_entry_id=context.optional_int(
                        payload.get("position_entry_id"),
                    ),
                    auto_resolve=bool(payload.get("auto_resolve")),
                    db_path=db_path,
                )
            elif action == "unlink":
                from journal_links import unlink_note

                updated = unlink_note(note_id, db_path=db_path)
            elif action == "route":
                route_intent = str(payload.get("intent") or "").strip()
                updated = notes_store.route_triage_note(
                    note_id,
                    intent=route_intent,
                    db_path=db_path,
                )
                if updated is not None and updated.source_ref:
                    parts = updated.source_ref.split("/")
                    if len(parts) == 3:
                        with contextlib.suppress(Exception):
                            comments.update_comment(
                                context.repo_root,
                                parts[0],
                                date.fromisoformat(parts[1]),
                                parts[2],
                                intent=cast(
                                    "comments.IntentType",
                                    route_intent,
                                ),
                            )
            else:
                return ({"error": f"unknown action {action!r}"}, 404)
        except notes_store.NoteRevisionConflictError as exc:
            return (
                {
                    "error": "revision_conflict",
                    "current_revision": exc.current_revision,
                },
                409,
            )
        except ValueError as exc:
            return ({"error": str(exc)}, 400)
        except LookupError as exc:
            return ({"error": str(exc)}, 404)
        if updated is None:
            return ({"error": f"note {note_id} not found"}, 404)
        if request.headers.get("HX-Request") and action in (
            "archive",
            "unarchive",
        ):
            from dashboard.inbox import acted_span, restored_note_button

            if action == "archive":
                return Response(
                    acted_span(
                        "✕ archived",
                        "archived",
                        undo_url=f"/api/notes/{note_id}/unarchive",
                    ),
                    mimetype="text/html",
                )
            return Response(
                restored_note_button(note_id),
                mimetype="text/html",
            )
        result: dict[str, object] = {
            "note": context.note_to_json(updated),
        }
        if action == "route":
            from pipeline import triage_panel

            raw_intent_labels: object = vars(triage_panel).get("_INTENT_LABELS")
            intent_labels = (
                cast("dict[str, str]", raw_intent_labels)
                if isinstance(raw_intent_labels, dict)
                else {}
            )

            result["receipt"] = (
                f"Routed to {intent_labels.get(route_intent, route_intent) or 'the suggested category'}"
            )
        elif action == "archive":
            result["receipt"] = "Dismissed"
        return result
