"""Owner-profile and tenet routes for the local comments server.

This module owns the fact mutation endpoints plus the tenet lifecycle routes.
The small writer context manager keeps the SQLite transaction shape local to
this slice so future extractions do not need to reconstruct the commit rules.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from comments_server_route_support import ActivationCounter, InternalFailureResponder
from flask import Flask, request

from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


@dataclass(frozen=True, slots=True)
class ProfileRouteContext:
    """Explicit dependencies for the owner-profile / tenets route family."""

    db_path: Path
    default_user_id: str
    bump_activation_count: ActivationCounter
    internal_failure: InternalFailureResponder


@contextmanager
def profile_writer(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    """Open one writer connection and preserve the route-local transaction shape."""

    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        conn.close()


def register_profile_routes(app: Flask, context: ProfileRouteContext) -> None:
    """Register the tenets and owner-profile APIs directly on ``app``."""

    db_path = context.db_path
    default_user_id = context.default_user_id
    bump_activation_count = context.bump_activation_count
    internal_failure = context.internal_failure

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

    @app.route("/api/profile/fact/<int:fact_id>/affirm", methods=["POST", "OPTIONS"])
    def profile_fact_affirm(fact_id: int):
        """Ratify one proposed owner-profile fact."""
        if request.method == "OPTIONS":
            return ("", 204)
        from owner_profile.store import affirm_fact

        with profile_writer(db_path) as conn:
            row = affirm_fact(conn, fact_id)
        bump_activation_count("act:profile:affirm")
        if row is None:
            return ({"ok": False}, 404)
        return (
            {
                "ok": True,
                "status": row.status,
                "receipt": "Affirmed — the coach may now cite this when reviewing your trades",
            },
            200,
        )

    @app.route("/api/profile/fact/<int:fact_id>/reject", methods=["POST", "OPTIONS"])
    def profile_fact_reject(fact_id: int):
        """Reject one proposed owner-profile fact."""
        if request.method == "OPTIONS":
            return ("", 204)
        from owner_profile.store import reject_fact

        with profile_writer(db_path) as conn:
            ok = reject_fact(conn, fact_id)
        bump_activation_count("act:profile:reject")
        if not ok:
            return ({"ok": False}, 404)
        return ({"ok": True, "receipt": "Dropped — never used, won't be re-proposed"}, 200)

    @app.route("/api/profile/fact/<int:fact_id>/reaffirm", methods=["POST", "OPTIONS"])
    def profile_fact_reaffirm(fact_id: int):
        """Refresh an expiring affirmed fact without changing its value."""
        if request.method == "OPTIONS":
            return ("", 204)
        from owner_profile.store import reaffirm_fact

        with profile_writer(db_path) as conn:
            row = reaffirm_fact(conn, fact_id)
        bump_activation_count("act:profile:reaffirm")
        if row is None:
            return ({"ok": False}, 404)
        return (
            {
                "ok": True,
                "status": row.status,
                "receipt": "Confirmed — good for another review cycle",
            },
            200,
        )

    @app.route("/api/profile/fact/<int:fact_id>/retire", methods=["POST", "OPTIONS"])
    def profile_fact_retire(fact_id: int):
        """Retire an expiring affirmed fact."""
        if request.method == "OPTIONS":
            return ("", 204)
        from owner_profile.store import retire_fact

        with profile_writer(db_path) as conn:
            ok = retire_fact(conn, fact_id)
        bump_activation_count("act:profile:retire")
        if not ok:
            return ({"ok": False}, 404)
        return ({"ok": True, "receipt": "Dropped — the coach will stop citing this fact"}, 200)

    @app.route("/api/profile/fact/<int:fact_id>/update", methods=["POST", "OPTIONS"])
    def profile_fact_update(fact_id: int):
        """Create a new proposed fact from a narrative-only edit."""
        if request.method == "OPTIONS":
            return ("", 204)
        from owner_profile.store import append_fact, get_fact

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        narrative = payload.get("narrative")
        if not isinstance(narrative, str) or not narrative.strip():
            return ({"ok": False, "error": "narrative is required"}, 400)
        with profile_writer(db_path) as conn:
            old = get_fact(conn, fact_id)
            if old is None:
                return ({"ok": False}, 404)
            new_id = append_fact(
                conn,
                category=old.category,
                key=old.key,
                value=old.value,
                narrative=narrative.strip(),
                provenance="owner",
                status="proposed",
                review_horizon_days=old.review_horizon_days,
                source_detail="ledger_update",
            )
        bump_activation_count("act:profile:update")
        return (
            {
                "ok": True,
                "new_fact_id": new_id,
                "receipt": "Saved — your edit awaits your affirm next walk",
            },
            200,
        )

    @app.route("/api/tenets/distill", methods=["POST", "OPTIONS"])
    def tenets_distill():
        """Distill flagged musings into proposed Tenets."""
        if request.method == "OPTIONS":
            return ("", 204)
        from synthesis.tenet_distill import run_tenet_distill

        try:
            counts = run_tenet_distill(db_path, user_id=default_user_id)
        except Exception as exc:  # a distill failure must not 500 the tap
            return internal_failure("distillation failed", exc, status=500)
        return {"ok": True, **counts}


__all__ = ["ProfileRouteContext", "register_profile_routes"]
