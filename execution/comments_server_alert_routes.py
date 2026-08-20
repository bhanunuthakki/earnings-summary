"""Alert-feed and queued-action routes for the local comments server."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import cast

from flask import Blueprint, Flask, Response, redirect, request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ui.controls import controls_css
from ui.tokens import FAVICON_LINK, palette_css


@dataclass(frozen=True)
class AppContext:
    """Explicit dependencies available to the alert-route module."""

    db_path: Path
    default_user_id: str
    referer_back_path: Callable[[str], str | None]
    approve_consequence_href: Callable[[str], str | None]


class _AcknowledgePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=1000)
    next_review_at: datetime | None = None

    @field_validator("note")
    @classmethod
    def _normalize_note(cls, value: str | None) -> str | None:
        return None if value is None else value.strip() or None

    @field_validator("next_review_at")
    @classmethod
    def _require_aware_due(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("next_review_at must include a timezone")
        return value


def create_alert_blueprint(context: AppContext) -> Blueprint:
    """Build the alert route cluster without closing over the Flask app."""
    from approve_queued_action import (
        approve_alert_and_apply_all,
        approve_and_apply,
        dismiss_action,
        dismiss_alert_and_cancel_actions,
    )

    from dashboard import render_alert_feed

    db_path = context.db_path
    referer_back_path = context.referer_back_path
    approve_consequence_href = context.approve_consequence_href
    blueprint = Blueprint("alerts", __name__)

    @blueprint.route("/digest", methods=["GET"])
    def digest_page():
        return redirect("/#home")

    @blueprint.route("/feed", methods=["GET"])
    def feed_page():
        try:
            limit = int(request.args.get("limit", "200"))
        except ValueError:
            limit = 200
        html_text = render_alert_feed(
            user_id=context.default_user_id,
            ticker=request.args.get("ticker"),
            trigger_kind=request.args.get("trigger_kind"),
            status=request.args.get("status"),
            limit=limit,
            db_path=db_path,
        )
        return Response(html_text, mimetype="text/html")

    @blueprint.route("/alerts", methods=["GET"])
    def alerts_page():
        query_string = request.query_string.decode()
        return redirect("/feed" + (f"?{query_string}" if query_string else ""))

    @blueprint.route("/approve", methods=["GET", "POST"])
    def approve_or_dismiss_action():
        raw_alert_id = request.values.get("alert_id", "")
        raw_action_id = request.values.get("action_id", "")
        alert_id: int | None = None
        action_id: int | None = None
        if raw_alert_id:
            try:
                alert_id = int(raw_alert_id)
            except ValueError:
                return (
                    {"error": f"alert_id must be an integer, got {raw_alert_id!r}"},
                    400,
                )
        else:
            try:
                action_id = int(raw_action_id)
            except ValueError:
                return (
                    {"error": f"action_id must be an integer, got {raw_action_id!r}"},
                    400,
                )
        dismissed = request.values.get("dismiss") in ("1", "true", "True")
        referer = request.headers.get("Referer", "")
        referer_back = referer_back_path(referer)
        supplied_back = referer_back_path(str(request.values.get("return_to") or ""))
        back = supplied_back or referer_back or "/feed"

        if request.method == "GET":
            target_name = "alert" if alert_id is not None else "queued action"
            target_id = alert_id if alert_id is not None else action_id
            verb = "Dismiss" if dismissed else "Apply"
            id_name = "alert_id" if alert_id is not None else "action_id"
            dismiss_input = '<input type="hidden" name="dismiss" value="1">' if dismissed else ""
            body = (
                '<!doctype html><html lang="en" data-theme="dark"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                f"<title>Confirm {verb.lower()}</title>{FAVICON_LINK}"
                f"<style>{palette_css('dark')}{controls_css('dark')}</style></head><body>"
                '<main class="k-well"><h2 class="k-card-title">Confirm action</h2>'
                f"<p>{verb} {escape(target_name)} #{target_id}?</p>"
                '<form method="post" action="/approve">'
                f'<input type="hidden" name="{id_name}" value="{target_id}">'
                '<input type="hidden" name="confirm" value="1">'
                f'<input type="hidden" name="return_to" value="{escape(back, quote=True)}">'
                f"{dismiss_input}"
                f'<button class="k-btn k-btn-primary" type="submit">{verb}</button> '
                f'<a class="k-btn k-btn-quiet" href="{escape(back, quote=True)}">Cancel</a>'
                "</form></main></body></html>"
            )
            response = Response(body, mimetype="text/html")
            response.headers["Cache-Control"] = "no-store"
            return response

        if request.headers.get("Sec-Fetch-Site", "") == "cross-site" or (
            referer and referer_back is None
        ):
            return ({"error": "cross-site approve/dismiss rejected"}, 403)
        consequence = ""
        try:
            if alert_id is not None:
                consequence = (
                    dismiss_alert_and_cancel_actions(alert_id, db_path=db_path)
                    if dismissed
                    else approve_alert_and_apply_all(alert_id, db_path=db_path)
                )
            else:
                assert action_id is not None
                if dismissed:
                    dismiss_action(action_id, db_path=db_path)
                else:
                    consequence = approve_and_apply(action_id, db_path=db_path)
        except LookupError as exc:
            return ({"error": str(exc)}, 404)
        except (ValueError, KeyError) as exc:
            return ({"error": str(exc)}, 409)
        if request.headers.get("HX-Request"):
            from dashboard.inbox import acted_span

            if dismissed:
                undo = f"/api/actions/{action_id}/uncancel" if action_id is not None else None
                return Response(
                    acted_span(
                        "✕ dismissed",
                        "cancelled",
                        undo_url=undo,
                        detail=consequence,
                    ),
                    mimetype="text/html",
                )
            return Response(
                acted_span(
                    "✓ applied",
                    "applied",
                    detail=consequence,
                    detail_href=approve_consequence_href(consequence),
                ),
                mimetype="text/html",
            )
        if request.values.get("confirm") == "1":
            return redirect(back, code=303)
        from alerts import ACTION_STATUS_APPLIED, ACTION_STATUS_CANCELLED

        status = ACTION_STATUS_CANCELLED if dismissed else ACTION_STATUS_APPLIED
        if alert_id is not None:
            return {"ok": True, "alert_id": alert_id, "status": status}
        return {"ok": True, "action_id": action_id, "status": status}

    @blueprint.route("/api/alerts/<int:alert_id>/dismiss", methods=["POST", "OPTIONS"])
    def dismiss_alert_api(alert_id: int):
        if request.method == "OPTIONS":
            return ("", 204)
        if request.headers.get("Sec-Fetch-Site", "") == "cross-site":
            return ({"error": "cross-site dismiss rejected"}, 403)
        from alerts import (
            ACTION_STATUS_PENDING,
            ALERT_STATUS_DISMISSED,
            cancel_action,
            dismiss_alert,
            get_alert,
            list_queued_actions_for_alert,
            set_alert_dismiss_reason,
        )

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        reason = str(payload.get("reason") or request.values.get("reason") or "").strip() or None
        try:
            current = get_alert(alert_id, db_path=db_path)
        except LookupError as exc:
            return ({"error": str(exc)}, 404)
        if current.status == ALERT_STATUS_DISMISSED and reason is not None:
            try:
                dismissed_alert = set_alert_dismiss_reason(
                    alert_id,
                    reason,
                    db_path=db_path,
                )
            except LookupError as exc:
                return ({"error": str(exc)}, 404)
            except ValueError as exc:
                return ({"error": str(exc)}, 409)
            if request.headers.get("HX-Request"):
                return Response("", mimetype="text/html")
            return {
                "ok": True,
                "alert_id": dismissed_alert.id,
                "status": dismissed_alert.status,
                "dismiss_reason": dismissed_alert.dismiss_reason,
                "cancelled_actions": 0,
            }
        try:
            cancelled = 0
            for queued_action in list_queued_actions_for_alert(
                alert_id,
                db_path=db_path,
            ):
                if queued_action.status == ACTION_STATUS_PENDING:
                    cancel_action(queued_action.id, db_path=db_path)
                    cancelled += 1
            dismissed_alert = dismiss_alert(
                alert_id,
                db_path=db_path,
                reason=reason,
            )
        except LookupError as exc:
            return ({"error": str(exc)}, 404)
        except (ValueError, KeyError) as exc:
            return ({"error": str(exc)}, 409)
        if request.headers.get("HX-Request"):
            from dashboard.inbox import acted_span

            if dismissed_alert.dismiss_reason:
                return Response(
                    acted_span(
                        "✕ dismissed",
                        "cancelled",
                        detail=dismissed_alert.dismiss_reason,
                    ),
                    mimetype="text/html",
                )
            return Response(
                acted_span(
                    "✕ dismissed",
                    "cancelled",
                    dismiss_why_id=alert_id,
                ),
                mimetype="text/html",
            )
        return {
            "ok": True,
            "alert_id": dismissed_alert.id,
            "status": dismissed_alert.status,
            "dismiss_reason": dismissed_alert.dismiss_reason,
            "cancelled_actions": cancelled,
        }

    @blueprint.route(
        "/api/thesis-episodes/<episode_id>/acknowledge",
        methods=["POST", "OPTIONS"],
    )
    def acknowledge_thesis_episode_api(episode_id: str):
        if request.method == "OPTIONS":
            return ("", 204)
        referer = request.headers.get("Referer", "")
        if request.headers.get("Sec-Fetch-Site", "") == "cross-site" or (
            referer and referer_back_path(referer) is None
        ):
            return ({"error": "cross-site acknowledgement rejected"}, 403)

        from compute.thesis_episode_attention import (
            AttentionError,
            acknowledge_episode,
        )
        from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

        raw_json = request.get_json(silent=True)
        if raw_json is not None and not isinstance(raw_json, dict):
            return ({"error": "acknowledgement payload must be a JSON object"}, 400)
        raw_payload: dict[str, object]
        if raw_json is None:
            raw_payload = {
                "note": request.values.get("note") or None,
                "next_review_at": request.values.get("next_review_at") or None,
            }
        else:
            raw_payload = cast("dict[str, object]", raw_json)
        try:
            payload = _AcknowledgePayload.model_validate(raw_payload)
        except ValidationError:
            return ({"error": "invalid acknowledgement payload"}, 400)
        connection = connect_sqlite(
            db_path,
            role=SQLiteConnectionRole.WRITER,
            schema_preflight=True,
        )
        try:
            attention = acknowledge_episode(
                connection,
                episode_id,
                acknowledged_at=datetime.now(UTC),
                note=payload.note,
                next_review_at=payload.next_review_at,
            )
            connection.commit()
        except AttentionError as exc:
            connection.rollback()
            status = 404 if "unknown thesis episode" in str(exc) else 409
            return ({"error": str(exc)}, status)
        finally:
            connection.close()
        return {
            "ok": True,
            "episode_id": attention.episode_id,
            "state": attention.state.value,
            "next_review_at": (
                None if attention.next_review_at is None else attention.next_review_at.isoformat()
            ),
        }

    @blueprint.route("/api/actions/<int:action_id>/uncancel", methods=["POST", "OPTIONS"])
    def uncancel_action_api(action_id: int):
        if request.method == "OPTIONS":
            return ("", 204)
        if request.headers.get("Sec-Fetch-Site", "") == "cross-site":
            return ({"error": "cross-site uncancel rejected"}, 403)
        from alerts import uncancel_action
        from dashboard.inbox import restored_action_buttons

        try:
            uncancel_action(action_id, db_path=db_path)
        except LookupError as exc:
            return ({"error": str(exc)}, 404)
        except (ValueError, KeyError) as exc:
            return ({"error": str(exc)}, 409)
        return Response(
            restored_action_buttons(action_id),
            mimetype="text/html",
        )

    return blueprint


def register_alert_routes(app: Flask, context: AppContext) -> None:
    """Register the isolated alert Blueprint on ``app``."""
    app.register_blueprint(create_alert_blueprint(context))
