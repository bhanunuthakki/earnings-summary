"""Work OS hydration, evaluation, and investment-profile review routes."""

from __future__ import annotations

import concurrent.futures
import sqlite3
import time
from datetime import UTC, datetime
from typing import cast

from comments_server_context import ServerContext
from comments_server_evaluation_projection import resolve_work_os_evaluation_item
from flask import Flask, g, request

import ticker_validation
from integrations.portfolio_allocation import fetch_portfolio_allocation
from integrations.portfolio_offline_snapshot import read_configured_offline_portfolio_snapshot
from integrations.portfolio_tracker_client import fetch_live_portfolio
from llm.postprocess import strip_inline_markdown
from pipeline.research_cockpit import build_cockpit_rows
from pipeline.work_os_earnings import load_latest_earnings_readouts
from pipeline.work_os_evaluation import build_work_os_evaluation
from pipeline.work_os_portfolio import (
    build_work_os_portfolio,
    build_work_os_portfolio_research_links,
    load_work_os_price_action_bands,
)
from portfolio_risk_snapshot_store import read_latest_snapshot
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def register_work_os_routes(app: Flask, ctx: ServerContext) -> None:
    client_error = ctx.client_error
    db_path = ctx.db_path
    repo_root = ctx.repo_root

    @app.route("/api/work-os/portfolio", methods=["GET"])
    def work_os_portfolio_api():
        """Portfolio-only research state for Cockpit and Company Desk."""
        conn = ctx.get_read_db()
        # Preserve the build-start freshness bound even without a full memo.
        g.cockpit_memo_expires_at = time.monotonic() + ctx.cockpit_memo.ttl_seconds
        # The two tracker fetch groups — the legacy/v1 live-portfolio group and
        # the v1 allocation group — are independent snapshots of one tracker
        # process, so they overlap on a request-scoped 2-worker pool instead of
        # serializing on the render path (M2 audit F7: a slow tracker used to
        # cost the SUM of the two groups). Per-call timeouts, each group's
        # internal structure, and the fail-closed degraded envelopes are
        # unchanged; the pool holds no DB handle, so the request's read
        # connection stays on this thread.
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="work-os-tracker"
        ) as tracker_pool:
            live_future = tracker_pool.submit(fetch_live_portfolio)
            allocation_future = tracker_pool.submit(fetch_portfolio_allocation)
            # Growth/margin/attractiveness are not part of this payload. Avoid
            # their expensive canonical fallback and the full memo's in-flight
            # wait. Keep both rosters for the earnings-readout projection.
            rows_by_role = build_cockpit_rows(conn, repo_root, include_fundamentals=False)
            rows = rows_by_role.get("portfolio", [])
            evaluation_rows = rows_by_role.get("evaluation", [])
            coverage_roles = {
                row.base.ticker.strip().upper(): role
                for role, role_rows in (("portfolio", rows), ("evaluation", evaluation_rows))
                for row in role_rows
            }
            readout_projection = load_latest_earnings_readouts(
                conn,
                list(coverage_roles),
                coverage_roles=coverage_roles,
            )
            price_action_bands = load_work_os_price_action_bands(
                conn,
                [row.base.ticker for row in rows],
            )
            research_links = build_work_os_portfolio_research_links(rows, repo_root, conn)
            risk_snapshot = read_latest_snapshot(db_path=db_path)
            # Join the slowest group: both fetchers never raise on tracker
            # problems — they return explicit degraded envelopes — and any
            # unexpected error propagates exactly as the direct call did.
            live = live_future.result()
            allocation = allocation_future.result()
        payload = build_work_os_portfolio(
            rows,
            live,
            allocation,
            latest_readouts=readout_projection.readouts,
            research_links=research_links,
            price_action_bands=price_action_bands,
            readout_warnings=readout_projection.warnings,
            offline_snapshot=(
                read_configured_offline_portfolio_snapshot() if not live.available else None
            ),
            risk_snapshot=risk_snapshot,
        )
        response = app.json.response(payload.model_dump(mode="json"))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/work-os/evaluation", methods=["GET"])
    def work_os_evaluation_api():
        """Complete company and ETF evaluation coverage for the Research Engine."""
        conn = ctx.get_read_db()
        rows = ctx.cockpit_rows_snapshot(conn).get("evaluation", [])
        payload = build_work_os_evaluation(rows, repo_root, conn)
        response = app.json.response(payload.model_dump(mode="json"))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route(
        "/api/research/investment-profile/<ticker>/labels/<label>/<action>",
        methods=["POST", "OPTIONS"],
    )
    def investment_profile_label_review_api(ticker: str, label: str, action: str):
        """Append one owner label decision against the current evidence fingerprint.

        Governance-surface disposition: the primary surface is the Evaluation
        investment-profile peek.  This is deliberately excluded from Operations
        because it reviews research classification; it does not run or configure
        a service, pipeline, source, model route, or portfolio action.
        """
        if request.method == "OPTIONS":
            return ("", 204)
        from research.investment_profile import (
            CompanyProfileLabel,
            EtfProfileLabel,
            LabelReviewAction,
            record_label_review,
        )

        try:
            symbol = ticker_validation.safe_ticker(ticker)
            review_action = LabelReviewAction(action)
        except ValueError:
            return client_error("validation_error: invalid ticker, label, or action", 400)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return client_error("validation_error: JSON request body must be an object", 400)
        review_body = cast("dict[str, object]", body)
        fingerprint = str(review_body.get("suggestion_fingerprint") or "").strip()

        read_conn = ctx.get_read_db()
        item = resolve_work_os_evaluation_item(
            read_conn,
            repo_root,
            symbol,
            safe_ticker=ticker_validation.safe_ticker,
            # The route keeps its own projection bindings so tests (and any
            # future seam) patch the comments_server names exactly as before.
            build_cockpit_rows=build_cockpit_rows,
            build_work_os_evaluation=build_work_os_evaluation,
        )
        if item is None or item.profile is None:
            return client_error("not_found: current investment profile is unavailable", 404)
        try:
            profile_label = (
                EtfProfileLabel(label)
                if item.instrument_type == "etf"
                else CompanyProfileLabel(label)
            )
        except ValueError:
            return client_error("validation_error: label is invalid for this instrument", 400)
        presentation = next(
            (candidate for candidate in item.profile.labels if candidate.label is profile_label),
            None,
        )
        if presentation is None:
            return client_error("conflict_error: label is not in the current projection", 409)
        if fingerprint != presentation.suggestion_fingerprint:
            return client_error(
                "conflict_error: suggestion evidence changed; reopen the profile", 409
            )
        if (
            review_action in {LabelReviewAction.RATIFY, LabelReviewAction.REJECT}
            and not presentation.suggested
        ):
            return client_error("conflict_error: label is no longer system-suggested", 409)
        if review_action is LabelReviewAction.RETIRE and presentation.state.value not in {
            "owner_ratified",
            "review_suggested",
        }:
            return client_error("conflict_error: only an owner-reviewed label can be retired", 409)

        writer = connect_sqlite(
            ctx.resolved_db_path,
            role=SQLiteConnectionRole.WRITER,
            schema_preflight=True,
        )
        try:
            review_id = record_label_review(
                writer,
                ticker=symbol,
                label=profile_label,
                action=review_action,
                suggestion_fingerprint=fingerprint,
                evidence={
                    "profile_refresh_fingerprint": item.profile.refresh_fingerprint,
                    "source_artifact_id": item.profile.source_artifact_id,
                    "source_kind": presentation.source_kind,
                    "prior_state": presentation.state.value,
                    "suggested": presentation.suggested,
                },
            )
            writer.commit()
        except sqlite3.Error:
            writer.rollback()
            return client_error("unavailable_error: label review could not be persisted", 503)
        finally:
            writer.close()
        response = app.json.response(
            {
                "ok": True,
                "review_id": review_id,
                "ticker": symbol,
                "label": profile_label.value,
                "action": review_action.value,
            }
        )
        response.status_code = 201
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/work-os/open-loops", methods=["GET"])
    def work_os_open_loops_api():
        """Generation 3 Today Console aggregated open-loops priority queue."""
        conn = ctx.get_read_db()
        open_loops: list[dict[str, object]] = []

        try:
            cursor = conn.execute(
                """
                SELECT id, ticker, recommendation_kind, status, created_at, action_memo_md
                FROM decisions
                WHERE status IN ('draft', 'unconfirmed', 'review_requested')
                ORDER BY created_at DESC LIMIT 10
                """
            )
            for row in cursor.fetchall():
                open_loops.append(
                    {
                        "id": int(row["id"]),
                        "type": "decision_draft",
                        "ticker": str(row["ticker"]).upper(),
                        "title": f"Decision draft ({row['recommendation_kind']})",
                        "detail": strip_inline_markdown(str(row["action_memo_md"] or ""))[:140],
                        "urgency": "high",
                        "created_at": str(row["created_at"] or ""),
                    }
                )
        except sqlite3.Error:
            pass

        try:
            cursor = conn.execute(
                """
                SELECT id, ticker, body, created_at
                FROM analyst_notes
                WHERE (status = 'triage' OR status IS NULL)
                ORDER BY created_at DESC LIMIT 10
                """
            )
            for row in cursor.fetchall():
                open_loops.append(
                    {
                        "id": int(row["id"]),
                        "type": "unmapped_note",
                        "ticker": str(row["ticker"] or "GENERAL").upper(),
                        "title": "Unmapped analyst thought",
                        "detail": strip_inline_markdown(str(row["body"] or ""))[:140],
                        "urgency": "medium",
                        "created_at": str(row["created_at"] or ""),
                    }
                )
        except sqlite3.Error:
            pass

        response = app.json.response(
            {
                "open_loops": open_loops,
                "total_count": len(open_loops),
                "as_of": datetime.now(UTC).isoformat(),
            }
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/work-os/evaluation-dialogues", methods=["GET"])
    def evaluation_dialogues_api():
        """Bounded, read-only Evaluation Dialogues hydration for Portfolio Copilot."""
        from typing import Literal, cast

        from pipeline.evaluation_dialogues import (
            ALLOWED_FILTERS,
            ALLOWED_LIMITS,
            ALLOWED_SORTS,
            load_evaluation_dialogues,
        )

        limit_raw = request.args.get("limit")
        if limit_raw is None:
            limit = 3
        else:
            try:
                limit = int(limit_raw)
            except (TypeError, ValueError):
                return ({"error": f"limit must be an integer in {ALLOWED_LIMITS}"}, 400)
            if limit not in ALLOWED_LIMITS:
                return ({"error": f"limit must be one of {ALLOWED_LIMITS}"}, 400)

        sort_raw = request.args.get("sort", "relevance")
        if sort_raw not in ALLOWED_SORTS:
            return ({"error": f"sort must be one of {ALLOWED_SORTS}"}, 400)
        sort = cast(Literal["relevance", "ticker_asc"], sort_raw)

        filter_raw = request.args.get("filter", "all")
        if filter_raw not in ALLOWED_FILTERS:
            return ({"error": f"filter must be one of {ALLOWED_FILTERS}"}, 400)
        filter_state = cast(Literal["all", "has_dialogue", "has_notes", "ready"], filter_raw)

        projection = load_evaluation_dialogues(
            db_path,
            limit=limit,
            sort=sort,
            filter_state=filter_state,
        )
        return app.json.response(projection.model_dump(mode="json"))
