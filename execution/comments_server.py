"""execution/comments_server.py
-------------------------------
Tiny Flask server backing the workspace report's inline-comments + chat
panel. Default port 7421 (matches the `server_url` boot value the renderer
inlines).

Endpoints:
  POST   /comments            create a new comment
  GET    /comments?ticker=&report_date=   list comments for a (ticker, date)
  PATCH  /comments/<id>       update status / append thread
  DELETE /comments/<id>       hard-delete
  POST   /chat/<ticker>       streaming chat (Phase 3 — see workspace_chat.py)
  POST   /chat/<ticker>/apply apply a chatbot-proposed diff (Phase 4)
  GET    /healthz             health check

Usage:
    python execution/comments_server.py
    python execution/comments_server.py --port 7421 --repo-root /path/to/repo

CORS: the server never emits `Access-Control-Allow-Origin: *`. It echoes back
only the file:// renderer's `null` Origin and loopback Origins — so the local
dashboard works while a cross-site Origin gets no CORS header (CSRF defense).
If you bind to 0.0.0.0 or another interface, set `COMMENTS_SERVER_CORS_WHITELIST`
to a comma-separated list of allowed Origins; the server echoes a request's
Origin back only when it matches.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import json
import os
import queue
import sys
import urllib.parse
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(SCRIPT_DIR))  # import sibling execution/ modules (refresh_dispatch)

try:
    from flask import Flask, Response, abort, redirect, request, send_file, stream_with_context
except ImportError:  # pragma: no cover - install hint
    print(
        "Flask not installed. Install with: pip install flask",
        file=sys.stderr,
    )
    sys.exit(1)

import sqlite3  # noqa: E402

from process_report_comments import (  # noqa: E402
    _resolve_latest_report_date,
    preview_thesis_edits,
    process_comments_for_ticker,
)
from refresh_dispatch import STEP_NAMES  # noqa: E402

import comments  # noqa: E402
import llm_budget  # noqa: E402
import ticker_settings  # noqa: E402
from chat_session import apply_chat_diff, build_chat_response  # noqa: E402
from dashboard import render_alert_feed, render_morning_digest  # noqa: E402
from dispatch_registry import Registry, RegistryConflict  # noqa: E402
from identity import DEFAULT_USER_ID  # noqa: E402
from llm.cli import LLMBudgetExceeded, is_hard_stop  # noqa: E402
from pipeline.analytical_dashboard import build_analytical_dashboard  # noqa: E402
from pipeline.command_center_shell import render_overview_panel, render_shell  # noqa: E402
from pipeline.dashboard_status import build_dashboard_rows  # noqa: E402
from pipeline.research_cockpit import build_cockpit_rows  # noqa: E402
from pipeline.ticker_command_center import (  # noqa: E402
    build_ticker_command_center,
    render_holding_fragment,
)
from pipeline.tier_runner import tier_coverage_summary  # noqa: E402

# Repo-wide maintenance chores exposed on the dashboard, each dispatched as a
# single-flight job running an existing CLI under execution/. (Onboarding a
# specific ticker is handled separately — it needs a ticker argument.)
_MAINTENANCE_ACTIONS: dict[str, list[str]] = {
    "seed_kpis": ["seed_kpi_definitions.py", "--all"],
    "process_inbox": ["register_dropped_documents.py", "--all"],
    "sweep_history": ["sweep_output_history.py"],
    "onboard_pending": ["onboard_pending_tickers.py"],
}


def _cors_allow_origin(origin: str) -> str | None:
    """Return the ``Access-Control-Allow-Origin`` value to echo for ``origin``, or None.

    Allows the file:// workspace renderer (Origin ``"null"``) and any loopback
    origin so the local dashboard keeps working; a cross-site origin gets no
    CORS header, so the browser blocks its preflighted state-changing request
    (CSRF defense). For a non-loopback bind, an explicit comma-separated
    ``COMMENTS_SERVER_CORS_WHITELIST`` of allowed origins is honored.
    """
    if not origin:
        return None  # same-origin / non-browser caller needs no CORS header
    if origin == "null":
        return "null"
    try:
        hostname = urllib.parse.urlparse(origin).hostname or ""
    except ValueError:
        return None
    if hostname in ("127.0.0.1", "localhost", "::1"):
        return origin
    whitelist = [
        o.strip()
        for o in os.environ.get("COMMENTS_SERVER_CORS_WHITELIST", "").split(",")
        if o.strip()
    ]
    return origin if origin in whitelist else None


def _linked_gsheet(repo_root: Path, ticker: str) -> tuple[str | None, str | None]:
    """The ``(sheet_id, edit_url)`` of the Google Sheet linked to a ticker's DCF,
    or ``(None, None)`` when no ``dcf_defaults.gsheet_id`` is set in the holdings
    JSON. Shared by the ``/dcf/<T>`` redirect and the ``/api/dcf-sheet/<T>``
    endpoint so the two never diverge on how a Sheet link is resolved."""
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not path.exists():
        return None, None
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(raw, dict):
        return None, None
    dd = cast("dict[str, object]", raw).get("dcf_defaults")
    if not isinstance(dd, dict):
        return None, None
    gid = cast("dict[str, object]", dd).get("gsheet_id")
    if isinstance(gid, str) and gid:
        return gid, f"https://docs.google.com/spreadsheets/d/{gid}/edit"
    return None, None


def create_app(
    repo_root: Path,
    *,
    registry: Registry | None = None,
    chat_executor: concurrent.futures.Executor | None = None,
) -> Flask:
    app = Flask(__name__)
    db_path = repo_root / "data" / "portfolio.db"
    job_registry = registry or Registry()
    app.config["DISPATCH_REGISTRY"] = job_registry
    # Dedicated pool so a long-running LLM subprocess doesn't pin a Flask
    # request thread for the full 10-60s of a chat turn. Pool size caps
    # the number of concurrent chats; chunks flow back via per-request
    # queues. Tests can inject their own executor for isolation.
    chat_pool = chat_executor or concurrent.futures.ThreadPoolExecutor(
        max_workers=4, thread_name_prefix="comments-server-chat"
    )
    app.config["CHAT_EXECUTOR"] = chat_pool

    def _open_db() -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    @app.after_request
    def add_cors_headers(response):
        # The workspace report HTML opens via file://, so its browser Origin is
        # the literal string "null"; pages served by this server carry a
        # loopback Origin. Echo back ONLY those — never "*". A wildcard let any
        # site the user happened to be visiting drive state-changing POSTs
        # (refresh/onboard jobs, comment writes, the chat-apply file write)
        # against this unauthenticated localhost server: those routes require a
        # JSON content-type, which forces a CORS preflight that "*" answered.
        # Withholding the header makes the preflight fail, so the cross-site
        # request never fires. (See _cors_allow_origin for the whitelist path.)
        allowed = _cors_allow_origin(request.headers.get("Origin", ""))
        if allowed is not None:
            response.headers["Access-Control-Allow-Origin"] = allowed
            response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return response

    @app.route("/healthz", methods=["GET"])
    def healthz():
        return {"status": "ok", "repo_root": str(repo_root)}

    # ----- DASHBOARD (unified tabbed command-center shell) -----

    @app.route("/", methods=["GET"])
    def dashboard_page():
        """Unified tabbed command center. Overview — the Research cockpit
        (one attention-ranked row per holding: thesis health · valuation ·
        events) + the tier-coverage strip — is server-inlined for instant
        first paint; every other tab lazy-loads from ``GET /api/panel/<name>``
        on first activation. The standalone ``/analytical`` and ``/ticker/<t>``
        pages remain as deep-link targets."""
        conn = _open_db()
        try:
            rows = build_cockpit_rows(conn, repo_root)
        finally:
            conn.close()
        coverage = tier_coverage_summary(repo_root)
        overview = render_overview_panel(rows, coverage)
        return Response(render_shell(overview_html=overview), mimetype="text/html")

    @app.route("/api/dashboard", methods=["GET"])
    def dashboard_api():
        conn = _open_db()
        try:
            rows = build_dashboard_rows(conn, repo_root)
        finally:
            conn.close()
        return {k: [r.to_dict() for r in v] for k, v in rows.items()}

    @app.route("/api/overview", methods=["GET"])
    def overview_api():
        """Cross-ticker analytical overview as JSON: trigger ladder, insider
        activity, predictions, decisions ledger, and the (read-only) LLM
        spend/budget panel, plus tier coverage. Same data the static export
        and ``GET /analytical`` render — one code path, no divergence.

        Budget WRITES are intentionally not here — dashboard-managed budgets
        (editable caps + modes + override) are owned by the #215 track; this
        surfaces spend/cap/headroom read-only."""
        dash = build_analytical_dashboard(db_path)
        payload = dash.to_dict()
        payload["tier_coverage"] = tier_coverage_summary(repo_root)
        return payload

    @app.route("/api/source-calls", methods=["GET"])
    def source_calls_api():
        """Data-fetch cache effectiveness as JSON: the cross-source headline
        rollup (skip rate, calls avoided, dollars saved) + per-(source, kind)
        detail. Previously reachable only from the show_source_calls CLI; this
        makes cache effectiveness measurable from the app (v6 re-grade, Smart
        caching). ``?since=YYYY-MM-DD`` bounds the window."""
        from dataclasses import asdict

        from sources.registry import cache_effectiveness_overview

        since = request.args.get("since") or None
        return asdict(cache_effectiveness_overview(since=since, db_path=db_path))

    @app.route("/export/cio", methods=["GET"])
    def export_cio():
        """Download the Personal-CIO substrate (alerts / queued actions / thesis
        ledger) as an .xlsx workbook. Previously this existed only as the
        ``export_cio_xlsx`` CLI — unreachable from the :7421 app (v6 re-grade,
        Richness). ``?user_id=`` scopes the export. Built to a stable path under
        data/dashboard and streamed as an attachment."""
        from dashboard.cio_export import export_cio_workbook

        user_id = request.args.get("user_id", DEFAULT_USER_ID)
        out_path = repo_root / "data" / "dashboard" / "cio_export.xlsx"
        written = export_cio_workbook(out_path, user_id=user_id, db_path=db_path)
        return send_file(
            written,
            as_attachment=True,
            download_name="cio_export.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.route("/api/panel/<name>", methods=["GET"])
    def panel_fragment(name: str):
        """One analytical panel as a head/foot-less HTML fragment, for the lazy
        command-center shell — builds only that panel's section. ``?ticker=``
        scopes the dropdown-driven panels (prereads, insiders) to one name.
        404 for an unknown panel."""
        if name == "portfolio":
            # The Portfolio tab is enriched with live positions / % of book /
            # taxable breakdown from the companion tracker, layered on top of the
            # cached cross-portfolio synthesis memo. Degrades when the tracker is
            # offline (the synthesis still renders).
            from pipeline.portfolio_panel import render_portfolio_panel

            return Response(render_portfolio_panel(db_path), mimetype="text/html")

        if name == "ir_coverage":
            # Per-name IR auto-fetch coverage: which portfolio/eval names have
            # auto-fetched IR docs vs. which need a manual pull (+ why).
            from pipeline.ir_coverage_panel import render_ir_coverage_panel

            return Response(render_ir_coverage_panel(db_path), mimetype="text/html")

        if name == "source_calls":
            # Data-fetch cache effectiveness: per-source skip rate / calls avoided
            # / dollars saved, read from the source_calls provenance log.
            from pipeline.source_calls_panel import render_source_calls_panel

            return Response(render_source_calls_panel(db_path), mimetype="text/html")

        if name == "actions":
            # The IR-KPI refresh + repo-maintenance blocks, relocated from the
            # Overview tab to Governance → Actions (master build P1.2). Their
            # inline <script> wiring re-executes on injection (the shell's
            # injectHtml re-creates script tags).
            from pipeline.dashboard_html import render_actions_panel

            return Response(render_actions_panel(), mimetype="text/html")

        if name == "thesis_ledger":
            # The append-only history of every accepted, alert-driven thesis edit
            # (thesis_ledger_entries) — the populated decision history that was
            # reachable only via the /digest route (v6 re-grade, Richness).
            from pipeline.thesis_ledger_panel import render_thesis_ledger_panel

            user_id = request.args.get("user_id", DEFAULT_USER_ID)
            return Response(
                render_thesis_ledger_panel(db_path, user_id=user_id), mimetype="text/html"
            )

        from pipeline.analytical_dashboard_html import (
            PANEL_TO_SECTION,
            render_panel_fragment,
        )

        section_key = PANEL_TO_SECTION.get(name)
        if section_key is None:
            abort(404)
        ticker = request.args.get("ticker") or None
        dash = build_analytical_dashboard(db_path, sections={section_key}, ticker=ticker)
        fragment = render_panel_fragment(dash, name)
        if fragment is None:
            abort(404)
        return Response(fragment, mimetype="text/html")

    @app.route("/api/panel/holding", methods=["GET"])
    def holding_panel_fragment():
        """The per-holding drill-down as a head/foot-less fragment for the shell's
        Holding tab: command-center sections (freshness, position, analyses,
        decisions, artifacts, thesis) + the 5-min reread + report/DCF links + an
        embedded ``/reports/<t>`` iframe that carries the inline comment/chat/apply
        pipeline. Requires ``?ticker=`` (the cc-picker supplies it)."""
        ticker = request.args.get("ticker")
        if not ticker:
            return Response(
                '<div class="cc-empty">Pick a holding from the dropdown above.</div>',
                mimetype="text/html",
            )
        return Response(render_holding_fragment(repo_root, ticker), mimetype="text/html")

    @app.route("/analytical", methods=["GET"])
    def analytical_page():
        """The standalone analytical dashboard is folded into the unified shell;
        its content is the shell's Triggers tab. 302-redirect to that deep link
        so existing bookmarks keep working."""
        return redirect("/#holdings")

    # ----- PERSONAL-CIO ALERTING SURFACES (digest / feed) -----
    # Previously emitted only as static files (data/dashboard/...), unreachable
    # from the live command center — so a user living in the app never saw their
    # alerts. Serve the same renderers as live routes (linked from the shell
    # topbar). Both are read-only and degrade to a valid empty-state document
    # when the substrate tables are absent.

    @app.route("/digest", methods=["GET"])
    def digest_page():
        """Morning digest (what's new, outstanding actions, recent thesis
        changes). ``?date=YYYY-MM-DD`` overrides today; ``?user_id=`` scopes it."""
        render_date = datetime.now(UTC).date()
        date_arg = request.args.get("date")
        if date_arg:
            # Malformed ?date= falls back to today rather than 500-ing.
            with contextlib.suppress(ValueError):
                render_date = date.fromisoformat(date_arg)
        user_id = request.args.get("user_id", DEFAULT_USER_ID)
        html_text = render_morning_digest(date=render_date, user_id=user_id, db_path=db_path)
        return Response(html_text, mimetype="text/html")

    @app.route("/feed", methods=["GET"])
    def feed_page():
        """Chronological alert feed. Optional AND-composed filters:
        ``?ticker=``, ``?trigger_kind=``, ``?status=``, ``?limit=`` (default 200)."""
        try:
            limit = int(request.args.get("limit", "200"))
        except ValueError:
            limit = 200
        html_text = render_alert_feed(
            user_id=request.args.get("user_id", DEFAULT_USER_ID),
            ticker=request.args.get("ticker"),
            trigger_kind=request.args.get("trigger_kind"),
            status=request.args.get("status"),
            limit=limit,
            db_path=db_path,
        )
        return Response(html_text, mimetype="text/html")

    @app.route("/alerts", methods=["GET"])
    def alerts_page():
        """Alias for the alert feed (the alerts surface), preserving any filters."""
        qs = request.query_string.decode()
        return redirect("/feed" + (f"?{qs}" if qs else ""))

    # ----- LLM BUDGET (editable caps + on_exceed modes — the #215 track) -----

    @app.route("/api/llm-budgets", methods=["GET"])
    def llm_budgets_api():
        """Per-purpose budget rows (cap, MTD spend, headroom, on_exceed mode)
        for the editable budget panel. Read-only sibling of the POST below."""

        def _num(v: object) -> float:
            return float(str(v))  # Decimal/str/float at the dict boundary -> float

        out = [
            {
                "purpose": str(r["purpose"]),
                "monthly_cap_usd": _num(r["monthly_cap_usd"]),
                "on_exceed": str(r.get("on_exceed", "warn")),
                "current_spend_usd": _num(r["current_spend_usd"]),
                "headroom_pct": _num(r["headroom_pct"]),
                "warn_threshold_pct": _num(r["warn_threshold_pct"]),
                "hard_block": bool(r["hard_block"]),
            }
            for r in llm_budget.list_budgets(db_path=db_path)
        ]
        return {"budgets": out}

    @app.route("/api/llm-budgets/<purpose>", methods=["POST", "OPTIONS"])
    def set_llm_budget(purpose: str):
        """Update a purpose's monthly cap and/or on_exceed mode. JSON body:
        {"cap_usd": <number>, "on_exceed": "skip|block|warn"} — either or both.
        400 on bad input, 404 when the purpose has no budget row."""
        if request.method == "OPTIONS":
            return ("", 204)
        body = request.get_json(silent=True) or {}
        cap = body.get("cap_usd")
        mode = body.get("on_exceed")
        if cap is None and mode is None:
            return ({"error": "provide cap_usd and/or on_exceed"}, 400)
        applied = False
        if mode is not None:
            try:
                applied = llm_budget.set_mode(purpose, str(mode), db_path=db_path) or applied
            except ValueError as e:
                return ({"error": str(e)}, 400)
        if cap is not None:
            try:
                cap_f = float(cap)
            except (TypeError, ValueError):
                return ({"error": f"cap_usd must be a number, got {cap!r}"}, 400)
            if cap_f < 0:
                return ({"error": "cap_usd must be >= 0"}, 400)
            applied = llm_budget.set_cap(purpose, cap_f, db_path=db_path) or applied
        if not applied:
            return ({"error": f"no budget row for purpose {purpose!r}"}, 404)
        return {"purpose": purpose, "ok": True}

    @app.route("/api/ticker-settings/<ticker>", methods=["GET", "POST", "OPTIONS"])
    def ticker_settings_api(ticker: str):
        """Per-ticker dashboard settings. Today: `bypass_budget` — the persistent
        "always ignore LLM budget caps for this ticker" flag. GET returns it;
        POST {"bypass_budget": bool} upserts it (the #215 track's persistent
        override; build_artifacts ORs it with the one-shot --force-budget-bypass)."""
        if request.method == "OPTIONS":
            return ("", 204)
        t = ticker.upper()
        if request.method == "GET":
            return {
                "ticker": t,
                "bypass_budget": ticker_settings.get_bypass_budget(t, db_path=db_path),
            }
        body = request.get_json(silent=True) or {}
        if "bypass_budget" not in body:
            return ({"error": "bypass_budget required"}, 400)
        value = bool(body["bypass_budget"])
        if not ticker_settings.set_bypass_budget(t, value, db_path=db_path):
            return ({"error": "could not persist (ticker_settings table missing?)"}, 500)
        return {"ticker": t, "bypass_budget": value}

    @app.route("/api/ticker/<ticker>", methods=["GET"])
    def ticker_api(ticker: str):
        """Full per-ticker command-center state as JSON: identity/freshness,
        artifacts on disk, analyses run, recent decisions, read-only thesis,
        and the live position (from the companion portfolio-tracker, if
        reachable)."""
        tcc = build_ticker_command_center(repo_root, ticker)
        return tcc.to_dict()

    @app.route("/ticker/<ticker>", methods=["GET"])
    def ticker_page(ticker: str):
        """The standalone per-ticker page is folded into the unified shell; its
        content is the shell's Holding drill-down tab. 302-redirect to that deep
        link (ticker uppercased) so existing bookmarks keep working."""
        return redirect(f"/#holding={ticker.upper()}")

    @app.route("/reports/<ticker>", methods=["GET"])
    def latest_report_for_ticker(ticker: str):
        """Serve the most recently built workspace HTML for the ticker.

        Uses the latest filename (`<DATE>_workspace.html`) since YYYY-MM-DD
        sorts chronologically. Returns 404 if no build exists.
        """
        research_dir = repo_root / "output" / "research" / ticker.upper()
        if not research_dir.exists():
            abort(404)
        matches = sorted(research_dir.glob("*_workspace.html"))
        if not matches:
            abort(404)
        return send_file(matches[-1])

    @app.route("/dcf/<ticker>", methods=["GET"])
    def latest_dcf_for_ticker(ticker: str):
        """Open the ticker's DCF model. When a Google Sheet is linked (holdings
        ``dcf_defaults.gsheet_id``, set by ``dcf_sheets.py export``), 302-redirect
        to the live Sheet so the brief's DCF link opens the editable model in the
        browser instead of downloading an ``.xlsx``. Otherwise stream the live
        workbook (``dcf/<TICKER>.xlsx``), falling back to the most recent dated
        workbook under ``output/research/<T>/``. 404 if neither exists."""
        t = ticker.upper()
        _sid, sheet_url = _linked_gsheet(repo_root, t)
        if sheet_url:
            return redirect(sheet_url, code=302)
        live = repo_root / "dcf" / f"{t}.xlsx"
        if live.exists():
            return send_file(live)
        research_dir = repo_root / "output" / "research" / t
        dated = sorted(research_dir.glob("*_dcf.xlsx")) if research_dir.exists() else []
        if not dated:
            abort(404)
        return send_file(dated[-1])

    @app.route("/api/tickers", methods=["GET"])
    def tickers_api():
        """Tracked tickers (non-archived) for the command-center dropdowns:
        ``{"tickers": [{ticker, name, list_type}, ...]}`` sorted by list then symbol."""
        conn = _open_db()
        try:
            rows = conn.execute(
                "SELECT ticker, name, list_type FROM tracked_companies "
                "WHERE archived_at IS NULL ORDER BY list_type, ticker"
            ).fetchall()
        finally:
            conn.close()
        return {
            "tickers": [
                {"ticker": r["ticker"], "name": r["name"], "list_type": r["list_type"]}
                for r in rows
            ]
        }

    @app.route("/api/dcf-sheet/<ticker>", methods=["GET"])
    def dcf_sheet_link(ticker: str):  # pyright: ignore[reportUnusedFunction]  # registered via decorator
        """The Google Sheet linked to a ticker's DCF, if any:
        ``{"ticker", "sheet_id", "url"}`` (sheet_id/url null when unlinked). Read
        from holdings ``dcf_defaults.gsheet_id``, which an `export` populates."""
        t = ticker.upper()
        sheet_id, url = _linked_gsheet(repo_root, t)
        return {"ticker": t, "sheet_id": sheet_id, "url": url}

    # ----- ACTIONS (PR 2a — refresh dispatcher) -----

    @app.route("/actions/refresh", methods=["POST", "OPTIONS"])
    def start_refresh():
        if request.method == "OPTIONS":
            return ("", 204)
        body = request.get_json(silent=True) or {}
        try:
            ticker = str(body["ticker"]).upper()
            mode = body.get("mode", "stale")
        except (KeyError, TypeError):
            return ({"error": "ticker required"}, 400)
        if mode not in ("stale", "full"):
            return ({"error": f"mode must be 'stale' or 'full', got {mode!r}"}, 400)
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

        dispatcher = repo_root / "execution" / "refresh_dispatch.py"
        argv = [sys.executable, str(dispatcher), "--ticker", ticker, "--mode", mode]
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
        body = request.get_json(silent=True) or {}
        ticker = str(body.get("ticker", "")).upper()
        if not ticker:
            return ({"error": "ticker required"}, 400)
        try:
            quarters = int(body.get("quarters", 8))
        except (TypeError, ValueError):
            return ({"error": "quarters must be an integer"}, 400)

        script = repo_root / "execution" / "refresh_ir_kpis.py"
        argv = [
            sys.executable,
            str(script),
            "--ticker",
            ticker,
            "--discover",
            "--quarters",
            str(quarters),
            "--repo-root",
            str(repo_root),
        ]
        try:
            job = job_registry.start(ticker=ticker, kind="refresh-ir", argv=argv)
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
    def start_dcf_export():  # pyright: ignore[reportUnusedFunction]  # registered via decorator
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
        script = repo_root / "execution" / "dcf_sheets.py"
        argv = [
            sys.executable,
            str(script),
            "export",
            "--ticker",
            ticker,
            "--repo-root",
            str(repo_root),
        ]
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
    def start_dcf_import():  # pyright: ignore[reportUnusedFunction]  # registered via decorator
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
        script = repo_root / "execution" / "dcf_sheets.py"
        argv = [
            sys.executable,
            str(script),
            "import",
            "--ticker",
            ticker,
            "--repo-root",
            str(repo_root),
        ]
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

    @app.route("/actions/maintenance", methods=["POST", "OPTIONS"])
    def start_maintenance():
        """Repo-wide maintenance chores (seed KPI defs · process dropped docs ·
        sweep output history · onboard pending · onboard <ticker>) dispatched as
        single-flight jobs, streamed over /actions/stream/<job_id>. Each runs an
        existing CLI under execution/."""
        if request.method == "OPTIONS":
            return ("", 204)
        body = request.get_json(silent=True) or {}
        action = str(body.get("action", ""))
        if action == "onboard":
            ticker = str(body.get("ticker", "")).upper()
            if not ticker:
                return ({"error": "onboard requires a ticker"}, 400)
            parts = ["onboard_ticker.py", "--ticker", ticker]
            slot_ticker, kind = ticker, "maint-onboard"
        elif action in _MAINTENANCE_ACTIONS:
            parts = _MAINTENANCE_ACTIONS[action]
            slot_ticker, kind = "_REPO", f"maint-{action}"
        else:
            valid = [*sorted(_MAINTENANCE_ACTIONS), "onboard"]
            return ({"error": f"unknown action {action!r}; valid: {valid}"}, 400)
        argv = [sys.executable, str(repo_root / "execution" / parts[0]), *parts[1:]]
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
        body = request.get_json(silent=True) or {}
        try:
            ticker = body["ticker"]
            report_date = _parse_date(body["report_date"])
            anchor = comments.Anchor(**body["anchor"])
            text = body["comment"]
            intent = body.get("intent") or None
            selected_text = body.get("selected_text")
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
        body = request.get_json(silent=True) or {}
        try:
            ticker = body["ticker"]
            report_date = _parse_date(body["report_date"])
        except (KeyError, ValueError, TypeError):
            return ({"error": "ticker + report_date required"}, 400)
        status = body.get("status")
        resolution = body.get("resolution_note")
        intent = body.get("intent")
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
        body = request.get_json(silent=True) or {}
        try:
            ticker = body["ticker"]
            report_date = _parse_date(body["report_date"])
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
        body = request.get_json(silent=True) or {}
        try:
            report_date = _parse_date(body["report_date"])
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
                return ({"error": str(exc), "kind": type(exc).__name__}, status)
            return ({"degraded": True, "reason": f"{type(exc).__name__}: {exc}"}, 200)
        return (result, 200)

    @app.route("/api/comments/process", methods=["POST", "OPTIONS"])
    def comments_process():
        """Process a ticker's open comments. apply=false → synchronous dry-run
        (each comment's drafted resolution, inline). apply=true → dispatch the
        real run (mutations + auto-rebuild) as a single-flight job, streamed
        over /actions/stream/<job_id>."""
        if request.method == "OPTIONS":
            return ("", 204)
        body = request.get_json(silent=True) or {}
        ticker = str(body.get("ticker", "")).upper()
        if not ticker:
            return ({"error": "ticker required"}, 400)
        apply_flag = bool(body.get("apply", False))
        report_date_str = body.get("report_date")
        report_date: date | None = None
        if report_date_str:
            try:
                report_date = _parse_date(report_date_str)
            except (ValueError, TypeError):
                return ({"error": "bad report_date"}, 400)

        if not apply_flag:
            rd = report_date or _resolve_latest_report_date(repo_root, ticker)
            if rd is None:
                return ({"error": "no report found for ticker; pass report_date"}, 404)
            try:
                res = process_comments_for_ticker(repo_root, ticker, rd, apply=False, clear=False)
            except Exception as exc:
                if is_hard_stop(exc):
                    status = 402 if isinstance(exc, LLMBudgetExceeded) else 503
                    return ({"error": str(exc), "kind": type(exc).__name__}, status)
                return ({"degraded": True, "reason": f"{type(exc).__name__}: {exc}"}, 200)
            return (res, 200)

        # apply=true → dispatch the real run as a single-flight job.
        script = repo_root / "execution" / "process_report_comments.py"
        argv = [sys.executable, str(script), "--ticker", ticker, "--apply"]
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

    # ----- CHAT (Phase 3) -----

    @app.route("/chat/<ticker>", methods=["OPTIONS"])
    def chat_options(ticker: str):
        del ticker
        return ("", 204)

    @app.route("/chat/<ticker>", methods=["GET"])
    def list_chat_endpoint(ticker: str):
        report_date_str = request.args.get("report_date")
        if not report_date_str:
            return ({"error": "report_date required"}, 400)
        report_date = _parse_date(report_date_str)
        thread = build_chat_response.load_thread(repo_root, ticker, report_date)
        return (
            {
                "ticker": ticker,
                "report_date": report_date.isoformat(),
                "thread": [t.model_dump(mode="json") for t in thread],
            },
            200,
        )

    @app.route("/chat/<ticker>", methods=["POST"])
    def chat_endpoint(ticker: str):
        body = request.get_json(silent=True) or {}
        try:
            report_date = _parse_date(body["report_date"])
            user_message = body["message"]
        except (KeyError, ValueError, TypeError) as e:
            return ({"error": f"bad payload: {e}"}, 400)

        # The LLM subprocess (Claude CLI) drives `stream_response` for
        # 10-60s; running it inline would pin the Flask request thread
        # for that whole window. Dispatch to the chat pool and pipe its
        # chunks through a Queue, then drain the queue into SSE frames.
        chunks: queue.Queue[dict[str, object] | None] = queue.Queue()
        chat_pool.submit(
            _drain_chat_stream,
            repo_root,
            ticker,
            report_date,
            user_message,
            chunks,
        )

        def generate():
            while True:
                item = chunks.get()
                if item is None:
                    break
                yield f"data: {json.dumps(item)}\n\n"

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # ----- APPLY (Phase 4) -----

    @app.route("/chat/<ticker>/apply", methods=["OPTIONS"])
    def chat_apply_options(ticker: str):
        del ticker
        return ("", 204)

    @app.route("/chat/<ticker>/apply", methods=["POST"])
    def chat_apply_endpoint(ticker: str):
        body = request.get_json(silent=True) or {}
        try:
            diff = body["diff"]
            dry_run = bool(body.get("dry_run", False))
        except (KeyError, TypeError):
            return ({"error": "diff required"}, 400)
        return apply_chat_diff(repo_root, ticker, diff, dry_run=dry_run)

    return app


def _parse_date(s: str) -> date:
    return date.fromisoformat(s[:10])


def _drain_chat_stream(
    repo_root: Path,
    ticker: str,
    report_date: date,
    user_message: str,
    chunks: queue.Queue[dict[str, object] | None],
) -> None:
    """Iterate `stream_response` on a pool thread and push each chunk
    onto `chunks`. `None` marks end-of-stream so the SSE generator on
    the request thread can stop pumping."""
    try:
        for chunk in build_chat_response.stream_response(
            repo_root=repo_root,
            ticker=ticker,
            report_date=report_date,
            user_message=user_message,
        ):
            chunks.put(chunk)
    except Exception as e:
        # Surface as an SSE error frame instead of crashing the pool —
        # the request thread is still waiting on the queue and needs the
        # `None` sentinel below to terminate.
        chunks.put({"type": "error", "error": f"chat stream failed: {e}"})
    finally:
        chunks.put(None)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=7421)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    print(
        f"comments_server: repo_root={repo_root} host={args.host} port={args.port}",
        file=sys.stderr,
    )
    app = create_app(repo_root)
    # Flask's built-in dev server is fine here — this is a single-user
    # localhost tool, not a production service.
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
