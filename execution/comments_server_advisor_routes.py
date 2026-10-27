"""Advisor memos, red team, eval, and job-stream routes."""

from __future__ import annotations

from typing import cast

from comments_server_context import ServerContext
from flask import Flask, Response, request, stream_with_context

import ticker_validation
from dispatch_registry import RegistryConflict
from logging_config import get_correlation_id
from runtime.python_process import managed_python_argv


def register_advisor_routes(app: Flask, ctx: ServerContext) -> None:
    log_redacted_failure = ctx.log_redacted_failure
    db_path = ctx.db_path
    job_registry = ctx.job_registry
    repo_root = ctx.repo_root

    @app.route("/api/earnings-readout/generate", methods=["POST", "OPTIONS"])
    def generate_earnings_readout_api():
        """Persist the latest reported-quarter readout on explicit request.

        Evaluation names can reach the paid path only through this owner action;
        the scheduled generator's query remains structurally portfolio-only.
        """
        if request.method == "OPTIONS":
            return ("", 204)
        from earnings_readout import (
            BUDGET_SKIPPED,
            ReadoutUnavailableError,
            generate_for_ticker,
        )

        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        try:
            ticker = ticker_validation.safe_ticker(str(body.get("ticker") or ""))
        except ValueError:
            return ({"error": "valid ticker required"}, 400)
        try:
            outcome = generate_for_ticker(db_path, repo_root, ticker)
        except ReadoutUnavailableError as exc:
            return ({"error": str(exc)}, 404)
        except Exception as exc:
            log_redacted_failure(f"post earnings readout request failed for {ticker}", exc)
            return (
                {
                    "error": "readout generation failed; retry the request",
                    "correlation_id": get_correlation_id(),
                },
                503,
            )
        payload = {
            "status": outcome.status,
            "ticker": outcome.ticker,
            "fiscal_period": outcome.fiscal_period,
            "artifact_id": outcome.artifact_id,
        }
        if outcome.status == BUDGET_SKIPPED:
            return ({**payload, "error": "monthly readout budget exhausted"}, 409)
        return payload

    @app.route("/api/red_team/<int:item_id>/respond", methods=["POST", "OPTIONS"])
    def respond_red_team_item(item_id: int):
        """Forced-response action on one ``red_team_items`` row (PR6 —
        monthly_red_team.md Phase 2 "Forced response" bullet). JSON body
        ``{"action": "refute" | "accept" | "defer", "response_md"?: str}``.
        Unlike the sibling ``/actions/*`` endpoints this is a SYNCHRONOUS DB
        write (the ``resolve_issue`` idiom above), not a streamed job.

        REFUTE requires non-empty ``response_md`` -> 400 without it. A
        SECOND defer on an already-``deferred`` item is rejected -> 409 (the
        Red Team panel then re-renders that item as escalated; PR6's
        Home-band banner picks it up too). All state-machine logic lives in
        ``redteam.response.respond`` — the SAME function the Telegram
        ``/redteam`` command calls, so a response typed in the app and one
        typed from Telegram behave identically.
        """
        if request.method == "OPTIONS":
            return ("", 204)
        from redteam import response as rt_response

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        action_raw = payload.get("action")
        if action_raw not in ("refute", "accept", "defer"):
            return ({"error": "action must be one of refute | accept | defer"}, 400)
        action = action_raw
        response_md_raw = payload.get("response_md")
        response_md = str(response_md_raw) if response_md_raw is not None else None

        try:
            result = rt_response.respond(
                db_path=db_path, item_id=item_id, action=action, response_md=response_md
            )
        except rt_response.ItemNotFoundError:
            return ({"error": f"red_team_items id={item_id} not found"}, 404)
        except rt_response.ResponseRequiresTextError as exc:
            return ({"error": str(exc)}, 400)
        except rt_response.AlreadyRespondedError as exc:
            return ({"error": str(exc)}, 409)
        except rt_response.SecondDeferRejectedError as exc:
            return (
                {
                    "error": str(exc),
                    "escalated": True,
                    "item_id": exc.item.id,
                    "status": exc.item.status,
                    "defer_count": exc.item.defer_count,
                },
                409,
            )
        return {
            "ok": True,
            "item_id": result.item.id,
            "status": result.item.status,
            "defer_count": result.item.defer_count,
            "artifact_kind": result.artifact_kind,
            "artifact_id": result.artifact_id,
        }

    @app.route("/actions/position-review", methods=["POST", "OPTIONS"])
    def start_position_review():
        """Run the full governed position review (PR5 — the calibration
        feeder) as a streamed single-flight job: {"ticker": str}. Runs
        ``execution/review_position.py <TICKER> --verdict`` — the LLM verdict +
        deterministic behavioral guard, PERSISTING an ``advisor_memos`` row
        (kind ``position_review``) so the review finally lands a gradeable
        memo. The review peek's "Full calibrated review (LLM)" button POSTs
        here and streams the job log in place."""
        if request.method == "OPTIONS":
            return ("", 204)
        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        ticker = str(body.get("ticker", "")).strip().upper()
        if not ticker:
            return ({"error": "ticker required"}, 400)
        argv = managed_python_argv(
            repo_root,
            repo_root / "execution" / "review_position.py",
            ticker,
            "--verdict",
            "--db",
            str(db_path),
            # An in-app owner click — tag it so it counts in the Coach P&L (the
            # CLI defaults to 'agent', which would exclude it).
            "--source",
            "doorway",
        )
        try:
            job = job_registry.start(ticker=ticker, kind="position-review", argv=argv)
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

    @app.route("/actions/run-eval", methods=["POST", "OPTIONS"])
    def start_eval_run():
        """Run one purpose's LLM eval (llm_evals_plan §2.6) as a streamed
        single-flight job: {"purpose": "viewspec_compile" | "bear_case" |
        "transcript_summary" | "advisor_next_dollar"}. Runs
        execution/run_llm_evals.py against this repo's DB; the Evals panel
        consumes the SSE stream and refetches itself on success. Buttons run
        the FULL corpus — the weekly cron covers fresh-only (--since-days)."""
        if request.method == "OPTIONS":
            return ("", 204)
        from pipeline.evals_panel import RUNNABLE_PURPOSES

        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        purpose = str(body.get("purpose", ""))
        if purpose not in RUNNABLE_PURPOSES:
            return ({"error": f"purpose must be one of {list(RUNNABLE_PURPOSES)}"}, 400)
        argv = managed_python_argv(
            repo_root,
            repo_root / "execution" / "run_llm_evals.py",
            "--purpose",
            purpose,
            "--repo-root",
            str(repo_root),
        )
        try:
            job = job_registry.start(ticker="_REPO", kind=f"eval-{purpose}", argv=argv)
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

    @app.route("/actions/stream/<job_id>", methods=["GET"])
    def stream_action(job_id: str):
        job = job_registry.get(job_id)
        if job is None:
            return ({"error": "job not found"}, 404)
        return Response(
            stream_with_context(job.stream_events()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.route("/actions/jobs", methods=["GET"])
    def list_jobs():
        return {"jobs": job_registry.list_jobs()}
