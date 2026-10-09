"""Saved views, discovery queue/build, sizing intents, and coach routes."""

from __future__ import annotations

import sqlite3
from typing import cast

from comments_server_context import ServerContext
from comments_server_shared import (
    DISCOVERY_OWNER_STATUSES,
    candidate_to_json,
    payload_text,
    record_dismiss_pass,
    view_to_json,
)
from flask import Flask, Response, abort, request
from pydantic import ValidationError

from discovery.store import BUILDABLE_STATUSES
from dispatch_registry import RegistryConflict
from identity import DEFAULT_USER_ID
from runtime.python_process import managed_python_argv


def register_views_discovery_routes(app: Flask, ctx: ServerContext) -> None:
    client_error = ctx.client_error
    db_path = ctx.db_path
    job_registry = ctx.job_registry
    repo_root = ctx.repo_root
    resolved_db_path = ctx.resolved_db_path

    @app.route("/api/views", methods=["GET", "POST"])
    def views_api():
        """Saved views CRUD (P5.1, saved_views 0079). GET lists; POST
        ``{"name": ..., "spec": {...}}`` validates the spec then upserts by
        (user, name) — saving an existing name replaces its spec."""
        from user_state import saved_views as views_store
        from viewspec.spec import ViewSpec, ViewSpecError

        user_id = DEFAULT_USER_ID
        if request.method == "GET":
            try:
                rows = views_store.list_views(user_id=user_id, db_path=db_path)
            except sqlite3.Error:
                rows = []  # pre-0079 schema degrades to empty
            return {"views": [view_to_json(v) for v in rows]}

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        name = str(payload.get("name") or "").strip()
        if not name:
            return ({"error": "name required"}, 400)
        try:
            spec = ViewSpec.from_dict(payload.get("spec"))
        except ViewSpecError as exc:
            return ({"error": str(exc)}, 400)
        try:
            row = views_store.save_view(
                name=name, spec=spec.to_dict(), user_id=user_id, db_path=db_path
            )
        except sqlite3.Error:
            return ({"error": "saved_views table missing (run alembic upgrade)"}, 500)
        return ({"view": view_to_json(row)}, 201)

    @app.route("/api/views/<int:view_id>", methods=["DELETE"])
    def views_delete_api(view_id: int):
        """Hard-delete one saved view (a query, not memory)."""
        from user_state import saved_views as views_store

        try:
            deleted = views_store.delete_view(view_id, db_path=db_path)
        except sqlite3.Error:
            return ({"error": "saved_views table missing (run alembic upgrade)"}, 500)
        if not deleted:
            return ({"error": f"view {view_id} not found"}, 404)
        return {"deleted": True}

    @app.route("/api/views/<int:view_id>/fragment", methods=["GET"])
    def views_fragment_api(view_id: int):
        """One saved view, executed and rendered — the embed hook (P5.1) the
        Explore panel's chips use and any cockpit/report surface can iframe
        or fetch-inject. ``?chart=0`` renders the matrix only."""
        from user_state import saved_views as views_store
        from viewspec.engine import execute_view
        from viewspec.render import render_view_fragment
        from viewspec.spec import ViewSpec, ViewSpecError

        try:
            row = views_store.get_view(view_id, db_path=db_path)
        except sqlite3.Error:
            row = None
        if row is None:
            abort(404)
        try:
            spec = ViewSpec.from_dict(row.spec)
        except ViewSpecError as exc:
            return ({"error": f"stored spec no longer valid: {exc}"}, 400)
        include_chart = request.args.get("chart") not in ("0", "false")
        result = execute_view(spec, db_path=db_path)
        return Response(
            render_view_fragment(result, include_chart=include_chart), mimetype="text/html"
        )

    @app.route("/api/discovery/candidates", methods=["GET"])
    def discovery_candidates_api():
        """The discovery queue as JSON (P5.4): ``?status=live`` (default,
        everything except dismissed) or one lifecycle bucket."""
        from discovery.store import CANDIDATE_STATUSES, list_candidates

        user_id = DEFAULT_USER_ID
        status_raw = (request.args.get("status") or "live").strip()
        status = None if status_raw == "live" else status_raw
        if status is not None and status not in CANDIDATE_STATUSES:
            return ({"error": f"unknown status {status_raw!r}"}, 400)
        try:
            rows = list_candidates(user_id=user_id, status=status, db_path=db_path)
        except sqlite3.Error:
            rows = []  # pre-0081 schema degrades to empty
        return {"candidates": [candidate_to_json(c) for c in rows]}

    @app.route("/api/discovery/candidates/<int:cand_id>/status", methods=["POST"])
    def discovery_status_api(cand_id: int):
        """Owner lifecycle moves (P5.4): queued / dismissed / new (re-open).
        ``building``/``built`` belong to the build pathway and are rejected
        here — the queue can't hand-wave a name into built.

        On a dismiss the owner may attach a ``reason`` (why pass) and optional
        ``revisit_if`` text — when present, the dismiss becomes a first-class,
        gradeable ``avoid`` decision (L11) so a passed name that later triples
        leaves a trace. Absent a reason the dismiss is queue-state only, as
        before."""
        from discovery.store import set_status

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        status = str(payload.get("status") or "")
        if status not in DISCOVERY_OWNER_STATUSES:
            return (
                {"error": f"status must be one of {sorted(DISCOVERY_OWNER_STATUSES)}"},
                400,
            )
        try:
            row = set_status(cand_id, status, db_path=db_path)
        except sqlite3.Error:
            return ({"error": "discovery_candidates table missing (run alembic upgrade)"}, 500)
        if row is None:
            return ({"error": f"candidate {cand_id} not found"}, 404)

        recorded: dict[str, object] | None = None
        if status == "dismissed":
            reason = payload_text(payload.get("reason"))
            if reason is not None:
                recorded = record_dismiss_pass(
                    ticker=row.ticker,
                    reason=reason,
                    revisit_text=payload_text(payload.get("revisit_if")),
                    source_dismissal_id=cand_id,
                    db_path=db_path,
                )
        out: dict[str, object] = {"candidate": candidate_to_json(row)}
        if recorded is not None:
            out["pass_decision"] = recorded
        return out

    @app.route("/api/discovery/candidates/<int:cand_id>/watch", methods=["POST", "OPTIONS"])
    def discovery_watch_api(cand_id: int):
        """The Watch action (PRD §8.2, P1-B): promote the candidate's ticker
        into the watchlist (``tracked_companies.list_type = 'watchlist'``,
        mirroring ``discovery_build._promote_to_evaluation``'s direct-UPDATE
        shape — no onboard subprocess, unlike ``db.track_company``). The
        discovery candidate's own status is NEVER touched: Watch is a
        tracked-universe move, not a queue disposition. Idempotent — watching
        an already-watched (or better: portfolio/evaluation) name is a no-op
        that still returns 200."""
        if request.method == "OPTIONS":
            return ("", 204)
        from discovery.store import get_candidate, promote_to_watchlist

        user_id = DEFAULT_USER_ID
        try:
            cand = get_candidate(cand_id, db_path=db_path)
        except sqlite3.Error:
            return ({"error": "discovery_candidates table missing (run alembic upgrade)"}, 500)
        if cand is None:
            return ({"error": f"candidate {cand_id} not found"}, 404)
        ok = promote_to_watchlist(
            ticker=cand.ticker, name=cand.name, user_id=user_id, db_path=db_path
        )
        if not ok:
            return ({"error": "tracked_companies unavailable (run alembic upgrade)"}, 500)
        return {"candidate": candidate_to_json(cand), "watch": {"ticker": cand.ticker, "ok": True}}

    @app.route("/api/decisions/pass", methods=["POST"])
    def record_pass_api():
        """Manual entry path for an error-of-omission (L11): record "I passed on
        TICKER because ... / I'd revisit if ..." as a first-class ``avoid``
        decision for ANY ticker, queue or not. ``reason`` is required; the
        optional ``revisit_if`` text is extracted into falsifiable numeric +
        qualitative conditions by the morning-pipeline attach rungs."""
        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        ticker = payload_text(payload.get("ticker"))
        reason = payload_text(payload.get("reason"))
        if not ticker:
            return ({"error": "ticker required"}, 400)
        if not reason:
            return ({"error": "reason required (the why behind the pass)"}, 400)
        recorded = record_dismiss_pass(
            ticker=ticker.upper(),
            reason=reason,
            revisit_text=payload_text(payload.get("revisit_if")),
            source_dismissal_id=None,
            db_path=db_path,
        )
        if recorded is None:
            return ({"error": "decisions ledger unavailable (run alembic upgrade)"}, 500)
        return {"pass_decision": recorded}

    @app.route("/api/decisions/<int:decision_id>/process-quality", methods=["POST", "OPTIONS"])
    def record_process_quality_api(decision_id: int):
        """Score a decision's PROCESS quality (Track B seam 8) — the axis
        distinct from its outcome, so 'right for the wrong reasons' can be
        aggregated on the scorecard. ``quality`` ∈ {sound, flawed, lucky}."""
        if request.method == "OPTIONS":
            return ("", 204)
        from decision_extractor import (
            PROCESS_QUALITY_VOCAB,
            ProcessQuality,
            record_process_quality,
        )

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        quality = payload_text(payload.get("quality"))
        if quality not in PROCESS_QUALITY_VOCAB:
            return ({"error": f"quality must be one of {sorted(PROCESS_QUALITY_VOCAB)}"}, 400)
        ok = record_process_quality(
            decision_id=decision_id,
            process_quality=cast(ProcessQuality, quality),
            db_path=db_path,
        )
        if not ok:
            return ({"error": "decisions ledger unavailable (run alembic upgrade)"}, 500)
        return {"decision_id": decision_id, "process_quality": quality}

    @app.route("/api/discovery/sources", methods=["GET"])
    def discovery_sources_api():
        """The discovery_sources weight registry as JSON (the Discovery rule's
        editable lever). ``?signal_class=investor_13f`` filters one class."""
        from dataclasses import asdict

        from discovery.sources import list_sources

        signal_class = request.args.get("signal_class") or None
        try:
            rows = list_sources(signal_class=signal_class, db_path=db_path)
        except sqlite3.Error:
            rows = []
        return {"sources": [asdict(s) for s in rows]}

    @app.route("/api/discovery/sources/<source_key>/weight", methods=["POST", "OPTIONS"])
    def discovery_source_weight_api(source_key: str):
        """Edit a source's ``base_weight`` (the panel's weight-edit surface +
        quarterly recalibration). A non-negative float; re-ranks the queue on
        the next refresh. 404 when the source_key is unknown."""
        if request.method == "OPTIONS":
            return ("", 204)
        from dataclasses import asdict

        from discovery.sources import set_source_weight

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        raw = payload.get("weight")
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            return ({"error": "weight (number) required"}, 400)
        try:
            row = set_source_weight(source_key, float(raw), db_path=db_path)
        except sqlite3.Error:
            return ({"error": "discovery_sources table missing (run alembic upgrade)"}, 500)
        if row is None:
            return ({"error": f"source {source_key!r} not found"}, 404)
        return {"source": asdict(row)}

    @app.route("/actions/discovery-run", methods=["POST", "OPTIONS"])
    def start_discovery_run():
        """Re-run the P5.3 pipelines (screens + adjacency) as a streamed job.
        Deterministic and LLM-free — this surfaces candidates; it never
        builds them."""
        if request.method == "OPTIONS":
            return ("", 204)
        argv = managed_python_argv(
            repo_root,
            repo_root / "execution" / "run_discovery.py",
            "--repo-root",
            str(repo_root),
        )
        try:
            job = job_registry.start(ticker="DISCOVERY", kind="discovery-run", argv=argv)
        except RegistryConflict as e:
            return ({"error": str(e)}, 409)
        return (
            {
                "job_id": job.job_id,
                "ticker": job.ticker,
                "kind": job.kind,
                "stream_url": f"/actions/stream/{job.job_id}",
                "started_at": job.started_at.isoformat(),
            },
            201,
        )

    @app.route("/actions/discovery-build", methods=["POST", "OPTIONS"])
    def start_discovery_build():
        """Eval-build approved candidates (P5.4) — THE budget gate's other
        side: this only runs because the owner clicked/typed it. JSON body
        ``{"tickers": ["WDC", ...]}`` (1..MAX_BUILD_BATCH names, each must
        be a live candidate in new/queued status). One sequential job;
        ~25 min + LLM spend per name; streamed via the jobs SSE."""
        if request.method == "OPTIONS":
            return ("", 204)
        from discovery_build import MAX_BUILD_BATCH

        from discovery.store import list_candidates

        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        raw = body.get("tickers")
        if not isinstance(raw, list) or not raw:
            return ({"error": "tickers (non-empty list) required"}, 400)
        tickers = [str(t).strip().upper() for t in cast("list[object]", raw) if str(t).strip()]
        if not tickers:
            return ({"error": "tickers (non-empty list) required"}, 400)
        if len(tickers) > MAX_BUILD_BATCH:
            return (
                {"error": f"at most {MAX_BUILD_BATCH} builds per run, got {len(tickers)}"},
                400,
            )
        user_id = DEFAULT_USER_ID
        try:
            live = list_candidates(user_id=user_id, db_path=db_path)
        except sqlite3.Error:
            return ({"error": "discovery_candidates table missing (run alembic upgrade)"}, 500)
        by_ticker = {c.ticker: c for c in live}
        not_buildable = [
            t
            for t in tickers
            if t not in by_ticker or by_ticker[t].status not in BUILDABLE_STATUSES
        ]
        if not_buildable:
            return (
                {
                    "error": "not buildable (must be live candidates in new/queued "
                    f"status): {not_buildable}"
                },
                400,
            )
        argv = managed_python_argv(
            repo_root,
            repo_root / "execution" / "discovery_build.py",
            "--tickers",
            ",".join(tickers),
            "--repo-root",
            str(repo_root),
            "--user-id",
            user_id,
        )
        slot_ticker = tickers[0] if len(tickers) == 1 else "DISCOVERY-BULK"
        try:
            job = job_registry.start(ticker=slot_ticker, kind="discovery-build", argv=argv)
        except RegistryConflict as e:
            return ({"error": str(e)}, 409)
        return (
            {
                "job_id": job.job_id,
                "ticker": job.ticker,
                "kind": job.kind,
                "tickers": tickers,
                "stream_url": f"/actions/stream/{job.job_id}",
                "started_at": job.started_at.isoformat(),
            },
            201,
        )

    @app.route("/api/sizing-intents", methods=["POST", "OPTIONS"])
    def sizing_intents_api():
        """Record a sizing-posture statement (master build P2.2). JSON body:
        {"ticker": "NU", "conviction": 4, "target_weight_pct": 6, "narrative": "..."}
        — at least one of conviction (1–5) / target_weight_pct (0–100). Each
        provided kind appends its own ``position_sizing_intent`` row and
        explicitly supersedes the current row of that kind. History remains
        immutable and audit-readable; active views show only the replacement."""
        if request.method == "OPTIONS":
            return ("", 204)
        from user_state.sizing import append_intent, list_intents, supersede_intents

        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        ticker = str(body.get("ticker") or "").strip().upper()
        if not ticker:
            return ({"error": "ticker required"}, 400)
        narrative_raw = body.get("narrative")
        narrative = str(narrative_raw).strip() or None if narrative_raw is not None else None
        user_id = DEFAULT_USER_ID
        to_write: list[tuple[str, float]] = []
        if (conviction := body.get("conviction")) is not None:
            try:
                conv_f = float(cast("str | float | int", conviction))
            except (TypeError, ValueError):
                return ({"error": f"conviction must be a number, got {conviction!r}"}, 400)
            if not 1.0 <= conv_f <= 5.0:
                return ({"error": "conviction must be between 1 and 5"}, 400)
            to_write.append(("conviction", conv_f))
        if (target := body.get("target_weight_pct")) is not None:
            try:
                target_f = float(cast("str | float | int", target))
            except (TypeError, ValueError):
                return ({"error": f"target_weight_pct must be a number, got {target!r}"}, 400)
            if not 0.0 <= target_f <= 100.0:
                return ({"error": "target_weight_pct must be between 0 and 100"}, 400)
            to_write.append(("target_weight_pct", target_f))
        if not to_write:
            return ({"error": "provide conviction and/or target_weight_pct"}, 400)
        created: list[int] = []
        for kind, value_f in to_write:
            prior_ids = tuple(
                row.id
                for row in list_intents(user_id=user_id, ticker=ticker, db_path=db_path)
                if row.intent_kind == kind
            )
            row = (
                supersede_intents(
                    user_id=user_id,
                    ticker=ticker,
                    intent_kind=kind,
                    intent_value=value_f,
                    narrative=narrative,
                    supersedes_intent_ids=prior_ids,
                    reason="Owner recorded a newer sizing posture through the sizing API.",
                    db_path=db_path,
                )
                if prior_ids
                else append_intent(
                    user_id=user_id,
                    ticker=ticker,
                    intent_kind=kind,
                    intent_value=value_f,
                    narrative=narrative,
                    db_path=db_path,
                )
            )
            created.append(row.id)
        return {"ticker": ticker, "ok": True, "created_ids": created}

    @app.route("/api/sizing-intents/<ticker>/checkpoint", methods=["POST", "OPTIONS"])
    def sizing_intent_checkpoint_api(ticker: str):
        """Confirm one reviewed sizing intent through the canonical checkpoint writer.

        This localhost-only, CSRF-guarded route records owner evidence; it does
        not submit, simulate, or otherwise execute a broker action.
        """
        if request.method == "OPTIONS":
            return ("", 204)
        from advisor.sizing_intent_checkpoint_api import (
            SizingIntentCheckpointConflictError,
            SizingIntentCheckpointRequest,
            SizingIntentCheckpointUnavailableError,
            SizingIntentCheckpointValidationError,
            confirm_sizing_intent_checkpoint,
        )

        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return client_error("validation_error: JSON request body must be an object", 400)
        try:
            checkpoint_request = SizingIntentCheckpointRequest.model_validate(body)
            result = confirm_sizing_intent_checkpoint(
                checkpoint_request,
                ticker=ticker,
                db_path=resolved_db_path,
            )
        except ValidationError as exc:
            return client_error(f"validation_error: {exc.errors()[0]['msg']}", 400)
        except SizingIntentCheckpointValidationError as exc:
            return client_error(f"validation_error: {exc}", 400)
        except SizingIntentCheckpointConflictError as exc:
            return client_error(f"conflict_error: {exc}", 409)
        except SizingIntentCheckpointUnavailableError as exc:
            return client_error(f"unavailable_error: {exc}", 503)
        return result.model_dump(mode="json"), 201 if result.receipt.created else 200

    @app.route("/advisor/sizing-intents/<ticker>", methods=["GET"])
    def sizing_intent_review_page(ticker: str):
        """Standalone local owner review; writes remain behind the checkpoint API."""
        from advisor.sizing_intent_review_page import render_sizing_intent_review_page

        return Response(
            render_sizing_intent_review_page(
                resolved_db_path, ticker, work_os_origin=request.args.get("work_os_origin")
            ),
            mimetype="text/html",
        )

    @app.route("/api/coach/unmute", methods=["POST", "OPTIONS"])
    def coach_unmute_api():
        """Clear a coach_mutes row (REQ-12: mutes must be visible AND
        reversible) — the first production caller of
        ``research.governor.unmute``. JSON body: {"class_": "falsifier_breach"}.
        CSRF-guarded by the global Origin check (csrf_origin_guard)."""
        if request.method == "OPTIONS":
            return ("", 204)
        from research.governor import unmute

        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        class_ = str(body.get("class_") or "").strip()
        if not class_:
            return ({"error": "class_ required"}, 400)
        unmuted = unmute(class_, db_path=db_path)
        return {"class_": class_, "ok": True, "unmuted": unmuted}

    @app.route("/api/coach/attest-change", methods=["POST", "OPTIONS"])
    def coach_attest_change_api():
        """Record the owner's explicit "this review changed my call" attestation
        on a guard_override position_review memo — the SOLE input that moves the
        Coach P&L's Q3'26 "changed >= 1" bar (the silence-implies-heeded window
        heuristic feeds only the separate "candidate" line, never the target).
        JSON body: {"memo_id": int}. CSRF-guarded by the global Origin check.
        ``attested`` is False when nothing matched or it was already recorded —
        the counter it feeds must never be inflated by a no-op click."""
        if request.method == "OPTIONS":
            return ("", 204)
        from advisor.position_review import attest_review_changed

        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        try:
            memo_id = int(cast("int", body.get("memo_id")))
        except (TypeError, ValueError):
            return ({"error": "memo_id (int) required"}, 400)
        attested = attest_review_changed(db_path, memo_id)
        return {"memo_id": memo_id, "ok": True, "attested": attested}
