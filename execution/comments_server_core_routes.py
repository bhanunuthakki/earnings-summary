"""Health, capture/musings, reconcile, dashboard, and overview read routes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from comments_server_context import ServerContext
from flask import Flask, Response, redirect, request, send_file

from identity import DEFAULT_USER_ID
from pipeline.dashboard_status import build_dashboard_rows
from pipeline.work_os_shell import render_work_os_shell_result


def register_core_routes(app: Flask, ctx: ServerContext) -> None:
    bump_activation_count = ctx.bump_activation_count
    db_path = ctx.db_path
    get_read_db = ctx.get_read_db
    repo_root = ctx.repo_root

    @app.route("/healthz", methods=["GET"])
    def healthz():
        # No repo_root — a network-reachable liveness endpoint must not leak the
        # absolute server filesystem path.
        return {"status": "ok"}

    @app.route("/api/capture/text", methods=["POST", "OPTIONS"])
    def capture_text():
        """The Ledger at-desk tray: land a typed musing through the SAME LLM-free
        ingest pipeline (channel='tray'). CSRF-guarded by
        the global Origin check on JSON state-changing requests."""
        if request.method == "OPTIONS":
            return ("", 204)
        from capture.ingest import ingest_capture

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        text = str(payload.get("text") or "").strip()
        if not text:
            return ({"error": "text required"}, 400)
        bump_activation_count("act:capture")
        result = ingest_capture(channel="tray", media_kind="text", text=text, db_path=db_path)
        return {
            "status": result.status,
            "note_id": result.note_id,
            "ticker": result.ticker,
            "needs_ticker": result.needs_ticker,
        }

    @app.route("/api/reconcile/<kind>/<int:item_id>/<verdict>", methods=["POST", "OPTIONS"])
    def reconcile_verdict(kind: str, item_id: int, verdict: str):
        """Seed-corpus freshness pass: stamp a one-tap verdict on a seed note or
        theme. The coach only leans on items that survived this list. CSRF-guarded
        by the global Origin check."""
        if request.method == "OPTIONS":
            return ("", 204)
        from synthesis.reconcile import RECONCILE_VERDICTS, reconcile_note, reconcile_theme

        if kind not in ("note", "theme"):
            return ({"error": f"unknown kind {kind!r}"}, 400)
        if verdict not in RECONCILE_VERDICTS:
            return ({"error": f"unknown verdict {verdict!r}"}, 400)
        fn = reconcile_note if kind == "note" else reconcile_theme
        bump_activation_count(f"act:reconcile:{verdict}")
        ok = fn(item_id, verdict, db_path=db_path)
        if not ok:
            return ({"ok": False}, 404)
        # Consequence receipt: what the verdict means for whether the coach can
        # still cite this item — built from the verdict tapped, no extra query.
        receipts = {
            "live": "Kept live — the coach can still cite this",
            "superseded": "Marked superseded — retired from the coach's context",
            "resolved-rejected": "Marked rejected — retired from the coach's context",
            "done": "Marked played out — retired from the coach's context",
        }
        return ({"ok": True, "receipt": receipts.get(verdict, "Saved")}, 200)

    @app.route("/api/reconcile/falsifier/<int:decision_id>", methods=["POST", "OPTIONS"])
    def reconcile_falsifier(decision_id: int):
        """Ratify / rewrite / drop an '(inferred)' falsifier on an owner decision —
        the coach may only quote falsifiers in the owner's own words.

        Consequence receipt (0142): a successful ``ratify`` gains a
        ``receipt`` string reporting the tripwire-arming state. Arming itself
        (``decision_conditions.attach_conditions``) always calls an LLM
        purpose for an unstamped row — real spend that must never ride an
        inline ratify click — so this NEVER runs extraction; it only READS
        whether a prior batch pass already reached this decision
        (``decision_conditions.arming_status``, zero-LLM). 'armed' when
        conditions are already stamped, else "queued for arming" — honest
        about the fact that the pass hasn't run yet, not a lie that it just
        did."""
        if request.method == "OPTIONS":
            return ("", 204)
        from synthesis.reconcile import FALSIFIER_ACTIONS, falsifier_action

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        action = str(payload.get("action") or "")
        if action not in FALSIFIER_ACTIONS:
            return ({"error": f"unknown action {action!r}"}, 400)
        bump_activation_count(f"act:falsifier:{action}")
        text = str(payload.get("text") or "").strip() or None
        try:
            ok = falsifier_action(decision_id, action, text=text, db_path=db_path)
        except ValueError as exc:
            return ({"error": str(exc)}, 400)
        if not ok:
            return ({"ok": False}, 404)
        result: dict[str, object] = {"ok": True}
        if action == "ratify":
            from decision_conditions import arming_status

            status = arming_status(decision_id, db_path=db_path)
            result["receipt"] = (
                "armed — now watched by the tripwire engine"
                if status == "armed"
                else "ratified — queued for arming (next extraction pass)"
            )
        elif action == "edit":
            result["receipt"] = "Saved — falsifier rewritten in your words"
        else:  # drop
            result["receipt"] = "Dropped — no tripwire will watch this decision"
        return (result, 200)

    @app.route("/", methods=["GET"])
    def dashboard_page():
        """Eight-screen Work OS; legacy panel endpoints remain drill-throughs."""
        render = render_work_os_shell_result(db_path=db_path)
        response = Response(render.html, mimetype="text/html")
        # no-cache = store but always revalidate. The ETag is derived from the
        # render memo key (30s bucket, pinned stamp if any, prototype
        # fingerprint), so a repeat load inside the bucket revalidates to a 304
        # with no body transfer and even a full revalidation render is a memo
        # hit. Staleness contract: the shell's data-generated-at stamp may
        # trail wall-clock time by up to one 30s bucket; the shell JS never
        # parses the stamp (it only rewrites the element's textContent).
        response.headers["ETag"] = render.etag
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Panel-Cache"] = render.cache_state
        # make_conditional mutates + returns self; the cast restores the
        # Flask subclass the werkzeug stub erases.
        return cast("Response", response.make_conditional(request))

    @app.route("/api/dashboard", methods=["GET"])
    def dashboard_api():
        rows = build_dashboard_rows(get_read_db(), repo_root)
        return {k: [r.to_dict() for r in v] for k, v in rows.items()}

    @app.route("/api/panel/since_last", methods=["GET"])
    def since_last_fragment():
        """The "since you last looked" headline band (navigation_ia §4 PR3),
        fetched by the shell only when the client's ``ix-last-seen:overview``
        localStorage stamp is >6h stale. ``?since=<ISO 8601>`` is required —
        the client always supplies its own stamp, so a missing/unparseable
        value is a 400, not a silent "now" fallback. ``now`` is always the
        server clock. Naive-UTC per the repo convention; an aware value is
        normalized rather than rejected."""
        from pipeline.since_last import build_since_last, render_since_last_band

        raw_since = request.args.get("since", "")
        try:
            since = datetime.fromisoformat(raw_since)
        except (ValueError, TypeError):
            return ({"error": "since=<ISO 8601 timestamp> required"}, 400)
        now = datetime.now(UTC).replace(tzinfo=None)
        story = build_since_last(db_path, since=since, now=now)
        return Response(render_since_last_band(story), mimetype="text/html")

    @app.route("/api/overview", methods=["GET"])
    def overview_api():
        """Retired analytical overview endpoint.

        The static/JSON analytical dashboard was retired; clients should use
        the cockpit screen instead. (``GET /analytical`` itself is a 302 into
        the shell.)"""
        return (
            {"error": "retired", "replacement": "/#screen-cockpit"},
            410,
            {"Cache-Control": "no-store"},
        )

    @app.route("/api/source-calls", methods=["GET"])
    def source_calls_api():
        """Data-fetch cache effectiveness as JSON: the cross-source headline
        rollup (skip rate, calls avoided, dollars saved) + per-(source, kind)
        detail. Previously reachable only from the show_source_calls CLI; this
        makes cache effectiveness measurable from the app (v6 re-grade, Smart
        caching). ``?since=YYYY-MM-DD`` bounds the window."""
        from dataclasses import asdict

        from sources.registry import cache_effectiveness_overview

        since = request.args.get("since") or None
        return asdict(cache_effectiveness_overview(since=since, db_path=db_path))

    @app.route("/export/cio", methods=["GET"])
    def export_cio():
        """Download the Personal-CIO substrate (alerts / queued actions / thesis
        ledger) as an .xlsx workbook. Previously this existed only as the
        ``export_cio_xlsx`` CLI — unreachable from the :7421 app (v6 re-grade,
        Richness). The server is intentionally single-user, so identity is fixed
        by repository configuration rather than request parameters. Built to a
        stable path under data/dashboard and streamed as an attachment."""
        from dashboard.cio_export import export_cio_workbook

        user_id = DEFAULT_USER_ID
        out_path = repo_root / "data" / "dashboard" / "cio_export.xlsx"
        written = export_cio_workbook(out_path, user_id=user_id, db_path=db_path)
        return send_file(
            written,
            as_attachment=True,
            download_name="cio_export.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.route("/analytical", methods=["GET"])
    def analytical_page():
        """The standalone analytical dashboard is folded into the unified shell;
        its content is the shell's Triggers tab. 302-redirect to that deep link
        so existing bookmarks keep working."""
        return redirect("/#holdings")

    @app.route("/mobile/inbox", methods=["GET"])
    def mobile_inbox_page():
        """Mobile uses the same responsive Cockpit and canonical action queue."""
        return redirect("/#screen-cockpit", code=302)
