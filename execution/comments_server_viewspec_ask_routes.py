"""ViewSpec builder, Ask stream/sessions, positioning, and peers routes."""

from __future__ import annotations

from typing import cast

from comments_server_context import ServerContext
from flask import Flask, Response, request

from ask.context import build_portfolio_pack
from ask.engine import AskTurn, ask_retrieval_mode, fold_events, respond_turn
from ask.exchange_store import (
    BeginExchangeResult,
    ExchangeConflictError,
    ExchangeStateError,
    PendingExchangeError,
    RevisionConflictError,
    SessionContextConflictError,
    get_session_context,
    list_session_exchange_artifacts,
    orchestrate_exchange_events,
    replay_exchange_events,
)
from ask.store import (
    delete_session,
    ensure_session,
    get_session,
    list_sessions,
    load_turns,
    rename_session,
)
from logging_config import get_correlation_id
from research.proposal_approval import bind_ask_proposal_events
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def register_viewspec_ask_routes(app: Flask, ctx: ServerContext) -> None:
    client_error = ctx.client_error
    durable_conflict = ctx.durable_conflict
    internal_failure = ctx.internal_failure
    log_redacted_failure = ctx.log_redacted_failure
    parse_ask_turn_with_session = ctx.parse_ask_turn_with_session
    prepare_durable_ask = ctx.prepare_durable_ask
    session_to_json = ctx.session_to_json
    stream_engine_events_with_session = ctx.stream_engine_events_with_session
    db_path = ctx.db_path
    job_registry = ctx.job_registry
    repo_root = ctx.repo_root

    @app.route("/api/viewspec/catalog", methods=["GET"])
    def viewspec_catalog_api():
        """Metric catalog for the Explore builder (P5.1): what's plottable
        for ``?tickers=A,B`` — admitted statement facts, KPIs, segments,
        customer concentrations, and lease ladders — as typed entries."""
        from viewspec.engine import metric_catalog

        tickers = [t for t in (request.args.get("tickers") or "").split(",") if t.strip()]
        return metric_catalog(db_path, tickers)

    @app.route("/api/viewspec/run", methods=["POST"])
    def viewspec_run_api():
        """Execute a ViewSpec (P5.1). JSON body: the spec object, optionally
        wrapped as ``{"spec": {...}, "chart": bool}``. Returns the rendered
        HTML fragment (matrix + chips + chart); 400 with the full validation
        message list on a bad spec — the panel (and the P5.2 NL box) surface
        it and degrade to the builder."""
        from viewspec.engine import execute_view
        from viewspec.render import render_view_fragment
        from viewspec.spec import ViewSpec, ViewSpecError

        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        raw_spec = body.get("spec", body)
        include_chart = bool(body.get("chart", True))
        # The Ask card's +Peers path passes summary=false (it shows the one
        # summary on its actions row); the DIY builder omits it and keeps the
        # self-describing caption band.
        include_summary = bool(body.get("summary", True))
        try:
            spec = ViewSpec.from_dict(raw_spec)
        except ViewSpecError as exc:
            return ({"error": str(exc)}, 400)
        result = execute_view(spec, db_path=db_path)
        return Response(
            render_view_fragment(
                result, include_chart=include_chart, include_summary=include_summary
            ),
            mimetype="text/html",
        )

    @app.route("/api/viewspec/compile", methods=["POST"])
    def viewspec_compile_api():
        """NL → ViewSpec (P5.2). JSON body ``{"query": ..., "tickers": [...]}``
        (tickers = the panel's current universe, used for vocabulary grounding
        and as the default when the question names none). Always 200 with a
        tri-state payload — ``{"status": "ok", "spec": {...}}`` or
        ``{"status": "budget_skipped" | "error", "message": ...}`` — so the
        panel degrades to the builder UI instead of surfacing an HTTP error.
        400 only for a missing query."""
        from viewspec.nl_compile import compile_nl_to_viewspec

        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        query = str(body.get("query") or "").strip()
        if not query:
            return ({"error": "query required"}, 400)
        raw_tickers = body.get("tickers")
        context = (
            [str(t) for t in cast("list[object]", raw_tickers)]
            if isinstance(raw_tickers, list)
            else []
        )
        raw_context_spec = body.get("context_spec")
        context_spec = (
            cast("dict[str, object]", raw_context_spec)
            if isinstance(raw_context_spec, dict)
            else None
        )
        result = compile_nl_to_viewspec(
            query,
            db_path=db_path,
            context_tickers=context,
            context_spec=context_spec,
        )
        payload: dict[str, object] = {"status": result.status}
        if result.message:
            payload["message"] = result.message
        if result.spec is not None:
            payload["spec"] = result.spec.to_dict()
        return payload

    @app.route("/api/ask/stream", methods=["POST", "OPTIONS"])
    def ask_stream_api():
        """The sole research-conversation route: durable raw SSE frames.

        The first frame is always
        ``{type: "session", session_id: "..."}`` so the client can store
        the id and pass it back on the next turn."""
        if request.method == "OPTIONS":
            return ("", 204)
        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        durable = "request_id" in body
        try:
            turn, sess = parse_ask_turn_with_session()
        except ValueError as exc:
            return client_error(str(exc), 400)
        if turn is None:
            return ({"error": "query required"}, 400)
        begun: BeginExchangeResult | None = None
        if durable:
            try:
                turn, begun = prepare_durable_ask(body, turn, sess)
                if begun.disposition == "pending":
                    raise PendingExchangeError("request_id is already pending")
            except ValueError as exc:
                return client_error(str(exc), 400)
            except (
                ExchangeConflictError,
                ExchangeStateError,
                PendingExchangeError,
                RevisionConflictError,
                SessionContextConflictError,
            ) as exc:
                return durable_conflict(exc, sess)
        pack = build_portfolio_pack(repo_root, db_path)
        if begun is not None and begun.disposition == "replayed":
            events = replay_exchange_events(
                begun.exchange,
                db_path=db_path,
                session_revision=begun.session_revision,
            )
        else:
            raw_engine_events = respond_turn(
                turn,
                pack,
                db_path=db_path,
                repo_root=repo_root,
                registry=job_registry,
                retrieval_mode=ask_retrieval_mode(),
                source_context=ctx.source_context,
            )
            engine_events = (
                bind_ask_proposal_events(
                    raw_engine_events,
                    repo_root=repo_root,
                    db_path=db_path,
                    exchange_request_id=begun.exchange.request_id,
                )
                if begun is not None
                else raw_engine_events
            )
            events = (
                orchestrate_exchange_events(engine_events, exchange=begun.exchange, db_path=db_path)
                if begun is not None
                else engine_events
            )
        sid = sess.id if sess else (turn.session_id or "")
        durable_context = get_session_context(sid, db_path=db_path) if begun is not None else None
        return stream_engine_events_with_session(
            events,
            sid,
            disconnect_safe=begun is not None,
            session_revision=begun.session_revision if begun is not None else None,
            session_context=(
                durable_context.context.model_dump(mode="json")
                if durable_context is not None
                else None
            ),
        )

    # ------------------------------------------------------------------
    # Positioning coach (the fit-v2 positioning surface)
    # ------------------------------------------------------------------

    @app.route("/api/positioning/coach", methods=["POST"])
    def positioning_coach():
        """One coach turn — the unified ask engine with the POSITIONING pack
        (socratic push-back + live book grounding, billed under
        ``positioning_coach_turn``). Sessions are scoped ``positioning`` so
        coach threads never mix into the Ask tab's list. Buffered JSON (the
        panel shows a thinking indicator); always carries ``session_id``."""
        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        query = str(body.get("query") or "").strip()
        if not query:
            return ({"error": "query required"}, 400)
        raw_sid = body.get("session_id")
        client_sid = str(raw_sid).strip() if isinstance(raw_sid, str) and raw_sid else None
        sess = ensure_session(client_sid, scope="positioning", db_path=db_path)
        from positioning.coach_pack import build_positioning_pack

        pack = build_positioning_pack(repo_root, db_path)
        events = respond_turn(
            AskTurn(text=query, session_id=sess.id),
            pack,
            db_path=db_path,
            repo_root=repo_root,
            registry=job_registry,
            source_context=ctx.source_context,
        )
        result = fold_events(events)
        result["session_id"] = sess.id
        return result

    @app.route("/api/positioning/propose", methods=["POST"])
    def positioning_propose():
        """Encode the coach conversation into an owner-editable approval form
        (HTML fragment). Encode failures are loud 400s with the reason —
        never a silently-empty proposal."""
        from llm.cli import is_hard_stop
        from pipeline.positioning_panel import render_approval_form
        from positioning.encode import EncodeError, propose_profile

        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        sid = str(body.get("session_id") or "").strip()
        if not sid:
            return ("session_id required", 400)
        try:
            proposal = propose_profile(db_path, repo_root, session_id=sid)
        except EncodeError as exc:
            return (str(exc), 400)
        except Exception as exc:  # structured-call failures: hard stops propagate loud
            if is_hard_stop(exc):
                raise
            return internal_failure("encode failed", exc)
        return Response(render_approval_form(proposal), mimetype="text/html")

    @app.route("/api/positioning/approve", methods=["POST"])
    def positioning_approve():
        """Persist a positioning intent FROM THE SUBMITTED FORM VALUES (the
        owner-wins seam: edits beat the LLM proposal). Returns the refreshed
        active-target card fragment; validation problems are owner-facing
        400s."""
        from pipeline.positioning_panel import (
            FormError,
            profile_from_form,
            render_active_target_card,
        )
        from positioning.store import append_intent

        form = {k: v for k, v in request.form.items()}
        try:
            profile, narrative = profile_from_form(form)
        except FormError as exc:
            return (str(exc), 400)
        session_id = (form.get("session_id") or "").strip() or None
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        try:
            append_intent(
                conn,
                narrative=narrative,
                profile=profile,
                source="coach" if session_id else "manual",
                coach_session_id=session_id,
            )
            conn.commit()
        finally:
            conn.close()
        return Response(render_active_target_card(db_path, repo_root), mimetype="text/html")

    @app.route("/api/positioning/confirm-posture", methods=["POST", "OPTIONS"])
    def positioning_confirm_posture():
        """Portfolio Posture's "Mostly right" action (P0.4b, PRD §7.5): persist
        the owner's confirmation of the DERIVED posture narrative as an
        affirmed ``behavioral`` owner-profile fact — through the existing
        ``owner_profile.store`` (no parallel profile table). This is an
        explicit owner click confirming a machine-derived characterization,
        not a machine inference, so it lands directly as ``status='affirmed'``
        (mirrors the packet-walk's approve action, not an auto-promotion)."""
        if request.method == "OPTIONS":
            return ("", 204)
        from datetime import UTC, datetime

        from owner_profile.store import append_fact

        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        narrative = str(body.get("narrative") or "").strip()
        if not narrative:
            return ({"error": "narrative required"}, 400)
        now = datetime.now(UTC).replace(tzinfo=None).isoformat()
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        try:
            fact_id = append_fact(
                conn,
                category="behavioral",
                key="portfolio_posture_confirmation",
                value={"narrative": narrative, "confirmed_at": now},
                narrative=narrative,
                provenance="owner",
                status="affirmed",
            )
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "fact_id": fact_id}

    # ------------------------------------------------------------------
    # Investment Decision Card (P1.1, personal_investment_partner_prd.md §8.1).
    # Generation is a build-time step (execution/discovery_build.py,
    # execution/refresh_dirty_artifacts.py) — this POST route is the explicit
    # owner-triggered refresh. The governed call is retry-capped and falls
    # back deterministically, so it never hangs the request indefinitely.
    # ------------------------------------------------------------------

    @app.route("/api/research/card/<ticker>/refresh", methods=["POST", "OPTIONS"])
    def investment_decision_card_refresh(ticker: str):
        """Generate/refresh the Investment Decision Card for ``ticker`` and
        re-render today's static workspace HTML so the strip reflects it
        immediately, without waiting for the next full build."""
        if request.method == "OPTIONS":
            return ("", 204)
        from llm.cli import is_hard_stop
        from research.investment_decision_card import generate_card

        symbol = ticker.strip().upper()
        if not symbol:
            return ({"error": "ticker required"}, 400)
        try:
            result = generate_card(db_path, repo_root, symbol)
        except Exception as exc:
            if is_hard_stop(exc):
                raise
            return internal_failure("card generation failed", exc)
        if result.failure_reason is not None:
            return internal_failure("card generation failed", result.failure_reason)
        try:
            from build_investment_decision_card import (
                rerender_workspace,  # sibling execution/ module
            )

            rerender_workspace(symbol, repo_root, db_path=db_path)
        except Exception:
            pass  # the card itself is persisted; a stale workspace HTML self-heals on the next build
        return {
            "artifact_id": result.artifact_id,
            "cache_hit": result.cache_hit,
            "selection_mode": result.selection_mode,
            "degraded_reasons": list(result.degraded_reasons),
            "card": result.card.model_dump(mode="json") if result.card is not None else None,
        }

    @app.route("/api/research/card/<int:artifact_id>/<verb>", methods=["POST", "OPTIONS"])
    def investment_decision_card_disposition(artifact_id: int, verb: str):
        """Owner disposition on a card artifact — pass|watch|research_further|
        promote. Delegates entirely to
        ``research.investment_decision_card.act_on_card`` (the ONE action
        core), so a future Telegram dispatcher reaches the same write path."""
        if request.method == "OPTIONS":
            return ("", 204)
        from research.investment_decision_card import CardActionError, act_on_card

        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        raw_notes = body.get("notes")
        notes = str(raw_notes).strip() if isinstance(raw_notes, str) and raw_notes.strip() else None
        try:
            status = act_on_card(artifact_id, verb, db_path=db_path, notes=notes)
        except CardActionError as exc:
            return ({"error": str(exc)}, 400)
        return {"status": status}

    # ------------------------------------------------------------------
    # Ask session management (S3 thread list / rename / delete)
    # ------------------------------------------------------------------

    @app.route("/api/ask/sessions", methods=["GET"])
    def ask_sessions_list():
        """List portfolio Ask sessions, most-recently-updated first.
        ``?limit=N`` (default 50) caps the result.
        Returns ``{"sessions": [{id, title, created_at, updated_at}, …]}``."""
        try:
            limit = min(int(request.args.get("limit", 50)), 200)
        except (TypeError, ValueError):
            limit = 50
        try:
            rows = list_sessions(scope="portfolio", limit=limit, db_path=db_path)
        except Exception:
            rows = []
        return {"sessions": [session_to_json(s) for s in rows]}

    @app.route("/api/ask/sessions/<session_id>", methods=["GET", "PATCH", "DELETE"])
    def ask_session_detail(session_id: str):
        """Single-session CRUD.

        GET  → ``{id, title, created_at, updated_at, turns: [{role, text, citations, created_at}]}``
        PATCH ``{"title": "…"}`` → rename, returns updated session JSON.
        DELETE → 204.
        """
        sid = session_id.strip()
        if not sid:
            return ({"error": "session_id required"}, 400)

        if request.method == "GET":
            sess = get_session(sid, db_path=db_path)
            if sess is None:
                return ({"error": "not found"}, 404)
            try:
                turns = load_turns(sid, db_path=db_path)
            except Exception:
                turns = []
            payload = session_to_json(sess)
            payload["turns"] = [
                {
                    "id": t.id,
                    "role": t.role,
                    "text": t.text,
                    "citations": t.citations,
                    "grounding_trace_id": t.grounding_trace_id,
                    "model": t.model,
                    "created_at": t.created_at,
                }
                for t in turns
            ]
            try:
                payload["exchange_artifacts"] = [
                    item.model_dump(mode="json")
                    for item in list_session_exchange_artifacts(sid, db_path=db_path)
                ]
            except Exception as exc:
                log_redacted_failure("Ask session artifact validation failed", exc)
                payload["exchange_artifacts"] = []
            return payload

        if request.method == "PATCH":
            body = cast("dict[str, object]", request.get_json(silent=True) or {})
            new_title = str(body.get("title") or "").strip()
            if not new_title:
                return ({"error": "title required"}, 400)
            ok = rename_session(sid, new_title, db_path=db_path)
            if not ok:
                return ({"error": "not found"}, 404)
            sess = get_session(sid, db_path=db_path)
            return session_to_json(sess) if sess else ({"error": "not found"}, 404)

        # DELETE
        ok = delete_session(sid, db_path=db_path)
        if not ok:
            return ({"error": "not found"}, 404)
        return ("", 204)

    @app.route("/api/ask/sessions/<session_id>/distill", methods=["POST"])
    def ask_session_distill(session_id: str):
        """The explicit "Distill now" tap (B4): run the session-distillation
        pass on ONE Ask thread immediately, skipping the 4h-idle gate used by
        the manual batch command. Consequence (owner ruling 2026-07-19): distilled
        belief revisions AUTO-ADOPT — live immediately, announced with a
        one-tap Revert. Returns the per-session counts dict. 409 when the
        thread was already distilled (re-running would double-land the same
        candidates — batch idempotency comes from ``distilled_at``,
        which this route must therefore honor too)."""
        sid = session_id.strip()
        if not sid:
            return ({"error": "session_id required"}, 400)
        sess = get_session(sid, db_path=db_path)
        if sess is None:
            return ({"error": "not found"}, 404)
        import sqlite3 as _sqlite3

        try:
            conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
            try:
                row = conn.execute(
                    "SELECT distilled_at FROM ask_sessions WHERE id = ?", (sid,)
                ).fetchone()
            finally:
                conn.close()
            if row is not None and row[0]:
                return ({"error": "already distilled", "distilled_at": row[0]}, 409)
        except _sqlite3.OperationalError:
            return ({"error": "distillation substrate not migrated (0190)"}, 503)
        from synthesis.session_distill import SessionRef, distill_session

        try:
            counts = distill_session(
                SessionRef(source="ask", session_id=sid),
                db_path=db_path,
                repo_root=repo_root,
            )
        except Exception:
            # comments_server has no logger; the route contract (like its
            # siblings) is an error tuple — session_distill already logged
            # the failure with full context on its own logger.
            return ({"error": "distillation failed — nothing landed"}, 500)
        return {"counts": counts}

    @app.route("/api/peers/<ticker>", methods=["GET"])
    def peers_api(ticker: str) -> dict[str, object] | tuple[dict[str, str], int]:
        """The scored comparable set for one ticker (the PR #400 peer
        scoring) — the Ask thread's "+ peers" action injects these into the
        pivot universe instead of FMP's alphabetical screen head. Always
        200 with ``{"ticker", "peers": [{"ticker", "name", "reasons"}]}``;
        a failed lookup degrades to an empty list with an ``error`` note."""
        from report.sections import p3_data  # lazy: pulls the report graph

        sym = ticker.strip().upper()
        if not sym:
            return ({"error": "ticker required"}, 400)
        try:
            rows = p3_data.load_peer_comp(sym, db_path=db_path, repo_root=repo_root)
        except Exception as exc:  # best-effort surface, never a 500
            log_redacted_failure(f"peer lookup failed for {sym}", exc)
            return {
                "ticker": sym,
                "peers": [],
                "error": "peer lookup failed; retry the request",
                "correlation_id": get_correlation_id(),
            }
        peers: list[dict[str, object]] = [
            {
                "ticker": r.peer_ticker,
                "name": r.peer_name,
                "reasons": list(r.match_reasons),
            }
            for r in rows
        ]
        return {
            "ticker": sym,
            "peers": peers,
        }
