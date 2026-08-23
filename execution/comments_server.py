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
  GET/POST /chat/<ticker>     retired report-chat compatibility tombstone
  POST   /chat/<ticker>/apply retired proposal compatibility tombstone
  POST   /api/ask/stream      the single research-conversation path (SSE
                              session/stage/delta/fragment/final frames)
  GET    /healthz             health check

Usage:
    python execution/sqlite_bootstrap.py execution/comments_server.py
    python execution/sqlite_bootstrap.py execution/comments_server.py --port 7421 --repo-root /path/to/repo

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
import hashlib
import json
import logging
import math
import os
import queue
import re
import sys
import threading
import time
import urllib.parse
from collections import deque
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from types import TracebackType
from typing import cast

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(SCRIPT_DIR))  # import sibling execution/ modules (refresh_dispatch)

try:
    from flask import Flask, Response, abort, g, redirect, request, send_file, stream_with_context
except ImportError:  # pragma: no cover - install hint
    print(
        "Flask not installed. Install with: pip install flask",
        file=sys.stderr,
    )
    sys.exit(1)

import sqlite3  # noqa: E402

from comments_server_alert_routes import AppContext, register_alert_routes  # noqa: E402
from comments_server_content_routes import (  # noqa: E402
    ContentRouteContext,
    register_content_routes,
)
from comments_server_dcf_routes import DcfRouteContext, register_dcf_routes  # noqa: E402
from comments_server_ir_approval_routes import (  # noqa: E402
    IrApprovalRouteContext,
    register_ir_approval_routes,
)
from comments_server_journal_routes import (  # noqa: E402
    JournalRouteContext,
    register_journal_routes,
)
from comments_server_panel_cache import (  # noqa: E402
    PanelCacheEntry,
    PanelCacheHit,
    PanelCacheReservation,
    PanelResponseCache,
)
from comments_server_settings_routes import (  # noqa: E402
    SettingsRouteContext,
    register_settings_routes,
)
from process_report_comments import (  # noqa: E402
    _resolve_latest_report_date,
    preview_thesis_edits,
    process_comments_for_ticker,
)
from pydantic import ValidationError  # noqa: E402
from refresh_dispatch import STEP_NAMES  # noqa: E402
from update_readme import (  # noqa: E402
    collect_repository_evidence,
    current_candidate_violations,
)

import comments  # noqa: E402
import ticker_validation  # noqa: E402
from ask.context import build_portfolio_pack  # noqa: E402
from ask.engine import (  # noqa: E402
    AskTurn,
    ask_retrieval_mode,
    fold_events,
    respond_turn,
    sanitize_history,
)
from ask.exchange_store import (  # noqa: E402
    BeginExchangeResult,
    ExchangeConflictError,
    ExchangeStateError,
    PendingExchangeError,
    ResearchContextV1,
    RevisionConflictError,
    SessionContextConflictError,
    SessionContextV1,
    begin_exchange,
    get_session_context,
    hash_request_payload,
    list_session_exchange_artifacts,
    orchestrate_exchange_events,
    put_session_context,
    replay_exchange_events,
)
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
from dashboard.inbox import (  # noqa: E402
    collect_inbox,
    render_inbox_stream,
    schema_drift_notice,
)
from dashboard.upcoming import render_upcoming_strip  # noqa: E402
from dcf import persist as dcf_persist  # noqa: E402
from dcf import redesign as dcf_redesign  # noqa: E402
from discovery.store import BUILDABLE_STATUSES  # noqa: E402
from dispatch_registry import Job, Registry, RegistryConflict  # noqa: E402
from identity import DEFAULT_USER_ID  # noqa: E402
from integrations.portfolio_allocation import fetch_portfolio_allocation  # noqa: E402
from integrations.portfolio_tracker_client import fetch_live_portfolio  # noqa: E402
from integrations.portfolio_tracker_v1 import TrackerV1Client  # noqa: E402
from llm.cli import LLMBudgetExceeded, is_hard_stop  # noqa: E402
from log_redact import redact  # noqa: E402
from logging_config import (  # noqa: E402
    configure_logging,
    get_correlation_id,
    new_correlation_id,
    set_correlation_id,
)
from operations.models import OperationsRegistry  # noqa: E402
from operations.paths import (  # noqa: E402
    portfolio_tracker_receipt_path,
    scheduler_receipt_path,
    service_receipt_path,
)
from operations.readme_governance import (  # noqa: E402
    ReadmeGovernanceStatus,
    collect_readme_governance_status,
)
from operations.registry import build_operations_registry  # noqa: E402
from operations.snapshot import collect_operations_snapshot  # noqa: E402
from pipeline.analytical_dashboard import build_analytical_dashboard  # noqa: E402
from pipeline.dashboard_status import build_dashboard_rows  # noqa: E402
from pipeline.operations_panel import (  # noqa: E402
    build_operations_panel_view,
    render_operations_panel,
)
from pipeline.research_cockpit import build_cockpit_rows  # noqa: E402
from pipeline.ticker_command_center import (  # noqa: E402
    build_ticker_command_center,
    render_holding_fragment,
    render_holding_picker_band,
    render_notes_drawer_fragment,
)
from pipeline.tier_runner import tier_coverage_summary  # noqa: E402
from pipeline.work_os_earnings import load_latest_earnings_readouts  # noqa: E402
from pipeline.work_os_overview import render_overview_panel  # noqa: E402
from pipeline.work_os_portfolio import build_work_os_portfolio  # noqa: E402
from pipeline.work_os_shell import render_work_os_shell  # noqa: E402
from readme_updater import evidence_sha256  # noqa: E402
from research.proposal_approval import (  # noqa: E402
    AskProposalDecisionV1,
    ProposalConflictError,
    StoredProposalError,
    TargetDriftError,
    bind_ask_proposal_events,
    decide_ask_proposal,
    get_ask_proposal_detail,
)
from run_lock import RunLockHeldError  # noqa: E402
from runtime.job_runtime import portfolio_db_path  # noqa: E402
from runtime.portfolio_tracker import (  # noqa: E402
    AtomicFileLease,
    ListenerObservation,
    PortfolioTrackerRuntimeManager,
    RuntimeConfig,
    derive_daily_refresh_idempotency_key,
    endpoint_owner_matches_pid,
    health_is_healthy,
    is_loopback_bind_host,
    parse_tracker_bind_url,
    write_runtime_receipt,
)
from runtime.python_process import managed_python_argv  # noqa: E402
from runtime.secrets import load_project_env, secret_read_path  # noqa: E402
from schema_compat import SchemaRevisionMismatch  # noqa: E402
from server_runtime.access import (  # noqa: E402
    REPORT_CAPABILITY_HEADER,
    ReportCapabilityStore,
    is_allowed_client_address,
    is_allowed_origin,
    private_mobile_origin,
    resolve_tailscale_ipv4,
    tailscale_access_enabled,
    validate_bind_host,
)
from server_runtime.streaming import drain_events  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

# Repo-wide maintenance chores exposed on the dashboard, each dispatched as a
# single-flight job running an existing CLI under execution/. (Onboarding a
# specific ticker is handled separately — it needs a ticker argument.)
_MAINTENANCE_ACTIONS: dict[str, list[str]] = {
    "seed_kpis": ["seed_kpi_definitions.py", "--all"],
    "process_inbox": ["register_dropped_documents.py", "--all"],
    "sweep_history": ["sweep_output_history.py"],
    "onboard_pending": ["onboard_pending_tickers.py"],
}

_MAX_REQUEST_BYTES = 262_144
_MAX_USER_INPUT_CHARS = 8_000
_STREAM_QUEUE_MAXSIZE = 64
_CORRELATION_ID_RX = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_BROWSER_USER_AGENT_RX = re.compile(r"(?:mozilla|chrome|chromium|safari|firefox|edg)/", re.I)
_README_RUN_ID_RX = re.compile(r"[0-9a-f]{32}\Z")
_LOGGER = logging.getLogger(__name__)


def _drain_durable_events(
    events: Iterator[dict[str, object]],
    chunks: queue.Queue[dict[str, object] | None],
    stop: threading.Event,
) -> None:
    """Consume persistence-owning streams to completion after a disconnect."""

    def put_if_connected(item: dict[str, object] | None) -> None:
        while not stop.is_set():
            try:
                chunks.put(item, timeout=0.1)
                return
            except queue.Full:
                continue

    try:
        for item in events:
            if stop.is_set():
                continue
            put_if_connected(item)
    except Exception as exc:
        _LOGGER.error("durable Ask stream failed: %s", redact(exc))
        put_if_connected({"type": "error", "error": "ask failed; retry the request"})
    finally:
        put_if_connected(None)


def _cors_allow_origin(origin: str, *, repo_root: Path = PROJECT_ROOT) -> str | None:
    """Return the ``Access-Control-Allow-Origin`` value to echo for ``origin``, or None.

    Allows the file:// workspace renderer (Origin ``"null"``) and any loopback
    origin so the local dashboard keeps working; a cross-site origin gets no
    CORS header, so the browser blocks its preflighted state-changing request
    (CSRF defense). For a non-loopback bind, an explicit comma-separated
    ``COMMENTS_SERVER_CORS_WHITELIST`` of allowed origins is honored.
    """
    whitelist = {
        o.strip()
        for o in os.environ.get("COMMENTS_SERVER_CORS_WHITELIST", "").split(",")
        if o.strip()
    }
    configured_private_origin = private_mobile_origin(
        config_path=secret_read_path("private_mobile_base_url", repo_root=repo_root)
    )
    if configured_private_origin:
        whitelist.add(configured_private_origin)
    return is_allowed_origin(
        origin,
        allow_tailscale=tailscale_access_enabled(),
        whitelist=frozenset(whitelist),
    )


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


def _is_browser_user_agent(user_agent: str) -> bool:
    """Identify browser requests only for the no-fetch-metadata CSRF fallback.

    Browsers normally send ``Origin`` and/or ``Sec-Fetch-Site`` on mutations.
    Privacy-hardened clients can omit both, so a browser-shaped request with no
    trustworthy metadata must present the existing report capability. Local
    non-browser callers (curl, the Python CLI, Flask's test client) remain
    usable without turning a browser's ambient loopback access into a bypass.
    """

    return _BROWSER_USER_AGENT_RX.search(user_agent) is not None


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
    tab into ``decisions_record`` — the Work OS surface owns the doorway
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


def _canonical_session_context(repo_root: Path, context: SessionContextV1) -> SessionContextV1:
    """Bind company context to the exact canonical thesis bytes, when present."""

    ticker = context.company_ticker
    canonical_ref: str | None = None
    canonical_version: str | None = None
    if ticker is not None:
        relative = Path("micro_thesis") / "holdings" / f"{ticker}.json"
        thesis_path = repo_root / relative
        if thesis_path.is_file():
            canonical_ref = relative.as_posix()
            canonical_version = hashlib.sha256(thesis_path.read_bytes()).hexdigest()
    if context.thesis_ref not in {None, canonical_ref}:
        raise ValueError("session_context thesis_ref conflicts with the canonical thesis")
    if context.thesis_version not in {None, canonical_version}:
        raise ValueError("session_context thesis_version conflicts with the canonical thesis")
    return context.model_copy(
        update={
            "thesis_ref": canonical_ref,
            "thesis_version": canonical_version,
        }
    )


def _parse_bbox_param(raw: str | None) -> tuple[float, float, float, float] | None:
    """``?bbox=x0,y0,x1,y1`` (PDF page coords) → tuple, or None on any
    malformed input — the highlight is enrichment, never a 400."""
    if not raw:
        return None
    parts = raw.split(",")
    if len(parts) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(p) for p in parts)
    except ValueError:
        return None
    return (x0, y0, x1, y1)


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


class _RedactingFlask(Flask):
    """Flask boundary that never writes an exception traceback to logs."""

    def log_exception(
        self,
        exc_info: tuple[type, BaseException, TracebackType] | tuple[None, None, None],
    ) -> None:
        exc = exc_info[1]
        if exc is None:
            return
        self.logger.error(
            "unhandled request exception: %s",
            redact(f"{type(exc).__name__}: {exc}")[:500],
        )


def create_app(
    repo_root: Path,
    *,
    db_path: Path | None = None,
    registry: Registry | None = None,
    operations_registry: OperationsRegistry | None = None,
    code_root: Path | None = None,
    chat_executor: concurrent.futures.Executor | None = None,
) -> Flask:
    app = _RedactingFlask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = _MAX_REQUEST_BYTES
    resolved_db_path = (db_path or repo_root / "data" / "portfolio.db").resolve()
    db_path = resolved_db_path
    job_registry = registry or Registry(repo_root=repo_root)
    report_capability = ReportCapabilityStore(repo_root)
    report_capability.load_or_create()
    app.config["DISPATCH_REGISTRY"] = job_registry
    # Panel fragments are expensive database/calculation reads but are treated
    # as fresh by the shell for 30 seconds. Keep the exact rendered response for
    # that same window so HTTP revalidation can stop *before* the route builder,
    # rather than paying the full build merely to discover the ETag is unchanged.
    panel_cache = PanelResponseCache(ttl_seconds=30.0, max_entries=256)
    resolved_code_root = (code_root or PROJECT_ROOT).resolve()
    declared_operations = operations_registry or build_operations_registry(resolved_code_root)
    app.config["CODE_ROOT"] = resolved_code_root
    app.config["OPERATIONS_REGISTRY"] = declared_operations

    def _collect_current_readme_status() -> ReadmeGovernanceStatus:
        evidence = collect_repository_evidence(resolved_code_root)
        return collect_readme_governance_status(
            resolved_code_root,
            current_evidence_sha256=evidence_sha256(evidence),
            candidate_validator=lambda markdown: current_candidate_violations(
                markdown, resolved_code_root
            ),
        )

    # Dedicated pool so a long-running LLM subprocess doesn't pin a Flask
    # request thread for the full 10-60s of a chat turn. Pool size caps
    # the number of concurrent chats; chunks flow back via per-request
    # queues. Tests can inject their own executor for isolation.
    chat_pool = chat_executor or concurrent.futures.ThreadPoolExecutor(
        max_workers=4, thread_name_prefix="comments-server-chat"
    )
    app.config["CHAT_EXECUTOR"] = chat_pool

    def _client_error(message: str, status: int) -> tuple[dict[str, str], int]:
        return ({"error": message, "correlation_id": get_correlation_id()}, status)

    def _internal_failure(
        message: str, exc: object, *, status: int = 502
    ) -> tuple[dict[str, str], int]:
        app.logger.error(
            "%s: %s",
            message,
            redact(f"{type(exc).__name__}: {exc}")[:500],
        )
        return _client_error(f"{message}; retry the request", status)

    def _log_redacted_failure(message: str, exc: object, *, level: str = "error") -> None:
        log = getattr(app.logger, level)
        log(
            "%s: %s",
            message,
            redact(f"{type(exc).__name__}: {exc}")[:500],
        )

    def _drain_stream(
        events: Iterator[dict[str, object]],
        chunks: queue.Queue[dict[str, object] | None],
        stop: threading.Event,
        correlation_id: str,
    ) -> None:
        set_correlation_id(correlation_id)
        drain_events(events, chunks, stop)

    def _drain_durable_stream(
        events: Iterator[dict[str, object]],
        chunks: queue.Queue[dict[str, object] | None],
        stop: threading.Event,
        correlation_id: str,
    ) -> None:
        set_correlation_id(correlation_id)
        _drain_durable_events(events, chunks, stop)

    def _sse_frame(item: dict[str, object], correlation_id: str) -> str:
        if item.get("type") == "error":
            item = {
                "type": "error",
                "error": "chat stream failed; retry the request",
                "correlation_id": correlation_id,
            }
        return f"data: {json.dumps(item)}\n\n"

    def _stream_engine_events(events: Iterator[dict[str, object]]) -> Response:
        """Pump one ask-engine event stream into an SSE response.

        The narrative path drives an LLM subprocess (Claude CLI) for
        10-60s; running it inline would pin the Flask request thread for
        that whole window. Dispatch to the chat pool (the generator body
        executes lazily, on the pool thread) and pipe its events through
        a Queue, then drain the queue into SSE frames for /api/ask/stream."""
        correlation_id = get_correlation_id()
        chunks: queue.Queue[dict[str, object] | None] = queue.Queue(maxsize=_STREAM_QUEUE_MAXSIZE)
        stop = threading.Event()
        chat_pool.submit(_drain_stream, events, chunks, stop, correlation_id)

        def generate():
            try:
                while True:
                    item = chunks.get()
                    if item is None:
                        break
                    yield _sse_frame(item, correlation_id)
            finally:
                stop.set()

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    def _stream_engine_events_with_session(
        events: Iterator[dict[str, object]],
        session_id: str,
        *,
        disconnect_safe: bool = False,
        session_revision: int | None = None,
        session_context: dict[str, object] | None = None,
    ) -> Response:
        """Like ``_stream_engine_events`` but emits a leading
        ``{type: "session", session_id: "…"}`` frame so the client always
        knows which session this turn belongs to."""
        correlation_id = get_correlation_id()
        chunks: queue.Queue[dict[str, object] | None] = queue.Queue(maxsize=_STREAM_QUEUE_MAXSIZE)
        stop = threading.Event()
        drain = _drain_durable_stream if disconnect_safe else _drain_stream
        chat_pool.submit(drain, events, chunks, stop, correlation_id)

        def generate():
            try:
                session_event: dict[str, object] = {
                    "type": "session",
                    "session_id": session_id,
                }
                if session_revision is not None:
                    session_event["session_revision"] = session_revision
                if session_context is not None:
                    session_event["session_context"] = session_context
                yield f"data: {json.dumps(session_event)}\n\n"
                while True:
                    item = chunks.get()
                    if item is None:
                        break
                    yield _sse_frame(item, correlation_id)
            finally:
                stop.set()

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    def _open_db() -> sqlite3.Connection:
        return connect_sqlite(
            resolved_db_path,
            role=SQLiteConnectionRole.WRITER,
            schema_preflight=True,
        )

    @app.before_request
    def enforce_network_boundary():
        remote_address = request.remote_addr or ""
        if not is_allowed_client_address(
            remote_address,
            allow_tailscale=tailscale_access_enabled(),
        ):
            return ({"error": "client address is outside the allowed network"}, 403)
        return None

    @app.before_request
    def bind_correlation_id() -> None:
        # Fresh correlation id per request so all log lines for one operation
        # stitch together (sre-4). Honor an upstream X-Correlation-ID if present.
        incoming = request.headers.get("X-Correlation-ID", "").strip()
        if _CORRELATION_ID_RX.fullmatch(incoming):
            set_correlation_id(incoming)
        else:
            new_correlation_id()

    @app.before_request
    def start_request_timer() -> None:
        g.request_started_ns = time.perf_counter_ns()
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.path != (
            "/api/metrics/panel"
        ):
            # A successful mutation can affect several panels. Clear before it
            # runs so the next read cannot reuse a pre-mutation fragment; a
            # rejected mutation merely causes a harmless extra rebuild. Panel
            # timing telemetry is observational and must not evict the fragment
            # whose latency it just measured.
            panel_cache.clear()

    @app.errorhandler(413)
    def request_too_large(_error: object):
        return _client_error("request body is too large", 413)

    @app.errorhandler(500)
    def internal_server_error(error: object):
        if getattr(error, "original_exception", None) is None:
            app.logger.error(
                "unhandled request failure: %s",
                redact(f"{type(error).__name__}: {error}")[:500],
            )
        return _client_error("request failed; retry the request", 500)

    @app.teardown_request
    def close_request_db(exception: BaseException | None = None) -> None:
        db_conn = g.pop("request_read_db", None)
        if db_conn is not None:
            with contextlib.suppress(Exception):
                db_conn.close()
        reservation = g.pop("panel_cache_reservation", None)
        if isinstance(reservation, PanelCacheReservation):
            panel_cache.abandon(reservation)

    def get_read_db() -> sqlite3.Connection:
        if "request_read_db" not in g:
            conn = connect_sqlite(
                resolved_db_path,
                role=SQLiteConnectionRole.READ_ONLY,
                schema_preflight=True,
            )
            conn.row_factory = sqlite3.Row
            g.request_read_db = conn
        return g.request_read_db

    @app.before_request
    def csrf_origin_guard():
        # CSRF defense-in-depth for the unauthenticated localhost control plane.
        # A site the operator is visiting can drive a cross-origin state-changing
        # request at this server; reject any unsafe-method request whose browser
        # Origin is cross-site (judged by the same loopback / "null" / whitelist
        # rule as CORS, via _cors_allow_origin). Safe methods and the OPTIONS
        # preflight are exempt. An absent Origin remains allowed for loopback
        # non-browser CLI callers, but a browser with no Origin, Referer, or
        # Fetch Metadata must prove possession of the static-report capability.
        # Remote mutations must come from a same-origin browser surface. This
        # complements the
        # CORS-withholding in add_cors_headers, which only stops requests the
        # browser bothers to preflight — the Origin check also covers a simple
        # or forged cross-site request that skips preflight.
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        remote_address = request.remote_addr or ""
        origin = request.headers.get("Origin", "")
        capability_matches = report_capability.matches(
            request.headers.get(REPORT_CAPABILITY_HEADER, "")
        )
        if origin == "null" and not capability_matches:
            return ({"error": "static report capability required"}, 403)
        if (
            not origin
            and tailscale_access_enabled()
            and remote_address
            not in (
                "127.0.0.1",
                "::1",
            )
        ):
            return ({"error": "Origin required for remote state-changing request"}, 403)
        if origin and _cors_allow_origin(origin, repo_root=repo_root) is None:
            return ({"error": "cross-origin state-changing request refused"}, 403)
        if not origin:
            fetch_site = request.headers.get("Sec-Fetch-Site", "").strip().lower()
            if fetch_site == "cross-site":
                return ({"error": "cross-site state-changing request refused"}, 403)
            referer = request.headers.get("Referer", "").strip()
            if referer:
                try:
                    parsed = urllib.parse.urlparse(referer)
                    referer_origin = f"{parsed.scheme}://{parsed.netloc}"
                except ValueError:
                    referer_origin = ""
                if (
                    not referer_origin
                    or _cors_allow_origin(referer_origin, repo_root=repo_root) is None
                ):
                    return ({"error": "cross-site state-changing request refused"}, 403)
            elif fetch_site != "same-origin" and not capability_matches:
                if _is_browser_user_agent(request.headers.get("User-Agent", "")):
                    return ({"error": "state-changing request capability required"}, 403)
        return None

    @app.before_request
    def serve_fresh_panel_cache() -> Response | None:
        if request.method != "GET" or not request.path.startswith("/api/panel/"):
            return None
        if request.path == "/api/panel/cron_health" and request.args.get("fragment") == "live":
            g.panel_cache_bypass = True
            return None
        cache_key = request.full_path.removesuffix("?")
        lookup = panel_cache.get_or_reserve(cache_key)
        if isinstance(lookup, PanelCacheReservation):
            g.panel_cache_reservation = lookup
            return None
        assert isinstance(lookup, PanelCacheHit)
        body = lookup.entry.body
        content_type = lookup.entry.content_type
        etag = lookup.entry.etag
        g.panel_cache_hit = True
        if request.if_none_match.contains(etag.strip('"')):
            response = Response(status=304)
        else:
            response = Response(body, content_type=content_type)
        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Panel-Cache"] = "hit"
        return response

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
        allowed = _cors_allow_origin(request.headers.get("Origin", ""), repo_root=repo_root)
        if allowed is not None:
            response.headers["Access-Control-Allow-Origin"] = allowed
            response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = (
            f"Content-Type, {REPORT_CAPABILITY_HEADER}"
        )
        # Security headers — the dashboard is network-reachable over Tailscale.
        # SAMEORIGIN (not DENY) because the command center embeds /reports/<T> in
        # a same-origin iframe. no-referrer so ticker-bearing report URLs (which
        # reveal positions) never leak in a Referer to any external destination.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers["X-Correlation-ID"] = get_correlation_id()
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
        if getattr(g, "panel_cache_bypass", False):
            response.headers["Cache-Control"] = "no-store"
            return response
        if (
            request.method == "GET"
            and request.path.startswith("/api/panel/")
            and response.status_code == 200
            and not response.direct_passthrough
        ):
            response.add_etag()
            response.headers["Cache-Control"] = "no-cache"
            if not getattr(g, "panel_cache_hit", False):
                reservation = g.pop("panel_cache_reservation", None)
                if isinstance(reservation, PanelCacheReservation):
                    panel_cache.store(
                        reservation,
                        PanelCacheEntry(
                            body=response.get_data(),
                            content_type=response.content_type or "application/octet-stream",
                            etag=response.headers["ETag"],
                        ),
                    )
            # make_conditional mutates + returns self; the cast restores the
            # Flask subclass the werkzeug stub erases.
            return cast("Response", response.make_conditional(request))
        return response

    @app.after_request
    def add_server_timing(response: Response) -> Response:
        started_ns = getattr(g, "request_started_ns", None)
        if isinstance(started_ns, int):
            duration_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
            response.headers["Server-Timing"] = f"app;dur={duration_ms:.1f}"
            if duration_ms >= 500:
                app.logger.info(
                    json.dumps(
                        {
                            "event": "slow_http_request",
                            "method": request.method,
                            "path": request.path,
                            "status": response.status_code,
                            "duration_ms": round(duration_ms, 1),
                            "panel_cache": (
                                "hit" if getattr(g, "panel_cache_hit", False) else "miss"
                            ),
                            "correlation_id": get_correlation_id(),
                        },
                        separators=(",", ":"),
                    )
                )
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
        _bump_activation_count("act:capture")
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
        # The answer tap: a question-shaped capture gets answered via the
        # unified ask engine on a BACKGROUND thread and stored on the note —
        # the POST returns immediately (the old inline call pinned this
        # request, and the Capture button, on a multi-second LLM round-trip).
        # The note is marked ledger_answer_pending first so the card renders
        # an honest "Answering…" state; the capture JS polls
        # /api/onmymind/<id>/answer and swaps the card when the answer lands.
        # LEDGER_ANSWER_SYNC=1 restores the inline call (tests + a no-JS ops
        # fallback); the Telegram poller keeps its own synchronous reply path.
        ledger_answer: str | None = None
        answering = False
        if result.status == "landed" and result.note_id is not None:
            from onmymind.respond import answer_capture, will_answer
            from user_state.notes import patch_note_context

            answer_sync = os.environ.get("LEDGER_ANSWER_SYNC", "").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )
            if answer_sync:
                ledger_answer = answer_capture(result.note_id, repo_root=repo_root, db_path=db_path)
            elif will_answer(result.note_id, db_path=db_path):
                note_id = result.note_id
                patch_note_context(note_id, {"ledger_answer_pending": True}, db_path=db_path)
                threading.Thread(
                    target=answer_capture,
                    args=(note_id,),
                    kwargs={"repo_root": repo_root, "db_path": db_path},
                    daemon=True,
                    name=f"ledger-answer-{note_id}",
                ).start()
                answering = True
        return {
            "status": result.status,
            "note_id": result.note_id,
            "ticker": result.ticker,
            "needs_ticker": result.needs_ticker,
            "wondering_task_id": wondering_task_id,
            "pledge_challenge": pledge_challenge,
            "annotated_decision_id": annotated_decision_id,
            "answer": ledger_answer,
            "answering": answering,
        }

    @app.route("/api/onmymind/<int:note_id>/reply", methods=["POST", "OPTIONS"])
    def onmymind_reply(note_id: int):
        """The universal reply box (Phase B): free text on a feed card, routed
        by the ``ledger_reply_intent`` classifier instead of a verb-button
        taxonomy. Action intents flow through the SAME ``act_on_feed_item``
        core the old buttons (and the Telegram keyboard) call; ``question``
        hands back ``{mode: 'chat'}`` and the client streams the answer via
        /api/ask/stream; ``note`` appends the reply to the card's thread
        context. The classify is one short FAST-tier call — the client shows
        the pending bubble the moment the POST leaves."""
        if request.method == "OPTIONS":
            return ("", 204)
        from onmymind.reply import handle_reply
        from user_state.notes import get_note

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        text = str(payload.get("text") or "").strip()
        if not text:
            return ({"error": "text required"}, 400)
        if get_note(note_id, db_path=db_path) is None:
            return ({"error": "not found"}, 404)
        # The classify → route body is the SHARED reply core (one brain, two
        # mouths — the Telegram reply-to-a-card path calls the same handle_reply).
        result = handle_reply(note_id, text, db_path=db_path)
        _bump_activation_count(f"act:reply:{result.get('intent', 'question')}")
        return result

    @app.route("/api/onmymind/<int:note_id>/answer", methods=["GET"])
    def onmymind_answer(note_id: int):
        """The answer-poll read behind the async capture tap: the stored
        ``ledger_answer`` text (if it has landed) + the pending flag. Cheap
        context read — no LLM ever runs here."""
        from user_state.notes import get_note

        note = get_note(note_id, db_path=db_path)
        if note is None:
            return ({"error": "not found"}, 404)
        ctx = note.context or {}
        ans = ctx.get("ledger_answer")
        text = (
            str(cast("dict[str, object]", ans).get("text") or "") if isinstance(ans, dict) else ""
        )
        return {"answer": text or None, "pending": bool(ctx.get("ledger_answer_pending"))}

    @app.route("/api/research/task/<int:task_id>/run", methods=["POST", "OPTIONS"])
    def research_run(task_id: int):
        """W1-5d: run the two-pass research engine on a proposed task → an inert
        proposal. Gated by LEDGER_RESEARCH_RUN (the only place the expensive web
        pass is triggered, and only on an explicit owner tap). CSRF-guarded by the
        global Origin check.

        The engine takes seconds-to-minutes (web pass + two LLM passes) —
        running it inline pinned this request, and the "Research it" button,
        for that whole window. The run happens on a background thread and this
        returns ``{started: true}`` immediately; the panel polls
        ``/api/research/task/<id>/status`` (``run_research_task`` moves the row
        proposed → running → drafted, reverting to proposed on failure)."""
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
                _log_redacted_failure(
                    f"research run failed for task {task_id}", exc, level="warning"
                )
                return
            if proposal_id is None:
                return
            # Push the drafted proposal to the owner's Telegram thread (Phase C:
            # close the loop where the owner lives). The Telegram-initiated run
            # already did this; a WEB-initiated run's draft used to settle
            # silently into the collapsed Queues block — a click whose payoff
            # arrives minutes later, invisible, reads as a dead button.
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
                _log_redacted_failure("research telegram push skipped", exc, level="debug")

        threading.Thread(target=_run_bg, daemon=True, name=f"research-run-{task_id}").start()
        _bump_activation_count("act:research_run")
        return {"started": True}

    @app.route("/api/research/task/<int:task_id>/status", methods=["GET"])
    def research_task_status(task_id: int):
        """The run-poll read: the task's current status (proposed / running /
        drafted / …) so the panel knows when the background run finished."""
        from research.proposals import get_task

        task = get_task(task_id, db_path=db_path)
        if task is None:
            return ({"error": "not found"}, 404)
        return {"status": task.status}

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
        _bump_activation_count("act:research_reject")
        set_task_status(task_id, "rejected", db_path=db_path)
        return {"ok": True}

    @app.route("/api/research/proposal/<int:proposal_id>/<verb>", methods=["POST", "OPTIONS"])
    def research_act(proposal_id: int, verb: str):
        """W1-7: the 4-action core (approve / further / steer / reject). 'approve'
        flips status; a view artifact then writes its saved view via the separate
        write-dispatch (no web fetch, so never a trifecta). CSRF-guarded."""
        if request.method == "OPTIONS":
            return ("", 204)
        from research.proposals import PROPOSAL_VERBS, act_on_proposal, get_proposal

        if verb not in PROPOSAL_VERBS:
            return ({"error": f"unknown verb {verb!r}"}, 400)
        proposal = get_proposal(proposal_id, db_path=db_path)
        if proposal is not None and proposal.canonical_content_json is not None:
            return (
                {
                    "error": "governed Ask proposals require the revisioned decision endpoint",
                    "detail_url": f"/api/research/proposals/{proposal_id}",
                    "decision_url": f"/api/research/proposals/{proposal_id}/decision",
                },
                409,
            )
        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        steer_text = str(payload.get("steer_text") or "").strip() or None
        _bump_activation_count(f"act:proposal:{verb}")
        if verb == "approve" and proposal is not None and proposal.kind == "question":
            from research.question_artifact import approve_question_proposal
            from user_state.notes import NoteRevisionConflictError

            try:
                note = approve_question_proposal(proposal_id, db_path=db_path)
            except NoteRevisionConflictError as exc:
                return (
                    {"error": "revision_conflict", "current_revision": exc.current_revision},
                    409,
                )
            except LookupError as exc:
                return ({"error": str(exc)}, 404)
            except ValueError as exc:
                return ({"error": str(exc)}, 400)
            applied = f"open question #{note.id} persisted for {note.ticker or 'portfolio'}"
            return {
                "status": "approved",
                "applied": applied,
                "receipt": f"Approved — {applied}",
            }
        status = act_on_proposal(proposal_id, verb, steer_text=steer_text, db_path=db_path)
        applied = ""
        apply_failed = False
        if verb == "approve":
            from research.apply import apply_approved_proposal

            try:
                applied = apply_approved_proposal(proposal_id, db_path=db_path)
            except Exception as exc:  # a bad apply must not 500 the action
                apply_failed = True
                _log_redacted_failure(
                    f"research proposal apply failed for proposal {proposal_id}", exc
                )
        # Consequence receipt (Ledger UX overhaul): a plain-English line of what
        # just happened, built from the SAME status/applied values above — never
        # a second query. 'approve' echoes the live write when there was one
        # (a saved view); memo/dcf/thesis/code approvals write nothing here.
        receipts = {
            "approved": f"Approved — {applied}" if applied else "Approved — marked for follow-up",
            "researching": "Sent back for deeper research",
            "steered": "Steered — your direction was recorded",
            "rejected": "Rejected — this proposal won't be revisited",
        }
        receipt = receipts.get(status, "Saved")
        response: dict[str, object] = {"status": status, "applied": applied, "receipt": receipt}
        if apply_failed:
            response.update(
                {
                    "apply_error": "approved proposal could not be applied; retry the request",
                    "correlation_id": get_correlation_id(),
                }
            )
        return response

    def _ask_proposal_error(
        code: str,
        message: str,
        *,
        proposal_id: int,
        status: int,
        **details: object,
    ) -> tuple[dict[str, object], int]:
        error: dict[str, object] = {
            "code": code,
            "message": message,
            "proposal_id": proposal_id,
        }
        error.update({key: value for key, value in details.items() if value is not None})
        return ({"schema_version": "ask_proposal_error.v1", "error": error}, status)

    @app.route("/api/research/proposals/<int:proposal_id>", methods=["GET"])
    def ask_proposal_detail(proposal_id: int):
        try:
            detail = get_ask_proposal_detail(proposal_id, db_path=db_path)
        except StoredProposalError as exc:
            _log_redacted_failure("governed Ask proposal detail invalid", exc)
            return _ask_proposal_error(
                "stored_proposal_invalid",
                "proposal data is unavailable",
                proposal_id=proposal_id,
                status=500,
            )
        if detail is None:
            return _ask_proposal_error(
                "proposal_not_found",
                "governed proposal was not found",
                proposal_id=proposal_id,
                status=404,
            )
        return detail.model_dump(mode="json")

    @app.route(
        "/api/research/proposals/<int:proposal_id>/decision",
        methods=["POST", "OPTIONS"],
    )
    def ask_proposal_decision(proposal_id: int):
        if request.method == "OPTIONS":
            return ("", 204)
        if request.headers.get("Sec-Fetch-Site", "") == "cross-site":
            return _ask_proposal_error(
                "cross_site_rejected",
                "cross-site proposal decisions are not allowed",
                proposal_id=proposal_id,
                status=403,
            )
        try:
            decision = AskProposalDecisionV1.model_validate(request.get_json(silent=True))
        except ValidationError:
            return _ask_proposal_error(
                "invalid_request",
                "decision payload does not match ask_proposal_decision.v1",
                proposal_id=proposal_id,
                status=400,
            )
        if decision.proposal_id != proposal_id:
            return _ask_proposal_error(
                "proposal_id_mismatch",
                "path and payload proposal_id must match",
                proposal_id=proposal_id,
                status=400,
            )
        try:
            receipt = decide_ask_proposal(
                decision,
                repo_root=repo_root,
                db_path=db_path,
            )
        except ProposalConflictError as exc:
            return _ask_proposal_error(
                exc.code,
                str(exc),
                proposal_id=proposal_id,
                status=409,
                current_proposal_revision=exc.current_proposal_revision,
                current_status=exc.current_status,
            )
        except TargetDriftError as exc:
            return _ask_proposal_error(
                "target_drift",
                "proposal target changed after the proposal was created",
                proposal_id=proposal_id,
                status=412,
                expected_target_sha256=exc.expected_target_sha256,
                actual_target_sha256=exc.actual_target_sha256,
            )
        except RunLockHeldError:
            return _ask_proposal_error(
                "mutation_busy",
                "another portfolio mutation is in progress; retry the decision",
                proposal_id=proposal_id,
                status=409,
            )
        except (StoredProposalError, ValueError, OSError, sqlite3.Error) as exc:
            _log_redacted_failure("governed Ask proposal decision failed", exc)
            return _ask_proposal_error(
                "decision_failed",
                "proposal decision could not be completed",
                proposal_id=proposal_id,
                status=500,
            )
        _bump_activation_count(f"act:ask_proposal:{decision.decision}")
        return receipt.model_dump(mode="json")

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
        _bump_activation_count(f"act:reconcile:{verdict}")
        ok = fn(item_id, verdict, db_path=db_path)
        if not ok:
            return ({"ok": False}, 404)
        # Consequence receipt: what the verdict means for whether the coach can
        # still cite this item — built from the verdict tapped, no extra query.
        receipts = {
            "live": "Kept live — the coach can still cite this",
            "superseded": "Marked superseded — retired from the coach's context",
            "resolved-rejected": "Marked rejected — retired from the coach's context",
            "done": "Marked played out — retired from the coach's context",
        }
        return ({"ok": True, "receipt": receipts.get(verdict, "Saved")}, 200)

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
        _bump_activation_count(f"act:falsifier:{action}")
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
        elif action == "edit":
            result["receipt"] = "Saved — falsifier rewritten in your words"
        else:  # drop
            result["receipt"] = "Dropped — no tripwire will watch this decision"
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
        _bump_activation_count(f"act:om:{verb}")
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
        """Approve, reject, or revert a Tenet/stance insight. Approve promotes a
        ``proposed`` Tenet to ``current`` (superseding the prior belief on that
        topic); reject retires a ``proposed`` Tenet; revert (B4) undoes an
        auto-adopted ``current`` row — restoring the prior belief on a
        revision, or simply retiring a brand-new adoption. CSRF-guarded."""
        if request.method == "OPTIONS":
            return ("", 204)
        from synthesis.tenets import approve_tenet, reject_tenet, revert_tenet

        if action == "approve":
            row = approve_tenet(tenet_id, db_path=db_path)
            if row is None:
                return ({"ok": False}, 404)
            return (
                {
                    "ok": True,
                    "status": row.status,
                    "receipt": "Adopted — now a standing Tenet in your decision prompts",
                },
                200,
            )
        if action == "reject":
            ok = reject_tenet(tenet_id, db_path=db_path)
            if not ok:
                return ({"ok": False}, 404)
            return ({"ok": True, "receipt": "Retired — this Tenet was not adopted"}, 200)
        if action == "revert":
            reverted = revert_tenet(tenet_id, db_path=db_path)
            if reverted is None:
                return ({"ok": False}, 404)
            receipt = (
                "Reverted — restores your prior belief"
                if reverted.status == "current"
                else "Reverted — retired, no longer live"
            )
            return ({"ok": True, "status": reverted.status, "receipt": receipt}, 200)
        return ({"error": f"unknown action {action!r}"}, 400)

    @app.route("/api/profile/fact/<int:fact_id>/affirm", methods=["POST", "OPTIONS"])
    def profile_fact_affirm(fact_id: int):
        """Ratify one proposed owner-profile fact (tenet-2 Phase 1 gated
        assertion, §7.1) — the ONLY way a fact becomes 'affirmed'. CSRF-guarded
        by the global Origin check."""
        if request.method == "OPTIONS":
            return ("", 204)
        from owner_profile.store import affirm_fact

        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        try:
            row = affirm_fact(conn, fact_id)
            conn.commit()
        finally:
            conn.close()
        _bump_activation_count("act:profile:affirm")
        if row is None:
            return ({"ok": False}, 404)
        return (
            {
                "ok": True,
                "status": row.status,
                "receipt": "Affirmed — the coach may now cite this when reviewing your trades",
            },
            200,
        )

    @app.route("/api/profile/fact/<int:fact_id>/reject", methods=["POST", "OPTIONS"])
    def profile_fact_reject(fact_id: int):
        """Reject one proposed owner-profile fact — retires it without ever
        conditioning advice. CSRF-guarded by the global Origin check."""
        if request.method == "OPTIONS":
            return ("", 204)
        from owner_profile.store import reject_fact

        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        try:
            ok = reject_fact(conn, fact_id)
            conn.commit()
        finally:
            conn.close()
        _bump_activation_count("act:profile:reject")
        if not ok:
            return ({"ok": False}, 404)
        return ({"ok": True, "receipt": "Dropped — never used, won't be re-proposed"}, 200)

    @app.route("/api/profile/fact/<int:fact_id>/reaffirm", methods=["POST", "OPTIONS"])
    def profile_fact_reaffirm(fact_id: int):
        """ "Still true" on an EXPIRING affirmed fact (tenet-2 Phase 3 packet
        walk) — refreshes ``affirmed_at``, no value change. Distinct from
        ``affirm`` (proposed -> affirmed). CSRF-guarded by the global Origin
        check."""
        if request.method == "OPTIONS":
            return ("", 204)
        from owner_profile.store import reaffirm_fact

        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        try:
            row = reaffirm_fact(conn, fact_id)
            conn.commit()
        finally:
            conn.close()
        _bump_activation_count("act:profile:reaffirm")
        if row is None:
            return ({"ok": False}, 404)
        return (
            {
                "ok": True,
                "status": row.status,
                "receipt": "Confirmed — good for another review cycle",
            },
            200,
        )

    @app.route("/api/profile/fact/<int:fact_id>/retire", methods=["POST", "OPTIONS"])
    def profile_fact_retire(fact_id: int):
        """ "Drop" on an EXPIRING affirmed fact — retires it (status ->
        'rejected'). Distinct from ``reject`` (proposed-only). CSRF-guarded by
        the global Origin check."""
        if request.method == "OPTIONS":
            return ("", 204)
        from owner_profile.store import retire_fact

        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        try:
            ok = retire_fact(conn, fact_id)
            conn.commit()
        finally:
            conn.close()
        _bump_activation_count("act:profile:retire")
        if not ok:
            return ({"ok": False}, 404)
        return ({"ok": True, "receipt": "Dropped — the coach will stop citing this fact"}, 200)

    @app.route("/api/profile/fact/<int:fact_id>/update", methods=["POST", "OPTIONS"])
    def profile_fact_update(fact_id: int):
        """ "Update" on an EXPIRING affirmed fact — the minimal edit route
        (§4 delivery seam 5): narrative-only (never a structured-value
        re-entry — an actual balance/date change belongs to a fresh importer
        run), landing a NEW ``proposed`` fact that supersedes the old via
        ``append_fact``. Gated assertion holds even on the owner's own edit —
        it resurfaces at the next packet walk for an explicit affirm tap, the
        SAME proposed-facts source Phase 1 wired. CSRF-guarded by the global
        Origin check."""
        if request.method == "OPTIONS":
            return ("", 204)
        from owner_profile.store import append_fact, get_fact

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        narrative = payload.get("narrative")
        if not isinstance(narrative, str) or not narrative.strip():
            return ({"ok": False, "error": "narrative is required"}, 400)
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        try:
            old = get_fact(conn, fact_id)
            if old is None:
                return ({"ok": False}, 404)
            new_id = append_fact(
                conn,
                category=old.category,
                key=old.key,
                value=old.value,
                narrative=narrative.strip(),
                provenance="owner",
                status="proposed",
                review_horizon_days=old.review_horizon_days,
                source_detail="ledger_update",
            )
            conn.commit()
        finally:
            conn.close()
        _bump_activation_count("act:profile:update")
        return (
            {
                "ok": True,
                "new_fact_id": new_id,
                "receipt": "Saved — your edit awaits your affirm next walk",
            },
            200,
        )

    @app.route("/api/tenets/distill", methods=["POST", "OPTIONS"])
    def tenets_distill():
        """Owner-tapped Worldview distillation: distil the owner's flagged
        (saved/incorporated) musings into ``proposed`` Tenets. Never automatic; the
        deterministic $0 triage means nothing-flagged ⇒ zero LLM. CSRF-guarded."""
        if request.method == "OPTIONS":
            return ("", 204)
        from synthesis.tenet_distill import run_tenet_distill

        try:
            counts = run_tenet_distill(db_path, user_id=DEFAULT_USER_ID)
        except Exception as exc:  # a distill failure must not 500 the tap
            return _internal_failure("distillation failed", exc, status=500)
        return {"ok": True, **counts}

    # ----- DASHBOARD (unified tabbed command-center shell) -----

    def _overview_fragment_response() -> Response:
        """Build the legacy Overview fragment for compatible live drill-throughs."""
        read_conn = get_read_db()
        rows = build_cockpit_rows(read_conn, repo_root)
        coverage = tier_coverage_summary(repo_root, conn=read_conn)
        # Schema drift must not 500 the whole Home page, and must not render as
        # an empty rail either — the rail says it cannot be read, and the rest
        # of the cockpit still loads.
        try:
            inbox_html = render_inbox_stream(
                collect_inbox(db_path, limit=14, conn=read_conn),
                db_path=db_path,
                compact=True,
                surface="home",
                show_filters=True,
            )
        except SchemaRevisionMismatch as exc:
            inbox_html = schema_drift_notice(exc)
        # The compact earnings look-ahead above the rail — the surviving piece
        # of the retired /digest page.
        upcoming_html = render_upcoming_strip(db_path, datetime.now(UTC).date(), conn=read_conn)
        # The ritual-debt band above the cockpit — the owner's open queues
        # (Reconcile / Tenets / proposals / decision stubs / coach digest /
        # coach sent-today / the Sunday packet state) lead the first screen;
        # never raises on a thin DB. Home-band consolidation (wave3b,
        # navigation_ia.md D1 — ~2-viewport budget): the coach strip's one
        # non-redundant count (today's Telegram sends) and the packet-state
        # line now render INSIDE this band instead of as separate bands.
        from pipeline.open_loops import render_open_loops_band

        open_loops_html = render_open_loops_band(db_path, conn=read_conn)
        # P2.2 (PRD §9.1): the Senior Partner Brief + the Incremental Dollar
        # Recommendation Today doorways LEAD this composition — the brief
        # owns delivery for the four governor-routed moment classes (see
        # research.governor.BRIEF_ROUTED_CLASSES), so its doorway sits above
        # the ritual-debt band rather than beside it. Task 2 (wave3b): both
        # cards fold into ONE shared well via
        # senior_partner_brief_panel.render_today_doorways_card (each card's
        # own render function stays untouched; only the wrapping merges).
        # Isolated like its siblings; renders "" when neither card has
        # anything to show yet.
        try:
            from pipeline.senior_partner_brief_panel import render_today_doorways_card

            open_loops_html = render_today_doorways_card(db_path, conn=read_conn) + open_loops_html
        except Exception:
            pass
        overview = render_overview_panel(
            rows,
            coverage,
            inbox_html=inbox_html,
            upcoming_html=upcoming_html,
            open_loops_html=open_loops_html,
        )
        return Response(overview, mimetype="text/html")

    @app.route("/", methods=["GET"])
    def dashboard_page():
        """Eight-screen Work OS; legacy panel endpoints remain drill-throughs."""
        return Response(render_work_os_shell(db_path=db_path), mimetype="text/html")

    @app.route("/api/work-os/portfolio", methods=["GET"])
    def work_os_portfolio_api():
        """Portfolio-only research state for Cockpit and Company Desk."""
        conn = get_read_db()
        rows_by_role = build_cockpit_rows(conn, repo_root)
        rows = rows_by_role.get("portfolio", [])
        evaluation_rows = rows_by_role.get("evaluation", [])
        coverage_roles = {
            row.base.ticker.strip().upper(): role
            for role, role_rows in (("portfolio", rows), ("evaluation", evaluation_rows))
            for row in role_rows
        }
        readout_projection = load_latest_earnings_readouts(
            conn,
            list(coverage_roles),
            coverage_roles=coverage_roles,
        )
        payload = build_work_os_portfolio(
            rows,
            fetch_live_portfolio(),
            fetch_portfolio_allocation(),
            latest_readouts=readout_projection.readouts,
            readout_warnings=readout_projection.warnings,
        )
        response = app.json.response(payload.model_dump(mode="json"))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/dashboard", methods=["GET"])
    def dashboard_api():
        rows = build_dashboard_rows(get_read_db(), repo_root)
        return {k: [r.to_dict() for r in v] for k, v in rows.items()}

    @app.route("/api/panel/since_last", methods=["GET"])
    def since_last_fragment():
        """The "since you last looked" headline band (navigation_ia §4 PR3),
        fetched by the shell only when the client's ``ix-last-seen:overview``
        localStorage stamp is >6h stale. ``?since=<ISO 8601>`` is required —
        the client always supplies its own stamp, so a missing/unparseable
        value is a 400, not a silent "now" fallback. ``now`` is always the
        server clock. Naive-UTC per the repo convention; an aware value is
        normalized rather than rejected."""
        from pipeline.since_last import build_since_last, render_since_last_band

        raw_since = request.args.get("since", "")
        try:
            since = datetime.fromisoformat(raw_since)
        except (ValueError, TypeError):
            return ({"error": "since=<ISO 8601 timestamp> required"}, 400)
        now = datetime.now(UTC).replace(tzinfo=None)
        story = build_since_last(db_path, since=since, now=now)
        return Response(render_since_last_band(story), mimetype="text/html")

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
        Richness). The server is intentionally single-user, so identity is fixed
        by repository configuration rather than request parameters. Built to a
        stable path under data/dashboard and streamed as an attachment."""
        from dashboard.cio_export import export_cio_workbook

        user_id = DEFAULT_USER_ID
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
        if name == "overview":
            # The document shell paints immediately; Today assembles as a
            # cacheable fragment instead of blocking the initial HTML TTFB.
            return _overview_fragment_response()

        if name == "operations":
            snapshot = collect_operations_snapshot(
                declared_operations,
                repo_root=repo_root,
                conn=get_read_db(),
                observed_at=datetime.now(UTC),
                scheduler_receipt_path=scheduler_receipt_path(repo_root),
                service_receipt_path=service_receipt_path(repo_root),
            )
            return Response(
                render_operations_panel(
                    build_operations_panel_view(
                        declared_operations,
                        snapshot,
                        readme_status=_collect_current_readme_status(),
                    )
                ),
                mimetype="text/html",
            )

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

            # tenet-2 Phase 2 cash-aware mode: an optional ?cash_to_deploy=
            # query param opts the next-dollar model into per-holding dollar
            # allocations of that cash. Absent/unparseable -> distribution-only
            # (unchanged pre-Phase-2 behavior).
            cash_raw = request.args.get("cash_to_deploy")
            cash_to_deploy_usd: float | None = None
            if cash_raw:
                try:
                    cash_to_deploy_usd = float(cash_raw)
                except ValueError:
                    cash_to_deploy_usd = None

            return Response(
                render_portfolio_synthesis_panel(db_path, cash_to_deploy_usd=cash_to_deploy_usd),
                mimetype="text/html",
            )

        if name == "positioning":
            # Portfolio → Positioning: the owner's durable target book
            # (positioning_intents) + version history + the coach thread with
            # its propose→approve flow. GET path reads the materialized fit
            # meta + local DB only — never the tracker.
            from pipeline.positioning_panel import render_positioning_panel

            return Response(render_positioning_panel(db_path, repo_root), mimetype="text/html")

        if name == "allocation_recommendation":
            # Portfolio → Allocation's Incremental Dollar Recommendation card
            # (P0.4b, PRD §7.4). A pure read over the current llm_artifacts
            # row — no tracker call, no LLM call. Fetched by the card's own
            # JS after a generate/refresh POST to swap itself in place.
            from pipeline.allocation_recommendation_panel import (
                render_allocation_recommendation_section,
            )

            return Response(
                render_allocation_recommendation_section(db_path, repo_root), mimetype="text/html"
            )

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

        if name == "portfolio_health":
            # Portfolio -> Health (redesigned 2026-07-30): the Band-1 read +
            # two chip-tab cards (Theses / Book risk). ``?fragment=<key>``
            # serves one chip pane (thesis / exposure / collisions / bets /
            # drawdown / crowding / tail) — the cards lazy-fetch these on
            # first activation. The old builder routes above stay live for
            # deep dives, peeks and direct fetch.
            from pipeline.portfolio_console_panel import render_portfolio_health_panel
            from pipeline.portfolio_panel import render_health_fragment

            fragment = request.args.get("fragment")
            if fragment:
                return Response(render_health_fragment(db_path, fragment), mimetype="text/html")
            user_id = DEFAULT_USER_ID
            return Response(
                render_portfolio_health_panel(db_path, user_id=user_id), mimetype="text/html"
            )

        if name == "portfolio_allocation":
            # Portfolio -> Allocation (Phase-5 IA): composes Positioning +
            # Performance. Performance's own window bar re-windows via the
            # /api/panel/portfolio route (kept live); this landing shows the
            # default window.
            from pipeline.portfolio_console_panel import render_portfolio_allocation_panel

            user_id = DEFAULT_USER_ID
            return Response(
                render_portfolio_allocation_panel(db_path, repo_root, user_id=user_id),
                mimetype="text/html",
            )

        if name == "portfolio_record":
            # Portfolio -> Record (Phase-5 IA): composes the allocation-decisions
            # record + advisor Memos + the Triggers ladder (old `holdings`).
            from pipeline.portfolio_console_panel import render_portfolio_record_panel

            user_id = DEFAULT_USER_ID
            return Response(
                render_portfolio_record_panel(db_path, user_id=user_id), mimetype="text/html"
            )

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
            from pipeline.cron_health_panel import (
                render_cron_health_live_body,
                render_cron_health_panel,
            )

            if request.args.get("fragment") == "live":
                return Response(render_cron_health_live_body(db_path), mimetype="text/html")
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

            user_id = DEFAULT_USER_ID
            return Response(
                render_provenance_panel(db_path, repo_root, user_id=user_id),
                mimetype="text/html",
            )

        if name == "overrides":
            # Provenance's authoritative company-document figures. Served
            # standalone so the consolidated console can reveal it lazily.
            from pipeline.fact_overrides_panel import render_fact_overrides_panel

            return Response(render_fact_overrides_panel(db_path), mimetype="text/html")

        if name == "credibility":
            # Confidence-prior calibration against disagreement/restatement
            # ground truth; deferred by the consolidated Provenance shell.
            from pipeline.credibility_panel import render_credibility_panel

            return Response(
                render_credibility_panel(db_path, user_id=DEFAULT_USER_ID),
                mimetype="text/html",
            )

        if name == "section_coverage":
            # Per-ticker section coverage (P4.2): the visible counterpart of
            # the hide-don't-stub policy — reports hide cold sections, this
            # matrix is where the gaps stay accountable. Still served standalone
            # for the Provenance console's anchor + any direct fetch.
            from pipeline.section_coverage_panel import render_section_coverage_panel

            user_id = DEFAULT_USER_ID
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

            user_id = DEFAULT_USER_ID
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

        if name == "diet":
            # Companies → Diet: the information-diet curation layer (the
            # alerts→diet split). The PULL lane over the typed `signals`
            # substrate — non-decaying sell-side ratings + news + the forward
            # investor-day agenda. Pure read; never feeds the inbox scorer.
            from pipeline.diet_panel import render_diet_panel

            return Response(render_diet_panel(db_path), mimetype="text/html")

        if name == "musings":
            # Review → Ledger (Phase-5 IA): the `musings` panel id now serves the
            # composite Ledger console (feed + Triage + Journal behind an
            # anchor-nav band) on the default render; the in-panel refresh
            # ``?fragment=…`` sub-routes still return just the Ledger builder's
            # fragments (list / onmymind / research / reconcile / worldview) so
            # the capture box + On-My-Mind feed reload in place unchanged.
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

        if name == "discovery":
            # Research → Discovery (P5.4): the candidate approval queue —
            # the budget gate ("queue, never auto-build"). ``?fragment=list``
            # returns just the table; ``?fragment=sources`` the weight editor.
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

            user_id = DEFAULT_USER_ID
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

            user_id = DEFAULT_USER_ID
            t_renderer = (
                render_triage_list
                if request.args.get("fragment") == "list"
                else render_triage_panel
            )
            return Response(t_renderer(db_path, user_id=user_id), mimetype="text/html")

        if name == "ledger_decisions":
            # Review -> Ledger -> Decisions (P2.1, PRD §9.3): the owner-first
            # v_decision_journal reader. ``?fragment=list`` returns just the
            # filtered row list the panel's own JS swaps after a chip click.
            # NOT "decisions" — that id is RETIRED (superseded by
            # decisions_record; test_retired_panel_fragments_404 pins the 404).
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
            return Response(
                render_decision_journal_panel(db_path, filter_=dj_filter), mimetype="text/html"
            )

        if name == "ticker_settings":
            # Settings-drawer section (P3.4): per-ticker persistent overrides
            # (bypass_budget) listed + editable via /api/ticker-settings/<T>.
            from pipeline.ticker_settings_panel import render_ticker_settings_panel

            return Response(render_ticker_settings_panel(db_path), mimetype="text/html")

        if name == "data_policy_settings":
            # Operations -> Settings: read-only collection authorization and
            # issuer-adapter policy plus a read-only FMP recovery projection.
            from pipeline.data_policy_settings_panel import render_data_policy_settings_panel

            return Response(
                render_data_policy_settings_panel(db_path=db_path), mimetype="text/html"
            )

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

            user_id = DEFAULT_USER_ID
            return Response(
                render_thesis_ledger_panel(db_path, user_id=user_id), mimetype="text/html"
            )

        if name == "decisions_record":
            # The allocation-decisions record (master build P2.2): the sizing
            # audit (stated conviction/target vs live weight vs DCF gap vs
            # window alpha, mismatches ranked) + the merged decisions timeline
            # (thesis ledger + sizing intents + decision notes).
            from pipeline.allocation_decisions_panel import render_allocation_decisions_panel

            user_id = DEFAULT_USER_ID
            return Response(
                render_allocation_decisions_panel(db_path, user_id=user_id),
                mimetype="text/html",
            )

        if name == "advisor_memos":
            # Advisor memos (master build P2.3): run bar (next-dollar / swap
            # checks via the jobs SSE machinery) + the deterministic
            # swap-discipline screen + the durable memo record.
            from pipeline.advisor_memos_panel import render_advisor_memos_panel

            user_id = DEFAULT_USER_ID
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
            render_position_lifecycle_section(db_path, ticker, user_id=DEFAULT_USER_ID),
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
        """UPSERT +1 for (panel, today); Alembic owns table creation.

        A pre-migration database degrades to an omitted metric. Request paths
        never perform DDL or contend on schema locks.
        """
        try:
            conn = _open_db()
            try:
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
                json.dumps(
                    {
                        "event": "panel_activation_count_failed",
                        "panel": panel,
                        "error": redact(f"{type(exc).__name__}: {exc}")[:500],
                    }
                ),
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

    register_alert_routes(
        app,
        AppContext(
            db_path=db_path,
            default_user_id=DEFAULT_USER_ID,
            referer_back_path=_referer_back_path,
            approve_consequence_href=_approve_consequence_href,
        ),
    )
    register_settings_routes(
        app,
        SettingsRouteContext(db_path=db_path),
    )
    register_ir_approval_routes(
        app,
        IrApprovalRouteContext(db_path=db_path, owner_actor=DEFAULT_USER_ID),
    )
    register_journal_routes(
        app,
        JournalRouteContext(
            repo_root=repo_root,
            db_path=db_path,
            default_user_id=DEFAULT_USER_ID,
            note_to_json=_note_to_json,
            optional_int=_opt_int,
            bump_activation_count=_bump_activation_count,
        ),
    )

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

    @app.route("/api/ask/stream", methods=["POST", "OPTIONS"])
    def ask_stream_api():
        """The sole research-conversation route: durable raw SSE frames.

        The first frame is always
        ``{type: "session", session_id: "..."}`` so the client can store
        the id and pass it back on the next turn."""
        if request.method == "OPTIONS":
            return ("", 204)
        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        durable = "request_id" in body
        try:
            turn, sess = _parse_ask_turn_with_session()
        except ValueError as exc:
            return _client_error(str(exc), 400)
        if turn is None:
            return ({"error": "query required"}, 400)
        begun: BeginExchangeResult | None = None
        if durable:
            try:
                turn, begun = _prepare_durable_ask(body, turn, sess)
                if begun.disposition == "pending":
                    raise PendingExchangeError("request_id is already pending")
            except ValueError as exc:
                return _client_error(str(exc), 400)
            except (
                ExchangeConflictError,
                ExchangeStateError,
                PendingExchangeError,
                RevisionConflictError,
                SessionContextConflictError,
            ) as exc:
                return _durable_conflict(exc, sess)
        pack = build_portfolio_pack(repo_root, db_path)
        if begun is not None and begun.disposition == "replayed":
            events = replay_exchange_events(
                begun.exchange,
                db_path=db_path,
                session_revision=begun.session_revision,
            )
        else:
            raw_engine_events = respond_turn(
                turn,
                pack,
                db_path=db_path,
                repo_root=repo_root,
                registry=job_registry,
                retrieval_mode=ask_retrieval_mode(),
            )
            engine_events = (
                bind_ask_proposal_events(
                    raw_engine_events,
                    repo_root=repo_root,
                    db_path=db_path,
                    exchange_request_id=begun.exchange.request_id,
                )
                if begun is not None
                else raw_engine_events
            )
            events = (
                orchestrate_exchange_events(engine_events, exchange=begun.exchange, db_path=db_path)
                if begun is not None
                else engine_events
            )
        sid = sess.id if sess else (turn.session_id or "")
        durable_context = get_session_context(sid, db_path=db_path) if begun is not None else None
        return _stream_engine_events_with_session(
            events,
            sid,
            disconnect_safe=begun is not None,
            session_revision=begun.session_revision if begun is not None else None,
            session_context=(
                durable_context.context.model_dump(mode="json")
                if durable_context is not None
                else None
            ),
        )

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
            return _internal_failure("encode failed", exc)
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
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
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

    @app.route("/api/positioning/confirm-posture", methods=["POST", "OPTIONS"])
    def positioning_confirm_posture():
        """Portfolio Posture's "Mostly right" action (P0.4b, PRD §7.5): persist
        the owner's confirmation of the DERIVED posture narrative as an
        affirmed ``behavioral`` owner-profile fact — through the existing
        ``owner_profile.store`` (no parallel profile table). This is an
        explicit owner click confirming a machine-derived characterization,
        not a machine inference, so it lands directly as ``status='affirmed'``
        (mirrors the packet-walk's approve action, not an auto-promotion)."""
        if request.method == "OPTIONS":
            return ("", 204)
        from datetime import UTC, datetime

        from owner_profile.store import append_fact

        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        narrative = str(body.get("narrative") or "").strip()
        if not narrative:
            return ({"error": "narrative required"}, 400)
        now = datetime.now(UTC).replace(tzinfo=None).isoformat()
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        try:
            fact_id = append_fact(
                conn,
                category="behavioral",
                key="portfolio_posture_confirmation",
                value={"narrative": narrative, "confirmed_at": now},
                narrative=narrative,
                provenance="owner",
                status="affirmed",
            )
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "fact_id": fact_id}

    # ------------------------------------------------------------------
    # Incremental Dollar Recommendation (P0.4a backend, P0.4b UI — PRD §7.4
    # frontend/§11.6). The Allocation console, Today card, Telegram summary,
    # and Ask allocation pack (P0.4b) all read the SAME artifact via the
    # routes below.
    # ------------------------------------------------------------------

    @app.route("/api/allocation/recommendation", methods=["GET"])
    def allocation_recommendation_get():
        """The current governed Incremental Dollar Recommendation artifact,
        or a 404-shaped JSON body when none has been generated yet."""
        import llm_artifact_store
        from allocation.recommendation_artifact import PURPOSE

        artifact = llm_artifact_store.read_current(
            ticker=None, purpose=PURPOSE, scope="portfolio", db_path=db_path
        )
        if artifact is None:
            return ({"error": "no recommendation generated yet"}, 404)
        stale = bool(
            artifact.expires_at and artifact.expires_at < datetime.now(UTC).replace(tzinfo=None)
        )
        return {
            "artifact_id": artifact.id,
            "content_json": artifact.content_json,
            "created_at": artifact.generated_at.isoformat(),
            "dirty": artifact.dirty,
            "stale": stale,
        }

    @app.route("/api/allocation/recommendation", methods=["POST", "OPTIONS"])
    def allocation_recommendation_post():
        """Generate (or cache-hit) a governed Incremental Dollar Recommendation
        for ``{"cash_usd": <num>, "horizon": <str, optional>}``. Synchronous —
        the governed call is retry-capped and falls back deterministically, so
        it never hangs the request indefinitely."""
        if request.method == "OPTIONS":
            return ("", 204)
        from allocation.recommendation_artifact import generate_recommendation
        from llm.cli import is_hard_stop

        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        raw_cash = body.get("cash_usd")
        try:
            cash_usd = float(cast("str | float | int", raw_cash))
        except (TypeError, ValueError):
            return ({"error": "cash_usd (number > 0) required"}, 400)
        if not (cash_usd > 0):
            return ({"error": "cash_usd must be > 0"}, 400)
        raw_horizon = body.get("horizon")
        horizon = str(raw_horizon).strip() if isinstance(raw_horizon, str) and raw_horizon else None

        try:
            result = generate_recommendation(db_path, repo_root, cash_usd=cash_usd, horizon=horizon)
        except Exception as exc:
            if is_hard_stop(exc):
                raise
            return _internal_failure("recommendation generation failed", exc)
        return {
            "artifact_id": result.artifact_id,
            "selection_mode": result.selection_mode,
            "degraded_reasons": list(result.degraded_reasons),
            "recommendation": result.recommendation.model_dump(mode="json"),
        }

    @app.route(
        "/api/allocation/recommendation/<int:artifact_id>/adopt", methods=["POST", "OPTIONS"]
    )
    def allocation_recommendation_adopt(artifact_id: int):
        """Owner disposition on a recommendation artifact —
        ``{"verb": "save_intent"|"hold_accountable"|"dismiss", "notes": <str, optional>}``.
        Delegates entirely to ``allocation.actions.act_on_recommendation`` (the
        ONE action core), so a future Telegram dispatcher reaches the same
        write path."""
        if request.method == "OPTIONS":
            return ("", 204)
        from allocation.actions import RecommendationActionError, act_on_recommendation

        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        verb = str(body.get("verb") or "").strip()
        raw_notes = body.get("notes")
        notes = str(raw_notes).strip() if isinstance(raw_notes, str) and raw_notes.strip() else None
        try:
            status = act_on_recommendation(artifact_id, verb, db_path=db_path, notes=notes)
        except RecommendationActionError as exc:
            return ({"error": str(exc)}, 400)
        return {"status": status}

    @app.route("/api/allocation/compare", methods=["POST", "OPTIONS"])
    def allocation_compare():
        """Deterministic, NO-LLM comparison of up to 3 tickers (or 'CASH') for
        a given cash amount — eligibility, current weight, concentration
        zone, and diversification read, assembled from the same components
        ``allocation.recommendation.build_frontier`` uses (PRD §7.4 Compare)."""
        if request.method == "OPTIONS":
            return ("", 204)
        from allocation.concentration import classify_zone
        from allocation.eligibility import assess_universe, cash_assessment
        from candidate_fit_cache import read_materialized_candidate_fit
        from integrations.portfolio_tracker_client import fetch_live_portfolio
        from portfolio_weights import read_materialized_weights

        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        raw_tickers = body.get("tickers")
        if not isinstance(raw_tickers, list) or not raw_tickers:
            return ({"error": "tickers (list of 1-3 symbols) required"}, 400)
        ticker_list = cast("list[object]", raw_tickers)
        tickers = [str(t).strip().upper() for t in ticker_list if str(t).strip()]
        if not tickers or len(tickers) > 3:
            return ({"error": "tickers must be a non-empty list of at most 3 symbols"}, 400)
        raw_cash = body.get("cash_usd")
        try:
            cash_usd = float(cast("str | float | int", raw_cash))
        except (TypeError, ValueError):
            return ({"error": "cash_usd (number > 0) required"}, 400)
        if not (cash_usd > 0):
            return ({"error": "cash_usd must be > 0"}, 400)

        assessments = assess_universe(db_path, repo_root)
        weights = read_materialized_weights(repo_root)
        fit_cache = read_materialized_candidate_fit(repo_root)
        live = fetch_live_portfolio()
        total_value = (
            live.total_market_value if live.available and live.total_market_value > 0 else None
        )

        rows: list[dict[str, object]] = []
        for ticker in tickers:
            assessment = cash_assessment() if ticker == "CASH" else assessments.get(ticker)
            if assessment is None:
                rows.append(
                    {
                        "ticker": ticker,
                        "eligible": False,
                        "blocking_reasons": ["not on the tracked-companies universe"],
                    }
                )
                continue
            current_weight_pct = None
            zone = None
            if total_value is not None and ticker != "CASH":
                current_weight_pct = weights.get(ticker, 0.0) * 100.0
                za = classify_zone(current_weight_pct)
                zone = za.zone if za is not None else None
            fit = fit_cache.get(ticker)
            rows.append(
                {
                    "ticker": ticker,
                    "eligible": assessment.eligible,
                    "list_type": assessment.list_type,
                    "blocking_reasons": list(assessment.blocking_reasons),
                    "warning_reasons": list(assessment.warning_reasons),
                    "portfolio_fit_status": assessment.portfolio_fit_status,
                    "current_weight_pct": current_weight_pct,
                    "zone": zone,
                    "sharpe_delta_bps": fit.sharpe_delta_bps if fit is not None else None,
                }
            )
        return {"cash_usd": cash_usd, "tickers": rows}

    # ------------------------------------------------------------------
    # Investment Decision Card (P1.1, personal_investment_partner_prd.md §8.1).
    # Generation is a build-time step (execution/discovery_build.py,
    # execution/refresh_dirty_artifacts.py) — this POST route is the explicit
    # owner-triggered refresh, synchronous like the allocation recommendation
    # POST route above (the governed call is retry-capped and falls back
    # deterministically, so it never hangs the request indefinitely).
    # ------------------------------------------------------------------

    @app.route("/api/research/card/<ticker>/refresh", methods=["POST", "OPTIONS"])
    def investment_decision_card_refresh(ticker: str):
        """Generate/refresh the Investment Decision Card for ``ticker`` and
        re-render today's static workspace HTML so the strip reflects it
        immediately, without waiting for the next full build."""
        if request.method == "OPTIONS":
            return ("", 204)
        from llm.cli import is_hard_stop
        from research.investment_decision_card import generate_card

        symbol = ticker.strip().upper()
        if not symbol:
            return ({"error": "ticker required"}, 400)
        try:
            result = generate_card(db_path, repo_root, symbol)
        except Exception as exc:
            if is_hard_stop(exc):
                raise
            return _internal_failure("card generation failed", exc)
        if result.failure_reason is not None:
            return _internal_failure("card generation failed", result.failure_reason)
        try:
            from build_investment_decision_card import (
                rerender_workspace,  # sibling execution/ module
            )

            rerender_workspace(symbol, repo_root)
        except Exception:
            pass  # the card itself is persisted; a stale workspace HTML self-heals on the next build
        return {
            "artifact_id": result.artifact_id,
            "cache_hit": result.cache_hit,
            "selection_mode": result.selection_mode,
            "degraded_reasons": list(result.degraded_reasons),
            "card": result.card.model_dump(mode="json") if result.card is not None else None,
        }

    @app.route("/api/research/card/<int:artifact_id>/<verb>", methods=["POST", "OPTIONS"])
    def investment_decision_card_disposition(artifact_id: int, verb: str):
        """Owner disposition on a card artifact — pass|watch|research_further|
        promote. Delegates entirely to
        ``research.investment_decision_card.act_on_card`` (the ONE action
        core), so a future Telegram dispatcher reaches the same write path."""
        if request.method == "OPTIONS":
            return ("", 204)
        from research.investment_decision_card import CardActionError, act_on_card

        body = cast("dict[str, object]", request.get_json(silent=True) or {})
        raw_notes = body.get("notes")
        notes = str(raw_notes).strip() if isinstance(raw_notes, str) and raw_notes.strip() else None
        try:
            status = act_on_card(artifact_id, verb, db_path=db_path, notes=notes)
        except CardActionError as exc:
            return ({"error": str(exc)}, 400)
        return {"status": status}

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
                    "id": t.id,
                    "role": t.role,
                    "text": t.text,
                    "citations": t.citations,
                    "grounding_trace_id": t.grounding_trace_id,
                    "model": t.model,
                    "created_at": t.created_at,
                }
                for t in turns
            ]
            try:
                payload["exchange_artifacts"] = [
                    item.model_dump(mode="json")
                    for item in list_session_exchange_artifacts(sid, db_path=db_path)
                ]
            except Exception as exc:
                _log_redacted_failure("Ask session artifact validation failed", exc)
                payload["exchange_artifacts"] = []
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

    @app.route("/api/ask/sessions/<session_id>/distill", methods=["POST"])
    def ask_session_distill(session_id: str):
        """The explicit "Distill now" tap (B4): run the session-distillation
        pass on ONE Ask thread immediately, skipping the 4h-idle gate used by
        the manual batch command. Consequence (owner ruling 2026-07-19): distilled
        belief revisions AUTO-ADOPT — live immediately, announced with a
        one-tap Revert. Returns the per-session counts dict. 409 when the
        thread was already distilled (re-running would double-land the same
        candidates — batch idempotency comes from ``distilled_at``,
        which this route must therefore honor too)."""
        sid = session_id.strip()
        if not sid:
            return ({"error": "session_id required"}, 400)
        sess = get_session(sid, db_path=db_path)
        if sess is None:
            return ({"error": "not found"}, 404)
        import sqlite3 as _sqlite3

        try:
            conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
            try:
                row = conn.execute(
                    "SELECT distilled_at FROM ask_sessions WHERE id = ?", (sid,)
                ).fetchone()
            finally:
                conn.close()
            if row is not None and row[0]:
                return ({"error": "already distilled", "distilled_at": row[0]}, 409)
        except _sqlite3.OperationalError:
            return ({"error": "distillation substrate not migrated (0190)"}, 503)
        from synthesis.session_distill import SessionRef, distill_session

        try:
            counts = distill_session(
                SessionRef(source="ask", session_id=sid),
                db_path=db_path,
                repo_root=repo_root,
            )
        except Exception:
            # comments_server has no logger; the route contract (like its
            # siblings) is an error tuple — session_distill already logged
            # the failure with full context on its own logger.
            return ({"error": "distillation failed — nothing landed"}, 500)
        return {"counts": counts}

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
            _log_redacted_failure(f"peer lookup failed for {sym}", exc)
            return {
                "ticker": sym,
                "peers": [],
                "error": "peer lookup failed; retry the request",
                "correlation_id": get_correlation_id(),
            }
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
        if len(query) > _MAX_USER_INPUT_CHARS:
            raise ValueError(f"query exceeds the {_MAX_USER_INPUT_CHARS} character limit")
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
        if len(query) > _MAX_USER_INPUT_CHARS:
            raise ValueError(f"query exceeds the {_MAX_USER_INPUT_CHARS} character limit")
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

    def _prepare_durable_ask(
        body: dict[str, object],
        turn: AskTurn,
        sess: _AskSession | None,
    ) -> tuple[AskTurn, BeginExchangeResult]:
        raw_request_id = body.get("request_id")
        if not isinstance(raw_request_id, str) or not raw_request_id.strip():
            raise ValueError("request_id must be a non-empty string")
        request_id = raw_request_id.strip()
        raw_revision = body.get("expected_revision")
        if not isinstance(raw_revision, int) or isinstance(raw_revision, bool):
            raise ValueError("expected_revision must be an integer")
        if sess is None or turn.session_id is None:
            raise ExchangeStateError("durable Ask requires a writable portfolio session")
        raw_session_context = body.get("session_context")
        if not isinstance(raw_session_context, dict):
            raise ValueError("session_context must be an object")
        client_context = SessionContextV1.model_validate(raw_session_context)
        stored_context = get_session_context(sess.id, db_path=db_path)
        if stored_context is None:
            context = _canonical_session_context(repo_root, client_context)
            put_session_context(sess.id, context, db_path=db_path)
        else:
            if client_context != stored_context.context:
                raise SessionContextConflictError(
                    "session_context does not match the session's historical snapshot"
                )
            context = stored_context.context
        raw_research_context = body.get("research_context")
        if not isinstance(raw_research_context, dict):
            raise ValueError("research_context must be an object")
        research_context = ResearchContextV1.model_validate(raw_research_context)
        research_payload = research_context.model_dump(mode="json", exclude_none=True)
        payload_sha256 = hash_request_payload(
            {
                "session_id": sess.id,
                "query": turn.text,
                "tickers": list(turn.tickers),
                "context_spec": turn.context_spec,
                "session_context": context.model_dump(mode="json"),
                "research_context": research_payload,
            }
        )
        begun = begin_exchange(
            session_id=sess.id,
            request_id=request_id,
            payload_sha256=payload_sha256,
            user_text=turn.text,
            expected_revision=raw_revision,
            db_path=db_path,
        )
        turn.persistence_mode = "external_exchange"
        turn.authoritative_user_turn_id = begun.exchange.user_turn_id
        turn.research_context = cast("dict[str, object]", research_payload)
        turn.history = []
        return turn, begun

    def _durable_conflict(
        exc: object,
        sess: _AskSession | None,
    ) -> tuple[dict[str, object], int]:
        payload: dict[str, object] = {
            "error": str(exc),
            "correlation_id": get_correlation_id(),
        }
        if sess is not None:
            try:
                context = get_session_context(sess.id, db_path=db_path)
            except Exception:
                context = None
            if context is not None:
                payload["session_revision"] = context.revision
        return payload, 409

    def _session_to_json(sess: _AskSession) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": sess.id,
            "title": sess.title,
            "created_at": sess.created_at,
            "updated_at": sess.updated_at,
        }
        try:
            context = get_session_context(sess.id, db_path=db_path)
        except Exception as exc:
            _log_redacted_failure("Ask session context validation failed", exc)
            context = None
        payload["session_context"] = (
            context.context.model_dump(mode="json") if context is not None else None
        )
        payload["session_revision"] = context.revision if context is not None else 0
        return payload

    @app.route("/api/views", methods=["GET", "POST"])
    def views_api():
        """Saved views CRUD (P5.1, saved_views 0079). GET lists; POST
        ``{"name": ..., "spec": {...}}`` validates the spec then upserts by
        (user, name) — saving an existing name replaces its spec."""
        from user_state import saved_views as views_store
        from viewspec.spec import ViewSpec, ViewSpecError

        user_id = DEFAULT_USER_ID
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

        user_id = DEFAULT_USER_ID
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

    @app.route("/api/discovery/candidates/<int:cand_id>/watch", methods=["POST", "OPTIONS"])
    def discovery_watch_api(cand_id: int):
        """The Watch action (PRD §8.2, P1-B): promote the candidate's ticker
        into the watchlist (``tracked_companies.list_type = 'watchlist'``,
        mirroring ``discovery_build._promote_to_evaluation``'s direct-UPDATE
        shape — no onboard subprocess, unlike ``db.track_company``). The
        discovery candidate's own status is NEVER touched: Watch is a
        tracked-universe move, not a queue disposition. Idempotent — watching
        an already-watched (or better: portfolio/evaluation) name is a no-op
        that still returns 200."""
        if request.method == "OPTIONS":
            return ("", 204)
        from discovery.store import get_candidate, promote_to_watchlist

        user_id = DEFAULT_USER_ID
        try:
            cand = get_candidate(cand_id, db_path=db_path)
        except sqlite3.Error:
            return ({"error": "discovery_candidates table missing (run alembic upgrade)"}, 500)
        if cand is None:
            return ({"error": f"candidate {cand_id} not found"}, 404)
        ok = promote_to_watchlist(
            ticker=cand.ticker, name=cand.name, user_id=user_id, db_path=db_path
        )
        if not ok:
            return ({"error": "tracked_companies unavailable (run alembic upgrade)"}, 500)
        return {"candidate": _candidate_to_json(cand), "watch": {"ticker": cand.ticker, "ok": True}}

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

    @app.route("/api/decision-drafts", methods=["GET"])
    def decision_drafts_api():
        """Pending Decision Drafts for the Inbox (PRD §11.6). Thin — the
        typed read lives in ``capture.decision_draft.list_pending_drafts``."""
        from capture.decision_draft import list_pending_drafts

        drafts = list_pending_drafts(db_path=db_path)
        return {
            "drafts": [
                {
                    "id": d.id,
                    "source_channel": d.source_channel,
                    "status": d.status,
                    "original_text": d.original_text,
                    "draft": d.draft.model_dump() if d.draft is not None else None,
                    "parse_confidence": d.parse_confidence,
                    "created_at": d.created_at,
                }
                for d in drafts
            ]
        }

    @app.route("/api/decision-drafts/<int:draft_id>/confirm", methods=["POST", "OPTIONS"])
    def decision_draft_confirm_api(draft_id: int):
        """Confirm a draft and create/link one Owner Decision idempotently —
        thin wrapper over ``capture.decision_draft_actions.confirm_draft``
        (the SAME action core Telegram callbacks and the mobile Inbox call)."""
        if request.method == "OPTIONS":
            return ("", 204)
        from capture.decision_draft_actions import DraftActionError, confirm_draft

        try:
            result = confirm_draft(draft_id, db_path=db_path)
        except DraftActionError as exc:
            return ({"error": str(exc)}, 400)
        return result

    @app.route("/api/decision-drafts/<int:draft_id>/correct", methods=["POST", "OPTIONS"])
    def decision_draft_correct_api(draft_id: int):
        """Validate owner-supplied corrected fields, then apply the same
        resolution ``confirm`` uses — ``capture.decision_draft_actions.
        correct_draft``."""
        if request.method == "OPTIONS":
            return ("", 204)
        from capture.decision_draft_actions import DraftActionError, correct_draft

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        try:
            result = correct_draft(draft_id, payload, db_path=db_path)
        except DraftActionError as exc:
            return ({"error": str(exc)}, 400)
        return result

    @app.route("/api/decision-drafts/<int:draft_id>/dismiss", methods=["POST", "OPTIONS"])
    def decision_draft_dismiss_api(draft_id: int):
        """Dismiss the draft without deleting the raw capture."""
        if request.method == "OPTIONS":
            return ("", 204)
        from capture.decision_draft_actions import DraftActionError, dismiss_draft

        try:
            result = dismiss_draft(draft_id, db_path=db_path)
        except DraftActionError as exc:
            return ({"error": str(exc)}, 400)
        return result

    @app.route("/api/decision-draft-groups/<int:draft_id>/confirm", methods=["POST", "OPTIONS"])
    def decision_draft_group_confirm_api(draft_id: int):
        """Confirm one tracker trade group as one aggregated Owner Decision."""
        if request.method == "OPTIONS":
            return ("", 204)
        from capture.decision_draft_actions import (
            DraftActionError,
            confirm_tracker_fill_group,
        )

        try:
            result = confirm_tracker_fill_group(draft_id, db_path=db_path)
        except DraftActionError as exc:
            return ({"error": str(exc)}, 400)
        return result

    @app.route("/api/decision-draft-groups/<int:draft_id>/dismiss", methods=["POST", "OPTIONS"])
    def decision_draft_group_dismiss_api(draft_id: int):
        """Dismiss every pending fill in one tracker group without deleting evidence."""
        if request.method == "OPTIONS":
            return ("", 204)
        from capture.decision_draft_actions import (
            DraftActionError,
            dismiss_tracker_fill_group,
        )

        try:
            result = dismiss_tracker_fill_group(draft_id, db_path=db_path)
        except DraftActionError as exc:
            return ({"error": str(exc)}, 400)
        return result

    @app.route("/api/decision-draft-groups/<int:draft_id>/correct", methods=["POST", "OPTIONS"])
    def decision_draft_group_correct_api(draft_id: int):
        """Correct one tracker group and its shared Owner Decision atomically."""
        if request.method == "OPTIONS":
            return ("", 204)
        from capture.decision_draft_actions import (
            DraftActionError,
            correct_tracker_fill_group,
        )

        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        try:
            result = correct_tracker_fill_group(draft_id, payload, db_path=db_path)
        except DraftActionError as exc:
            return ({"error": str(exc)}, 400)
        return result

    @app.route("/api/senior-partner-brief/dismiss-item/<int:ping_id>", methods=["POST", "OPTIONS"])
    def senior_partner_brief_dismiss_item_api(ping_id: int):
        """Dismiss ONE governor-routed moment (calibration_finding/
        capacity_breach/life_event_checkpoint/profile_drift) from the brief —
        the SAME action core the mobile Inbox buttons and the Telegram
        ``spb:dismiss_item:<ping_id>`` callback call
        (``advisor.senior_partner_brief.dismiss_routed_moment``, a thin
        wrapper over ``research.governor.record_dismissal``). Consequence-
        first receipt: the response names the muted class when this was the
        3rd consecutive dismissal, so the owner knows the class just went
        silent rather than discovering it later."""
        if request.method == "OPTIONS":
            return ("", 204)
        from advisor.senior_partner_brief import dismiss_routed_moment

        recorded, muted_class = dismiss_routed_moment(ping_id, db_path=db_path)
        if not recorded:
            return ({"error": f"no dismissable ping for id={ping_id}"}, 404)
        return {
            "dismissed": True,
            "ping_id": ping_id,
            "muted_class": muted_class,
        }

    @app.route("/mobile/inbox", methods=["GET"])
    def mobile_inbox_page():
        """Mobile uses the same responsive Cockpit and canonical action queue."""
        return redirect("/#screen-cockpit", code=302)

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
        argv = managed_python_argv(
            repo_root,
            repo_root / "execution" / "run_discovery.py",
            "--repo-root",
            str(repo_root),
        )
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
        user_id = DEFAULT_USER_ID
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
        argv = managed_python_argv(
            repo_root,
            repo_root / "execution" / "discovery_build.py",
            "--tickers",
            ",".join(tickers),
            "--repo-root",
            str(repo_root),
            "--user-id",
            user_id,
        )
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
        user_id = DEFAULT_USER_ID
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
            _log_redacted_failure(f"post earnings readout request failed for {ticker}", exc)
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

    register_content_routes(
        app,
        ContentRouteContext(
            repo_root=repo_root,
            db_path=db_path,
            open_db=_open_db,
            get_read_db=get_read_db,
            safe_ticker=ticker_validation.safe_ticker,
            build_ticker_command_center=build_ticker_command_center,
            linked_gsheet=_linked_gsheet,
            fetch_live_portfolio=fetch_live_portfolio,
            default_user_id=DEFAULT_USER_ID,
        ),
    )

    register_dcf_routes(
        app,
        DcfRouteContext(
            repo_root=repo_root,
            db_path=db_path,
            linked_gsheet=_linked_gsheet,
            recompute_payload=_dcf_recompute_payload,
        ),
    )

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
        argv = managed_python_argv(repo_root, dispatcher, "--ticker", ticker, "--mode", mode)
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
        """Start the companion Portfolio Tracker from explicit API/root config.

        The offline Portfolio tab gets a button instead of a prose CLI hint.
        The configured root supplies the tracker's environment/data files, and
        the registry job's startup log streams over ``/actions/stream``.
        Success requires the exact owned command, a live process, endpoint
        ownership, and healthy HealthV1; 503 means that proof is unavailable.
        A missing configured root is a 404.
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
        venv_python = tracker_root / (
            ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"
        )
        python_bin = str(venv_python) if venv_python.exists() else sys.executable
        expected_argv = [
            python_bin,
            "-m",
            "uvicorn",
            "portfolio_tracker.api.main:app",
            "--host",
            bind_host,
            "--port",
            str(bind_port),
        ]
        start_job: list[Job] = []

        def inspect_listener() -> ListenerObservation:
            fetch = TrackerV1Client(base_url=api_url).probe_v1()
            tracked = None
            for item in job_registry.list_jobs():
                job_id = item.get("job_id")
                if (
                    item.get("kind") != "tracker-server"
                    or item.get("is_running") is not True
                    or not isinstance(job_id, str)
                ):
                    continue
                job = job_registry.get(job_id)
                process = getattr(job, "_process", None)
                pid = getattr(process, "pid", None)
                host_index = job.argv.index("--host") + 1 if job and "--host" in job.argv else -1
                port_index = job.argv.index("--port") + 1 if job and "--port" in job.argv else -1
                if (
                    job is not None
                    and job.is_running
                    and job.cwd == str(tracker_root)
                    and job.argv == expected_argv
                    and host_index > 0
                    and port_index > 0
                    and host_index < len(job.argv)
                    and port_index < len(job.argv)
                    and isinstance(pid, int)
                    and pid > 0
                    and process is not None
                    and process.poll() is None
                    and endpoint_owner_matches_pid(bind_host, bind_port, pid) is True
                    and job.argv[host_index] == bind_host
                    and job.argv[port_index] == str(bind_port)
                ):
                    tracked = {**item, "pid": pid}
                    break
            attributed_owner = (
                owner
                if fetch.data is not None
                and health_is_healthy(fetch.data, now=datetime.now(UTC))
                and tracked is not None
                else None
            )
            tracked_pid = tracked.get("pid") if tracked is not None else None
            tracked_job_id = tracked.get("job_id") if tracked is not None else None
            return ListenerObservation(
                healthy=fetch.available and health_is_healthy(fetch.data, now=datetime.now(UTC)),
                owner=attributed_owner,
                pid=tracked_pid if isinstance(tracked_pid, int) else None,
                job_id=tracked_job_id if isinstance(tracked_job_id, str) else None,
                health_checked_at=datetime.now(UTC),
                health=fetch.data,
            )

        def start_listener(config: RuntimeConfig) -> None:
            del config
            job = job_registry.start(
                ticker="_REPO",
                kind="tracker-server",
                argv=expected_argv,
                cwd=str(tracker_root),
                write_sets=[],
            )
            start_job.append(job)

        now = datetime.now(UTC)
        manager = PortfolioTrackerRuntimeManager(
            config=RuntimeConfig(
                listener_owner=owner,
                daily_refresh_owner=owner,
                idempotency_key=derive_daily_refresh_idempotency_key(now),
            ),
            inspect_listener=inspect_listener,
            start_listener=start_listener,
            now=lambda: datetime.now(UTC),
            lease=AtomicFileLease(portfolio_tracker_receipt_path(repo_root).with_suffix(".lease")),
        )
        persisted = manager.ensure_running(receipt_path=portfolio_tracker_receipt_path(repo_root))
        if persisted.lifecycle_state in {"started", "already_running"} and (
            not persisted.listener.healthy
            or not health_is_healthy(persisted.listener.health, now=persisted.recorded_at)
            or persisted.listener.owner != owner
            or persisted.listener.pid is None
            or persisted.listener.job_id is None
        ):
            # Keep a terminal, truthful receipt if an injected/legacy manager
            # violates the manager contract at this boundary.
            failed = persisted.model_copy(
                update={
                    "lifecycle_state": "failed",
                    "listener": persisted.listener.model_copy(update={"healthy": False}),
                    "failure_detail": "listener HealthV1 health/ownership proof is missing",
                }
            )
            repair_lease = AtomicFileLease(
                portfolio_tracker_receipt_path(repo_root).with_suffix(".lease")
            )
            if repair_lease.acquire():
                try:
                    write_runtime_receipt(portfolio_tracker_receipt_path(repo_root), failed)
                finally:
                    repair_lease.release()
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
        job = start_job[0] if start_job else None
        if job is None:
            return (
                {
                    "lifecycle_state": persisted.lifecycle_state,
                    "idempotency_key": persisted.idempotency_key,
                },
                200,
            )
        return (
            {
                "job_id": job.job_id,
                "ticker": job.ticker,
                "kind": job.kind,
                "stream_url": f"/actions/stream/{job.job_id}",
                "started_at": job.started_at.isoformat(),
                "idempotency_key": persisted.idempotency_key,
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
        argv = managed_python_argv(
            repo_root,
            script,
            "--ticker",
            ticker,
            "--discover",
            "--quarters",
            str(quarters),
            "--repo-root",
            str(repo_root),
        )
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

    @app.route("/api/readme-governance/status", methods=["GET"])
    def readme_governance_status():
        """Return the bounded status projection, never candidate prose or paths."""

        status = _collect_current_readme_status()
        return status.model_dump(mode="json")

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
            if _README_RUN_ID_RX.fullmatch(run_id) is None:
                return ({"error": "valid README updater run_id required"}, 400)
            status = _collect_current_readme_status()
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
        argv = managed_python_argv(
            repo_root,
            repo_root / "execution" / "run_advisor_memos.py",
            "--kind",
            memo_kind,
            "--repo-root",
            str(repo_root),
        )
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
            )
        except ValueError as exc:  # length mismatch / empty answers / bad horizon
            return ({"error": str(exc)}, 400)
        except Exception as exc:  # hard stops surface loudly to the form
            return _internal_failure("memo generation failed", exc)
        if not result.ok or result.memo_id is None:
            return _internal_failure(
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
                return _internal_failure("thesis preview unavailable", exc, status=status)
            _log_redacted_failure("thesis preview degraded", exc, level="warning")
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
                    return _internal_failure("comment preview unavailable", exc, status=status)
                _log_redacted_failure("comment preview degraded", exc, level="warning")
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

    # ----- CHAT (Phase 3) -----

    def _legacy_chat_migrated(ticker: str) -> dict[str, object]:
        return {
            "schema_version": "chat_migrated.v1",
            "status": "migrated",
            "ticker": ticker.strip().upper(),
            "message": "Report chat moved to Copilot Ask; no legacy history was imported.",
            "replacement_url": (f"/?copilot=1&ticker={ticker.strip().upper()}#screen-workspace"),
        }

    @app.route("/chat/<ticker>", methods=["OPTIONS"])
    def chat_options(ticker: str):
        del ticker
        return ("", 204)

    @app.route("/chat/<ticker>", methods=["GET"])
    def list_chat_endpoint(ticker: str):
        return (_legacy_chat_migrated(ticker), 410)

    @app.route("/chat/<ticker>", methods=["POST"])
    def chat_endpoint(ticker: str):
        return (_legacy_chat_migrated(ticker), 410)

    # ----- APPLY (Phase 4) -----

    @app.route("/chat/<ticker>/apply", methods=["OPTIONS"])
    def chat_apply_options(ticker: str):
        del ticker
        return ("", 204)

    @app.route("/chat/<ticker>/apply", methods=["POST"])
    def chat_apply_endpoint(ticker: str):
        return (_legacy_chat_migrated(ticker), 410)

    return app


def _parse_date(s: str) -> date:
    return date.fromisoformat(s[:10])


def configure_runtime_db(repo_root: Path) -> Path:
    """Bind implicit DB consumers to the same canonical DB as request handlers."""
    import db

    load_project_env(repo_root)
    db_path = portfolio_db_path(repo_root)
    db.set_db_path(db_path)
    return db_path


def main() -> int:
    configure_logging()  # structured root logging + correlation ids (sre-4)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=7421)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--host")
    parser.add_argument(
        "--tailscale",
        action="store_true",
        help="Bind to this device's Tailscale IPv4 address; no app login is added.",
    )
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    db_path = configure_runtime_db(repo_root)
    if args.tailscale:
        os.environ["COMMENTS_SERVER_ALLOW_TAILSCALE"] = "1"
    host = args.host or (resolve_tailscale_ipv4() if args.tailscale else "127.0.0.1")
    try:
        validate_bind_host(host, allow_tailscale=args.tailscale)
    except ValueError as exc:
        parser.error(str(exc))
    print(
        f"comments_server: repo_root={repo_root} host={host} port={args.port}",
        file=sys.stderr,
    )
    app = create_app(repo_root, db_path=db_path)
    # Flask's built-in dev server is fine here — this is a single-user
    # localhost tool, not a production service.
    app.run(host=host, port=args.port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
