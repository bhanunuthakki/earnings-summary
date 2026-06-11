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
  POST   /chat/<ticker>       streaming chat — the unified ask engine with the
                              ticker context pack (src/ask/engine.py)
  POST   /chat/<ticker>/apply apply a chatbot-proposed diff (Phase 4)
  POST   /api/ask             one Ask-tab turn — the same engine with the
                              portfolio context pack (single folded payload)
  POST   /api/ask/stream      streaming sibling of /api/ask (SSE frames —
                              live stage/delta/fragment progress)
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
import json
import os
import queue
import sys
import urllib.parse
from collections.abc import Iterator
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

from approve_queued_action import approve_and_apply, dismiss_action  # noqa: E402
from process_report_comments import (  # noqa: E402
    _resolve_latest_report_date,
    preview_thesis_edits,
    process_comments_for_ticker,
)
from refresh_dispatch import STEP_NAMES  # noqa: E402

import comments  # noqa: E402
import llm_budget  # noqa: E402
import ticker_settings  # noqa: E402
from alerts import ACTION_STATUS_APPLIED, ACTION_STATUS_CANCELLED  # noqa: E402
from ask.context import build_portfolio_pack, build_ticker_pack  # noqa: E402
from ask.engine import AskTurn, fold_events, respond_turn, sanitize_history  # noqa: E402
from chat_session import apply_chat_diff, build_chat_response  # noqa: E402
from dashboard import render_alert_feed  # noqa: E402
from dashboard.inbox import collect_inbox, render_inbox_stream  # noqa: E402
from dashboard.upcoming import render_upcoming_strip  # noqa: E402
from discovery.store import BUILDABLE_STATUSES  # noqa: E402
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
    render_holding_picker_band,
    render_notes_drawer_fragment,
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


def _referer_back_path(referer: str) -> str | None:
    """The relative ``path?query`` an ``/approve`` click bounces back to,
    derived from its Referer — or None when the Referer is absent,
    unparseable, or cross-site (judged by the same loopback/whitelist rule
    as CORS, via ``_cors_allow_origin``). Scheme and host never survive into
    the redirect target (and ``//host``-style paths are rejected), so a
    crafted Referer can't turn ``/approve`` into an open redirect."""
    if not referer:
        return None
    try:
        parsed = urllib.parse.urlparse(referer)
    except ValueError:
        return None
    if (parsed.scheme or parsed.netloc) and _cors_allow_origin(
        f"{parsed.scheme}://{parsed.netloc}"
    ) is None:
        return None
    path = parsed.path or "/"
    if not path.startswith("/") or path[1:2] in ("/", "\\"):
        return None
    return path + (f"?{parsed.query}" if parsed.query else "")


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


def _note_to_json(note: object) -> dict[str, object]:
    """AnalystNoteRow → JSON-safe dict for the /api/notes responses (P4.5)."""
    from dataclasses import asdict
    from datetime import datetime as _dt

    payload = asdict(note)  # pyright: ignore[reportArgumentType]  # always an AnalystNoteRow
    return {k: (v.isoformat() if isinstance(v, _dt) else v) for k, v in payload.items()}


def _view_to_json(view: object) -> dict[str, object]:
    """SavedViewRow → JSON-safe dict for the /api/views responses (P5.1)."""
    from dataclasses import asdict
    from datetime import datetime as _dt

    payload = asdict(view)  # pyright: ignore[reportArgumentType]  # always a SavedViewRow
    return {k: (v.isoformat() if isinstance(v, _dt) else v) for k, v in payload.items()}


def _candidate_to_json(cand: object) -> dict[str, object]:
    """CandidateRow → JSON-safe dict for the /api/discovery responses (P5.4)."""
    from dataclasses import asdict
    from datetime import datetime as _dt

    payload = asdict(cand)  # pyright: ignore[reportArgumentType]  # always a CandidateRow
    return {k: (v.isoformat() if isinstance(v, _dt) else v) for k, v in payload.items()}


# Lifecycle moves the OWNER may make from the queue UI / chat. ``building``
# and ``built`` are written only by the build pathway (discovery_build.py) —
# the queue can't hand-wave a name into "built".
_DISCOVERY_OWNER_STATUSES: frozenset[str] = frozenset({"new", "queued", "dismissed"})

# The deterministic /discovery chat commands moved to ask.commands (the
# unified ask engine intercepts them from BOTH chat surfaces); the REST
# build routes share its buildable-status set via discovery.store.


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

    def _stream_engine_events(events: Iterator[dict[str, object]]) -> Response:
        """Pump one ask-engine event stream into an SSE response.

        The narrative path drives an LLM subprocess (Claude CLI) for
        10-60s; running it inline would pin the Flask request thread for
        that whole window. Dispatch to the chat pool (the generator body
        executes lazily, on the pool thread) and pipe its events through
        a Queue, then drain the queue into SSE frames. Shared by
        /chat/<ticker> and /api/ask/stream."""
        chunks: queue.Queue[dict[str, object] | None] = queue.Queue()
        chat_pool.submit(_drain_events, events, chunks)

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
        inbox_html = render_inbox_stream(
            collect_inbox(db_path, limit=14),
            db_path=db_path,
            compact=True,
            surface="home",
            show_filters=True,
        )
        # The compact earnings look-ahead above the rail — the surviving piece
        # of the retired /digest page.
        upcoming_html = render_upcoming_strip(db_path, datetime.now(UTC).date())
        overview = render_overview_panel(
            rows, coverage, inbox_html=inbox_html, upcoming_html=upcoming_html
        )
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
        (execution/build_analytical_dashboard.py) renders — one code path,
        no divergence. (``GET /analytical`` itself is a 302 into the shell.)

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
            # Portfolio → Performance: tracker analytics + live positions /
            # % of book / taxable breakdown from the companion tracker.
            # Degrades when the tracker is offline. ``?start_date`` /
            # ``?end_date`` / ``?include_backfill`` re-window the tracker
            # analytics — the page's own window bar drives these. (The
            # synthesis layer is its own sub-tab: portfolio_synthesis.)
            from pipeline.portfolio_panel import render_portfolio_panel

            return Response(
                render_portfolio_panel(
                    start_date=request.args.get("start_date"),
                    end_date=request.args.get("end_date"),
                    include_backfill=request.args.get("include_backfill") in ("1", "true", "True"),
                ),
                mimetype="text/html",
            )

        if name == "portfolio_synthesis":
            # Portfolio → Synthesis (UX round 4): the reading layer that used
            # to ride the bottom of the Performance tab — thesis rollup +
            # sector exposure, the next-dollar allocation distribution with
            # its factor waterfall, the cached cross-portfolio lens memo.
            # Tracker down → quiet equal-weight fallback (the offline/start
            # card stays on Performance).
            from pipeline.portfolio_panel import render_portfolio_synthesis_panel

            return Response(render_portfolio_synthesis_panel(db_path), mimetype="text/html")

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

        if name == "validation":
            # Whole-book data-quality state over validation_issues (P3.4) —
            # range violations, magnitude jumps, source disagreement, unit
            # mismatches, previously visible only per-ticker in reports.
            from pipeline.validation_issues_panel import render_validation_panel

            return Response(render_validation_panel(db_path), mimetype="text/html")

        if name == "section_coverage":
            # Per-ticker section coverage (P4.2): the visible counterpart of
            # the hide-don't-stub policy — reports hide cold sections, this
            # matrix is where the gaps stay accountable.
            from pipeline.section_coverage_panel import render_section_coverage_panel

            user_id = request.args.get("user_id", DEFAULT_USER_ID)
            return Response(
                render_section_coverage_panel(db_path, repo_root, user_id=user_id),
                mimetype="text/html",
            )

        if name == "explore":
            # Research → Explore (P5.1): the ViewSpec builder. ``?fragment=
            # views`` returns just the saved-view chip strip — the panel JS
            # refreshes it after every save/delete.
            from pipeline.explore_panel import render_explore_panel, render_saved_views_list

            user_id = request.args.get("user_id", DEFAULT_USER_ID)
            if request.args.get("fragment") == "views":
                return Response(
                    render_saved_views_list(db_path, user_id=user_id), mimetype="text/html"
                )
            return Response(render_explore_panel(db_path, user_id=user_id), mimetype="text/html")

        if name == "discovery":
            # Research → Discovery (P5.4): the candidate approval queue —
            # the budget gate ("queue, never auto-build"). ``?fragment=list``
            # returns just the table for the panel JS's refreshes.
            from pipeline.discovery_panel import (
                render_discovery_list,
                render_discovery_panel,
            )

            user_id = request.args.get("user_id", DEFAULT_USER_ID)
            d_status = (request.args.get("status") or "live").strip() or "live"
            try:
                d_min = float(request.args.get("min_score") or 0)
            except ValueError:
                d_min = 0.0
            d_renderer = (
                render_discovery_list
                if request.args.get("fragment") == "list"
                else render_discovery_panel
            )
            return Response(
                d_renderer(db_path, user_id=user_id, status=d_status, min_score=d_min),
                mimetype="text/html",
            )

        if name == "journal":
            # Research → Journal (P4.5): the analyst_notes lifecycle UI.
            # ``?fragment=list`` returns just the filtered note list — the
            # panel's own JS refreshes that fragment after every action.
            from pipeline.journal_panel import render_journal_list, render_journal_panel

            user_id = request.args.get("user_id", DEFAULT_USER_ID)
            j_ticker = (request.args.get("ticker") or "").strip().upper() or None
            j_kind = (request.args.get("kind") or "").strip() or None
            j_status = (request.args.get("status") or "open").strip() or "open"
            renderer = (
                render_journal_list
                if request.args.get("fragment") == "list"
                else render_journal_panel
            )
            return Response(
                renderer(db_path, user_id=user_id, ticker=j_ticker, kind=j_kind, status=j_status),
                mimetype="text/html",
            )

        if name == "ticker_settings":
            # Settings-drawer section (P3.4): per-ticker persistent overrides
            # (bypass_budget) listed + editable via /api/ticker-settings/<T>.
            from pipeline.ticker_settings_panel import render_ticker_settings_panel

            return Response(render_ticker_settings_panel(db_path), mimetype="text/html")

        if name == "restatements":
            # "was X, now Y" over the supersede chains (P3.5) — every place a
            # later filing changed an already-reported number, linking both
            # documents into the /source/<doc_id> viewers.
            from pipeline.restatements_panel import render_restatements_panel

            return Response(render_restatements_panel(db_path), mimetype="text/html")

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
            # once reachable only via the now-retired /digest page (v6 re-grade,
            # Richness). Folded into the Decisions tab (P2.2); kept for old links.
            from pipeline.thesis_ledger_panel import render_thesis_ledger_panel

            user_id = request.args.get("user_id", DEFAULT_USER_ID)
            return Response(
                render_thesis_ledger_panel(db_path, user_id=user_id), mimetype="text/html"
            )

        if name == "decisions_record":
            # The allocation-decisions record (master build P2.2): the sizing
            # audit (stated conviction/target vs live weight vs DCF gap vs
            # window alpha, mismatches ranked) + the merged decisions timeline
            # (thesis ledger + sizing intents + decision notes).
            from pipeline.allocation_decisions_panel import render_allocation_decisions_panel

            user_id = request.args.get("user_id", DEFAULT_USER_ID)
            return Response(
                render_allocation_decisions_panel(db_path, user_id=user_id),
                mimetype="text/html",
            )

        if name == "advisor_memos":
            # Advisor memos (master build P2.3): run bar (next-dollar / swap
            # checks via the jobs SSE machinery) + the deterministic
            # swap-discipline screen + the durable memo record.
            from pipeline.advisor_memos_panel import render_advisor_memos_panel

            user_id = request.args.get("user_id", DEFAULT_USER_ID)
            return Response(
                render_advisor_memos_panel(db_path, user_id=user_id),
                mimetype="text/html",
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
        Holding tab: a one-line utility band (search combobox · verdict · freshness ·
        report/DCF links · Ops/Notes icons) above the embedded ``/reports/<t>``
        iframe that carries the inline comment/chat/apply pipeline. With no
        ``?ticker=`` it returns the combobox band alone (UX9c) — the search picker
        is always present, including before any holding is opened."""
        ticker = request.args.get("ticker")
        if not ticker:
            return Response(render_holding_picker_band(repo_root), mimetype="text/html")
        return Response(render_holding_fragment(repo_root, ticker), mimetype="text/html")

    @app.route("/api/panel/notes_drawer", methods=["GET"])
    def notes_drawer_panel_fragment():
        """The shell's shared ✎ Notes drawer (UX9b) as a fragment: quick-add
        (POSTs to /api/notes) above the open-notes list. ``?ticker=`` scopes
        it to one name (the Holding tab supplies its selection) and adds that
        name's recent alerts; without it, the newest open notes book-wide."""
        return Response(
            render_notes_drawer_fragment(repo_root, request.args.get("ticker")),
            mimetype="text/html",
        )

    @app.route("/analytical", methods=["GET"])
    def analytical_page():
        """The standalone analytical dashboard is folded into the unified shell;
        its content is the shell's Triggers tab. 302-redirect to that deep link
        so existing bookmarks keep working."""
        return redirect("/#holdings")

    # ----- PERSONAL-CIO ALERTING SURFACE (feed) -----
    # Previously emitted only as static files (data/dashboard/...), unreachable
    # from the live command center — so a user living in the app never saw their
    # alerts. Served live (linked from the shell topbar): read-only, degrading
    # to a valid empty-state document when the substrate tables are absent.

    @app.route("/digest", methods=["GET"])
    def digest_page():
        """RETIRED (2026-06-11): the standalone morning digest added nothing
        over the Home rail — the same unified inbox plus the upcoming-earnings
        strip live there now. 302 so old bookmarks land on Home."""
        return redirect("/#home")

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

    @app.route("/approve", methods=["GET", "POST"])
    def approve_or_dismiss_action():
        """One-click approve / dismiss for the queued-action cards (feed,
        Home rail, Holding rail). ``?action_id=N`` approves — writes the downstream
        thesis-ledger / sizing-intent row, then marks the action applied, via
        the same shared core as the approve CLI; ``&dismiss=1`` cancels
        instead (no ledger write). GET 303-redirects back to the surface the
        click came from (Referer path, else /feed) so the re-rendered card
        shows its new status pill; POST (the Home rail's hover ✓/✕ fetch,
        params in the form body or query string) returns
        ``{ok, action_id, status}`` JSON instead, so the card updates in
        place without a reload.

        State-changing either way, so both methods carry the same-site guard
        the JSON-POST CORS defense can't provide for top-level navigations
        (and a urlencoded form POST never preflights): a cross-site Referer
        or ``Sec-Fetch-Site: cross-site`` gets 403, while no-Referer requests
        (address bar, curl) stay usable."""
        raw_id = request.values.get("action_id", "")
        try:
            action_id = int(raw_id)
        except ValueError:
            return ({"error": f"action_id must be an integer, got {raw_id!r}"}, 400)
        referer = request.headers.get("Referer", "")
        back = _referer_back_path(referer)
        if request.headers.get("Sec-Fetch-Site", "") == "cross-site" or (referer and back is None):
            return ({"error": "cross-site approve/dismiss rejected"}, 403)
        dismissed = request.values.get("dismiss") in ("1", "true", "True")
        try:
            if dismissed:
                dismiss_action(action_id, db_path=db_path)
            else:
                approve_and_apply(action_id, db_path=db_path)
        except LookupError as exc:
            return ({"error": str(exc)}, 404)
        except (ValueError, KeyError) as exc:
            # Status conflict (stale or double-clicked link) or a malformed
            # payload — 409 either way; the message says which. The CLI hint
            # on the card remains the fallback path.
            return ({"error": str(exc)}, 409)
        if request.method == "POST":
            status = ACTION_STATUS_CANCELLED if dismissed else ACTION_STATUS_APPLIED
            return {"ok": True, "action_id": action_id, "status": status}
        return redirect(back or "/feed", code=303)

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

    @app.route("/api/notes", methods=["GET", "POST", "OPTIONS"])
    def notes_api():
        """The analyst-journal REST surface (P4.5), thin over user_state.notes.

        GET  ?ticker=&kind=&status=   list notes (status defaults to live —
                                      everything except superseded/archived)
        POST {ticker?, kind, body, anchor_type?, anchor_key?, context?}
                                      create an open note (source="manual")
        """
        if request.method == "OPTIONS":
            return ("", 204)
        from user_state import notes as notes_store

        user_id = request.args.get("user_id", DEFAULT_USER_ID)
        if request.method == "GET":
            q_ticker = (request.args.get("ticker") or "").strip().upper() or None
            q_kind = (request.args.get("kind") or "").strip() or None
            q_status = (request.args.get("status") or "").strip() or None
            try:
                rows = notes_store.list_notes(
                    user_id=user_id,
                    ticker=q_ticker,
                    kind=q_kind,
                    status=q_status,
                    db_path=db_path,
                )
            except ValueError as exc:
                return ({"error": str(exc)}, 400)
            return {"notes": [_note_to_json(n) for n in rows]}

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        note_body = str(payload.get("body") or "").strip()
        if not note_body:
            return ({"error": "body required"}, 400)
        kind = str(payload.get("kind") or "observation")
        ticker_raw = payload.get("ticker")
        note_ticker = str(ticker_raw).strip().upper() or None if ticker_raw is not None else None
        anchor_type_raw = payload.get("anchor_type")
        anchor_key_raw = payload.get("anchor_key")
        context_raw = payload.get("context")
        try:
            created = notes_store.create_note(
                user_id=user_id,
                ticker=note_ticker,
                kind=kind,
                body=note_body,
                anchor_type=str(anchor_type_raw) if anchor_type_raw is not None else None,
                anchor_key=str(anchor_key_raw) if anchor_key_raw is not None else None,
                source="manual",
                context=cast("dict[str, object]", context_raw)
                if isinstance(context_raw, dict)
                else None,
                db_path=db_path,
            )
        except ValueError as exc:
            return ({"error": str(exc)}, 400)
        return ({"note": _note_to_json(created)}, 201)

    @app.route("/api/notes/<int:note_id>/<action>", methods=["POST", "OPTIONS"])
    def notes_action_api(note_id: int, action: str):
        """Lifecycle actions on one note (P4.5): resolve / reclassify /
        supersede / archive. Supersede creates the chained replacement and
        returns it; the others return the updated row. 404 on unknown id,
        400 on a bad kind or missing supersede body."""
        if request.method == "OPTIONS":
            return ("", 204)
        from user_state import notes as notes_store

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        try:
            if action == "resolve":
                res_raw = payload.get("resolution_note")
                updated = notes_store.resolve_note(
                    note_id,
                    resolution_note=str(res_raw).strip() or None if res_raw is not None else None,
                    db_path=db_path,
                )
            elif action == "archive":
                updated = notes_store.archive_note(note_id, db_path=db_path)
            elif action == "reclassify":
                updated = notes_store.reclassify_note(
                    note_id, kind=str(payload.get("kind") or ""), db_path=db_path
                )
            elif action == "supersede":
                new_body = str(payload.get("body") or "").strip()
                if not new_body:
                    return ({"error": "body required for supersede"}, 400)
                kind_raw = payload.get("kind")
                updated = notes_store.supersede_note(
                    note_id,
                    body=new_body,
                    kind=str(kind_raw) if kind_raw is not None else None,
                    db_path=db_path,
                )
            else:
                return ({"error": f"unknown action {action!r}"}, 404)
        except ValueError as exc:
            return ({"error": str(exc)}, 400)
        except LookupError as exc:
            return ({"error": str(exc)}, 404)
        if updated is None:
            return ({"error": f"note {note_id} not found"}, 404)
        return {"note": _note_to_json(updated)}

    @app.route("/api/viewspec/catalog", methods=["GET"])
    def viewspec_catalog_api():
        """Metric catalog for the Explore builder (P5.1): what's plottable
        for ``?tickers=A,B`` — financial line items, KPI names, segment
        slices — as token/label/coverage entries per domain."""
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
        try:
            spec = ViewSpec.from_dict(raw_spec)
        except ViewSpecError as exc:
            return ({"error": str(exc)}, 400)
        result = execute_view(spec, db_path=db_path)
        return Response(
            render_view_fragment(result, include_chart=include_chart), mimetype="text/html"
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
        result = compile_nl_to_viewspec(query, db_path=db_path, context_tickers=context)
        payload: dict[str, object] = {"status": result.status}
        if result.message:
            payload["message"] = result.message
        if result.spec is not None:
            payload["spec"] = result.spec.to_dict()
        return payload

    @app.route("/api/ask", methods=["POST", "OPTIONS"])
    def ask_api():
        """One Ask-thread turn — the unified ask engine with the PORTFOLIO
        context pack ("one brain, two entry points": same engine as the
        report drawer's /chat/<ticker>, different attached context).

        JSON body ``{"query": ..., "tickers": [...], "context_spec": {...},
        "history": [{"role", "text"}, ...]}`` — ``context_spec`` is the
        previous turn's view spec (follow-ups like "now annual" refine it),
        ``history`` the client-side thread tail for narrative continuity.
        Always 200 with a tri-state payload: ``{"status": "ok", "kind":
        "view", "spec", "fragment", "message"}`` for data answers,
        ``{"status": "ok", "kind": "narrative" | "command", "text"}`` for
        prose, or ``{"status": "budget_skipped" | "error", "message"}``.
        400 only for a missing query."""
        if request.method == "OPTIONS":
            return ("", 204)
        turn = _parse_ask_turn()
        if turn is None:
            return ({"error": "query required"}, 400)
        pack = build_portfolio_pack(repo_root, db_path)
        events = respond_turn(
            turn, pack, db_path=db_path, repo_root=repo_root, registry=job_registry
        )
        return fold_events(events)

    @app.route("/api/ask/stream", methods=["POST", "OPTIONS"])
    def ask_stream_api():
        """Streaming sibling of /api/ask (Ask v2): same engine, same
        PORTFOLIO pack, but the raw event stream as SSE frames instead of
        one folded payload — the Ask tab renders live progress (``stage``
        frames drive the busy line, ``delta`` frames stream prose,
        ``fragment``/``final`` assemble the answer card). Same frame shapes
        as /chat/<ticker>."""
        if request.method == "OPTIONS":
            return ("", 204)
        turn = _parse_ask_turn()
        if turn is None:
            return ({"error": "query required"}, 400)
        pack = build_portfolio_pack(repo_root, db_path)
        events = respond_turn(
            turn, pack, db_path=db_path, repo_root=repo_root, registry=job_registry
        )
        return _stream_engine_events(events)

    @app.route("/api/peers/<ticker>", methods=["GET"])
    def peers_api(ticker: str):
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
            rows = p3_data.load_peer_comp(sym, repo_root=repo_root)
        except Exception as exc:  # best-effort surface, never a 500
            return {"ticker": sym, "peers": [], "error": f"peer lookup failed: {exc}"}
        return {
            "ticker": sym,
            "peers": [
                {
                    "ticker": r.peer_ticker,
                    "name": r.peer_name,
                    "reasons": list(r.match_reasons),
                }
                for r in rows
            ],
        }

    def _parse_ask_turn() -> AskTurn | None:
        """The shared /api/ask request shape → AskTurn; None on a missing
        query (the routes 400)."""
        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        query = str(body.get("query") or "").strip()
        if not query:
            return None
        raw_tickers = body.get("tickers")
        tickers = (
            [str(t) for t in cast("list[object]", raw_tickers)]
            if isinstance(raw_tickers, list)
            else []
        )
        raw_ctx = body.get("context_spec")
        context_spec = cast("dict[str, object]", raw_ctx) if isinstance(raw_ctx, dict) else None
        return AskTurn(
            text=query,
            tickers=tickers,
            context_spec=context_spec,
            history=sanitize_history(body.get("history")),
        )

    @app.route("/api/views", methods=["GET", "POST"])
    def views_api():
        """Saved views CRUD (P5.1, saved_views 0079). GET lists; POST
        ``{"name": ..., "spec": {...}}`` validates the spec then upserts by
        (user, name) — saving an existing name replaces its spec."""
        from user_state import saved_views as views_store
        from viewspec.spec import ViewSpec, ViewSpecError

        user_id = request.args.get("user_id", DEFAULT_USER_ID)
        if request.method == "GET":
            try:
                rows = views_store.list_views(user_id=user_id, db_path=db_path)
            except sqlite3.Error:
                rows = []  # pre-0079 schema degrades to empty
            return {"views": [_view_to_json(v) for v in rows]}

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        name = str(payload.get("name") or "").strip()
        if not name:
            return ({"error": "name required"}, 400)
        try:
            spec = ViewSpec.from_dict(payload.get("spec"))
        except ViewSpecError as exc:
            return ({"error": str(exc)}, 400)
        try:
            row = views_store.save_view(
                name=name, spec=spec.to_dict(), user_id=user_id, db_path=db_path
            )
        except sqlite3.Error:
            return ({"error": "saved_views table missing (run alembic upgrade)"}, 500)
        return ({"view": _view_to_json(row)}, 201)

    @app.route("/api/views/<int:view_id>", methods=["DELETE"])
    def views_delete_api(view_id: int):
        """Hard-delete one saved view (a query, not memory)."""
        from user_state import saved_views as views_store

        try:
            deleted = views_store.delete_view(view_id, db_path=db_path)
        except sqlite3.Error:
            return ({"error": "saved_views table missing (run alembic upgrade)"}, 500)
        if not deleted:
            return ({"error": f"view {view_id} not found"}, 404)
        return {"deleted": True}

    @app.route("/api/views/<int:view_id>/fragment", methods=["GET"])
    def views_fragment_api(view_id: int):
        """One saved view, executed and rendered — the embed hook (P5.1) the
        Explore panel's chips use and any cockpit/report surface can iframe
        or fetch-inject. ``?chart=0`` renders the matrix only."""
        from user_state import saved_views as views_store
        from viewspec.engine import execute_view
        from viewspec.render import render_view_fragment
        from viewspec.spec import ViewSpec, ViewSpecError

        try:
            row = views_store.get_view(view_id, db_path=db_path)
        except sqlite3.Error:
            row = None
        if row is None:
            abort(404)
        try:
            spec = ViewSpec.from_dict(row.spec)
        except ViewSpecError as exc:
            return ({"error": f"stored spec no longer valid: {exc}"}, 400)
        include_chart = request.args.get("chart") not in ("0", "false")
        result = execute_view(spec, db_path=db_path)
        return Response(
            render_view_fragment(result, include_chart=include_chart), mimetype="text/html"
        )

    @app.route("/api/discovery/candidates", methods=["GET"])
    def discovery_candidates_api():
        """The discovery queue as JSON (P5.4): ``?status=live`` (default,
        everything except dismissed) or one lifecycle bucket."""
        from discovery.store import CANDIDATE_STATUSES, list_candidates

        user_id = request.args.get("user_id", DEFAULT_USER_ID)
        status_raw = (request.args.get("status") or "live").strip()
        status = None if status_raw == "live" else status_raw
        if status is not None and status not in CANDIDATE_STATUSES:
            return ({"error": f"unknown status {status_raw!r}"}, 400)
        try:
            rows = list_candidates(user_id=user_id, status=status, db_path=db_path)
        except sqlite3.Error:
            rows = []  # pre-0081 schema degrades to empty
        return {"candidates": [_candidate_to_json(c) for c in rows]}

    @app.route("/api/discovery/candidates/<int:cand_id>/status", methods=["POST"])
    def discovery_status_api(cand_id: int):
        """Owner lifecycle moves (P5.4): queued / dismissed / new (re-open).
        ``building``/``built`` belong to the build pathway and are rejected
        here — the queue can't hand-wave a name into built."""
        from discovery.store import set_status

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        status = str(payload.get("status") or "")
        if status not in _DISCOVERY_OWNER_STATUSES:
            return (
                {"error": f"status must be one of {sorted(_DISCOVERY_OWNER_STATUSES)}"},
                400,
            )
        try:
            row = set_status(cand_id, status, db_path=db_path)
        except sqlite3.Error:
            return ({"error": "discovery_candidates table missing (run alembic upgrade)"}, 500)
        if row is None:
            return ({"error": f"candidate {cand_id} not found"}, 404)
        return {"candidate": _candidate_to_json(row)}

    @app.route("/actions/discovery-run", methods=["POST", "OPTIONS"])
    def start_discovery_run():
        """Re-run the P5.3 pipelines (screens + adjacency) as a streamed job.
        Deterministic and LLM-free — this surfaces candidates; it never
        builds them."""
        if request.method == "OPTIONS":
            return ("", 204)
        argv = [
            sys.executable,
            str(repo_root / "execution" / "run_discovery.py"),
            "--repo-root",
            str(repo_root),
        ]
        try:
            job = job_registry.start(ticker="DISCOVERY", kind="discovery-run", argv=argv)
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

    @app.route("/actions/discovery-build", methods=["POST", "OPTIONS"])
    def start_discovery_build():
        """Eval-build approved candidates (P5.4) — THE budget gate's other
        side: this only runs because the owner clicked/typed it. JSON body
        ``{"tickers": ["WDC", ...]}`` (1..MAX_BUILD_BATCH names, each must
        be a live candidate in new/queued status). One sequential job;
        ~25 min + LLM spend per name; streamed via the jobs SSE."""
        if request.method == "OPTIONS":
            return ("", 204)
        from discovery_build import MAX_BUILD_BATCH

        from discovery.store import list_candidates

        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        raw = body.get("tickers")
        if not isinstance(raw, list) or not raw:
            return ({"error": "tickers (non-empty list) required"}, 400)
        tickers = [str(t).strip().upper() for t in cast("list[object]", raw) if str(t).strip()]
        if not tickers:
            return ({"error": "tickers (non-empty list) required"}, 400)
        if len(tickers) > MAX_BUILD_BATCH:
            return (
                {"error": f"at most {MAX_BUILD_BATCH} builds per run, got {len(tickers)}"},
                400,
            )
        user_id = str(body.get("user_id") or DEFAULT_USER_ID)
        try:
            live = list_candidates(user_id=user_id, db_path=db_path)
        except sqlite3.Error:
            return ({"error": "discovery_candidates table missing (run alembic upgrade)"}, 500)
        by_ticker = {c.ticker: c for c in live}
        not_buildable = [
            t
            for t in tickers
            if t not in by_ticker or by_ticker[t].status not in BUILDABLE_STATUSES
        ]
        if not_buildable:
            return (
                {
                    "error": "not buildable (must be live candidates in new/queued "
                    f"status): {not_buildable}"
                },
                400,
            )
        argv = [
            sys.executable,
            str(repo_root / "execution" / "discovery_build.py"),
            "--tickers",
            ",".join(tickers),
            "--repo-root",
            str(repo_root),
            "--user-id",
            user_id,
        ]
        slot_ticker = tickers[0] if len(tickers) == 1 else "DISCOVERY-BULK"
        try:
            job = job_registry.start(ticker=slot_ticker, kind="discovery-build", argv=argv)
        except RegistryConflict as e:
            return ({"error": str(e)}, 409)
        return (
            {
                "job_id": job.job_id,
                "ticker": job.ticker,
                "kind": job.kind,
                "tickers": tickers,
                "stream_url": f"/actions/stream/{job.job_id}",
                "started_at": job.started_at.isoformat(),
            },
            201,
        )

    @app.route("/api/sizing-intents", methods=["POST", "OPTIONS"])
    def sizing_intents_api():
        """Record a sizing-posture statement (master build P2.2). JSON body:
        {"ticker": "NU", "conviction": 4, "target_weight_pct": 6, "narrative": "..."}
        — at least one of conviction (1–5) / target_weight_pct (0–100). Each
        provided kind appends its own ``position_sizing_intent`` row (append-only
        history, never an update), sharing the optional narrative."""
        if request.method == "OPTIONS":
            return ("", 204)
        from user_state.sizing import append_intent

        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        ticker = str(body.get("ticker") or "").strip().upper()
        if not ticker:
            return ({"error": "ticker required"}, 400)
        narrative_raw = body.get("narrative")
        narrative = str(narrative_raw).strip() or None if narrative_raw is not None else None
        user_id = str(body.get("user_id") or DEFAULT_USER_ID)
        to_write: list[tuple[str, float]] = []
        if (conviction := body.get("conviction")) is not None:
            try:
                conv_f = float(cast("str | float | int", conviction))
            except (TypeError, ValueError):
                return ({"error": f"conviction must be a number, got {conviction!r}"}, 400)
            if not 1.0 <= conv_f <= 5.0:
                return ({"error": "conviction must be between 1 and 5"}, 400)
            to_write.append(("conviction", conv_f))
        if (target := body.get("target_weight_pct")) is not None:
            try:
                target_f = float(cast("str | float | int", target))
            except (TypeError, ValueError):
                return ({"error": f"target_weight_pct must be a number, got {target!r}"}, 400)
            if not 0.0 <= target_f <= 100.0:
                return ({"error": "target_weight_pct must be between 0 and 100"}, 400)
            to_write.append(("target_weight_pct", target_f))
        if not to_write:
            return ({"error": "provide conviction and/or target_weight_pct"}, 400)
        created = [
            append_intent(
                user_id=user_id,
                ticker=ticker,
                intent_kind=kind,
                intent_value=value_f,
                narrative=narrative,
                db_path=db_path,
            ).id
            for kind, value_f in to_write
        ]
        return {"ticker": ticker, "ok": True, "created_ids": created}

    @app.route("/source/<int:doc_id>", methods=["GET"])
    def source_viewer(doc_id: int):
        """In-app source viewers (P3.5). Routes by doc_type: processed
        transcripts get the numbered-line reader (#L<n> anchors for
        transcript_line locators/citations), parsed 10-K/10-Q JSONs the
        section reader (?section= for FactLocator.section); everything else
        302s to the document's source_url, falling back to a registry-
        metadata page so a /source link is never a dead end.

        ``?fragment=1`` (UX9) returns the chrome-less peek variant instead of
        a full document — and NEVER 302s externally (the shell's peek fetch
        can't follow a cross-origin redirect); non-viewable docs render their
        metadata card with the outbound link."""
        from pipeline.source_viewers import (
            load_document,
            render_fallback_page,
            render_form10k_page,
            render_transcript_page,
        )

        fragment = request.args.get("fragment") == "1"
        html = render_transcript_page(repo_root, db_path, doc_id, fragment=fragment)
        if html is None:
            html = render_form10k_page(
                repo_root, db_path, doc_id, request.args.get("section"), fragment=fragment
            )
        if html is not None:
            return Response(html, mimetype="text/html")
        doc = load_document(db_path, doc_id)
        if (
            not fragment
            and doc is not None
            and doc.source_url
            and doc.source_url.startswith(("http://", "https://"))
        ):
            return redirect(doc.source_url, code=302)
        return Response(
            render_fallback_page(db_path, doc_id, fragment=fragment), mimetype="text/html"
        )

    # ----- PEEK FRAGMENTS (UX9 quick-look popover) -----
    # Small head/foot-less payloads for the shell's peek primitive: review an
    # alert, glance at a ticker, read a memo — without leaving the panel.

    @app.route("/api/peek/alert/<int:alert_id>", methods=["GET"])
    def peek_alert(alert_id: int):
        """One full alert card (evidence drawer open, queued actions with
        their approve/dismiss links) for the inbox "review →" peek."""
        from pipeline.peeks import render_alert_peek

        html = render_alert_peek(db_path, alert_id)
        if html is None:
            abort(404)
        return Response(html, mimetype="text/html")

    @app.route("/api/peek/alerts", methods=["GET"])
    def peek_alerts():
        """A short stack of alert cards (``?ticker=`` / ``?status=`` filtered)
        for the cockpit's pending-alert pills; the full feed stays the
        overflow path."""
        from pipeline.peeks import render_alerts_list_peek

        return Response(
            render_alerts_list_peek(
                db_path,
                user_id=request.args.get("user_id", DEFAULT_USER_ID),
                ticker=request.args.get("ticker") or None,
                status=request.args.get("status") or None,
            ),
            mimetype="text/html",
        )

    @app.route("/api/peek/ticker/<ticker>", methods=["GET"])
    def peek_ticker(ticker: str):
        """The ticker hover mini-card: price + day move, thesis verdict, DCF
        gap, next ER, unreviewed count. 404 for untracked tickers (the hover
        simply doesn't show)."""
        from pipeline.peeks import render_ticker_peek

        conn = _open_db()
        try:
            html = render_ticker_peek(conn, repo_root, ticker)
        finally:
            conn.close()
        if html is None:
            abort(404)
        return Response(html, mimetype="text/html")

    @app.route("/api/peek/memo/<kind>", methods=["GET"])
    def peek_memo(kind: str):
        """The latest advisor memo of ``kind``, markdown-rendered, for the
        portfolio insights "full memo →" peek."""
        from pipeline.peeks import render_memo_peek

        html = render_memo_peek(db_path, kind)
        if html is None:
            abort(404)
        return Response(html, mimetype="text/html")

    @app.route("/api/peek/provenance", methods=["GET"])
    def peek_provenance():
        """Per-source data freshness with inline refresh buttons (UX9d) — the
        click-through behind the cockpit freshness dots, the holding-header
        dot, and the Home tier strip. ``?ticker=`` scopes to one holding;
        bare = portfolio-wide. The buttons POST the existing ``/actions/*``
        endpoints and stream their job log inside the peek; System → Actions
        stays the deep console. Always 200 — missing data degrades to
        em-dash ages."""
        from pipeline.peeks import render_provenance_peek

        return Response(
            render_provenance_peek(db_path, request.args.get("ticker") or None),
            mimetype="text/html",
        )

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

    @app.route("/actions/start-tracker", methods=["POST", "OPTIONS"])
    def start_tracker_server():
        """Start the companion portfolio-tracker API on :8000 (UX redesign
        PR6) — the Portfolio tab's offline card gets a button instead of a
        prose CLI hint. Runs the tracker's OWN venv python from the sibling
        checkout (so it finds its .env + data files) as a registry job whose
        startup log streams over the standard /actions/stream channel.
        409 when a tracker job is already running; 404 when the sibling
        checkout is missing."""
        if request.method == "OPTIONS":
            return ("", 204)
        tracker_root = repo_root.parent / "portfolio-tracker"
        if not tracker_root.exists():
            return (
                {"error": f"portfolio-tracker checkout not found at {tracker_root}"},
                404,
            )
        venv_python = tracker_root / (
            ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"
        )
        python_bin = str(venv_python) if venv_python.exists() else sys.executable
        argv = [
            python_bin,
            "-m",
            "uvicorn",
            "portfolio_tracker.api.main:app",
            "--port",
            "8000",
        ]
        try:
            job = job_registry.start(
                ticker="_REPO",
                kind="tracker-server",
                argv=argv,
                cwd=str(tracker_root),
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
        argv = [
            sys.executable,
            str(repo_root / "execution" / "run_advisor_memos.py"),
            "--kind",
            memo_kind,
            "--repo-root",
            str(repo_root),
        ]
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

    # ----- SOCRATIC THINK-THROUGH (master build P2.4) -----
    # The only path to a per-holding stance (locked advisor posture). Both
    # calls run SYNCHRONOUSLY in the request — the owner is sitting in front
    # of the form, localhost has no proxy timeout, and the UI shows progress;
    # the jobs/SSE machinery stays for fire-and-forget runs.

    @app.route("/api/socratic/questions", methods=["POST", "OPTIONS"])
    def socratic_questions():
        """Step 1: 3-5 pointed questions for the owner, grounded in the
        holding's numbers. Body: {"ticker": "NU"}. Returns the questions +
        the context block they cite. 502 on an LLM/parse failure (the owner
        retries from the form)."""
        if request.method == "OPTIONS":
            return ("", 204)
        from advisor.socratic import generate_questions

        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        ticker = str(body.get("ticker") or "").strip().upper()
        if not ticker:
            return ({"error": "ticker required"}, 400)
        try:
            prelude = generate_questions(repo_root, ticker)
        except Exception as exc:  # surface to the form; owner-driven retry
            return ({"error": f"{type(exc).__name__}: {exc}"}, 502)
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
            )
        except ValueError as exc:  # length mismatch / empty answers / bad horizon
            return ({"error": str(exc)}, 400)
        except Exception as exc:  # hard stops surface loudly to the form
            return ({"error": f"{type(exc).__name__}: {exc}"}, 502)
        if not result.ok or result.memo_id is None:
            return ({"error": result.skipped_reason or "memo generation failed"}, 502)
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
            user_message = str(body["message"])
        except (KeyError, ValueError, TypeError) as e:
            return ({"error": f"bad payload: {e}"}, 400)

        # The unified ask engine with this report's TICKER context pack
        # ("one brain, two entry points" — same engine as /api/ask).
        # Deterministic commands reply instantly, metric questions render
        # live view fragments, everything else streams from the narrative
        # LLM with the report context + persisted thread.
        raw_ctx = body.get("context_spec")
        turn = AskTurn(
            text=user_message,
            context_spec=cast("dict[str, object]", raw_ctx) if isinstance(raw_ctx, dict) else None,
        )
        pack = build_ticker_pack(ticker, report_date)

        events = respond_turn(
            turn, pack, db_path=db_path, repo_root=repo_root, registry=job_registry
        )
        return _stream_engine_events(events)

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


def _drain_events(
    events: Iterator[dict[str, object]],
    chunks: queue.Queue[dict[str, object] | None],
) -> None:
    """Iterate an ask-engine event stream on a pool thread and push each
    event onto `chunks`. `None` marks end-of-stream so the SSE generator
    on the request thread can stop pumping."""
    try:
        for chunk in events:
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
