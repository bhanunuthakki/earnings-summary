"""Local, evidence-bound HTTP boundary for governed alert lifecycle actions.

The mutation route deliberately owns its SQLite transaction.  It binds the
path alert id and server actor into the typed core request, so a browser cannot
retarget a valid evidence digest at another alert.  The adjacent evidence route
is intentionally a read-only receipt/evidence view; it is not a compatibility
surface for the legacy approve/dismiss workflow.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import cast

from flask import Flask, Response, abort, request
from pydantic import ValidationError

from alerts.governed_actions import (
    GovernedAlertAction,
    GovernedAlertActionError,
    execute_governed_alert_action,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_BROWSER_OWNED_FIELDS = frozenset(
    {
        "idempotency_key",
        "evidence_ref",
        "action_type",
        "occurred_at",
        "note",
        "dismiss_reason",
        "defer_until",
        "decision_id",
        "replacement_episode_id",
    }
)


@dataclass(frozen=True)
class GovernedAlertRouteContext:
    """Explicit local dependencies for governed alert routes."""

    db_path: Path
    owner_actor: str


def _receipt_payload(receipt: object) -> dict[str, object]:
    """Return only the immutable receipt fields safe for the action response."""
    from alerts.governed_actions import GovernedAlertActionReceipt

    assert isinstance(receipt, GovernedAlertActionReceipt)
    return receipt.model_dump(mode="json")


def _render_evidence(
    connection: sqlite3.Connection, alert_id: int, *, owner_actor: str
) -> str | None:
    alert = connection.execute(
        "SELECT id,ticker,trigger_kind,status,signature_sha,evidence_json,fired_at "
        "FROM alerts WHERE id=? AND user_id=?",
        (alert_id, owner_actor),
    ).fetchone()
    if alert is None:
        return None
    receipts = connection.execute(
        "SELECT receipt_id,action_type,occurred_at,result_state,evidence_ref "
        "FROM governed_alert_action_receipts WHERE alert_id=? ORDER BY occurred_at,receipt_id",
        (alert_id,),
    ).fetchall()
    try:
        evidence = json.dumps(json.loads(str(alert["evidence_json"])), indent=2, sort_keys=True)
    except (TypeError, ValueError):
        evidence = str(alert["evidence_json"])
    receipt_rows = (
        "".join(
            f'<li data-governed-alert-receipt="{escape(str(row["receipt_id"]), quote=True)}">'
            f"<strong>{escape(str(row['action_type']))}</strong> · "
            f"{escape(str(row['result_state']))} · {escape(str(row['occurred_at']))}</li>"
            for row in receipts
        )
        or "<li>No governed lifecycle receipts recorded.</li>"
    )
    return (
        f'<section class="governed-alert-evidence" data-alert-id="{alert_id}" '
        f'data-source-ref="alert:{alert_id}" '
        f'data-evidence-ref="{escape(str(alert["signature_sha"]), quote=True)}">'
        f"<h2>Alert evidence · {escape(str(alert['ticker']))}</h2>"
        "<dl>"
        f"<dt>Alert</dt><dd>{alert_id}</dd>"
        f"<dt>Trigger</dt><dd>{escape(str(alert['trigger_kind']))}</dd>"
        f"<dt>Status</dt><dd>{escape(str(alert['status']))}</dd>"
        f"<dt>Evidence SHA-256</dt><dd><code>{escape(str(alert['signature_sha']))}</code></dd>"
        "</dl>"
        f"<pre data-alert-evidence-json>{escape(evidence)}</pre>"
        f"<h3>Governed lifecycle receipts</h3><ul>{receipt_rows}</ul>"
        "</section>"
    )


def register_governed_alert_routes(app: Flask, context: GovernedAlertRouteContext) -> None:
    """Register fail-closed localhost action and evidence endpoints."""

    @app.route("/api/governed-alerts/<int:alert_id>/actions", methods=["POST", "OPTIONS"])
    def governed_alert_action(alert_id: int):
        if request.method == "OPTIONS":
            return ("", 204)
        raw = request.get_json(silent=True)
        if not isinstance(raw, dict):
            return ({"ok": False, "code": "invalid_request", "error": "JSON body required"}, 400)
        body = cast("dict[str, object]", raw)
        unexpected = sorted(str(key) for key in body if key not in _BROWSER_OWNED_FIELDS)
        if unexpected:
            return (
                {
                    "ok": False,
                    "code": "server_owned_fields",
                    "error": "Alert identity, source reference, and actor are server-owned",
                },
                400,
            )
        try:
            action = GovernedAlertAction.model_validate(
                {
                    **body,
                    "alert_id": alert_id,
                    "source_ref": f"alert:{alert_id}",
                    "actor": context.owner_actor,
                }
            )
        except ValidationError:
            return (
                {
                    "ok": False,
                    "code": "invalid_request",
                    "error": "A complete, evidence-bound governed action is required",
                },
                400,
            )
        connection: sqlite3.Connection | None = None
        try:
            connection = connect_sqlite(
                context.db_path,
                role=SQLiteConnectionRole.WRITER,
                schema_preflight=True,
            )
            connection.execute("BEGIN IMMEDIATE")
            alert_owner = connection.execute(
                "SELECT user_id FROM alerts WHERE id=?", (alert_id,)
            ).fetchone()
            if alert_owner is None or str(alert_owner["user_id"]) != context.owner_actor:
                raise GovernedAlertActionError("alert is unavailable for the current owner")
            receipt = execute_governed_alert_action(connection, action)
            connection.commit()
        except GovernedAlertActionError as error:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            return ({"ok": False, "code": "action_conflict", "error": str(error)}, 409)
        except (OSError, RuntimeError, sqlite3.Error):
            if connection is not None and connection.in_transaction:
                connection.rollback()
            app.logger.error("governed alert action failed at the local database boundary")
            return (
                {
                    "ok": False,
                    "code": "store_unavailable",
                    "error": "Alert action store unavailable; no lifecycle action was recorded",
                },
                503,
            )
        finally:
            if connection is not None:
                connection.close()
        return {"ok": True, "receipt": _receipt_payload(receipt)}, 200

    @app.route("/api/governed-alerts/<int:alert_id>/evidence", methods=["GET"])
    def governed_alert_evidence(alert_id: int):
        connection: sqlite3.Connection | None = None
        try:
            connection = connect_sqlite(
                context.db_path,
                role=SQLiteConnectionRole.READ_ONLY,
                schema_preflight=True,
            )
            html = _render_evidence(connection, alert_id, owner_actor=context.owner_actor)
        except (OSError, RuntimeError, sqlite3.Error):
            return (
                {
                    "ok": False,
                    "code": "store_unavailable",
                    "error": "Alert evidence store unavailable",
                },
                503,
            )
        finally:
            if connection is not None:
                connection.close()
        if html is None:
            abort(404)
        response = Response(html, mimetype="text/html")
        response.headers["Cache-Control"] = "no-store"
        return response


__all__ = ["GovernedAlertRouteContext", "register_governed_alert_routes"]
