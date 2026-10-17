"""Advisor memos, red team, eval, socratic, and job-stream routes."""

from __future__ import annotations

from typing import cast

from comments_server_context import ServerContext
from flask import Flask, Response, request, stream_with_context

import ticker_validation
from dispatch_registry import RegistryConflict
from logging_config import get_correlation_id
from runtime.python_process import managed_python_argv


def register_advisor_routes(app: Flask, ctx: ServerContext) -> None:
    internal_failure = ctx.internal_failure
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

    @app.route("/actions/advisor-memo", methods=["POST", "OPTIONS"])
    def start_advisor_memo():
        """Run an advisor memo generation (master build P2.3) as a streamed
        single-flight job: {"kind": "next_dollar" | "swap_checks" | "all"}.
        Runs execution/run_advisor_memos.py; the Memos panel consumes the
        SSE stream and refetches itself on success."""
        if request.method == "OPTIONS":
            return ("", 204)
        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        memo_kind = str(body.get("kind", ""))
        if memo_kind not in ("next_dollar", "swap_checks", "all"):
            return ({"error": "kind must be next_dollar | swap_checks | all"}, 400)
        argv = managed_python_argv(
            repo_root,
            repo_root / "execution" / "run_advisor_memos.py",
            "--kind",
            memo_kind,
            "--repo-root",
            str(repo_root),
        )
        if ctx.source_state_root is not None:
            argv.extend(("--state-root", str(ctx.source_state_root)))
        try:
            job = job_registry.start(ticker="_REPO", kind=f"advisor-{memo_kind}", argv=argv)
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

    # ----- SOCRATIC THINK-THROUGH (master build P2.4) -----
    # The only path to a per-holding stance (locked advisor posture). Step 1
    # (wave3b Task 4) is now an honest BACKGROUND job — build_advisor_context
    # + the Opus premortem + the questions call routinely runs ~2 minutes,
    # and a synchronous fetch() stalling the browser that long is exactly
    # what the owner ratified away from. Step 2 (the memo) stays synchronous
    # on its own POST below: the owner is present and typing at that point.

    @app.route("/actions/socratic-questions", methods=["POST", "OPTIONS"])
    def start_socratic_questions():
        """Step 1 as a streamed single-flight job: {"ticker": "NU"}. Runs
        ``execution/run_socratic_questions.py``, which persists the result
        (``advisor.socratic.persist_prelude``); the /socratic/<T> page (and
        the Memos panel's think-through flow) stream this job's log via the
        shared ``/actions/stream/<job_id>`` SSE channel, then GET
        ``/api/socratic/questions/<ticker>`` once it reports done."""
        if request.method == "OPTIONS":
            return ("", 204)
        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        ticker = str(body.get("ticker") or "").strip().upper()
        if not ticker:
            return ({"error": "ticker required"}, 400)
        argv = managed_python_argv(
            repo_root,
            repo_root / "execution" / "run_socratic_questions.py",
            ticker,
            "--repo-root",
            str(repo_root),
        )
        if ctx.source_state_root is not None:
            argv.extend(("--state-root", str(ctx.source_state_root)))
        try:
            job = job_registry.start(ticker=ticker, kind="socratic-questions", argv=argv)
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

    @app.route("/api/socratic/questions/<ticker>", methods=["GET"])
    def socratic_questions_result(ticker: str):
        """The persisted Step-1 prelude the background job wrote — read back
        once its SSE stream reports done. 404 until a job has completed for
        this ticker (the page's honest-cost button is the only way forward;
        never a silently empty form)."""
        from advisor.socratic import read_current_prelude

        prelude = read_current_prelude(db_path, ticker.upper())
        if prelude is None:
            return ({"error": "no generated questions yet for this ticker"}, 404)
        return {
            "ticker": prelude.ticker,
            "questions": prelude.questions,
            "context_block": prelude.context_block,
        }

    @app.route("/api/socratic/memo", methods=["POST", "OPTIONS"])
    def socratic_memo():
        """Step 2: the decision memo from the owner's answers. Body:
        {"ticker", "questions": [...], "answers": [...], "horizon_days"}.
        Persists kind='socratic' (stance + horizon, P2.5-scoreable) and
        returns the memo id + stance + rendered body HTML."""
        if request.method == "OPTIONS":
            return ("", 204)
        from advisor.socratic import generate_decision_memo
        from advisor.store import get_memo
        from pipeline.analytical_dashboard_html import light_markdown_to_html

        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        ticker = str(body.get("ticker") or "").strip().upper()
        raw_q, raw_a = body.get("questions"), body.get("answers")
        if not ticker or not isinstance(raw_q, list) or not isinstance(raw_a, list):
            return ({"error": "ticker, questions[] and answers[] required"}, 400)
        questions = [str(q) for q in cast("list[object]", raw_q)]
        answers = [str(a) for a in cast("list[object]", raw_a)]
        try:
            horizon_days = int(cast("int | str | float", body.get("horizon_days") or 90))
        except (TypeError, ValueError):
            return ({"error": "horizon_days must be an integer"}, 400)
        try:
            result = generate_decision_memo(
                repo_root,
                ticker,
                questions=questions,
                answers=answers,
                horizon_days=horizon_days,
                source_context=ctx.source_context,
            )
        except ValueError as exc:  # length mismatch / empty answers / bad horizon
            return ({"error": str(exc)}, 400)
        except Exception as exc:  # hard stops surface loudly to the form
            return internal_failure("memo generation failed", exc)
        if not result.ok or result.memo_id is None:
            return internal_failure(
                "memo generation failed",
                result.skipped_reason or "no memo id returned",
            )
        memo = get_memo(result.memo_id, db_path=db_path)
        return {
            "memo_id": result.memo_id,
            "ticker": result.ticker,
            "title": result.title,
            "stance": memo.stance if memo else None,
            "horizon_days": horizon_days,
            "body_html": light_markdown_to_html(memo.body_md) if memo else "",
        }

    @app.route("/socratic/<ticker>", methods=["GET"])
    def socratic_page(ticker: str):
        """Standalone think-through page — the per-ticker workspace chat links
        here (its sidebar button + the chat system prompt's pointer)."""
        from pipeline.advisor_memos_panel import render_socratic_page

        return Response(render_socratic_page(ticker.upper()), mimetype="text/html")

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
