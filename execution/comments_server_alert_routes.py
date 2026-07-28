"""Alert-feed and queued-action routes for the local comments server."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from flask import Flask, Response, redirect, request


@dataclass(frozen=True)
class AlertRouteContext:
    db_path: Path
    default_user_id: str
    referer_back_path: Callable[[str], str | None]
    approve_consequence_href: Callable[[str], str | None]


def register_alert_routes(app: Flask, context: AlertRouteContext) -> None:
    """Register alert reads and actions directly on ``app``."""
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

    @app.route("/digest", methods=["GET"])
    def digest_page():
        return redirect("/#home")

    @app.route("/feed", methods=["GET"])
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

    @app.route("/alerts", methods=["GET"])
    def alerts_page():
        query_string = request.query_string.decode()
        return redirect("/feed" + (f"?{query_string}" if query_string else ""))

    @app.route("/approve", methods=["GET", "POST"])
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
        referer = request.headers.get("Referer", "")
        back = referer_back_path(referer)
        if request.headers.get("Sec-Fetch-Site", "") == "cross-site" or (referer and back is None):
            return ({"error": "cross-site approve/dismiss rejected"}, 403)
        dismissed = request.values.get("dismiss") in ("1", "true", "True")
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
        if request.method == "POST":
            from alerts import ACTION_STATUS_APPLIED, ACTION_STATUS_CANCELLED

            status = ACTION_STATUS_CANCELLED if dismissed else ACTION_STATUS_APPLIED
            if alert_id is not None:
                return {"ok": True, "alert_id": alert_id, "status": status}
            return {"ok": True, "action_id": action_id, "status": status}
        return redirect(back or "/feed", code=303)

    @app.route("/api/alerts/<int:alert_id>/dismiss", methods=["POST", "OPTIONS"])
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

    @app.route("/api/actions/<int:action_id>/uncancel", methods=["POST", "OPTIONS"])
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
