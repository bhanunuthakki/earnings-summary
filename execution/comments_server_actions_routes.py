"""Repo maintenance and job-dispatch action routes."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from comments_server_context import ServerContext
from comments_server_shared import (
    MAINTENANCE_ACTIONS,
    README_RUN_ID_RX,
    opt_int,
)
from flask import Flask, request
from refresh_dispatch import STEP_NAMES

import ticker_validation
from dispatch_registry import RegistryConflict
from identity import DEFAULT_USER_ID
from integrations.portfolio_tracker_v1 import TrackerV1Client
from operations.paths import (
    portfolio_tracker_activation_receipt_path,
    portfolio_tracker_receipt_path,
)
from runtime.portfolio_tracker import (
    AtomicFileLease,
    ListenerObservation,
    PortfolioTrackerRuntimeManager,
    RuntimeConfig,
    derive_activation_idempotency_key,
    health_is_healthy,
    is_loopback_bind_host,
    parse_tracker_bind_url,
    read_supervisor_listener_ownership,
    start_portfolio_tracker_scheduler_task,
    write_tracker_activation_receipt,
)
from runtime.python_process import (
    ManagedPythonUnavailableError,
    application_python_executable,
    managed_python_argv,
)


def register_actions_routes(app: Flask, ctx: ServerContext) -> None:
    collect_current_readme_status = ctx.collect_current_readme_status
    db_path = ctx.db_path
    job_registry = ctx.job_registry
    repo_root = ctx.repo_root
    resolved_code_root = ctx.resolved_code_root

    # ----- ACTIONS (PR 2a — refresh dispatcher) -----

    @app.route("/actions/refresh", methods=["POST", "OPTIONS"])
    def start_refresh():
        if request.method == "OPTIONS":
            return ("", 204)
        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        try:
            ticker = str(body["ticker"]).upper()
            mode = body.get("mode", "stale")
        except (KeyError, TypeError):
            return ({"error": "ticker required"}, 400)
        if mode not in ("stale", "full"):
            return ({"error": f"mode must be 'stale' or 'full', got {mode!r}"}, 400)
        try:
            ticker = ticker_validation.safe_ticker(ticker)
        except ValueError:
            return ({"error": "invalid ticker"}, 400)
        force_budget_bypass = bool(body.get("force_budget_bypass", False))
        force = bool(body.get("force", False))
        steps_raw = body.get("steps")
        steps: list[str] | None = None
        if steps_raw is not None:
            if not isinstance(steps_raw, list):
                return ({"error": "steps must be a list of step names"}, 400)
            steps = [str(s) for s in cast("list[object]", steps_raw)]
            bad = [s for s in steps if s not in STEP_NAMES]
            if bad:
                return ({"error": f"unknown step(s): {bad}; valid: {list(STEP_NAMES)}"}, 400)

        dispatcher = resolved_code_root / "execution" / "refresh_dispatch.py"
        try:
            executable = application_python_executable(resolved_code_root)
        except ManagedPythonUnavailableError:
            return ({"error": "managed_python_unavailable"}, 503)
        argv = managed_python_argv(
            resolved_code_root,
            dispatcher,
            "--ticker",
            ticker,
            "--mode",
            mode,
            "--state-root",
            str(repo_root),
            "--db",
            str(db_path),
            executable=executable,
        )
        if force:
            argv.append("--force")
        if steps:
            argv += ["--steps", ",".join(steps)]
        if force_budget_bypass:
            argv.append("--force-budget-bypass")
        try:
            job = job_registry.start(
                ticker=ticker,
                kind=f"refresh-{mode}",
                argv=argv,
                code_root=resolved_code_root,
                cwd=str(resolved_code_root),
            )
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

    @app.route("/actions/start-tracker", methods=["POST", "OPTIONS"])
    def start_tracker_server():
        """Start the companion Portfolio Tracker from explicit API/root config.

        The offline Portfolio tab gets a button instead of a prose CLI hint.
        Activation requests the canonical LOCAL SYSTEM Scheduler task; it never
        creates a dashboard/session-owned listener. Success requires its fresh
        supervisor receipt, exclusive endpoint ownership, and healthy HealthV1;
        503 means that proof is unavailable. A missing configured root is a 404.
        """
        if request.method == "OPTIONS":
            return ("", 204)
        owner = "portfolio-tracker-service"
        api_url = os.environ.get("PORTFOLIO_TRACKER_API_URL")
        if not api_url:
            return (
                {"error": "PORTFOLIO_TRACKER_API_URL is required for tracker activation"},
                400,
            )
        tracker_root_raw = os.environ.get("PORTFOLIO_TRACKER_ROOT")
        if not tracker_root_raw:
            return (
                {"error": "PORTFOLIO_TRACKER_ROOT is required for tracker activation"},
                400,
            )
        tracker_root = Path(tracker_root_raw).expanduser().resolve()
        if not tracker_root.exists():
            return (
                {"error": f"configured Portfolio Tracker root not found at {tracker_root}"},
                404,
            )
        bind = parse_tracker_bind_url(api_url)
        if bind is None or not is_loopback_bind_host(bind[0]):
            return ({"error": "configured Portfolio Tracker API URL cannot be safely bound"}, 400)
        bind_host, bind_port = bind
        supervisor_receipt_path = portfolio_tracker_receipt_path(repo_root)
        activation_receipt_path = portfolio_tracker_activation_receipt_path(repo_root)
        activation_lease_path = activation_receipt_path.with_name(
            "portfolio-tracker.activation.lease"
        )

        def inspect_listener() -> ListenerObservation:
            fetch = TrackerV1Client(base_url=api_url).probe_v1()
            checked_at = datetime.now(UTC)
            supervisor = read_supervisor_listener_ownership(
                supervisor_receipt_path,
                listener_owner=owner,
                bind_host=bind_host,
                bind_port=bind_port,
                observed_at=checked_at,
            )
            attributed_owner = (
                owner
                if fetch.data is not None
                and health_is_healthy(fetch.data, now=checked_at)
                and supervisor is not None
                else None
            )
            return ListenerObservation(
                healthy=fetch.available and health_is_healthy(fetch.data, now=checked_at),
                owner=attributed_owner,
                pid=supervisor.pid if supervisor is not None else None,
                health_checked_at=checked_at,
                health=fetch.data,
            )

        def start_listener(config: RuntimeConfig) -> None:
            del config
            start_portfolio_tracker_scheduler_task()

        now = datetime.now(UTC)
        manager = PortfolioTrackerRuntimeManager(
            config=RuntimeConfig(
                listener_owner=owner,
                daily_refresh_owner=owner,
                idempotency_key=derive_activation_idempotency_key(now),
            ),
            inspect_listener=inspect_listener,
            start_listener=start_listener,
            now=lambda: datetime.now(UTC),
            lease=AtomicFileLease(activation_lease_path),
        )
        persisted = manager.ensure_running(
            receipt_path=activation_receipt_path,
            receipt_writer=write_tracker_activation_receipt,
        )
        if persisted.lifecycle_state in {"started", "already_running"} and (
            not persisted.listener.healthy
            or not health_is_healthy(persisted.listener.health, now=persisted.recorded_at)
            or persisted.listener.owner != owner
            or persisted.listener.pid is None
            or persisted.listener.pid <= 0
        ):
            # Fail closed if an injected or legacy manager violates the
            # ownership-proof contract at this boundary.
            failed = persisted.model_copy(
                update={
                    "lifecycle_state": "failed",
                    "listener": persisted.listener.model_copy(update={"healthy": False}),
                    "failure_detail": "listener HealthV1 health/ownership proof is missing",
                }
            )
            return ({"error": failed.failure_detail}, 503)
        if (
            persisted.lifecycle_state == "ownership_conflict"
            or persisted.failure_detail == "RegistryConflict"
        ):
            return (
                {"error": persisted.failure_detail or "tracker runtime ownership conflict"},
                409,
            )
        if persisted.lifecycle_state == "failed":
            return ({"error": persisted.failure_detail or "tracker runtime start failed"}, 503)
        return (
            {
                "lifecycle_state": persisted.lifecycle_state,
                "idempotency_key": persisted.idempotency_key,
            },
            200,
        )

    @app.route("/actions/run-scenario", methods=["POST", "OPTIONS"])
    def start_run_scenario():
        """Run the whole-book macro-stress lens for a named scenario (L5) —
        execution/run_scenario.py --scenario <id> --portfolio. The Portfolio →
        Risk tab's scenario picker POSTs here; the LLM digest streams over the
        standard /actions/stream channel and the panel re-fetches the cached
        result on done. 400 for an unknown scenario id; 409 when a scenario job
        is already running."""
        if request.method == "OPTIONS":
            return ("", 204)
        from macro_scenarios import all_scenario_ids

        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        scenario = str(body.get("scenario", "")).strip()
        if scenario not in all_scenario_ids():
            return ({"error": f"unknown scenario: {scenario or '(none)'}"}, 400)
        script = repo_root / "execution" / "run_scenario.py"
        argv = managed_python_argv(
            repo_root,
            script,
            "--scenario",
            scenario,
            "--portfolio",
            "--repo-root",
            str(repo_root),
        )
        try:
            job = job_registry.start(ticker="_REPO", kind="run-scenario", argv=argv)
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

    @app.route("/actions/refresh-ir", methods=["POST", "OPTIONS"])
    def start_refresh_ir():
        """Refresh a ticker's KPIs from its IR historical-data spreadsheet.

        Runs execution/refresh_ir_kpis.py --discover (headless browser resolves
        the current spreadsheet URL → download → parse → tier-ingest, superseding
        the LLM brief/press values). Streams via /actions/stream/<job_id> like
        /actions/refresh.
        """
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
        try:
            quarters = int(cast(str | int, body.get("quarters", 8)))
        except (TypeError, ValueError):
            return ({"error": "quarters must be an integer"}, 400)

        script = resolved_code_root / "execution" / "refresh_ir_kpis.py"
        try:
            executable = application_python_executable(resolved_code_root)
        except ManagedPythonUnavailableError:
            return ({"error": "managed_python_unavailable"}, 503)
        argv = managed_python_argv(
            resolved_code_root,
            script,
            "--ticker",
            ticker,
            "--discover",
            "--quarters",
            str(quarters),
            "--repo-root",
            str(repo_root),
            "--db",
            str(db_path),
            executable=executable,
        )
        try:
            job = job_registry.start(
                ticker=ticker,
                kind="refresh-ir",
                argv=argv,
                code_root=resolved_code_root,
                cwd=str(resolved_code_root),
            )
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

    @app.route("/actions/dcf-export", methods=["POST", "OPTIONS"])
    def start_dcf_export():
        """Push a ticker's dcf/<T>.xlsx to a Google Sheet (execution/dcf_sheets.py
        export). Re-exports the linked Sheet if one exists, else creates one (and,
        for service-account creds, shares it to `share_with`) and links its id in
        holdings. Streams via /actions/stream/<job_id>. Needs Google credentials —
        see directives/dcf_gsheets_setup.md."""
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
        script = repo_root / "execution" / "dcf_sheets.py"
        argv = managed_python_argv(
            repo_root,
            script,
            "export",
            "--ticker",
            ticker,
            "--repo-root",
            str(repo_root),
        )
        share_with = str(body.get("share_with", "")).strip()
        if share_with:
            argv += ["--share-with", share_with]
        if bool(body.get("new", False)):
            argv.append("--new")
        try:
            job = job_registry.start(ticker=ticker, kind="dcf-export", argv=argv)
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

    @app.route("/actions/dcf-import", methods=["POST", "OPTIONS"])
    def start_dcf_import():
        """Pull the ticker's linked Google Sheet and recompute the DCF
        (execution/dcf_sheets.py import → refresh_dcf.refresh_one → dcf_runs). The
        Sheet id comes from `sheet_id` in the body or holdings dcf_defaults.gsheet_id.
        Streams via /actions/stream/<job_id>. Needs Google credentials."""
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
        script = repo_root / "execution" / "dcf_sheets.py"
        argv = managed_python_argv(
            repo_root,
            script,
            "import",
            "--ticker",
            ticker,
            "--repo-root",
            str(repo_root),
        )
        sheet_id = str(body.get("sheet_id", "")).strip()
        if sheet_id:
            argv += ["--sheet-id", sheet_id]
        try:
            job = job_registry.start(ticker=ticker, kind="dcf-import", argv=argv)
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

    @app.route("/actions/rebuild-dcfs", methods=["POST", "OPTIONS"])
    def rebuild_dcfs():
        """Rebuild every DCF-maintained name so a change to the global DCF
        assumptions (risk-free / ERP / tax) propagates into the workbooks +
        dcf_runs. Single-flight job streamed over /actions/stream/<job_id>;
        ``refresh_dcf --all-named`` prints per-ticker results (fair value +
        over/under). The 'Rebuild affected models' button in the Global DCF
        assumptions drawer section calls this."""
        if request.method == "OPTIONS":
            return ("", 204)
        argv = managed_python_argv(
            repo_root,
            repo_root / "execution" / "refresh_dcf.py",
            "--all-named",
            "--repo-root",
            str(repo_root),
        )
        try:
            job = job_registry.start(ticker="_REPO", kind="rebuild-dcfs", argv=argv)
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

    @app.route("/actions/maintenance", methods=["POST", "OPTIONS"])
    def start_maintenance():
        """Repo-wide maintenance chores (seed KPI defs · process dropped docs ·
        sweep output history · onboard pending · onboard <ticker>) dispatched as
        single-flight jobs, streamed over /actions/stream/<job_id>. Each runs an
        existing CLI under execution/."""
        if request.method == "OPTIONS":
            return ("", 204)
        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        action = str(body.get("action", ""))
        if action == "onboard":
            ticker = str(body.get("ticker", "")).upper()
            if not ticker:
                return ({"error": "onboard requires a ticker"}, 400)
            try:
                ticker = ticker_validation.safe_ticker(ticker)
            except ValueError:
                return ({"error": "invalid ticker"}, 400)
            parts = ["onboard_ticker.py", "--ticker", ticker]
            slot_ticker, kind = ticker, "maint-onboard"
        elif action in MAINTENANCE_ACTIONS:
            parts = MAINTENANCE_ACTIONS[action]
            slot_ticker, kind = "_REPO", f"maint-{action}"
        else:
            valid = [*sorted(MAINTENANCE_ACTIONS), "onboard"]
            return ({"error": f"unknown action {action!r}; valid: {valid}"}, 400)
        argv = managed_python_argv(repo_root, repo_root / "execution" / parts[0], *parts[1:])
        try:
            job = job_registry.start(ticker=slot_ticker, kind=kind, argv=argv)
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

    @app.route("/actions/readme-update", methods=["POST", "OPTIONS"])
    def start_readme_update():
        """Preview-and-judge or apply one exact approved README candidate."""

        if request.method == "OPTIONS":
            return ("", 204)
        raw_body = request.get_json(silent=True)
        if not isinstance(raw_body, dict):
            return ({"error": "JSON request body must be an object"}, 400)
        body = cast("dict[str, object]", raw_body)
        action = str(body.get("action", "")).strip()
        argv = managed_python_argv(
            resolved_code_root,
            resolved_code_root / "execution" / "update_readme.py",
            "--repo-root",
            str(resolved_code_root),
            "--db",
            str(db_path),
        )
        if action == "preview":
            kind = "readme-preview"
        elif action == "apply":
            run_id = str(body.get("run_id", "")).strip()
            if README_RUN_ID_RX.fullmatch(run_id) is None:
                return ({"error": "valid README updater run_id required"}, 400)
            status = collect_current_readme_status()
            if (
                not status.can_apply
                or status.state != "approved_preview"
                or status.run_id != run_id
            ):
                return ({"error": "README candidate is not the current approved preview"}, 409)
            argv.extend(["--apply-run", run_id])
            kind = "readme-apply"
        else:
            return ({"error": "action must be 'preview' or 'apply'"}, 400)
        try:
            job = job_registry.start(
                ticker="_REPO",
                kind=kind,
                argv=argv,
                cwd=str(resolved_code_root),
                write_sets=["portfolio-db", "readme-updater"],
            )
        except RegistryConflict as exc:
            return ({"error": str(exc)}, 409)
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

    @app.route("/actions/resolve-issue", methods=["POST", "OPTIONS"])
    def resolve_issue():
        """Mark one open ``validation_issues`` row resolved (S10 — provenance is
        actionable). Unlike the sibling ``/actions/*`` endpoints this is a
        SYNCHRONOUS DB write, not a streamed job: JSON body
        ``{"issue_id": int, "resolution_note"?: str, "resolved_by"?: str}``.
        Returns ``{"ok": true, "issue_id", "resolved_at"}`` on success, 404 when
        the id is unknown or already resolved, 400 on a missing/bad id."""
        if request.method == "OPTIONS":
            return ("", 204)
        from validation_issues_store import resolve_validation_issue

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        issue_id = opt_int(payload.get("issue_id"))
        if issue_id is None:
            return ({"error": "issue_id (int) required"}, 400)
        note_raw = payload.get("resolution_note")
        by_raw = payload.get("resolved_by")
        resolved_at = resolve_validation_issue(
            issue_id,
            resolved_by=str(by_raw) if by_raw is not None else DEFAULT_USER_ID,
            resolution_note=str(note_raw) if note_raw is not None else None,
            db_path=db_path,
        )
        if resolved_at is None:
            return ({"error": f"issue {issue_id} not found or already resolved"}, 404)
        return {"ok": True, "issue_id": issue_id, "resolved_at": resolved_at}
