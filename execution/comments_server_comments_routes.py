"""Comment CRUD and thesis/comment processing routes."""

from __future__ import annotations

from datetime import date
from typing import cast

from comments_server_context import ServerContext
from flask import Flask, Response, request
from process_report_comments import (
    preview_thesis_edits,
    process_comments_for_ticker,
    resolve_latest_report_date,
)

import comments
import ticker_validation
from dispatch_registry import RegistryConflict
from llm.cli import LLMBudgetExceeded, is_hard_stop
from logging_config import get_correlation_id
from runtime.python_process import managed_python_argv


def _parse_date(s: str) -> date:
    return date.fromisoformat(s[:10])


def register_comments_routes(app: Flask, ctx: ServerContext) -> None:
    internal_failure = ctx.internal_failure
    log_redacted_failure = ctx.log_redacted_failure
    job_registry = ctx.job_registry
    repo_root = ctx.repo_root

    # ----- COMMENTS -----

    @app.route("/comments", methods=["OPTIONS"])
    def comments_options():
        return ("", 204)

    @app.route("/comments", methods=["GET"])
    def list_comments_endpoint():
        ticker = request.args.get("ticker")
        report_date_str = request.args.get("report_date")
        if not ticker or not report_date_str:
            return ({"error": "ticker and report_date required"}, 400)
        report_date = _parse_date(report_date_str)
        store = comments.load_store(repo_root, ticker, report_date)
        return Response(store.model_dump_json(indent=2), mimetype="application/json")

    @app.route("/comments", methods=["POST"])
    def create_comment_endpoint():
        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        try:
            ticker = cast(str, body["ticker"])
            report_date = _parse_date(cast(str, body["report_date"]))
            anchor = comments.Anchor.model_validate(body["anchor"])
            text = cast(str, body["comment"])
            intent = cast(comments.IntentType, body.get("intent") or None)
            selected_text = cast(str | None, body.get("selected_text"))
        except (KeyError, ValueError, TypeError) as e:
            return ({"error": f"bad payload: {e}"}, 400)
        c = comments.append_comment(
            repo_root,
            ticker,
            report_date,
            anchor=anchor,
            text=text,
            selected_text=selected_text,
            intent=intent,
        )
        return Response(c.model_dump_json(), mimetype="application/json", status=201)

    @app.route("/comments/<comment_id>", methods=["PATCH", "OPTIONS"])
    def patch_comment_endpoint(comment_id: str):
        if request.method == "OPTIONS":
            return ("", 204)
        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        try:
            ticker = cast(str, body["ticker"])
            report_date = _parse_date(cast(str, body["report_date"]))
        except (KeyError, ValueError, TypeError):
            return ({"error": "ticker + report_date required"}, 400)
        status = cast(comments.CommentStatus | None, body.get("status"))
        resolution = cast(str | None, body.get("resolution_note"))
        intent = cast(comments.IntentType, body.get("intent"))
        updated = comments.update_comment(
            repo_root,
            ticker,
            report_date,
            comment_id,
            status=status,
            resolution_note=resolution,
            intent=intent,
        )
        if updated is None:
            return ({"error": "comment not found"}, 404)
        return Response(updated.model_dump_json(), mimetype="application/json")

    @app.route("/comments/<comment_id>", methods=["DELETE"])
    def delete_comment_endpoint(comment_id: str):
        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        try:
            ticker = cast(str, body["ticker"])
            report_date = _parse_date(cast(str, body["report_date"]))
        except (KeyError, ValueError, TypeError):
            return ({"error": "ticker + report_date required"}, 400)
        ok = comments.delete_comment(repo_root, ticker, report_date, comment_id)
        return ({"deleted": ok}, 200 if ok else 404)

    # ----- COMMENT PROCESSING + THESIS EDITING (PR D) -----

    @app.route("/api/thesis/<ticker>/preview", methods=["POST", "OPTIONS"])
    def thesis_preview(ticker: str):
        """Synchronous dry-run preview of the open edit_thesis / edit_structured
        comments: before/after thesis + a unified diff + structured field
        changes, writing nothing. The Opus routers run here (apply=False), so a
        hard budget/setup stop propagates (402/503) while a transient or
        unparseable LLM response degrades at component scope (200 degraded)."""
        if request.method == "OPTIONS":
            return ("", 204)
        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        try:
            report_date = _parse_date(cast(str, body["report_date"]))
        except (KeyError, ValueError, TypeError):
            return ({"error": "report_date required (YYYY-MM-DD)"}, 400)
        raw_ids = body.get("comment_ids")
        comment_ids = (
            [str(x) for x in cast("list[object]", raw_ids)] if isinstance(raw_ids, list) else None
        )
        try:
            result = preview_thesis_edits(repo_root, ticker, report_date, comment_ids=comment_ids)
        except Exception as exc:
            # is_hard_stop (budget/setup) must propagate — re-running won't help;
            # everything else is transient and degrades at component scope.
            if is_hard_stop(exc):
                status = 402 if isinstance(exc, LLMBudgetExceeded) else 503
                return internal_failure("thesis preview unavailable", exc, status=status)
            log_redacted_failure("thesis preview degraded", exc, level="warning")
            return (
                {
                    "degraded": True,
                    "reason": "thesis preview unavailable; retry the request",
                    "correlation_id": get_correlation_id(),
                },
                200,
            )
        return (result, 200)

    @app.route("/api/comments/process", methods=["POST", "OPTIONS"])
    def comments_process():
        """Process a ticker's open comments. apply=false → synchronous dry-run
        (each comment's drafted resolution, inline). apply=true → dispatch the
        real run (mutations + auto-rebuild) as a single-flight job, streamed
        over /actions/stream/<job_id>."""
        if request.method == "OPTIONS":
            return ("", 204)
        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        ticker = str(body.get("ticker", "")).upper()
        if not ticker:
            return ({"error": "ticker required"}, 400)
        try:
            ticker = ticker_validation.safe_ticker(ticker)
        except ValueError:
            return ({"error": "invalid ticker"}, 400)
        apply_flag = bool(body.get("apply", False))
        report_date_str = cast(str | None, body.get("report_date"))
        report_date: date | None = None
        if report_date_str:
            try:
                report_date = _parse_date(report_date_str)
            except (ValueError, TypeError):
                return ({"error": "bad report_date"}, 400)

        if not apply_flag:
            rd = report_date or resolve_latest_report_date(repo_root, ticker)
            if rd is None:
                return ({"error": "no report found for ticker; pass report_date"}, 404)
            try:
                res = process_comments_for_ticker(repo_root, ticker, rd, apply=False, clear=False)
            except Exception as exc:
                if is_hard_stop(exc):
                    status = 402 if isinstance(exc, LLMBudgetExceeded) else 503
                    return internal_failure("comment preview unavailable", exc, status=status)
                log_redacted_failure("comment preview degraded", exc, level="warning")
                return (
                    {
                        "degraded": True,
                        "reason": "comment preview unavailable; retry the request",
                        "correlation_id": get_correlation_id(),
                    },
                    200,
                )
            return (res, 200)

        # apply=true → dispatch the real run as a single-flight job.
        script = repo_root / "execution" / "process_report_comments.py"
        argv = managed_python_argv(repo_root, script, "--ticker", ticker, "--apply")
        if report_date is not None:
            argv += ["--report-date", report_date.isoformat()]
        if bool(body.get("clear", False)):
            argv.append("--clear")
        if bool(body.get("no_rebuild", False)):
            argv.append("--no-rebuild")
        try:
            job = job_registry.start(ticker=ticker, kind="comments-process", argv=argv)
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
