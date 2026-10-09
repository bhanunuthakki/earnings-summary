"""Panel builder functions for the cockpit fragment dispatcher.

Each builder registers itself in ``PANEL_BUILDERS`` under its panel name.
"""

from __future__ import annotations

from datetime import UTC, datetime

from comments_server_context import ServerContext
from comments_server_panel_registry import PANEL_BUILDERS, register_panel
from flask import Response, abort, request

import ticker_validation
from identity import DEFAULT_USER_ID
from operations.attention_projection import build_attention_panel_view
from operations.paths import scheduler_receipt_path, service_receipt_path
from operations.snapshot import collect_operations_snapshot
from pipeline.operations_panel import build_operations_panel_view, render_operations_panel

__all__ = ["PANEL_BUILDERS"]


def _panel_overview(ctx: ServerContext) -> Response | tuple[str, int]:
    overview_fragment_response = ctx.overview_fragment_response
    return overview_fragment_response()


def _panel_operations(ctx: ServerContext) -> Response | tuple[str, int]:
    collect_current_readme_status = ctx.collect_current_readme_status
    declared_operations = ctx.declared_operations
    get_operations_read_db = ctx.get_operations_read_db
    repo_root = ctx.repo_root
    operations_conn = get_operations_read_db()
    snapshot = collect_operations_snapshot(
        declared_operations,
        repo_root=repo_root,
        conn=operations_conn,
        observed_at=datetime.now(UTC),
        scheduler_receipt_path=scheduler_receipt_path(repo_root),
        service_receipt_path=service_receipt_path(repo_root),
    )
    return Response(
        render_operations_panel(
            build_operations_panel_view(
                declared_operations,
                snapshot,
                readme_status=collect_current_readme_status(),
                attention=build_attention_panel_view(
                    operations_conn, observed_at=snapshot.observed_at
                ),
            )
        ),
        mimetype="text/html",
    )


def _panel_portfolio(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    from pipeline.portfolio_panel import render_portfolio_panel

    return Response(
        render_portfolio_panel(
            start_date=request.args.get("start_date"),
            end_date=request.args.get("end_date"),
            include_backfill=request.args.get("include_backfill") in ("1", "true", "True"),
            db_path=db_path,
        ),
        mimetype="text/html",
    )


def _panel_performance_risk(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    get_read_db = ctx.get_read_db
    repo_root = ctx.repo_root
    from pipeline.performance_risk_panel import (
        render_performance_risk_fragment,
        render_performance_risk_panel,
    )
    from pipeline.portfolio_panel import render_health_fragment

    fragment = request.args.get("fragment")
    if fragment in ("performance", "allocation", "posture"):
        return Response(
            render_performance_risk_fragment(
                db_path,
                repo_root,
                fragment,
                start_date=request.args.get("start_date"),
                end_date=request.args.get("end_date"),
                include_backfill=request.args.get("include_backfill") in ("1", "true", "True"),
            ),
            mimetype="text/html",
        )
    if fragment:
        return Response(
            render_health_fragment(db_path, fragment, conn=get_read_db()),
            mimetype="text/html",
        )
    return Response(
        render_performance_risk_panel(
            db_path,
            repo_root,
            start_date=request.args.get("start_date"),
            end_date=request.args.get("end_date"),
            include_backfill=request.args.get("include_backfill") in ("1", "true", "True"),
            lazy=True,
        ),
        mimetype="text/html",
    )


def _panel_portfolio_synthesis(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    get_read_db = ctx.get_read_db
    from pipeline.portfolio_panel import render_portfolio_synthesis_panel

    return Response(
        render_portfolio_synthesis_panel(db_path, conn=get_read_db()),
        mimetype="text/html",
    )


def _panel_positioning(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    repo_root = ctx.repo_root
    from pipeline.positioning_panel import render_positioning_panel

    return Response(render_positioning_panel(db_path, repo_root), mimetype="text/html")


def _panel_portfolio_risk(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    get_read_db = ctx.get_read_db
    from pipeline.portfolio_panel import render_portfolio_risk_panel

    return Response(
        render_portfolio_risk_panel(db_path=db_path, conn=get_read_db()),
        mimetype="text/html",
    )


def _panel_red_team(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    from pipeline.red_team_panel import render_red_team_panel

    return Response(render_red_team_panel(db_path=db_path), mimetype="text/html")


def _panel_portfolio_health(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    get_read_db = ctx.get_read_db
    from pipeline.portfolio_console_panel import render_portfolio_health_panel
    from pipeline.portfolio_panel import render_health_fragment

    fragment = request.args.get("fragment")
    if fragment:
        return Response(
            render_health_fragment(db_path, fragment, conn=get_read_db()),
            mimetype="text/html",
        )
    user_id = DEFAULT_USER_ID
    return Response(render_portfolio_health_panel(db_path, user_id=user_id), mimetype="text/html")


def _panel_portfolio_allocation(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    repo_root = ctx.repo_root
    from pipeline.portfolio_console_panel import render_portfolio_allocation_panel

    user_id = DEFAULT_USER_ID
    return Response(
        render_portfolio_allocation_panel(db_path, repo_root, user_id=user_id),
        mimetype="text/html",
    )


def _panel_portfolio_record(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    from pipeline.portfolio_console_panel import (
        render_portfolio_record_fragment,
        render_portfolio_record_panel,
    )

    user_id = DEFAULT_USER_ID
    fragment = request.args.get("fragment")
    if fragment:
        if fragment not in ("brief", "decisions", "research-items", "memos", "triggers"):
            abort(404)
        return Response(
            render_portfolio_record_fragment(db_path, fragment, user_id=user_id),
            mimetype="text/html",
        )
    return Response(
        render_portfolio_record_panel(db_path, user_id=user_id, lazy=True),
        mimetype="text/html",
    )


def _panel_ir_coverage(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    from pipeline.ir_coverage_panel import render_ir_coverage_panel

    return Response(render_ir_coverage_panel(db_path), mimetype="text/html")


def _panel_source_calls(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    from pipeline.source_calls_panel import render_source_calls_panel

    return Response(render_source_calls_panel(db_path), mimetype="text/html")


def _panel_cron_health(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    get_read_db = ctx.get_read_db
    from pipeline.cron_health_panel import (
        render_cron_health_live_body,
        render_cron_health_panel,
    )

    if request.args.get("fragment") == "live":
        return Response(
            render_cron_health_live_body(db_path, conn=get_read_db()),
            mimetype="text/html",
        )
    return Response(render_cron_health_panel(db_path, conn=get_read_db()), mimetype="text/html")


def _panel_dcf_coverage(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    repo_root = ctx.repo_root
    from pipeline.dcf_coverage_panel import render_dcf_coverage_panel

    return Response(render_dcf_coverage_panel(db_path, repo_root), mimetype="text/html")


def _panel_dcf_globals(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    from pipeline.dcf_globals_panel import render_dcf_globals_panel

    return Response(render_dcf_globals_panel(db_path), mimetype="text/html")


def _panel_validation(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    from pipeline.validation_issues_panel import render_validation_panel

    return Response(render_validation_panel(db_path), mimetype="text/html")


def _panel_provenance(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    repo_root = ctx.repo_root
    from pipeline.provenance_panel import render_provenance_panel

    user_id = DEFAULT_USER_ID
    return Response(
        render_provenance_panel(db_path, repo_root, user_id=user_id),
        mimetype="text/html",
    )


def _panel_overrides(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    from pipeline.fact_overrides_panel import render_fact_overrides_panel

    return Response(render_fact_overrides_panel(db_path), mimetype="text/html")


def _panel_credibility(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    from pipeline.credibility_panel import render_credibility_panel

    return Response(
        render_credibility_panel(db_path, user_id=DEFAULT_USER_ID),
        mimetype="text/html",
    )


def _panel_section_coverage(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    repo_root = ctx.repo_root
    from pipeline.section_coverage_panel import render_section_coverage_panel

    user_id = DEFAULT_USER_ID
    return Response(
        render_section_coverage_panel(db_path, repo_root, user_id=user_id),
        mimetype="text/html",
    )


def _panel_explore(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    from pipeline.explore_panel import (
        render_explore_panel,
        render_keymetrics_fragment,
        render_saved_views_list,
    )

    user_id = DEFAULT_USER_ID
    fragment = request.args.get("fragment")
    if fragment == "views":
        return Response(render_saved_views_list(db_path, user_id=user_id), mimetype="text/html")
    if fragment == "keymetrics":
        # The key-metrics preselect bubble row for a (changed) ticker set
        # — tier-graded baseline + cached LLM picks (key_metrics_picker.md).
        km_tickers = [
            t.strip().upper() for t in (request.args.get("tickers") or "").split(",") if t.strip()
        ]
        return Response(render_keymetrics_fragment(db_path, km_tickers), mimetype="text/html")
    if fragment == "work-os":
        requested: list[str] = []
        for raw_ticker in (request.args.get("tickers") or "").split(",")[:16]:
            if not raw_ticker.strip():
                continue
            try:
                requested.append(ticker_validation.safe_ticker(raw_ticker.strip()))
            except ValueError:
                abort(400)
        return Response(
            render_explore_panel(
                db_path,
                user_id=user_id,
                initial_tickers=requested,
                include_runtime=False,
            ),
            mimetype="text/html",
        )
    return Response(render_explore_panel(db_path, user_id=user_id), mimetype="text/html")


def _panel_diet(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    get_read_db = ctx.get_read_db
    from pipeline.diet_panel import render_diet_panel

    return Response(render_diet_panel(db_path, conn=get_read_db()), mimetype="text/html")


def _panel_musings(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    from pipeline.ledger_panel import (
        render_ledger_list,
        render_ledger_research_list,
        render_onmymind_list,
        render_reconcile_list,
    )

    user_id = DEFAULT_USER_ID
    fragment = request.args.get("fragment")
    if fragment == "research":
        # The Ledger → Research inbox lane re-fetched after a run / action.
        return Response(render_ledger_research_list(db_path), mimetype="text/html")
    if fragment == "reconcile":
        # Seed-corpus freshness pass — re-fetched after each verdict.
        return Response(render_reconcile_list(db_path), mimetype="text/html")
    if fragment == "onmymind":
        # On My Mind keyset page — the next page of feed cards + a fresh
        # 'Load more', which replaces the current one in place.
        return Response(
            render_onmymind_list(db_path, cursor=request.args.get("cursor"), user_id=user_id),
            mimetype="text/html",
        )
    if fragment == "worldview":
        # The Worldview review body — re-fetched after add / approve /
        # reject / distill.
        from pipeline.worldview_panel import render_worldview_body

        return Response(render_worldview_body(db_path), mimetype="text/html")
    if fragment == "list":
        return Response(render_ledger_list(db_path, user_id=user_id), mimetype="text/html")
    if fragment == "card":
        # One feed card by note id — the card-level refresh (set-ticker,
        # the freshly-captured prepend, the answer-poll swap) so an
        # action never repaints the whole list. 404 keeps the client on
        # its full-refresh fallback.
        from onmymind.feed import load_feed_item
        from pipeline.ledger_panel import render_feed_card

        raw_note = request.args.get("note", "")
        if not raw_note.isdigit():
            return ("note id required", 400)
        item = load_feed_item(int(raw_note), db_path=db_path)
        if item is None:
            return ("not found", 404)
        return Response(render_feed_card(item), mimetype="text/html")
    # Default: the composite Ledger console (Phase-5 IA).
    from pipeline.ledger_console_panel import render_ledger_console

    return Response(render_ledger_console(db_path, user_id=user_id), mimetype="text/html")


def _panel_discovery(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    from pipeline.discovery_panel import (
        render_discovery_list,
        render_discovery_panel,
        render_sources_editor,
    )

    user_id = DEFAULT_USER_ID
    fragment = request.args.get("fragment")
    if fragment == "sources":
        return Response(render_sources_editor(db_path), mimetype="text/html")
    d_status = (request.args.get("status") or "live").strip() or "live"
    try:
        d_min = float(request.args.get("min_score") or 0)
    except ValueError:
        d_min = 0.0
    d_renderer = render_discovery_list if fragment == "list" else render_discovery_panel
    return Response(
        d_renderer(db_path, user_id=user_id, status=d_status, min_score=d_min),
        mimetype="text/html",
    )


def _panel_journal(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    from pipeline.journal_panel import (
        render_journal_list,
        render_journal_panel,
        render_reconciliation_list,
        render_research_items_band,
    )

    user_id = DEFAULT_USER_ID
    j_ticker = (request.args.get("ticker") or "").strip().upper() or None
    j_kind = (request.args.get("kind") or "").strip() or None
    j_status = (request.args.get("status") or "open").strip() or "open"
    research_items_only = request.args.get("items") == "1"
    if request.args.get("band") == "brief":
        if not research_items_only or j_ticker is None:
            abort(400)
        return Response(
            render_research_items_band(db_path, user_id=user_id, ticker=j_ticker),
            mimetype="text/html",
        )
    if request.args.get("fragment") == "reconcile":
        return Response(
            render_reconciliation_list(db_path, user_id=user_id, ticker=j_ticker),
            mimetype="text/html",
        )
    renderer = (
        render_journal_list if request.args.get("fragment") == "list" else render_journal_panel
    )
    return Response(
        renderer(
            db_path,
            user_id=user_id,
            ticker=j_ticker,
            kind=j_kind,
            status=j_status,
            research_items_only=research_items_only,
        ),
        mimetype="text/html",
    )


def _panel_triage(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    from pipeline.triage_panel import render_triage_list, render_triage_panel

    user_id = DEFAULT_USER_ID
    t_renderer = (
        render_triage_list if request.args.get("fragment") == "list" else render_triage_panel
    )
    return Response(t_renderer(db_path, user_id=user_id), mimetype="text/html")


def _panel_ledger_decisions(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    from pipeline.decision_journal_panel import (
        render_decision_journal_list,
        render_decision_journal_panel,
    )

    dj_filter = (request.args.get("filter") or "owner").strip()
    if request.args.get("fragment") == "list":
        return Response(
            render_decision_journal_list(db_path, filter_=dj_filter),
            mimetype="text/html",
        )
    return Response(render_decision_journal_panel(db_path, filter_=dj_filter), mimetype="text/html")


def _panel_ticker_settings(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    from pipeline.ticker_settings_panel import render_ticker_settings_panel

    return Response(render_ticker_settings_panel(db_path), mimetype="text/html")


def _panel_data_policy_settings(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    get_read_db = ctx.get_read_db
    from pipeline.data_policy_settings_panel import render_data_policy_settings_panel

    return Response(
        render_data_policy_settings_panel(
            db_path=db_path,
            conn=get_read_db() if db_path.is_file() else None,
        ),
        mimetype="text/html",
    )


def _panel_restatements(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    from pipeline.restatements_panel import render_restatements_panel

    return Response(render_restatements_panel(db_path), mimetype="text/html")


def _panel_actions(ctx: ServerContext) -> Response | tuple[str, int]:
    from pipeline.dashboard_html import render_actions_panel

    return Response(render_actions_panel(), mimetype="text/html")


def _panel_thesis_ledger(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    from pipeline.thesis_ledger_panel import render_thesis_ledger_panel

    user_id = DEFAULT_USER_ID
    return Response(render_thesis_ledger_panel(db_path, user_id=user_id), mimetype="text/html")


def _panel_decisions_record(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    from pipeline.allocation_decisions_panel import render_allocation_decisions_panel

    user_id = DEFAULT_USER_ID
    return Response(
        render_allocation_decisions_panel(db_path, user_id=user_id),
        mimetype="text/html",
    )


def _panel_advisor_memos(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    from pipeline.advisor_memos_panel import render_advisor_memos_panel

    user_id = DEFAULT_USER_ID
    return Response(
        render_advisor_memos_panel(db_path, user_id=user_id),
        mimetype="text/html",
    )


def _panel_evals(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    from pipeline.evals_panel import render_evals_panel

    return Response(render_evals_panel(db_path), mimetype="text/html")


def _panel_model_eval(ctx: ServerContext) -> Response | tuple[str, int]:
    db_path = ctx.db_path
    from pipeline.model_eval_panel import render_model_eval_panel

    return Response(render_model_eval_panel(db_path), mimetype="text/html")


register_panel("overview", _panel_overview)
register_panel("operations", _panel_operations)
register_panel("portfolio", _panel_portfolio)
register_panel("performance_risk", _panel_performance_risk)
register_panel("portfolio_synthesis", _panel_portfolio_synthesis)
register_panel("positioning", _panel_positioning)
register_panel("portfolio_risk", _panel_portfolio_risk)
register_panel("red_team", _panel_red_team)
register_panel("portfolio_health", _panel_portfolio_health)
register_panel("portfolio_allocation", _panel_portfolio_allocation)
register_panel("portfolio_record", _panel_portfolio_record)
register_panel("ir_coverage", _panel_ir_coverage)
register_panel("source_calls", _panel_source_calls)
register_panel("cron_health", _panel_cron_health)
register_panel("dcf_coverage", _panel_dcf_coverage)
register_panel("dcf_globals", _panel_dcf_globals)
register_panel("validation", _panel_validation)
register_panel("provenance", _panel_provenance)
register_panel("overrides", _panel_overrides)
register_panel("credibility", _panel_credibility)
register_panel("section_coverage", _panel_section_coverage)
register_panel("explore", _panel_explore)
register_panel("diet", _panel_diet)
register_panel("musings", _panel_musings)
register_panel("discovery", _panel_discovery)
register_panel("journal", _panel_journal)
register_panel("triage", _panel_triage)
register_panel("ledger_decisions", _panel_ledger_decisions)
register_panel("ticker_settings", _panel_ticker_settings)
register_panel("data_policy_settings", _panel_data_policy_settings)
register_panel("restatements", _panel_restatements)
register_panel("actions", _panel_actions)
register_panel("thesis_ledger", _panel_thesis_ledger)
register_panel("decisions_record", _panel_decisions_record)
register_panel("advisor_memos", _panel_advisor_memos)
register_panel("evals", _panel_evals)
register_panel("model_eval", _panel_model_eval)
