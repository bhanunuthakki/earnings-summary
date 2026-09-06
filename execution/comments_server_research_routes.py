"""Research task lifecycle routes for the local comments server.

Proposal governance lives in ``comments_server_proposal_routes.py`` so the
task runner, status poll, and reject path stay readable on their own.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from comments_server_route_support import (
    ActivationCounter,
    BackgroundTaskStarter,
    RedactedFailureLogger,
)
from flask import Flask, request


@dataclass(frozen=True, slots=True)
class ResearchTaskRouteContext:
    """Explicit dependencies for the research task lifecycle routes."""

    repo_root: Path
    db_path: Path
    start_background_task: BackgroundTaskStarter
    bump_activation_count: ActivationCounter
    log_redacted_failure: RedactedFailureLogger


def register_research_task_routes(app: Flask, context: ResearchTaskRouteContext) -> None:
    """Register the research task lifecycle APIs directly on ``app``."""

    db_path = context.db_path
    repo_root = context.repo_root
    bump_activation_count = context.bump_activation_count
    log_redacted_failure = context.log_redacted_failure

    @app.route("/api/research/task/<int:task_id>/run", methods=["POST", "OPTIONS"])
    def research_run(task_id: int):
        """Run the two-pass research engine on a proposed task in the background."""
        if request.method == "OPTIONS":
            return ("", 204)
        from research.proposals import get_task, research_run_enabled

        if not research_run_enabled():
            return ({"error": "research run disabled; set LEDGER_RESEARCH_RUN=1"}, 403)
        task = get_task(task_id, db_path=db_path)
        if task is None or task.status != "proposed":
            return ({"error": "task not runnable (missing or already researched)"}, 409)
        from research.run import run_research_task

        def _run_bg() -> None:
            try:
                proposal_id = run_research_task(task_id, db_path=db_path, repo_root=repo_root)
            except Exception as exc:  # the engine reverts the row; the poll sees 'proposed'
                log_redacted_failure(
                    f"research run failed for task {task_id}",
                    exc,
                    level="warning",
                )
                return
            if proposal_id is None:
                return
            # Best-effort: no bot token / no chat id on file → skip quietly.
            try:
                from capture import research_notify, token_store
                from research.proposals import get_proposal

                token = token_store.load_token()
                chat_id = token_store.load_chat_id(
                    repo_root / "data" / "capture" / "telegram_chat_id.json"
                )
                prop = get_proposal(proposal_id, db_path=db_path)
                if token and chat_id is not None and prop is not None:
                    research_notify.send_proposal_card(token, chat_id, prop)
            except Exception as exc:
                log_redacted_failure("research telegram push skipped", exc, level="debug")

        context.start_background_task(_run_bg, f"research-run-{task_id}")
        bump_activation_count("act:research_run")
        return {"started": True}

    @app.route("/api/research/task/<int:task_id>/status", methods=["GET"])
    def research_task_status(task_id: int):
        """Poll the task's current status while the background run finishes."""
        from research.proposals import get_task

        task = get_task(task_id, db_path=db_path)
        if task is None:
            return ({"error": "not found"}, 404)
        return {"status": task.status}

    @app.route("/api/research/task/<int:task_id>/reject", methods=["POST", "OPTIONS"])
    def research_reject(task_id: int):
        """Reject a proposed research task from the open-wonderings list."""
        if request.method == "OPTIONS":
            return ("", 204)
        if request.headers.get("Sec-Fetch-Site", "") == "cross-site":
            return ({"error": "cross-site reject rejected"}, 403)
        from research.proposals import get_task, set_task_status

        if get_task(task_id, db_path=db_path) is None:
            return ({"error": "task not found"}, 404)
        bump_activation_count("act:research_reject")
        set_task_status(task_id, "rejected", db_path=db_path)
        return {"ok": True}


__all__ = ["ResearchTaskRouteContext", "register_research_task_routes"]
