"""Explicit tenet lifecycle routes for the local comments server."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from flask import Flask, request


@dataclass(frozen=True, slots=True)
class ProfileRouteContext:
    """Explicit dependencies for the manual tenet lifecycle routes."""

    db_path: Path


def register_profile_routes(app: Flask, context: ProfileRouteContext) -> None:
    """Register explicit tenet lifecycle routes directly on ``app``."""

    db_path = context.db_path

    @app.route("/api/tenets", methods=["POST", "OPTIONS"])
    def tenets_create():
        """Add an owner-stated Tenet and land it ``current`` immediately."""
        if request.method == "OPTIONS":
            return ("", 204)
        from synthesis.tenets import record_tenet

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        body_md = str(payload.get("body_md") or "").strip()
        if not body_md:
            return ({"error": "body_md required"}, 400)
        scope_key = str(payload.get("scope_key") or "").strip() or None
        tenet = record_tenet(
            body_md=body_md,
            scope_key=scope_key,
            status="current",
            provenance="owner",
            db_path=db_path,
        )
        return {"ok": True, "id": tenet.id, "scope_key": tenet.scope_key}

    @app.route("/api/tenets/<int:tenet_id>/<action>", methods=["POST", "OPTIONS"])
    def tenets_act(tenet_id: int, action: str):
        """Approve, reject, or revert a Tenet/stance insight."""
        if request.method == "OPTIONS":
            return ("", 204)
        from synthesis.tenets import approve_tenet, reject_tenet, revert_tenet

        if action == "approve":
            row = approve_tenet(tenet_id, db_path=db_path)
            if row is None:
                return ({"ok": False}, 404)
            return (
                {
                    "ok": True,
                    "status": row.status,
                    "receipt": "Adopted — now a standing Tenet in your decision prompts",
                },
                200,
            )
        if action == "reject":
            ok = reject_tenet(tenet_id, db_path=db_path)
            if not ok:
                return ({"ok": False}, 404)
            return ({"ok": True, "receipt": "Retired — this Tenet was not adopted"}, 200)
        if action == "revert":
            reverted = revert_tenet(tenet_id, db_path=db_path)
            if reverted is None:
                return ({"ok": False}, 404)
            receipt = (
                "Reverted — restores your prior belief"
                if reverted.status == "current"
                else "Reverted — retired, no longer live"
            )
            return ({"ok": True, "status": reverted.status, "receipt": receipt}, 200)
        return ({"error": f"unknown action {action!r}"}, 400)


__all__ = ["ProfileRouteContext", "register_profile_routes"]
