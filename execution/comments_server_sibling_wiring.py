"""Wiring for the pre-existing register_*_routes sibling modules."""

from __future__ import annotations

from comments_server_alert_routes import AppContext, register_alert_routes
from comments_server_attention_routes import AttentionRouteContext, register_attention_routes
from comments_server_content_routes import ContentRouteContext, register_content_routes
from comments_server_context import ServerContext
from comments_server_dcf_routes import DcfRouteContext, register_dcf_routes
from comments_server_decision_draft_routes import register_decision_draft_routes
from comments_server_governed_alert_routes import (
    GovernedAlertRouteContext,
    register_governed_alert_routes,
)
from comments_server_ir_approval_routes import IrApprovalRouteContext, register_ir_approval_routes
from comments_server_journal_routes import JournalRouteContext, register_journal_routes
from comments_server_profile_routes import ProfileRouteContext, register_profile_routes
from comments_server_proposal_routes import (
    ResearchProposalRouteContext,
    register_research_proposal_routes,
)
from comments_server_research_routes import ResearchTaskRouteContext, register_research_task_routes
from comments_server_settings_routes import SettingsRouteContext, register_settings_routes
from comments_server_shared import (
    dcf_recompute_payload,
    linked_gsheet,
    note_to_json,
    opt_int,
    referer_back_path,
)
from flask import Flask

import ticker_validation
from identity import DEFAULT_USER_ID
from integrations.portfolio_tracker_client import fetch_live_portfolio, probe_tracker
from pipeline.ticker_command_center import build_ticker_command_center


def register_sibling_routes(app: Flask, ctx: ServerContext) -> None:
    bump_activation_count = ctx.bump_activation_count
    client_error = ctx.client_error
    internal_failure = ctx.internal_failure
    log_redacted_failure = ctx.log_redacted_failure
    open_db = ctx.open_db
    start_background_task = ctx.start_background_task
    db_path = ctx.db_path
    get_read_db = ctx.get_read_db
    panel_cache = ctx.panel_cache
    repo_root = ctx.repo_root
    resolved_db_path = ctx.resolved_db_path

    # ----- PERSONAL-CIO ALERTING SURFACE (feed) -----
    # Previously emitted only as static files (data/dashboard/...), unreachable
    # from the live command center — so a user living in the app never saw their
    # alerts. Served live (linked from the shell topbar): read-only, degrading
    # to a valid empty-state document when the substrate tables are absent.

    register_alert_routes(
        app,
        AppContext(
            db_path=db_path,
            default_user_id=DEFAULT_USER_ID,
            referer_back_path=referer_back_path,
        ),
    )
    register_settings_routes(
        app,
        SettingsRouteContext(db_path=db_path),
    )
    register_ir_approval_routes(
        app,
        IrApprovalRouteContext(
            db_path=db_path,
            owner_actor=DEFAULT_USER_ID,
            get_read_db=get_read_db,
        ),
    )
    register_research_task_routes(
        app,
        ResearchTaskRouteContext(
            repo_root=repo_root,
            db_path=db_path,
            start_background_task=start_background_task,
            bump_activation_count=bump_activation_count,
            log_redacted_failure=log_redacted_failure,
        ),
    )
    register_research_proposal_routes(
        app,
        ResearchProposalRouteContext(
            repo_root=repo_root,
            db_path=db_path,
            bump_activation_count=bump_activation_count,
            log_redacted_failure=log_redacted_failure,
        ),
    )
    register_profile_routes(
        app,
        ProfileRouteContext(
            db_path=db_path,
            default_user_id=DEFAULT_USER_ID,
            bump_activation_count=bump_activation_count,
            internal_failure=internal_failure,
        ),
    )
    register_governed_alert_routes(
        app,
        GovernedAlertRouteContext(db_path=db_path, owner_actor=DEFAULT_USER_ID),
    )
    register_attention_routes(
        app,
        AttentionRouteContext(
            resolved_db_path,
            DEFAULT_USER_ID,
            lambda: panel_cache.invalidate_prefix("/api/panel/operations"),
            client_error,
        ),
    )
    register_journal_routes(
        app,
        JournalRouteContext(
            repo_root=repo_root,
            db_path=db_path,
            default_user_id=DEFAULT_USER_ID,
            note_to_json=note_to_json,
            optional_int=opt_int,
            bump_activation_count=bump_activation_count,
        ),
    )

    register_decision_draft_routes(app, db_path)

    register_content_routes(
        app,
        ContentRouteContext(
            repo_root=repo_root,
            db_path=db_path,
            open_db=open_db,
            get_read_db=get_read_db,
            safe_ticker=ticker_validation.safe_ticker,
            build_ticker_command_center=build_ticker_command_center,
            linked_gsheet=linked_gsheet,
            probe_tracker=probe_tracker,
            fetch_live_portfolio=fetch_live_portfolio,
            default_user_id=DEFAULT_USER_ID,
            source_context=ctx.source_context,
        ),
    )

    register_dcf_routes(
        app,
        DcfRouteContext(
            repo_root=repo_root,
            db_path=db_path,
            linked_gsheet=linked_gsheet,
            recompute_payload=dcf_recompute_payload,
            state_root=ctx.source_state_root,
        ),
    )
