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
import contextlib
import json
import math
import os
import queue
import sys
import urllib.parse
from collections import deque
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
import ticker_validation  # noqa: E402
from alerts import (  # noqa: E402
    ACTION_STATUS_APPLIED,
    ACTION_STATUS_CANCELLED,
    ACTION_STATUS_PENDING,
)
from ask.context import build_portfolio_pack, build_ticker_pack  # noqa: E402
from ask.engine import AskTurn, fold_events, respond_turn, sanitize_history  # noqa: E402
from ask.store import (  # noqa: E402
    AskSession as _AskSession,
)
from ask.store import (  # noqa: E402
    delete_session,
    ensure_session,
    get_session,
    list_sessions,
    load_turns,
    rename_session,
)
from chat_session import apply_chat_diff, build_chat_response  # noqa: E402
from dashboard import render_alert_feed  # noqa: E402
from dashboard.inbox import collect_inbox, render_inbox_stream  # noqa: E402
from dashboard.upcoming import render_upcoming_strip  # noqa: E402
from dcf import persist as dcf_persist  # noqa: E402
from dcf import redesign as dcf_redesign  # noqa: E402
from discovery.store import BUILDABLE_STATUSES  # noqa: E402
from dispatch_registry import Registry, RegistryConflict  # noqa: E402
from identity import DEFAULT_USER_ID  # noqa: E402
from llm.cli import LLMBudgetExceeded, is_hard_stop  # noqa: E402
from logging_config import (  # noqa: E402
    configure_logging,
    new_correlation_id,
    set_correlation_id,
)
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


# The panel id is interpolated rather than written as one literal '#decis…'
# string: a hex-color scan over CSS-emitting modules reads '#dec' as a raw
# 3-digit color (open_loops.py's documented idiom) — this constant isn't
# itself scanned (comments_server.py carries no CSS), but the value flows
# into acted_span()'s rendered href, so the same discipline applies at the
# source of truth.
_DECISIONS_RECORD_PANEL = "decisions_record"
_DECISIONS_RECORD_HASH = f"/#{_DECISIONS_RECORD_PANEL}"


def _approve_consequence_href(consequence: str) -> str | None:
    """The doorway an approve consequence string opens onto, or None when
    none applies. Only ever a REAL registered panel hash — never invented:
    a written thesis-ledger entry or a sizing intent both land in the
    Portfolio > Decisions panel (P2.2 folded the standalone Thesis Ledger
    tab into ``decisions_record`` — see command_center_shell.py's panel
    registry)."""
    if "Ledger entry id=" in consequence or "position_sizing_intent id=" in consequence:
        return _DECISIONS_RECORD_HASH
    return None


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


def _dcf_recompute_payload(inp: dcf_redesign.RedesignInputs) -> dict[str, object]:
    """Run the pure DCF engine over one assumption set → the JSON the in-app
    valuation card consumes.

    The whole in-app modify→recompute loop in one stateless call (resolves the
    DCF round-trip gap): base fair value, the Bull/Base/Bear scenario triplet,
    the live over/under (decimal, the 0076 convention), and the WACC × exit-
    multiple sensitivity grid (computed today but trapped in xlsx cells). No
    xlsx, no persistence — a save / Push-to-Sheets commit is a separate action.

    Raises :class:`dcf_redesign.RedesignError` only for a degenerate BASE (e.g. a
    perpetuity terminal with WACC ≤ g); a degenerate Bull/Bear degrades to
    ``None`` inside ``scenario_values`` rather than raising.
    """
    sv = dcf_redesign.scenario_values(inp)
    grid = dcf_redesign.sensitivity_grid(inp)
    return {
        "fair_value_per_share_usd": sv.base,
        "scenarios": {"base": sv.base, "bull": sv.bull, "bear": sv.bear},
        "over_under_pct": dcf_persist.derive_over_under(inp.current_price, sv.base),
        "wacc": inp.wacc,
        "terminal_method": inp.terminal_method,
        "terminal_basis": inp.terminal_basis,
        "exit_multiple": inp.exit_multiple,
        "current_price": inp.current_price,
        "sensitivity": {
            "wacc_axis": list(grid.wacc_axis),
            "multiple_axis": list(grid.multiple_axis),
            "values": [list(r) for r in grid.values],
            "base_wacc": grid.base_wacc,
            "base_multiple": grid.base_multiple,
            "basis": grid.basis,
            "current_price": grid.current_price,
        },
    }


def _note_to_json(note: object) -> dict[str, object]:
    """AnalystNoteRow → JSON-safe dict for the /api/notes responses (P4.5)."""
    from dataclasses import asdict
    from datetime import datetime as _dt

    payload = asdict(note)  # pyright: ignore[reportArgumentType]  # always an AnalystNoteRow
    return {k: (v.isoformat() if isinstance(v, _dt) else v) for k, v in payload.items()}


def _opt_int(raw: object) -> int | None:
    """A JSON field as int, or None when absent/empty/non-numeric — the
    note-link routes' tolerant id decode (S15)."""
    if raw is None or raw == "":
        return None
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None


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


def _payload_text(value: object) -> str | None:
    """A trimmed non-empty string from a JSON payload field, else None."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _record_dismiss_pass(
    *,
    ticker: str,
    reason: str,
    revisit_text: str | None,
    source_dismissal_id: int | None,
    db_path: Path,
) -> dict[str, object] | None:
    """Record a pass/avoid decision (L11), JSON-shaped for the response. None
    when the ledger is unavailable — the dismiss/queue move still succeeded, the
    optional decision capture just didn't land."""
    from pass_decisions import (
        LENS_DISCOVERY_DISMISSAL,
        LENS_MANUAL_PASS,
        record_pass_decision,
    )

    result = record_pass_decision(
        ticker=ticker,
        reason=reason,
        revisit_text=revisit_text,
        source_dismissal_id=source_dismissal_id,
        source_lens=(
            LENS_DISCOVERY_DISMISSAL if source_dismissal_id is not None else LENS_MANUAL_PASS
        ),
        db_path=db_path,
    )
    if result is None:
        return None
    return {"decision_id": result.decision_id, "created": result.created, "ticker": ticker}


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

    def _stream_engine_events_with_session(
        events: Iterator[dict[str, object]], session_id: str
    ) -> Response:
        """Like ``_stream_engine_events`` but emits a leading
        ``{type: "session", session_id: "…"}`` frame so the client always
        knows which session this turn belongs to."""
        chunks: queue.Queue[dict[str, object] | None] = queue.Queue()
        chat_pool.submit(_drain_events, events, chunks)

        def generate():
            yield f"data: {json.dumps({'type': 'session', 'session_id': session_id})}\n\n"
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

    @app.before_request
    def bind_correlation_id() -> None:
        # Fresh correlation id per request so all log lines for one operation
        # stitch together (sre-4). Honor an upstream X-Correlation-ID if present.
        incoming = request.headers.get("X-Correlation-ID", "")
        if incoming:
            set_correlation_id(incoming)
        else:
            new_correlation_id()

    @app.before_request
    def csrf_origin_guard():
        # CSRF defense-in-depth for the unauthenticated localhost control plane.
        # A site the operator is visiting can drive a cross-origin state-changing
        # request at this server; reject any unsafe-method request whose browser
        # Origin is cross-site (judged by the same loopback / "null" / whitelist
        # rule as CORS, via _cors_allow_origin). Safe methods and the OPTIONS
        # preflight are exempt; an absent Origin (local CLI / curl / tests /
        # same-origin non-browser caller) is allowed. This complements the
        # CORS-withholding in add_cors_headers, which only stops requests the
        # browser bothers to preflight — the Origin check also covers a simple
        # or forged cross-site request that skips preflight.
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        origin = request.headers.get("Origin", "")
        if origin and _cors_allow_origin(origin) is None:
            return ({"error": "cross-origin state-changing request refused"}, 403)
        return None

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
        # Security headers — the dashboard is network-reachable over Tailscale.
        # SAMEORIGIN (not DENY) because the command center embeds /reports/<T> in
        # a same-origin iframe. no-referrer so ticker-bearing report URLs (which
        # reveal positions) never leak in a Referer to any external destination.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    @app.after_request
    def add_panel_etag(response: Response) -> Response:
        # Cheap revalidation for the shell's panel fragments (S14): every
        # 200-OK GET under /api/panel/ carries a content ETag, and a matching
        # If-None-Match comes back 304 with an empty body — the client's
        # stale-while-revalidate refresh costs the panel BUILD, not the panel
        # TRANSFER, and an unchanged panel never re-renders. `no-cache` means
        # "store but always revalidate", so the browser's own HTTP cache gives
        # the drawer/notes/peek fetches the same cheap 304 path with zero
        # client changes. (Registered after add_cors_headers — Flask runs
        # after_request hooks in reverse order, so the 304 conversion happens
        # first and the CORS headers still land on the 304.)
        if (
            request.method == "GET"
            and request.path.startswith("/api/panel/")
            and response.status_code == 200
            and not response.direct_passthrough
        ):
            response.add_etag()
            response.headers["Cache-Control"] = "no-cache"
            # make_conditional mutates + returns self; the cast restores the
            # Flask subclass the werkzeug stub erases.
            return cast("Response", response.make_conditional(request))
        return response

    @app.route("/healthz", methods=["GET"])
    def healthz():
        # No repo_root — a network-reachable liveness endpoint must not leak the
        # absolute server filesystem path.
        return {"status": "ok"}

    @app.route("/api/capture/text", methods=["POST", "OPTIONS"])
    def capture_text():
        """The Ledger at-desk tray: land a typed musing through the SAME LLM-free
        ingest pipeline the Telegram poller uses (channel='tray'). CSRF-guarded by
        the global Origin check on JSON state-changing requests."""
        if request.method == "OPTIONS":
            return ("", 204)
        from capture.ingest import ingest_capture

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        text = str(payload.get("text") or "").strip()
        if not text:
            return ({"error": "text required"}, 400)
        result = ingest_capture(channel="tray", media_kind="text", text=text, db_path=db_path)
        # Fire the wondering tap on a landed tray musing — previously only the
        # Telegram poller tapped, so a TYPED wondering never became a chip.
        wondering_task_id: int | None = None
        pledge_challenge: str | None = None
        annotated_decision_id: int | None = None
        if result.status == "landed" and result.note_id is not None:
            from research.proposals import detect_and_create_task, tap_enabled

            if tap_enabled():
                wondering_task_id = detect_and_create_task(
                    result.note_id, db_path=db_path, channel="tray"
                )
            # Entry-coaching taps (W2): a pledge gets the catalyst-test
            # challenge back; an annotation-shaped follow-up fills the newest
            # pending stub's NULL conviction/falsifier. Never breaks capture.
            try:
                from research.pledge import (
                    annotate_latest_pending,
                    build_challenge,
                    detect_and_capture_pledge,
                )

                pledge = detect_and_capture_pledge(result.note_id, channel="tray", db_path=db_path)
                if pledge is not None:
                    pledge_challenge = build_challenge(pledge, repo_root=repo_root, db_path=db_path)
                else:
                    annotated_decision_id = annotate_latest_pending(text, db_path=db_path)
            except Exception:
                pass
        return {
            "status": result.status,
            "note_id": result.note_id,
            "ticker": result.ticker,
            "needs_ticker": result.needs_ticker,
            "wondering_task_id": wondering_task_id,
            "pledge_challenge": pledge_challenge,
            "annotated_decision_id": annotated_decision_id,
        }

    @app.route("/api/research/task/<int:task_id>/run", methods=["POST", "OPTIONS"])
    def research_run(task_id: int):
        """W1-5d: run the two-pass research engine on a proposed task → an inert
        proposal. Gated by LEDGER_RESEARCH_RUN (the only place the expensive web
        pass is triggered, and only on an explicit owner tap). CSRF-guarded by the
        global Origin check."""
        if request.method == "OPTIONS":
            return ("", 204)
        from research.proposals import research_run_enabled

        if not research_run_enabled():
            return ({"error": "research run disabled; set LEDGER_RESEARCH_RUN=1"}, 403)
        from research.run import run_research_task

        try:
            proposal_id = run_research_task(task_id, db_path=db_path, repo_root=repo_root)
        except Exception as exc:  # a run failure reverts the task; surface it
            return ({"error": f"research failed: {exc}"}, 500)
        if proposal_id is None:
            return ({"error": "task not runnable (missing or already researched)"}, 409)
        return {"proposal_id": proposal_id}

    @app.route("/api/research/task/<int:task_id>/reject", methods=["POST", "OPTIONS"])
    def research_reject(task_id: int):
        """Dismiss a proposed wondering from the Ledger's open-wonderings list —
        the counterpart to /run for a task that was never a real research
        question (e.g. a retrospective lesson mis-staged via Incorporate). Flips
        the task proposed → rejected so it drops out of the list. State-changing,
        so a cross-site fetch is rejected; 404 on an unknown id."""
        if request.method == "OPTIONS":
            return ("", 204)
        if request.headers.get("Sec-Fetch-Site", "") == "cross-site":
            return ({"error": "cross-site reject rejected"}, 403)
        from research.proposals import get_task, set_task_status

        if get_task(task_id, db_path=db_path) is None:
            return ({"error": "task not found"}, 404)
        set_task_status(task_id, "rejected", db_path=db_path)
        return {"ok": True}

    @app.route("/api/research/proposal/<int:proposal_id>/<verb>", methods=["POST", "OPTIONS"])
    def research_act(proposal_id: int, verb: str):
        """W1-7: the 4-action core (approve / further / steer / reject). 'approve'
        flips status; a view artifact then writes its saved view via the separate
        write-dispatch (no web fetch, so never a trifecta). CSRF-guarded."""
        if request.method == "OPTIONS":
            return ("", 204)
        from research.proposals import PROPOSAL_VERBS, act_on_proposal

        if verb not in PROPOSAL_VERBS:
            return ({"error": f"unknown verb {verb!r}"}, 400)
        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        steer_text = str(payload.get("steer_text") or "").strip() or None
        status = act_on_proposal(proposal_id, verb, steer_text=steer_text, db_path=db_path)
        applied = ""
        if verb == "approve":
            from research.apply import apply_approved_proposal

            try:
                applied = apply_approved_proposal(proposal_id, db_path=db_path)
            except Exception as exc:  # a bad apply must not 500 the action
                applied = f"apply failed: {exc}"
        return {"status": status, "applied": applied}

    @app.route("/api/reconcile/<kind>/<int:item_id>/<verdict>", methods=["POST", "OPTIONS"])
    def reconcile_verdict(kind: str, item_id: int, verdict: str):
        """Seed-corpus freshness pass: stamp a one-tap verdict on a seed note or
        theme. The coach only leans on items that survived this list. CSRF-guarded
        by the global Origin check."""
        if request.method == "OPTIONS":
            return ("", 204)
        from synthesis.reconcile import RECONCILE_VERDICTS, reconcile_note, reconcile_theme

        if kind not in ("note", "theme"):
            return ({"error": f"unknown kind {kind!r}"}, 400)
        if verdict not in RECONCILE_VERDICTS:
            return ({"error": f"unknown verdict {verdict!r}"}, 400)
        fn = reconcile_note if kind == "note" else reconcile_theme
        ok = fn(item_id, verdict, db_path=db_path)
        return ({"ok": ok}, 200 if ok else 404)

    @app.route("/api/reconcile/falsifier/<int:decision_id>", methods=["POST", "OPTIONS"])
    def reconcile_falsifier(decision_id: int):
        """Ratify / rewrite / drop an '(inferred)' falsifier on an owner decision —
        the coach may only quote falsifiers in the owner's own words.

        Consequence receipt (0142): a successful ``ratify`` gains a
        ``receipt`` string reporting the tripwire-arming state. Arming itself
        (``decision_conditions.attach_conditions``) always calls an LLM
        purpose for an unstamped row — real spend that must never ride an
        inline ratify click — so this NEVER runs extraction; it only READS
        whether a prior batch pass already reached this decision
        (``decision_conditions.arming_status``, zero-LLM). 'armed' when
        conditions are already stamped, else "queued for arming" — honest
        about the fact that the pass hasn't run yet, not a lie that it just
        did."""
        if request.method == "OPTIONS":
            return ("", 204)
        from synthesis.reconcile import FALSIFIER_ACTIONS, falsifier_action

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        action = str(payload.get("action") or "")
        if action not in FALSIFIER_ACTIONS:
            return ({"error": f"unknown action {action!r}"}, 400)
        text = str(payload.get("text") or "").strip() or None
        try:
            ok = falsifier_action(decision_id, action, text=text, db_path=db_path)
        except ValueError as exc:
            return ({"error": str(exc)}, 400)
        if not ok:
            return ({"ok": False}, 404)
        result: dict[str, object] = {"ok": True}
        if action == "ratify":
            from decision_conditions import arming_status

            status = arming_status(decision_id, db_path=db_path)
            result["receipt"] = (
                "armed — now watched by the tripwire engine"
                if status == "armed"
                else "ratified — queued for arming (next extraction pass)"
            )
        return (result, 200)

    @app.route("/api/onmymind/<int:note_id>/<verb>", methods=["POST", "OPTIONS"])
    def onmymind_act(note_id: int, verb: str):
        """The On My Mind action ladder: dismiss / save / discuss / incorporate on
        one captured item. Delegates to the ONE action core the Telegram callback
        also calls. Safe by construction — it archives / patches context / stages an
        inert research task; it never fetches the web or writes a live artifact.
        CSRF-guarded by the global Origin check."""
        if request.method == "OPTIONS":
            return ("", 204)
        from onmymind.feed import LADDER_LABELS, LADDER_VERBS, act_on_feed_item

        if verb not in LADDER_VERBS:
            return ({"error": f"unknown verb {verb!r}"}, 400)
        result = act_on_feed_item(note_id, verb, db_path=db_path)
        return (
            {
                "ok": result.ok,
                "removed": result.removed,
                "ladder": result.ladder,
                "ladder_label": LADDER_LABELS.get(result.ladder or "", ""),
                "task_id": result.task_id,
                "thread_url": result.thread_url,
                "message": result.message,
            },
            200 if result.ok else 404,
        )

    @app.route("/api/tenets", methods=["POST", "OPTIONS"])
    def tenets_create():
        """Add an owner-stated Tenet — a durable belief about how the owner invests
        (Worldview P2). Lands ``current`` immediately (the owner's own belief needs
        no approval); reusing a scope_key revises the standing Tenet via the
        supersede chain. CSRF-guarded by the global Origin check."""
        if request.method == "OPTIONS":
            return ("", 204)
        from synthesis.tenets import record_tenet

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        body_md = str(payload.get("body_md") or "").strip()
        if not body_md:
            return ({"error": "body_md required"}, 400)
        scope_key = str(payload.get("scope_key") or "").strip() or None
        tenet = record_tenet(
            body_md=body_md,
            scope_key=scope_key,
            status="current",
            provenance="owner",
            db_path=db_path,
        )
        return {"ok": True, "id": tenet.id, "scope_key": tenet.scope_key}

    @app.route("/api/tenets/<int:tenet_id>/<action>", methods=["POST", "OPTIONS"])
    def tenets_act(tenet_id: int, action: str):
        """Approve or reject a machine-distilled ``proposed`` Tenet. Approve promotes
        it to ``current`` (superseding the prior belief on that topic); reject retires
        it. CSRF-guarded."""
        if request.method == "OPTIONS":
            return ("", 204)
        from synthesis.tenets import approve_tenet, reject_tenet

        if action == "approve":
            row = approve_tenet(tenet_id, db_path=db_path)
            return (
                {"ok": row is not None, "status": row.status if row else None},
                200 if row else 404,
            )
        if action == "reject":
            ok = reject_tenet(tenet_id, db_path=db_path)
            return ({"ok": ok}, 200 if ok else 404)
        return ({"error": f"unknown action {action!r}"}, 400)

    @app.route("/api/tenets/distill", methods=["POST", "OPTIONS"])
    def tenets_distill():
        """Owner-tapped Worldview distillation: distil the owner's flagged
        (saved/incorporated) musings into ``proposed`` Tenets. Never automatic; the
        deterministic $0 triage means nothing-flagged ⇒ zero LLM. CSRF-guarded."""
        if request.method == "OPTIONS":
            return ("", 204)
        from synthesis.tenet_distill import run_tenet_distill

        try:
            counts = run_tenet_distill(
                db_path, user_id=request.args.get("user_id", DEFAULT_USER_ID)
            )
        except Exception as exc:  # a distill failure must not 500 the tap
            return ({"error": f"distill failed: {exc}"}, 500)
        return {"ok": True, **counts}

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
        # The ritual-debt band above the cockpit — the owner's open queues
        # (Reconcile / Tenets / proposals / decision stubs / coach digest)
        # lead the first screen; never raises on a thin DB.
        from pipeline.open_loops import render_open_loops_band

        open_loops_html = render_open_loops_band(db_path)
        overview = render_overview_panel(
            rows,
            coverage,
            inbox_html=inbox_html,
            upcoming_html=upcoming_html,
            open_loops_html=open_loops_html,
        )
        return Response(
            render_shell(overview_html=overview, repo_root=repo_root), mimetype="text/html"
        )

    @app.route("/api/dashboard", methods=["GET"])
    def dashboard_api():
        conn = _open_db()
        try:
            rows = build_dashboard_rows(conn, repo_root)
        finally:
            conn.close()
        return {k: [r.to_dict() for r in v] for k, v in rows.items()}

    @app.route("/api/cockpit", methods=["GET"])
    def cockpit_fragment():
        """The Research cockpit re-rendered as an HTML fragment for HTMX's
        periodic poll (Wave 3): the Overview's ``#cc-cockpit-live`` wrapper
        re-fetches this every 90s so the time-varying tiles (price · earnings
        countdown · staleness) refresh in place without a full page reload."""
        from pipeline.research_cockpit import render_research_cockpit

        conn = _open_db()
        try:
            rows = build_cockpit_rows(conn, repo_root)
        finally:
            conn.close()
        return Response(render_research_cockpit(rows), mimetype="text/html")

    @app.route("/api/cron-health", methods=["GET"])
    def cron_health_fragment():
        """The cron-health live body (KPI strip + 7-day timeline) for HTMX's
        periodic poll (Wave 9 live-tile): the Cron Health panel's
        ``#cc-cron-live`` wrapper re-fetches this every 60s so today's pipeline
        verdict flips from "Not run yet" to OK/FAILED in place — the same
        self-refresh idiom as ``/api/cockpit``, no bespoke JS."""
        from pipeline.cron_health_panel import render_cron_health_live_body

        return Response(render_cron_health_live_body(db_path), mimetype="text/html")

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
            # % of book / taxable breakdown from the companion tracker, plus
            # the per-position attribution narratives (S15 — db_path joins
            # lifecycle entries + thesis events onto the alpha rows).
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
                    db_path=db_path,
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

        if name == "positioning":
            # Portfolio → Positioning: the owner's durable target book
            # (positioning_intents) + version history + the coach thread with
            # its propose→approve flow. GET path reads the materialized fit
            # meta + local DB only — never the tracker.
            from pipeline.positioning_panel import render_positioning_panel

            return Response(render_positioning_panel(db_path, repo_root), mimetype="text/html")

        if name == "portfolio_risk":
            # Portfolio → Risk (L5): the whole-book risk cockpit — book drawdown
            # (max DD + underwater curve + recovery) computed from the tracker's
            # daily TWR, factor/style exposure rolled up from the per-ticker
            # correlation/beta rows, and the macro-stress lens with a scenario
            # picker (POST /actions/run-scenario). Tracker-fed sections degrade
            # to an offline note; macro stress reads the local cache regardless.
            from pipeline.portfolio_panel import render_portfolio_risk_panel

            return Response(render_portfolio_risk_panel(db_path=db_path), mimetype="text/html")

        if name == "red_team":
            # Portfolio -> Red Team (PR5): the monthly First-Saturday
            # adversarial brief. Read-only in this PR — status chips only,
            # the response loop lands in PR6.
            from pipeline.red_team_panel import render_red_team_panel

            return Response(render_red_team_panel(db_path=db_path), mimetype="text/html")

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

        if name == "cron_health":
            # Last-7-day pipeline run history from ingestion_runs, ordered by
            # criticality (backup_db → run_morning_pipeline → others); KPI strip
            # for today's morning pipeline verdict and consecutive-clean-day streak.
            from pipeline.cron_health_panel import render_cron_health_panel

            return Response(render_cron_health_panel(db_path), mimetype="text/html")

        if name == "dcf_coverage":
            # Which of the ~90 DCF workbooks are live / stale / skipped /
            # orphaned (S11): per-name workbook + dcf_runs freshness +
            # assumptions-JSON state + the workbook→JSON sync outcome (0091).
            from pipeline.dcf_coverage_panel import render_dcf_coverage_panel

            return Response(render_dcf_coverage_panel(db_path, repo_root), mimetype="text/html")

        if name == "dcf_globals":
            # Settings drawer: the editable global macro DCF inputs (risk-free /
            # ERP / tax, migration 0112) + each field's per-ticker overrides.
            from pipeline.dcf_globals_panel import render_dcf_globals_panel

            return Response(render_dcf_globals_panel(db_path), mimetype="text/html")

        if name == "validation":
            # Whole-book data-quality state over validation_issues (P3.4) —
            # range violations, magnitude jumps, source disagreement, unit
            # mismatches, previously visible only per-ticker in reports.
            from pipeline.validation_issues_panel import render_validation_panel

            return Response(render_validation_panel(db_path), mimetype="text/html")

        if name == "provenance":
            # System → Provenance (S10): the consolidated data-quality console —
            # one page composing the 8 diagnostics builders (Coverage prominent
            # + Validation + Evals + IR/cache/cron/DCF/restatements). Replaces the
            # old 8-tab strip; the killed ids alias here (_LEGACY_PANEL_REDIRECTS).
            from pipeline.provenance_panel import render_provenance_panel

            user_id = request.args.get("user_id", DEFAULT_USER_ID)
            return Response(
                render_provenance_panel(db_path, repo_root, user_id=user_id),
                mimetype="text/html",
            )

        if name == "section_coverage":
            # Per-ticker section coverage (P4.2): the visible counterpart of
            # the hide-don't-stub policy — reports hide cold sections, this
            # matrix is where the gaps stay accountable. Still served standalone
            # for the Provenance console's anchor + any direct fetch.
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
            from pipeline.explore_panel import (
                render_explore_panel,
                render_keymetrics_fragment,
                render_saved_views_list,
            )

            user_id = request.args.get("user_id", DEFAULT_USER_ID)
            fragment = request.args.get("fragment")
            if fragment == "views":
                return Response(
                    render_saved_views_list(db_path, user_id=user_id), mimetype="text/html"
                )
            if fragment == "keymetrics":
                # The key-metrics preselect bubble row for a (changed) ticker set
                # — tier-graded baseline + cached LLM picks (key_metrics_picker.md).
                km_tickers = [
                    t.strip().upper()
                    for t in (request.args.get("tickers") or "").split(",")
                    if t.strip()
                ]
                return Response(
                    render_keymetrics_fragment(db_path, km_tickers), mimetype="text/html"
                )
            return Response(render_explore_panel(db_path, user_id=user_id), mimetype="text/html")

        if name == "diet":
            # Companies → Diet: the information-diet curation layer (the
            # alerts→diet split). The PULL lane over the typed `signals`
            # substrate — non-decaying sell-side ratings + news + the forward
            # investor-day agenda. Pure read; never feeds the inbox scorer.
            from pipeline.diet_panel import render_diet_panel

            return Response(render_diet_panel(db_path), mimetype="text/html")

        if name == "musings":
            # Companies → Ledger (The Ledger capture program): the captured
            # stream-of-consciousness read-back + at-desk quick-capture box.
            # ``?fragment=list`` returns just the musings list the box reloads
            # after a POST /api/capture/text.
            from pipeline.ledger_panel import (
                render_ledger_list,
                render_ledger_panel,
                render_ledger_research_list,
                render_onmymind_list,
                render_reconcile_list,
            )

            user_id = request.args.get("user_id", DEFAULT_USER_ID)
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
                    render_onmymind_list(
                        db_path, cursor=request.args.get("cursor"), user_id=user_id
                    ),
                    mimetype="text/html",
                )
            if fragment == "worldview":
                # The Worldview review body — re-fetched after add / approve /
                # reject / distill.
                from pipeline.worldview_panel import render_worldview_body

                return Response(render_worldview_body(db_path), mimetype="text/html")
            l_renderer = render_ledger_list if fragment == "list" else render_ledger_panel
            return Response(l_renderer(db_path, user_id=user_id), mimetype="text/html")

        if name == "discovery":
            # Research → Discovery (P5.4): the candidate approval queue —
            # the budget gate ("queue, never auto-build"). ``?fragment=list``
            # returns just the table; ``?fragment=sources`` the weight editor.
            from pipeline.discovery_panel import (
                render_discovery_list,
                render_discovery_panel,
                render_sources_editor,
            )

            user_id = request.args.get("user_id", DEFAULT_USER_ID)
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

        if name == "journal":
            # Research → Journal (P4.5 + S15): the analyst_notes lifecycle UI.
            # ``?fragment=list`` returns just the filtered note list and
            # ``?fragment=reconcile`` the pending-reconciliation strip — the
            # panel's own JS refreshes those fragments after every action.
            from pipeline.journal_panel import (
                render_journal_list,
                render_journal_panel,
                render_reconciliation_list,
            )

            user_id = request.args.get("user_id", DEFAULT_USER_ID)
            j_ticker = (request.args.get("ticker") or "").strip().upper() or None
            j_kind = (request.args.get("kind") or "").strip() or None
            j_status = (request.args.get("status") or "open").strip() or "open"
            if request.args.get("fragment") == "reconcile":
                return Response(
                    render_reconciliation_list(db_path, user_id=user_id, ticker=j_ticker),
                    mimetype="text/html",
                )
            renderer = (
                render_journal_list
                if request.args.get("fragment") == "list"
                else render_journal_panel
            )
            return Response(
                renderer(db_path, user_id=user_id, ticker=j_ticker, kind=j_kind, status=j_status),
                mimetype="text/html",
            )

        if name == "triage":
            # Companies → Triage (S11): the parked-comment disposition queue —
            # comments the classifier couldn't route (`needs_triage`). A lens
            # over analyst_notes; ``?fragment=list`` returns just the table the
            # panel JS refreshes after a route / resolve / dismiss.
            from pipeline.triage_panel import render_triage_list, render_triage_panel

            user_id = request.args.get("user_id", DEFAULT_USER_ID)
            t_renderer = (
                render_triage_list
                if request.args.get("fragment") == "list"
                else render_triage_panel
            )
            return Response(t_renderer(db_path, user_id=user_id), mimetype="text/html")

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

        if name == "evals":
            # LLM eval scores (llm_evals_plan §2.6): latest run per purpose
            # (+ cost joined from llm_calls), score-by-prompt-version A/B
            # strip, failed-case drawers, per-purpose error/fallback health,
            # and run buttons via the jobs SSE machinery.
            from pipeline.evals_panel import render_evals_panel

            return Response(render_evals_panel(db_path), mimetype="text/html")

        if name == "model_eval":
            # Optimizer panel (model_eval_loop.md PR4): the model-downgrade
            # loop's surface over model_eval_verdicts + model_pin_overrides +
            # llm_calls — the anonymous-purpose alarm, active overrides with a
            # realized-savings rollup, per-(purpose, candidate) verdict history
            # (CANDIDATE_ERRORED as an infra flag), and per-purpose 30d cost.
            from pipeline.model_eval_panel import render_model_eval_panel

            return Response(render_model_eval_panel(db_path), mimetype="text/html")

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

    @app.route("/api/position-lifecycle/<ticker>", methods=["GET"])
    def position_lifecycle_fragment(ticker: str):
        """The holding page's position-lifecycle timeline as a standalone
        fragment (S5 PR2) — the grading form re-fetches this after a POST so
        the section refreshes in place without reloading the shell."""
        from pipeline.position_lifecycle_panel import render_position_lifecycle_section

        return Response(
            render_position_lifecycle_section(
                db_path, ticker, user_id=request.args.get("user_id", DEFAULT_USER_ID)
            ),
            mimetype="text/html",
        )

    @app.route("/api/position-entries/<int:entry_id>", methods=["POST", "OPTIONS"])
    def position_entry_grade(entry_id: int):
        """Write the analyst's post-exit grading onto one position_entries row:
        ``{exit_reason?, lessons?, outcome_vs_thesis?}``. Omitted keys are left
        untouched; empty strings clear a field. 400 on an unknown outcome
        label, 404 on a missing row."""
        if request.method == "OPTIONS":
            return ("", 204)
        from position_lifecycle import update_exit_fields

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})

        def _opt(key: str) -> str | None:
            value = payload.get(key)
            return str(value) if value is not None else None

        try:
            ok = update_exit_fields(
                db_path=db_path,
                entry_id=entry_id,
                exit_reason=_opt("exit_reason"),
                lessons=_opt("lessons"),
                outcome_vs_thesis=_opt("outcome_vs_thesis"),
            )
        except ValueError as exc:
            return ({"error": str(exc)}, 400)
        except LookupError as exc:
            return ({"error": str(exc)}, 404)
        if not ok:
            return ({"error": "position_entries unavailable (pre-0088 DB?)"}, 500)
        return {"id": entry_id, "ok": True}

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

    # ----- PANEL LATENCY METRICS (S14) + ACTIVATION COUNTS (navigation_ia §5) -----
    # The shell's loader POSTs one sample per panel activation/refresh
    # (fetch/render/total ms + which cache path served it). Latency stays an
    # in-memory ring — perceived-latency telemetry for a single-operator
    # localhost app is diagnostics, not data. The activation COUNT, though, is
    # data (instrument-first: "does the owner actually walk this surface?"),
    # so user-perceived activations (cold|swr — not prefetch/revalidate, which
    # are speculative/background) also bump a durable (panel_id, day) counter.
    # Surfaced in System → Data Cache (the panel fetches the GET aggregate).
    panel_metrics: deque[dict[str, object]] = deque(maxlen=500)
    metric_cache_modes = frozenset({"cold", "swr", "prefetch", "revalidate"})
    activation_cache_modes = frozenset({"cold", "swr"})

    def _bump_activation_count(panel: str) -> None:
        """UPSERT +1 for (panel, today) — lazy DDL so a fresh init_db database
        works without alembic (0147 records the schema lineage). Never raises:
        metrics must never break the shell."""
        try:
            conn = _open_db()
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS panel_activation_counts ("
                    " panel_id TEXT NOT NULL, day TEXT NOT NULL,"
                    " count INTEGER NOT NULL DEFAULT 0,"
                    " PRIMARY KEY (panel_id, day))"
                )
                conn.execute(
                    "INSERT INTO panel_activation_counts (panel_id, day, count)"
                    " VALUES (?, ?, 1)"
                    " ON CONFLICT(panel_id, day) DO UPDATE SET count = count + 1",
                    (panel, datetime.now(UTC).strftime("%Y-%m-%d")),
                )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            print(
                json.dumps({"event": "panel_activation_count_failed", "panel": panel, "error": str(exc)}),
                file=sys.stderr,
            )

    @app.route("/api/metrics/panel", methods=["POST", "OPTIONS"])
    def panel_metrics_post():
        """Record one client-side panel timing sample:
        ``{panel, cache, fetch_ms, render_ms, total_ms, status?}``.
        Fire-and-forget from the shell — always 204 on accepted shape."""
        if request.method == "OPTIONS":
            return ("", 204)
        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        panel = str(payload.get("panel") or "").strip()
        cache = str(payload.get("cache") or "").strip()
        if not panel or cache not in metric_cache_modes:
            return ({"error": "panel + cache (cold|swr|prefetch|revalidate) required"}, 400)

        def _ms(key: str) -> float | None:
            value = payload.get(key)
            if isinstance(value, (int, float)) and 0 <= float(value) < 600_000:
                return round(float(value), 1)
            return None

        status_raw = payload.get("status")
        panel_metrics.append(
            {
                "panel": panel[:40],
                "cache": cache,
                "fetch_ms": _ms("fetch_ms"),
                "render_ms": _ms("render_ms"),
                "total_ms": _ms("total_ms"),
                "status": int(status_raw) if isinstance(status_raw, (int, float)) else None,
                "at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        if cache in activation_cache_modes:
            _bump_activation_count(panel[:40])
        return ("", 204)

    @app.route("/api/metrics/panel", methods=["GET"])
    def panel_metrics_get():
        """Aggregate of the in-memory samples: per (panel, cache-path) count +
        p50/p95 total ms, plus the overall perceived-latency headline. Resets
        on server restart by design."""
        groups: dict[tuple[str, str], list[float]] = {}
        for s in panel_metrics:
            total = s.get("total_ms")
            if not isinstance(total, (int, float)):
                continue
            groups.setdefault((str(s["panel"]), str(s["cache"])), []).append(float(total))

        def _p(values: list[float], q: float) -> float:
            # Nearest-rank percentile: always an observed value, never the
            # beyond-max extrapolation statistics.quantiles produces on small n.
            ordered = sorted(values)
            idx = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
            return round(ordered[idx], 1)

        rows = [
            {
                "panel": panel,
                "cache": cache,
                "n": len(vals),
                "p50_ms": _p(vals, 0.50),
                "p95_ms": _p(vals, 0.95),
            }
            for (panel, cache), vals in sorted(groups.items())
        ]
        # The headline: what a tab activation FEELS like (cold first hits vs
        # the cache-served paths). `revalidate` is background work — excluded.
        perceived = [v for (_panel, c), vals in groups.items() if c != "revalidate" for v in vals]
        # Durable per-panel activation totals (navigation_ia §5) — unlike the
        # latency ring these survive restarts; absent table reads as empty
        # (fresh DB before the first POST creates it).
        activations: dict[str, int] = {}
        try:
            conn = _open_db()
            try:
                cur = conn.execute(
                    "SELECT panel_id, SUM(count) AS n FROM panel_activation_counts"
                    " WHERE day >= date('now', '-30 days') GROUP BY panel_id ORDER BY n DESC"
                )
                activations = {str(r["panel_id"]): int(r["n"]) for r in cur.fetchall()}
            finally:
                conn.close()
        except sqlite3.Error:
            activations = {}
        return {
            "rows": rows,
            "samples": len(panel_metrics),
            "perceived_p50_ms": _p(perceived, 0.50) if perceived else None,
            "perceived_p95_ms": _p(perceived, 0.95) if perceived else None,
            "activations_30d": activations,
        }

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
        consequence = ""
        try:
            if dismissed:
                dismiss_action(action_id, db_path=db_path)
            else:
                # approve_and_apply RETURNS the exact consequence string
                # ("Ledger entry id=N written: ...") — captured so the
                # HTMX quick-action path can show it instead of a bare chip
                # (REQ-11: every ritual action states its specific outcome).
                consequence = approve_and_apply(action_id, db_path=db_path)
        except LookupError as exc:
            return ({"error": str(exc)}, 404)
        except (ValueError, KeyError) as exc:
            # Status conflict (stale or double-clicked link) or a malformed
            # payload — 409 either way; the message says which. The CLI hint
            # on the card remains the fallback path.
            return ({"error": str(exc)}, 409)
        if request.headers.get("HX-Request"):
            # Wave 3b: the inbox quick action swaps its .ix-quick for a done-chip.
            # Approve is terminal (downstream ledger write); a dismiss (cancel)
            # is reversible, so its chip carries an Undo.
            from dashboard.inbox import acted_span

            if dismissed:
                return Response(
                    acted_span(
                        "✕ dismissed", "cancelled", undo_url=f"/api/actions/{action_id}/uncancel"
                    ),
                    mimetype="text/html",
                )
            return Response(
                acted_span(
                    "✓ applied",
                    "applied",
                    detail=consequence,
                    detail_href=_approve_consequence_href(consequence),
                ),
                mimetype="text/html",
            )
        if request.method == "POST":
            status = ACTION_STATUS_CANCELLED if dismissed else ACTION_STATUS_APPLIED
            return {"ok": True, "action_id": action_id, "status": status}
        return redirect(back or "/feed", code=303)

    @app.route("/api/alerts/<int:alert_id>/dismiss", methods=["POST", "OPTIONS"])
    def dismiss_alert_api(alert_id: int):
        """Dismiss one whole alert from the inbox rail's hover ✕ — the
        alert-level counterpart to /approve's action-level dismiss. Transitions
        the alert pending → dismissed AND cancels its still-pending queued
        actions first, so a dismissed alert can't leave an orphaned draft to
        resurface as a standalone inbox card (mirrors the approve CLI's
        --dismiss-alert). JSON in/out; OPTIONS preflight → 204. 404 on an
        unknown id, 409 if the alert is already terminal (a stale or
        double-clicked card). State-changing, so a cross-site fetch is rejected
        — the JSON content-type already blocks a simple cross-site form POST,
        and Sec-Fetch-Site closes the gap for any preflighted one.

        Consequence receipts (0142): the same endpoint doubles as the
        deferred "why?" affordance the acted-span chip renders after the
        dismiss swap — a second POST, alert already dismissed, body carrying
        only ``reason`` — so it must not re-attempt (or 409 on) a transition
        that already happened. An optional ``reason`` on the FIRST call (an
        alert not yet dismissed) is also honored in one round-trip."""
        if request.method == "OPTIONS":
            return ("", 204)
        if request.headers.get("Sec-Fetch-Site", "") == "cross-site":
            return ({"error": "cross-site dismiss rejected"}, 403)
        from alerts import (
            ALERT_STATUS_DISMISSED,
            cancel_action,
            dismiss_alert,
            get_alert,
            list_queued_actions_for_alert,
            set_alert_dismiss_reason,
        )

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        reason = str(payload.get("reason") or request.values.get("reason") or "").strip() or None

        try:
            current = get_alert(alert_id, db_path=db_path)
        except LookupError as exc:
            return ({"error": str(exc)}, 404)

        if current.status == ALERT_STATUS_DISMISSED and reason is not None:
            # The deferred reason-only round-trip: the dismiss already
            # happened, a reason came WITH this call — no re-cancel, no
            # re-transition, just attach the signal. A bare re-POST with no
            # reason on an already-dismissed alert is a stale/double-clicked
            # card, not this round-trip — it falls through to the normal
            # path below, which raises the same 409 conflict it always did.
            try:
                dismissed = set_alert_dismiss_reason(alert_id, reason, db_path=db_path)
            except LookupError as exc:
                return ({"error": str(exc)}, 404)
            except ValueError as exc:
                return ({"error": str(exc)}, 409)
            if request.headers.get("HX-Request"):
                return Response("", mimetype="text/html")
            return {
                "ok": True,
                "alert_id": dismissed.id,
                "status": dismissed.status,
                "dismiss_reason": dismissed.dismiss_reason,
                "cancelled_actions": 0,
            }

        try:
            cancelled = 0
            for qa in list_queued_actions_for_alert(alert_id, db_path=db_path):
                if qa.status == ACTION_STATUS_PENDING:
                    cancel_action(qa.id, db_path=db_path)
                    cancelled += 1
            dismissed = dismiss_alert(alert_id, db_path=db_path, reason=reason)
        except LookupError as exc:
            return ({"error": str(exc)}, 404)
        except (ValueError, KeyError) as exc:
            # Transition conflict — already dismissed/approved/expired.
            return ({"error": str(exc)}, 409)
        if request.headers.get("HX-Request"):
            # Wave 3b: alert-dismiss cascades (it cancels the alert's pending
            # actions too), so there's no clean single Undo — the chip is
            # terminal, matching approve. 0142: the chip grows a "why?" inline
            # affordance unless the reason was already supplied in this call.
            from dashboard.inbox import acted_span

            if dismissed.dismiss_reason:
                return Response(
                    acted_span("✕ dismissed", "cancelled", detail=dismissed.dismiss_reason),
                    mimetype="text/html",
                )
            return Response(
                acted_span("✕ dismissed", "cancelled", dismiss_why_id=alert_id),
                mimetype="text/html",
            )
        return {
            "ok": True,
            "alert_id": dismissed.id,
            "status": dismissed.status,
            "dismiss_reason": dismissed.dismiss_reason,
            "cancelled_actions": cancelled,
        }

    @app.route("/api/actions/<int:action_id>/uncancel", methods=["POST", "OPTIONS"])
    def uncancel_action_api(action_id: int):
        """Undo a dismiss-action (Wave 3b): restore a cancelled queued action to
        pending and return the inbox card's approve/dismiss buttons for HTMX to
        swap back in. 404 unknown id; 409 if the action isn't cancelled (stale
        or already restored)."""
        if request.method == "OPTIONS":
            return ("", 204)
        if request.headers.get("Sec-Fetch-Site", "") == "cross-site":
            return ({"error": "cross-site uncancel rejected"}, 403)
        from alerts import uncancel_action
        from dashboard.inbox import restored_action_buttons

        try:
            uncancel_action(action_id, db_path=db_path)
        except LookupError as exc:
            return ({"error": str(exc)}, 404)
        except (ValueError, KeyError) as exc:
            return ({"error": str(exc)}, 409)
        return Response(restored_action_buttons(action_id), mimetype="text/html")

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

    @app.route("/api/dcf-globals", methods=["GET", "POST", "OPTIONS"])
    def dcf_globals_api():
        """Global DCF macro assumptions (migration 0112). GET returns the
        effective field->value map (seed defaults overlaid with stored). POST
        {"field": <str>, "value": <number>} upserts one field. 400 on an unknown
        field or an out-of-range value; 500 when the DB can't be written."""
        from dcf import global_assumptions as ga

        if request.method == "OPTIONS":
            return ("", 204)
        if request.method == "GET":
            return {"globals": ga.get_all(db_path=db_path)}
        body = request.get_json(silent=True) or {}
        field = body.get("field")
        value = body.get("value")
        if not isinstance(field, str) or value is None:
            return ({"error": "provide field (str) and value (number)"}, 400)
        try:
            ok = ga.set_value(field, value, db_path=db_path)
        except ValueError as e:
            return ({"error": str(e)}, 400)
        if not ok:
            return ({"error": "could not persist global DCF assumption (no DB?)"}, 500)
        return {"field": field, "value": ga.get(field, db_path=db_path), "ok": True}

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
        fact_ref_raw = payload.get("fact_ref")
        context_raw = payload.get("context")
        # Optional 0093 links at capture time ("note this decision" flows).
        # Validated like the /link action — a dangling target is a 404.
        link_decision_id = _opt_int(payload.get("decision_id"))
        link_position_id = _opt_int(payload.get("position_entry_id"))
        if link_decision_id is not None or link_position_id is not None:
            from journal_links import get_target

            for target_kind, target_id in (
                ("decision", link_decision_id),
                ("position", link_position_id),
            ):
                if target_id is not None and (
                    get_target(kind=target_kind, target_id=target_id, db_path=db_path) is None
                ):
                    return ({"error": f"{target_kind} id={target_id} not found"}, 404)
        try:
            created = notes_store.create_note(
                user_id=user_id,
                ticker=note_ticker,
                kind=kind,
                body=note_body,
                anchor_type=str(anchor_type_raw) if anchor_type_raw is not None else None,
                anchor_key=str(anchor_key_raw) if anchor_key_raw is not None else None,
                fact_ref=str(fact_ref_raw) if fact_ref_raw is not None else None,
                source="manual",
                context=cast("dict[str, object]", context_raw)
                if isinstance(context_raw, dict)
                else None,
                decision_id=link_decision_id,
                position_entry_id=link_position_id,
                link_auto_resolve=bool(payload.get("auto_resolve")),
                db_path=db_path,
            )
        except ValueError as exc:
            return ({"error": str(exc)}, 400)
        return ({"note": _note_to_json(created)}, 201)

    @app.route("/api/notes/<int:note_id>/<action>", methods=["POST", "OPTIONS"])
    def notes_action_api(note_id: int, action: str):
        """Lifecycle actions on one note (P4.5 + S15): resolve / reclassify /
        supersede / archive / link / unlink / set_ticker. Supersede creates the
        chained replacement and returns it; the others return the updated row.
        404 on unknown id or dangling link target, 400 on a bad kind, missing
        supersede body, a link with no target, or a set_ticker on a note that
        already has one."""
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
            elif action == "unarchive":
                # Wave 3b: the inbox's optimistic-archive Undo.
                updated = notes_store.unarchive_note(note_id, db_path=db_path)
            elif action == "set_ticker":
                # PR9 Ledger set-ticker chips: attribute a needs_ticker musing to
                # one of its detected candidates with a single tap.
                set_ticker_raw = str(payload.get("ticker") or "").strip()
                if not set_ticker_raw:
                    return ({"error": "ticker required"}, 400)
                updated = notes_store.set_ticker(note_id, ticker=set_ticker_raw, db_path=db_path)
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
            elif action == "link":
                from journal_links import link_note

                updated = link_note(
                    note_id,
                    decision_id=_opt_int(payload.get("decision_id")),
                    position_entry_id=_opt_int(payload.get("position_entry_id")),
                    auto_resolve=bool(payload.get("auto_resolve")),
                    db_path=db_path,
                )
            elif action == "unlink":
                from journal_links import unlink_note

                updated = unlink_note(note_id, db_path=db_path)
            elif action == "route":
                # S11 Triage: route a parked `needs_triage` note to the real
                # comment intent the classifier missed. The note-side write is
                # durable; mirror it onto the underlying comment (system-of-
                # record) when that report build still exists, so a later
                # comment re-sync keeps the two in step instead of reverting.
                route_intent = str(payload.get("intent") or "").strip()
                updated = notes_store.route_triage_note(
                    note_id, intent=route_intent, db_path=db_path
                )
                if updated is not None and updated.source_ref:
                    parts = updated.source_ref.split("/")
                    if len(parts) == 3:
                        with contextlib.suppress(Exception):
                            comments.update_comment(
                                repo_root,
                                parts[0],
                                date.fromisoformat(parts[1]),
                                parts[2],
                                intent=cast("comments.IntentType", route_intent),
                            )
            else:
                return ({"error": f"unknown action {action!r}"}, 404)
        except ValueError as exc:
            return ({"error": str(exc)}, 400)
        except LookupError as exc:
            return ({"error": str(exc)}, 404)
        if updated is None:
            return ({"error": f"note {note_id} not found"}, 404)
        if request.headers.get("HX-Request") and action in ("archive", "unarchive"):
            # Wave 3b: the inbox quick action swaps its .ix-quick for a done-chip
            # (archive → "archived" + Undo) or restores the dismiss button (undo).
            from dashboard.inbox import acted_span, restored_note_button

            if action == "archive":
                return Response(
                    acted_span(
                        "✕ archived", "archived", undo_url=f"/api/notes/{note_id}/unarchive"
                    ),
                    mimetype="text/html",
                )
            return Response(restored_note_button(note_id), mimetype="text/html")
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
        # The Ask card's +Peers path passes summary=false (it shows the one
        # summary on its actions row); the DIY builder omits it and keeps the
        # self-describing caption band.
        include_summary = bool(body.get("summary", True))
        try:
            spec = ViewSpec.from_dict(raw_spec)
        except ViewSpecError as exc:
            return ({"error": str(exc)}, 400)
        result = execute_view(spec, db_path=db_path)
        return Response(
            render_view_fragment(
                result, include_chart=include_chart, include_summary=include_summary
            ),
            mimetype="text/html",
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
        "history": [{"role", "text"}, ...], "session_id": "..."}`` —
        ``session_id`` (optional) resumes a prior thread (S3 persistence);
        when omitted a new session is created and returned as
        ``{"session_id": "..."}`` in the response.  ``history`` is the
        legacy client-side tail, used only when no session_id is supplied.
        Always 200 with a tri-state payload augmented with ``session_id``."""
        if request.method == "OPTIONS":
            return ("", 204)
        turn, sess = _parse_ask_turn_with_session()
        if turn is None:
            return ({"error": "query required"}, 400)
        pack = build_portfolio_pack(repo_root, db_path)
        events = respond_turn(
            turn, pack, db_path=db_path, repo_root=repo_root, registry=job_registry
        )
        result = fold_events(events)
        result["session_id"] = sess.id if sess else turn.session_id
        return result

    @app.route("/api/ask/stream", methods=["POST", "OPTIONS"])
    def ask_stream_api():
        """Streaming sibling of /api/ask (Ask v2 / S3): same engine, same
        PORTFOLIO pack, raw SSE frames.  The first frame is always
        ``{type: "session", session_id: "..."}`` so the client can store
        the id and pass it back on the next turn."""
        if request.method == "OPTIONS":
            return ("", 204)
        turn, sess = _parse_ask_turn_with_session()
        if turn is None:
            return ({"error": "query required"}, 400)
        pack = build_portfolio_pack(repo_root, db_path)
        events = respond_turn(
            turn, pack, db_path=db_path, repo_root=repo_root, registry=job_registry
        )
        sid = sess.id if sess else (turn.session_id or "")
        return _stream_engine_events_with_session(events, sid)

    # ------------------------------------------------------------------
    # Positioning coach (the fit-v2 positioning surface)
    # ------------------------------------------------------------------

    @app.route("/api/positioning/coach", methods=["POST"])
    def positioning_coach():
        """One coach turn — the unified ask engine with the POSITIONING pack
        (socratic push-back + live book grounding, billed under
        ``positioning_coach_turn``). Sessions are scoped ``positioning`` so
        coach threads never mix into the Ask tab's list. Buffered JSON (the
        panel shows a thinking indicator); always carries ``session_id``."""
        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        query = str(body.get("query") or "").strip()
        if not query:
            return ({"error": "query required"}, 400)
        raw_sid = body.get("session_id")
        client_sid = str(raw_sid).strip() if isinstance(raw_sid, str) and raw_sid else None
        sess = ensure_session(client_sid, scope="positioning", db_path=db_path)
        from positioning.coach_pack import build_positioning_pack

        pack = build_positioning_pack(repo_root, db_path)
        events = respond_turn(
            AskTurn(text=query, session_id=sess.id),
            pack,
            db_path=db_path,
            repo_root=repo_root,
            registry=job_registry,
        )
        result = fold_events(events)
        result["session_id"] = sess.id
        return result

    @app.route("/api/positioning/propose", methods=["POST"])
    def positioning_propose():
        """Encode the coach conversation into an owner-editable approval form
        (HTML fragment). Encode failures are loud 400s with the reason —
        never a silently-empty proposal."""
        from llm.cli import is_hard_stop
        from pipeline.positioning_panel import render_approval_form
        from positioning.encode import EncodeError, propose_profile

        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        sid = str(body.get("session_id") or "").strip()
        if not sid:
            return ("session_id required", 400)
        try:
            proposal = propose_profile(db_path, repo_root, session_id=sid)
        except EncodeError as exc:
            return (str(exc), 400)
        except Exception as exc:  # structured-call failures: hard stops propagate loud
            if is_hard_stop(exc):
                raise
            return (f"encode failed: {exc}", 502)
        return Response(render_approval_form(proposal), mimetype="text/html")

    @app.route("/api/positioning/approve", methods=["POST"])
    def positioning_approve():
        """Persist a positioning intent FROM THE SUBMITTED FORM VALUES (the
        owner-wins seam: edits beat the LLM proposal). Returns the refreshed
        active-target card fragment; validation problems are owner-facing
        400s."""
        from pipeline.positioning_panel import (
            FormError,
            profile_from_form,
            render_active_target_card,
        )
        from positioning.store import append_intent

        form = {k: v for k, v in request.form.items()}
        try:
            profile, narrative = profile_from_form(form)
        except FormError as exc:
            return (str(exc), 400)
        session_id = (form.get("session_id") or "").strip() or None
        conn = sqlite3.connect(str(db_path))
        try:
            append_intent(
                conn,
                narrative=narrative,
                profile=profile,
                source="coach" if session_id else "manual",
                coach_session_id=session_id,
            )
            conn.commit()
        finally:
            conn.close()
        return Response(render_active_target_card(db_path, repo_root), mimetype="text/html")

    # ------------------------------------------------------------------
    # Ask session management (S3 thread list / rename / delete)
    # ------------------------------------------------------------------

    @app.route("/api/ask/sessions", methods=["GET"])
    def ask_sessions_list():
        """List portfolio Ask sessions, most-recently-updated first.
        ``?limit=N`` (default 50) caps the result.
        Returns ``{"sessions": [{id, title, created_at, updated_at}, …]}``."""
        try:
            limit = min(int(request.args.get("limit", 50)), 200)
        except (TypeError, ValueError):
            limit = 50
        try:
            rows = list_sessions(scope="portfolio", limit=limit, db_path=db_path)
        except Exception:
            rows = []
        return {"sessions": [_session_to_json(s) for s in rows]}

    @app.route("/api/ask/sessions/<session_id>", methods=["GET", "PATCH", "DELETE"])
    def ask_session_detail(session_id: str):
        """Single-session CRUD.

        GET  → ``{id, title, created_at, updated_at, turns: [{role, text, citations, created_at}]}``
        PATCH ``{"title": "…"}`` → rename, returns updated session JSON.
        DELETE → 204.
        """
        sid = session_id.strip()
        if not sid:
            return ({"error": "session_id required"}, 400)

        if request.method == "GET":
            sess = get_session(sid, db_path=db_path)
            if sess is None:
                return ({"error": "not found"}, 404)
            try:
                turns = load_turns(sid, db_path=db_path)
            except Exception:
                turns = []
            payload = _session_to_json(sess)
            payload["turns"] = [
                {
                    "role": t.role,
                    "text": t.text,
                    "citations": t.citations,
                    "model": t.model,
                    "created_at": t.created_at,
                }
                for t in turns
            ]
            return payload

        if request.method == "PATCH":
            body = cast("dict[str, object]", request.get_json(silent=True) or {})
            new_title = str(body.get("title") or "").strip()
            if not new_title:
                return ({"error": "title required"}, 400)
            ok = rename_session(sid, new_title, db_path=db_path)
            if not ok:
                return ({"error": "not found"}, 404)
            sess = get_session(sid, db_path=db_path)
            return _session_to_json(sess) if sess else ({"error": "not found"}, 404)

        # DELETE
        ok = delete_session(sid, db_path=db_path)
        if not ok:
            return ({"error": "not found"}, 404)
        return ("", 204)

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
        """Legacy helper — body → AskTurn without session management."""
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

    def _parse_ask_turn_with_session() -> tuple[AskTurn | None, _AskSession | None]:
        """Parse the request body and ensure a portfolio session exists.

        Returns ``(None, None)`` when the query is missing (the route 400s).
        When a ``session_id`` is supplied in the body, the existing session is
        loaded (or a new one is created if the id is unknown).  When no
        ``session_id`` is supplied, a new session is always created so the
        response can always carry one back.
        """
        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        query = str(body.get("query") or "").strip()
        if not query:
            return None, None
        raw_tickers = body.get("tickers")
        tickers = (
            [str(t) for t in cast("list[object]", raw_tickers)]
            if isinstance(raw_tickers, list)
            else []
        )
        raw_ctx = body.get("context_spec")
        context_spec = cast("dict[str, object]", raw_ctx) if isinstance(raw_ctx, dict) else None
        raw_sid = body.get("session_id")
        client_sid = str(raw_sid).strip() if isinstance(raw_sid, str) and raw_sid else None

        sess: _AskSession | None = None
        try:
            sess = ensure_session(client_sid, scope="portfolio", db_path=db_path)
            # Auto-title the session from the first question when it has no title.
            if sess and not sess.title:
                auto_title = query[:60]
                rename_session(sess.id, auto_title, db_path=db_path)
                sess = get_session(sess.id, db_path=db_path) or sess
        except Exception:
            sess = None  # best-effort — engine falls back to client history

        session_id = sess.id if sess else None
        turn = AskTurn(
            text=query,
            tickers=tickers,
            context_spec=context_spec,
            # history is the legacy fallback; engine uses server-side when session_id set
            history=sanitize_history(body.get("history")),
            session_id=session_id,
        )
        return turn, sess

    def _session_to_json(sess: _AskSession) -> dict[str, object]:
        return {
            "id": sess.id,
            "title": sess.title,
            "created_at": sess.created_at,
            "updated_at": sess.updated_at,
        }

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
        here — the queue can't hand-wave a name into built.

        On a dismiss the owner may attach a ``reason`` (why pass) and optional
        ``revisit_if`` text — when present, the dismiss becomes a first-class,
        gradeable ``avoid`` decision (L11) so a passed name that later triples
        leaves a trace. Absent a reason the dismiss is queue-state only, as
        before."""
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

        recorded: dict[str, object] | None = None
        if status == "dismissed":
            reason = _payload_text(payload.get("reason"))
            if reason is not None:
                recorded = _record_dismiss_pass(
                    ticker=row.ticker,
                    reason=reason,
                    revisit_text=_payload_text(payload.get("revisit_if")),
                    source_dismissal_id=cand_id,
                    db_path=db_path,
                )
        out: dict[str, object] = {"candidate": _candidate_to_json(row)}
        if recorded is not None:
            out["pass_decision"] = recorded
        return out

    @app.route("/api/decisions/pass", methods=["POST"])
    def record_pass_api():
        """Manual entry path for an error-of-omission (L11): record "I passed on
        TICKER because ... / I'd revisit if ..." as a first-class ``avoid``
        decision for ANY ticker, queue or not. ``reason`` is required; the
        optional ``revisit_if`` text is extracted into falsifiable numeric +
        qualitative conditions by the morning-pipeline attach rungs."""
        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        ticker = _payload_text(payload.get("ticker"))
        reason = _payload_text(payload.get("reason"))
        if not ticker:
            return ({"error": "ticker required"}, 400)
        if not reason:
            return ({"error": "reason required (the why behind the pass)"}, 400)
        recorded = _record_dismiss_pass(
            ticker=ticker.upper(),
            reason=reason,
            revisit_text=_payload_text(payload.get("revisit_if")),
            source_dismissal_id=None,
            db_path=db_path,
        )
        if recorded is None:
            return ({"error": "decisions ledger unavailable (run alembic upgrade)"}, 500)
        return {"pass_decision": recorded}

    @app.route("/api/decisions/<int:decision_id>/process-quality", methods=["POST", "OPTIONS"])
    def record_process_quality_api(decision_id: int):
        """Score a decision's PROCESS quality (Track B seam 8) — the axis
        distinct from its outcome, so 'right for the wrong reasons' can be
        aggregated on the scorecard. ``quality`` ∈ {sound, flawed, lucky}."""
        if request.method == "OPTIONS":
            return ("", 204)
        from decision_extractor import (
            PROCESS_QUALITY_VOCAB,
            ProcessQuality,
            record_process_quality,
        )

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        quality = _payload_text(payload.get("quality"))
        if quality not in PROCESS_QUALITY_VOCAB:
            return ({"error": f"quality must be one of {sorted(PROCESS_QUALITY_VOCAB)}"}, 400)
        ok = record_process_quality(
            decision_id=decision_id,
            process_quality=cast(ProcessQuality, quality),
            db_path=db_path,
        )
        if not ok:
            return ({"error": "decisions ledger unavailable (run alembic upgrade)"}, 500)
        return {"decision_id": decision_id, "process_quality": quality}

    @app.route("/api/discovery/sources", methods=["GET"])
    def discovery_sources_api():
        """The discovery_sources weight registry as JSON (the Discovery rule's
        editable lever). ``?signal_class=investor_13f`` filters one class."""
        from dataclasses import asdict

        from discovery.sources import list_sources

        signal_class = request.args.get("signal_class") or None
        try:
            rows = list_sources(signal_class=signal_class, db_path=db_path)
        except sqlite3.Error:
            rows = []
        return {"sources": [asdict(s) for s in rows]}

    @app.route("/api/discovery/sources/<source_key>/weight", methods=["POST", "OPTIONS"])
    def discovery_source_weight_api(source_key: str):
        """Edit a source's ``base_weight`` (the panel's weight-edit surface +
        quarterly recalibration). A non-negative float; re-ranks the queue on
        the next refresh. 404 when the source_key is unknown."""
        if request.method == "OPTIONS":
            return ("", 204)
        from dataclasses import asdict

        from discovery.sources import set_source_weight

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        raw = payload.get("weight")
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            return ({"error": "weight (number) required"}, 400)
        try:
            row = set_source_weight(source_key, float(raw), db_path=db_path)
        except sqlite3.Error:
            return ({"error": "discovery_sources table missing (run alembic upgrade)"}, 500)
        if row is None:
            return ({"error": f"source {source_key!r} not found"}, 404)
        return {"source": asdict(row)}

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

    @app.route("/api/coach/unmute", methods=["POST", "OPTIONS"])
    def coach_unmute_api():
        """Clear a coach_mutes row (REQ-12: mutes must be visible AND
        reversible) — the first production caller of
        ``research.governor.unmute``. JSON body: {"class_": "falsifier_breach"}.
        CSRF-guarded by the global Origin check (csrf_origin_guard)."""
        if request.method == "OPTIONS":
            return ("", 204)
        from research.governor import unmute

        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        class_ = str(body.get("class_") or "").strip()
        if not class_:
            return ({"error": "class_ required"}, 400)
        unmuted = unmute(class_, db_path=db_path)
        return {"class_": class_, "ok": True, "unmuted": unmuted}

    @app.route("/api/coach/attest-change", methods=["POST", "OPTIONS"])
    def coach_attest_change_api():
        """Record the owner's explicit "this review changed my call" attestation
        on a guard_override position_review memo — the SOLE input that moves the
        Coach P&L's Q3'26 "changed >= 1" bar (the silence-implies-heeded window
        heuristic feeds only the separate "candidate" line, never the target).
        JSON body: {"memo_id": int}. CSRF-guarded by the global Origin check.
        ``attested`` is False when nothing matched or it was already recorded —
        the counter it feeds must never be inflated by a no-op click."""
        if request.method == "OPTIONS":
            return ("", 204)
        from advisor.position_review import attest_review_changed

        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        try:
            memo_id = int(cast("int", body.get("memo_id")))
        except (TypeError, ValueError):
            return ({"error": "memo_id (int) required"}, 400)
        attested = attest_review_changed(db_path, memo_id)
        return {"memo_id": memo_id, "ok": True, "attested": attested}

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

    @app.route("/api/peek/review/<ticker>", methods=["GET"])
    def peek_review(ticker: str):
        """The instant, LLM-free position-review read (PR5) — the click-through
        behind the Holding band's "Review" link and the portfolio cockpit's
        review pill: grounded facts, mechanical read, tax block, the live
        graded-sells base rate, and a footer button that escalates to the full
        LLM-calibrated, memo-persisting review. Always 200 — build_pre_analysis
        degrades tracker-offline / no-thesis on its own."""
        from pipeline.peeks import render_review_peek

        return Response(render_review_peek(repo_root, db_path, ticker), mimetype="text/html")

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

    @app.route("/api/peek/documents", methods=["GET"])
    def peek_documents():
        """The documents fetched for ``?ticker=`` since its last report build —
        the click-through behind the cockpit's "N new docs" pill. Each row links
        by its id to the ``/source/<id>`` viewer. Always 200 — a missing ticker
        or empty window renders the peek's empty state."""
        from pipeline.peeks import render_new_docs_peek

        return Response(
            render_new_docs_peek(db_path, ticker=request.args.get("ticker") or ""),
            mimetype="text/html",
        )

    @app.route("/api/peek/score", methods=["GET"])
    def peek_score():
        """The next-dollar attractiveness breakdown for an evaluation name — the
        click-through behind the cockpit's Score chip. Factor-by-factor (DCF
        upside · Rev growth · FCF margin · PEG) with each band multiplier and
        the input it scored, recomputed from the same readers the cockpit row
        uses. 404 for an untracked ticker."""
        from pipeline.peeks import render_score_peek

        conn = _open_db()
        try:
            html = render_score_peek(conn, repo_root, request.args.get("ticker") or "")
        finally:
            conn.close()
        if html is None:
            abort(404)
        return Response(html, mimetype="text/html")

    @app.route("/api/peek/fit", methods=["GET"])
    def peek_fit():
        """The portfolio-fit breakdown for an evaluation name — the click-through
        behind the cockpit's Fit chip. Factor-by-factor (marginal Sharpe ·
        diversification · factor exposure · sector) read from the materialized
        candidate_fit.json. 404 when the ticker has no cached fit."""
        from pipeline.peeks import render_fit_peek

        html = render_fit_peek(repo_root, request.args.get("ticker") or "")
        if html is None:
            abort(404)
        return Response(html, mimetype="text/html")

    @app.route("/api/peek/whatif", methods=["GET"])
    def peek_whatif():
        """The before/after what-if for one name at a chosen weight — the
        click-through behind the cockpit's ΔSR chip and the fit peek's doorway.
        ``?w=`` snaps to the allowed weight menu (default 3%); the compute is
        user-initiated and module-cached (allocation/what_if.py), never a
        table-render cost. 404 for an untracked ticker or an empty weights
        cache."""
        from pipeline.peeks import render_what_if_peek

        try:
            ticker = ticker_validation.safe_ticker(request.args.get("ticker") or "")
        except ValueError:
            abort(404)
        try:
            w = float(request.args.get("w") or 0.03)
        except (TypeError, ValueError):
            w = 0.03
        html = render_what_if_peek(repo_root, ticker, w)
        if html is None:
            abort(404)
        return Response(html, mimetype="text/html")

    @app.route("/api/peek/etf_workup", methods=["GET"])
    def peek_etf_workup():
        """The ETF workup — profile strip, style loadings, look-through
        overlap + country exposure, precomputed what-if rows, and the governed
        role-in-portfolio one-pager. All disk/DB reads (the LLM one-pager is a
        cached artifact); 404 for a non-ETF ticker."""
        from pipeline.etf_workup import render_etf_workup

        try:
            ticker = ticker_validation.safe_ticker(request.args.get("ticker") or "")
        except ValueError:
            abort(404)
        conn = _open_db()
        try:
            html = render_etf_workup(conn, repo_root, db_path, ticker)
        finally:
            conn.close()
        if html is None:
            abort(404)
        return Response(html, mimetype="text/html")

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
        try:
            t = ticker_validation.safe_ticker(ticker)
        except ValueError:
            abort(400)
        return redirect(f"/#holding={t}")

    @app.route("/reports/<ticker>", methods=["GET"])
    def latest_report_for_ticker(ticker: str):
        """Serve the most recently built workspace HTML for the ticker.

        Uses the latest filename (`<DATE>_workspace.html`) since YYYY-MM-DD
        sorts chronologically. Returns 404 if no build exists.
        """
        try:
            t = ticker_validation.safe_ticker(ticker)
        except ValueError:
            abort(400)
        research_dir = repo_root / "output" / "research" / t
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
        try:
            t = ticker_validation.safe_ticker(ticker)
        except ValueError:
            abort(400)
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

    @app.route("/api/dcf/inputs/<ticker>", methods=["GET"])
    def dcf_inputs(ticker: str):  # pyright: ignore[reportUnusedFunction]  # registered via decorator
        """The current redesigned-DCF assumption set for a ticker as a JSON dict
        — what the in-app recompute card edits and POSTs back.

        Read once from the live workbook ``dcf/<T>.xlsx`` to seed the editable
        controls; the recompute LOOP itself never touches the xlsx. 404 when
        there is no redesigned workbook for the ticker, 422 when the workbook is
        present but structurally unreadable."""
        t = ticker.upper()
        live = repo_root / "dcf" / f"{t}.xlsx"
        if not live.exists():
            abort(404)
        try:
            inp = dcf_redesign.read_inputs(live)
        except dcf_redesign.RedesignError as e:
            return ({"error": str(e)}, 422)
        if inp is None:
            abort(404)  # present but not redesigned-format
        return {"ticker": t, "inputs": inp.to_dict()}

    @app.route("/api/dcf/recompute", methods=["POST", "OPTIONS"])
    def dcf_recompute():  # pyright: ignore[reportUnusedFunction]  # registered via decorator
        """Recompute a DCF from edited assumptions — NO xlsx in the loop.

        Body: ``{"inputs": {<RedesignInputs.to_dict() with edits applied>}}``.
        Runs the existing pure ``value()/scenario_values()/sensitivity_grid()``
        and returns base fair value + Bull/Base/Bear + the WACC × exit-multiple
        grid + the live over/under (decimal). Stateless: the in-app card calls it
        on every assumption edit; persistence is a separate explicit save /
        Push-to-Sheets commit, never this route. 400 on a malformed/invalid
        input set, 422 on a degenerate but well-formed one (perpetuity WACC ≤ g)."""
        if request.method == "OPTIONS":
            return ("", 204)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return ({"error": "JSON body required"}, 400)
        raw = cast("dict[str, object]", body).get("inputs")
        if not isinstance(raw, dict):
            return ({"error": "body.inputs (a DCF assumption object) required"}, 400)
        try:
            inp = dcf_redesign.RedesignInputs.from_dict(cast("dict[str, object]", raw))
        except dcf_redesign.RedesignError as e:
            return ({"error": f"invalid inputs: {e}"}, 400)
        try:
            return _dcf_recompute_payload(inp)
        except dcf_redesign.RedesignError as e:
            return ({"error": str(e)}, 422)

    @app.route("/api/dcf/save", methods=["POST", "OPTIONS"])
    def dcf_save():  # pyright: ignore[reportUnusedFunction]  # registered via decorator
        """Durably save edited DCF assumptions to the model — the explicit in-app
        commit (Push-to-Sheets stays the publish-to-Google-Sheet commit).

        Body: ``{"ticker": T, "inputs": {<edited RedesignInputs.to_dict()>}}``.
        Writes the edited cell-backed levers onto ``dcf/<T>.xlsx``, reconciles the
        S11 override ledger against the IMMUTABLE Opus baseline (the edit is
        recorded; the baseline is never overwritten), mirrors the edits to the
        from-scratch default, and re-persists ``dcf_runs``. Returns the recomputed
        card payload (canonical saved inputs — WACC re-derived from the saved CAPM
        drivers) plus the save outcome. 400 on a malformed input set, 409 when
        there is no redesigned workbook to edit, 422 on a degenerate one."""
        if request.method == "OPTIONS":
            return ("", 204)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return ({"error": "JSON body required"}, 400)
        data = cast("dict[str, object]", body)
        ticker = data.get("ticker")
        if not isinstance(ticker, str) or not ticker.strip():
            return ({"error": "body.ticker required"}, 400)
        raw = data.get("inputs")
        if not isinstance(raw, dict):
            return ({"error": "body.inputs (a DCF assumption object) required"}, 400)
        try:
            inp = dcf_redesign.RedesignInputs.from_dict(cast("dict[str, object]", raw))
        except dcf_redesign.RedesignError as e:
            return ({"error": f"invalid inputs: {e}"}, 400)
        # Reject a degenerate set up front (same 422 contract as recompute) so a
        # save never writes an un-valuable model to the workbook.
        try:
            _dcf_recompute_payload(inp)
        except dcf_redesign.RedesignError as e:
            return ({"error": str(e)}, 422)

        import refresh_dcf  # heavy CLI module — imported only on the save path

        try:
            t = ticker_validation.safe_ticker(ticker)
        except ValueError:
            return ({"error": "invalid ticker"}, 400)
        result = refresh_dcf.apply_edits(t, repo_root, db_path, inp)
        if result.get("status") != "ok":
            reason = str(result.get("reason", "save failed"))
            code = 409 if "no redesigned workbook" in reason else 500
            return ({"error": reason, "result": result}, code)
        # Reflect exactly what was persisted: recompute from the canonical saved
        # inputs (re-read from the workbook, WACC re-derived from saved drivers).
        saved_inp = dcf_redesign.read_inputs(repo_root / "dcf" / f"{t}.xlsx")
        payload = _dcf_recompute_payload(saved_inp) if saved_inp is not None else {}
        if saved_inp is not None:
            payload["inputs"] = saved_inp.to_dict()
        return {**payload, "saved": True, "result": result}

    @app.route("/api/dcf/inject-fact", methods=["POST", "OPTIONS"])
    def dcf_inject_fact():  # pyright: ignore[reportUnusedFunction]  # registered via decorator
        """Inject a picked DIY fact as a DCF driver (capture-every-number S6).

        Body: ``{"ticker": T, "token": "<metric token>", "field": "<driver key>"}``.
        Resolves the metric's LATEST value through the timeseries loaders (so
        company-doc ``fact_overrides`` win — S2), converts it into the target
        driver's units (the load-bearing units/scale step — percent→ratio, $→$M),
        sanity-bounds it, then commits via ``refresh_dcf.apply_edits`` (the
        clobber-safe cell + JSON-sync + provenance + dcf_runs path). Returns the
        recomputed card payload plus the resolved-fact/conversion detail.

        400 on a malformed body / unknown field / unparseable token; 404 when the
        ticker has no FCFF redesign workbook (archetype models are not editable
        this way); 422 on a fact that cannot be safely scaled, an out-of-bounds
        converted value, or a degenerate resulting model."""
        if request.method == "OPTIONS":
            return ("", 204)
        from dcf import fact_drivers
        from viewspec.spec import MetricRef, ViewSpecError

        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return ({"error": "JSON body required"}, 400)
        data = cast("dict[str, object]", body)
        ticker_raw = data.get("ticker")
        token = data.get("token")
        field_key = data.get("field")
        if not isinstance(ticker_raw, str) or not ticker_raw.strip():
            return ({"error": "body.ticker required"}, 400)
        if not isinstance(token, str) or not token.strip():
            return ({"error": "body.token (a picked metric token) required"}, 400)
        if not isinstance(field_key, str) or not field_key.strip():
            return ({"error": "body.field (a driver field key) required"}, 400)
        field = fact_drivers.DRIVER_FIELDS_BY_KEY.get(field_key)
        if field is None:
            return ({"error": f"unknown driver field {field_key!r}"}, 400)
        try:
            metric = MetricRef.parse_token(token)
        except ViewSpecError as exc:
            return ({"error": str(exc)}, 400)

        try:
            t = ticker_validation.safe_ticker(ticker_raw)
        except ValueError:
            return ({"error": "invalid ticker"}, 400)
        # FCFF-only guard: archetype models (bank/holdco/fintech/platform) and
        # un-built names have no redesigned workbook to seed from.
        live = repo_root / "dcf" / f"{t}.xlsx"
        try:
            base_inp = dcf_redesign.read_inputs(live) if live.exists() else None
        except dcf_redesign.RedesignError as exc:
            return ({"error": str(exc)}, 422)
        if base_inp is None:
            return ({"error": f"{t} has no editable FCFF DCF model"}, 404)

        # Resolve → convert → apply. A FactDriverError is a 422 (well-formed
        # request, but the fact can't be safely turned into this driver).
        try:
            resolved = fact_drivers.resolve_fact_value(
                metric, ticker=t, repo_root=repo_root, db_path=db_path
            )
            converted = fact_drivers.convert_to_driver(resolved.value, resolved.unit, field)
            edited = fact_drivers.apply_to_inputs(base_inp, field, converted.value)
        except fact_drivers.FactDriverError as exc:
            return ({"error": str(exc)}, 422)
        # Reject a degenerate resulting model up front (same 422 contract as save).
        try:
            _dcf_recompute_payload(edited)
        except dcf_redesign.RedesignError as exc:
            return ({"error": str(exc)}, 422)

        import refresh_dcf  # heavy CLI module — imported only on the commit path

        result = refresh_dcf.apply_edits(t, repo_root, db_path, edited)
        if result.get("status") != "ok":
            reason = str(result.get("reason", "injection failed"))
            code = 404 if "no redesigned workbook" in reason else 500
            return ({"error": reason, "result": result}, code)

        injection: dict[str, object] = {
            "ticker": t,
            "field_key": field.key,
            "field_label": field.label,
            "metric_token": token,
            "metric_label": metric.label,
            "raw_value": resolved.value,
            "raw_unit": resolved.unit,
            "applied_value": converted.value,
            "conversion": converted.note,
            "source": resolved.source,
            "fact_id": resolved.fact_id,
            "period_end": resolved.period_end,
        }
        # Durable fact lineage on the assumptions JSON (best-effort, additive).
        with contextlib.suppress(Exception):
            fact_drivers.record_driver_provenance(
                repo_root / "data" / "dcf_assumptions" / f"{t}.json",
                field_key=field.key,
                payload={
                    "metric": token,
                    "fact_id": resolved.fact_id,
                    "raw_value": resolved.value,
                    "raw_unit": resolved.unit,
                    "applied_value": converted.value,
                    "source": resolved.source,
                    "period_end": resolved.period_end,
                },
            )

        saved_inp = dcf_redesign.read_inputs(live)
        payload = _dcf_recompute_payload(saved_inp) if saved_inp is not None else {}
        if saved_inp is not None:
            payload["inputs"] = saved_inp.to_dict()
        return {**payload, "injected": True, "injection": injection, "result": result}

    @app.route("/api/dcf/inject-fact-sheet", methods=["POST", "OPTIONS"])
    def dcf_inject_fact_sheet():  # pyright: ignore[reportUnusedFunction]  # registered via decorator
        """Park a picked DIY fact on a ticker's DCF *reference sheet* (S7).

        Body: ``{"ticker": T, "token": "<metric token>"}`` (no driver field — a
        reference fact is not wired into the recompute). Resolves the metric's
        LATEST value through the SAME loaders the driver path uses (so company-doc
        ``fact_overrides`` win — S2), then writes it — value + unit + period +
        source/provenance, in its native unit (NOT converted) — into the companion
        workbook ``dcf/facts/<T>.xlsx`` that the refresh NEVER rebuilds, so it
        survives every model refresh (the S7 deliverable; see
        ``dcf.fact_sheet`` for the why).

        400 on a malformed body / unparseable token; 404 when the ticker has no
        DCF model workbook to attach a reference to; 422 when the fact cannot be
        resolved (no observations)."""
        if request.method == "OPTIONS":
            return ("", 204)
        from datetime import date as _date

        from dcf import fact_drivers, fact_sheet
        from viewspec.spec import MetricRef, ViewSpecError

        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return ({"error": "JSON body required"}, 400)
        data = cast("dict[str, object]", body)
        ticker_raw = data.get("ticker")
        token = data.get("token")
        if not isinstance(ticker_raw, str) or not ticker_raw.strip():
            return ({"error": "body.ticker required"}, 400)
        if not isinstance(token, str) or not token.strip():
            return ({"error": "body.token (a picked metric token) required"}, 400)
        try:
            metric = MetricRef.parse_token(token)
        except ViewSpecError as exc:
            return ({"error": str(exc)}, 400)

        try:
            t = ticker_validation.safe_ticker(ticker_raw)
        except ValueError:
            return ({"error": "invalid ticker"}, 400)
        # A reference attaches to a ticker's DCF — any model archetype qualifies
        # (FCFF/bank/holdco/...), since the companion file is model-agnostic. A
        # never-built name has nothing to reference.
        if not (repo_root / "dcf" / f"{t}.xlsx").exists():
            return ({"error": f"{t} has no DCF model to attach a reference to"}, 404)

        try:
            resolved = fact_drivers.resolve_fact_value(
                metric, ticker=t, repo_root=repo_root, db_path=db_path
            )
        except fact_drivers.FactDriverError as exc:
            return ({"error": str(exc)}, 422)

        fact = fact_sheet.ReferenceFact(
            token=token,
            label=metric.label,
            value=resolved.value,
            unit=resolved.unit,
            period_end=resolved.period_end,
            source=resolved.source,
            fact_id=resolved.fact_id,
            captured_on=_date.today().isoformat(),
        )
        path = fact_sheet.facts_workbook_path(repo_root, t)
        outcome = fact_sheet.upsert_fact(path, fact)
        return {
            "ticker": t,
            "added": True,
            "action": outcome["action"],
            "count": outcome["count"],
            "workbook": str(path),
            "fact": {
                "token": fact.token,
                "label": fact.label,
                "value": fact.value,
                "unit": fact.unit,
                "period_end": fact.period_end,
                "source": fact.source,
                "fact_id": fact.fact_id,
                "captured_on": fact.captured_on,
            },
        }

    @app.route("/api/dcf/reference-facts/<ticker>", methods=["GET"])
    def dcf_reference_facts(ticker: str):  # pyright: ignore[reportUnusedFunction]  # registered via decorator
        """Every reference fact parked on a ticker's DCF (the companion
        ``dcf/facts/<T>.xlsx``), as ``{"ticker", "facts": [...]}``. Empty list
        when none have been injected."""
        from dcf import fact_sheet

        t = ticker.upper()
        facts = fact_sheet.read_facts(fact_sheet.facts_workbook_path(repo_root, t))
        return {
            "ticker": t,
            "facts": [
                {
                    "token": f.token,
                    "label": f.label,
                    "value": f.value,
                    "unit": f.unit,
                    "period_end": f.period_end,
                    "source": f.source,
                    "fact_id": f.fact_id,
                    "captured_on": f.captured_on,
                }
                for f in facts
            ],
        }

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
        argv = [
            sys.executable,
            str(script),
            "--scenario",
            scenario,
            "--portfolio",
            "--repo-root",
            str(repo_root),
        ]
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
        body = request.get_json(silent=True) or {}
        ticker = str(body.get("ticker", "")).upper()
        if not ticker:
            return ({"error": "ticker required"}, 400)
        try:
            ticker = ticker_validation.safe_ticker(ticker)
        except ValueError:
            return ({"error": "invalid ticker"}, 400)
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
        try:
            ticker = ticker_validation.safe_ticker(ticker)
        except ValueError:
            return ({"error": "invalid ticker"}, 400)
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
        try:
            ticker = ticker_validation.safe_ticker(ticker)
        except ValueError:
            return ({"error": "invalid ticker"}, 400)
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
        argv = [
            sys.executable,
            str(repo_root / "execution" / "refresh_dcf.py"),
            "--all-named",
            "--repo-root",
            str(repo_root),
        ]
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
        body = request.get_json(silent=True) or {}
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
        issue_id = _opt_int(payload.get("issue_id"))
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
        action = cast("rt_response.Action", action_raw)
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
        argv = [
            sys.executable,
            str(repo_root / "execution" / "review_position.py"),
            ticker,
            "--verdict",
            "--db",
            str(db_path),
            # An in-app owner click — tag it so it counts in the Coach P&L (the
            # CLI defaults to 'agent', which would exclude it).
            "--source",
            "doorway",
        ]
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
        argv = [
            sys.executable,
            str(repo_root / "execution" / "run_llm_evals.py"),
            "--purpose",
            purpose,
            "--repo-root",
            str(repo_root),
        ]
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
        try:
            ticker = ticker_validation.safe_ticker(ticker)
        except ValueError:
            return ({"error": "invalid ticker"}, 400)
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
        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        try:
            diff = cast("dict[str, object]", body["diff"])
            report_date = _parse_date(str(body["report_date"]))
            dry_run = bool(body.get("dry_run", False))
        except (KeyError, TypeError, ValueError):
            return ({"error": "diff and report_date required"}, 400)
        return apply_chat_diff(repo_root, ticker, report_date, diff, dry_run=dry_run)

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
    configure_logging()  # structured root logging + correlation ids (sre-4)
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
