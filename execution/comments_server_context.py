"""Shared runtime context for the comments-server route modules.

Owns the app-scoped caches, registries, and the create_app helper closures that
the route modules receive as a single ``ServerContext``.
"""

from __future__ import annotations

import concurrent.futures
import json
import queue
import sqlite3
import sys
import threading
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from comments_server_cockpit_memo import (
    CockpitDataBusy,
    CockpitDataHit,
    CockpitDataMemo,
    CockpitDataReservation,
    cockpit_data_memo_key,
)
from comments_server_middleware import install_middleware
from comments_server_panel_cache import PanelResponseCache
from comments_server_route_support import (
    ActivationCounter,
    BackgroundTaskStarter,
    RedactedFailureLogger,
)
from comments_server_shared import (
    MAX_ADMITTED_CHATS,
    MAX_REQUEST_BYTES,
    MAX_USER_INPUT_CHARS,
    PROJECT_ROOT,
    STREAM_QUEUE_MAXSIZE,
    canonical_session_context,
    drain_durable_events,
)
from flask import Flask, Response, g, request, stream_with_context
from update_readme import collect_repository_evidence, current_candidate_violations

from ask.engine import AskTurn, sanitize_history
from ask.exchange_store import (
    BeginExchangeResult,
    ExchangeStateError,
    ResearchContextV1,
    SessionContextConflictError,
    SessionContextV1,
    begin_exchange,
    get_session_context,
    hash_request_payload,
    put_session_context,
)
from ask.store import AskSession as _AskSession
from ask.store import ensure_session, get_session, rename_session
from dashboard.inbox import collect_inbox, render_inbox_stream, schema_drift_notice
from dashboard.upcoming import render_upcoming_strip
from dcf.input_evidence import SourceReadContext
from dispatch_registry import Registry
from log_redact import redact
from logging_config import get_correlation_id, set_correlation_id
from operations.models import OperationsRegistry
from operations.readme_governance import ReadmeGovernanceStatus, collect_readme_governance_status
from operations.registry import build_operations_registry
from operations.review_bundle import review_code_identity
from pipeline.research_cockpit import CockpitRow, build_cockpit_rows
from pipeline.tier_runner import tier_coverage_summary
from pipeline.work_os_overview import render_overview_panel
from readme_updater import evidence_sha256
from schema_compat import SchemaRevisionMismatch
from server_runtime.access import ReportCapabilityStore
from server_runtime.streaming import drain_events
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


@dataclass(slots=True)
class ServerContext:
    """App-scoped values and helper closures shared by every route module."""

    app: Flask
    repo_root: Path
    db_path: Path
    resolved_db_path: Path
    resolved_code_root: Path
    server_origin: str | None
    job_registry: Registry
    report_capability: ReportCapabilityStore
    panel_cache: PanelResponseCache
    cockpit_memo: CockpitDataMemo
    cockpit_memo_key_value: str
    declared_operations: OperationsRegistry
    operations_review_code_identity: str
    operations_job_receipt_roots: dict[str, Path]
    source_context: SourceReadContext | None
    source_state_root: Path | None
    chat_pool: concurrent.futures.Executor
    chat_slots: threading.BoundedSemaphore
    panel_metrics: deque[dict[str, object]]
    metric_cache_modes: frozenset[str]
    activation_cache_modes: frozenset[str]
    collect_current_readme_status: Callable[[], ReadmeGovernanceStatus]
    start_background_task: BackgroundTaskStarter
    client_error: Callable[[str, int], tuple[dict[str, str], int]]
    internal_failure: Callable[..., tuple[dict[str, str], int]]
    log_redacted_failure: RedactedFailureLogger
    stream_engine_events_with_session: Callable[..., Response]
    open_db: Callable[[], sqlite3.Connection]
    get_read_db: Callable[[], sqlite3.Connection]
    get_operations_read_db: Callable[[], sqlite3.Connection]
    cockpit_rows_snapshot: Callable[[sqlite3.Connection], dict[str, list[CockpitRow]]]
    overview_fragment_response: Callable[[], Response]
    bump_activation_count: ActivationCounter
    parse_ask_turn_with_session: Callable[[], tuple[AskTurn | None, _AskSession | None]]
    prepare_durable_ask: Callable[
        [dict[str, object], AskTurn, _AskSession | None], tuple[AskTurn, BeginExchangeResult]
    ]
    durable_conflict: Callable[[object, _AskSession | None], tuple[dict[str, object], int]]
    session_to_json: Callable[[_AskSession], dict[str, object]]
    legacy_chat_migrated: Callable[[str], dict[str, object]]


def build_server_context(
    app: Flask,
    repo_root: Path,
    *,
    db_path: Path | None = None,
    registry: Registry | None = None,
    operations_registry: OperationsRegistry | None = None,
    code_root: Path | None = None,
    chat_executor: concurrent.futures.Executor | None = None,
    server_origin: str | None = None,
    source_context: SourceReadContext | None = None,
    source_state_root: Path | None = None,
) -> ServerContext:
    """Wire the app-scoped caches, registries, and helper closures."""
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
    resolved_db_path = (db_path or repo_root / "data" / "portfolio.db").resolve()
    db_path = resolved_db_path
    resolved_code_root = (code_root or PROJECT_ROOT).resolve()
    job_registry = registry or Registry(repo_root=repo_root)
    report_capability = ReportCapabilityStore(repo_root)
    report_capability.load_or_create()
    app.config["DISPATCH_REGISTRY"] = job_registry
    # Panel fragments are expensive database/calculation reads but are treated
    # as fresh by the shell for 30 seconds. Keep the exact rendered response for
    # that same window so HTTP revalidation can stop *before* the route builder,
    # rather than paying the full build merely to discover the ETag is unchanged.
    panel_cache = PanelResponseCache(ttl_seconds=30.0, max_entries=256)
    # Shared DATA-level memo for the cockpit rows (M2 F5): the three
    # paint-cycle routes (overview + the two work-os hydrations) rebuild the
    # same rows per response, so a cold paint paid the roster build up to
    # three times. Keyed on DATA identity (repo root + resolved DB path +
    # schema revision), never the request path; bounded to a few data
    # identities with oldest-first eviction; single-flight per key; cleared
    # by the same mutation-family hook that evicts the response cache.
    # Fail-closed: a Busy lookup or a failed build falls back to a direct,
    # unpublished build with unchanged semantics.
    cockpit_memo = CockpitDataMemo(ttl_seconds=30.0, max_entries=4, wait_seconds=5.0)
    app.config["COCKPIT_DATA_MEMO"] = cockpit_memo
    cockpit_memo_key_value = cockpit_data_memo_key(repo_root, resolved_db_path)
    app.config["COCKPIT_DATA_MEMO_KEY"] = cockpit_memo_key_value
    declared_operations = operations_registry or build_operations_registry(resolved_code_root)
    operations_job_receipt_roots = {
        step.job: (
            resolved_db_path.parent.parent
            if step.job == "prepare-kpi-semantic-review"
            else resolved_code_root
        )
        for step in declared_operations.job_steps
    }
    operations_review_code_identity = review_code_identity(resolved_code_root)
    app.config["CODE_ROOT"] = resolved_code_root
    app.config["OPERATIONS_REGISTRY"] = declared_operations

    def collect_current_readme_status() -> ReadmeGovernanceStatus:
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
    chat_slots = threading.BoundedSemaphore(MAX_ADMITTED_CHATS)

    def start_background_task(task: Callable[[], None], name: str) -> None:
        threading.Thread(target=task, daemon=True, name=name).start()

    def client_error(message: str, status: int) -> tuple[dict[str, str], int]:
        return ({"error": message, "correlation_id": get_correlation_id()}, status)

    def internal_failure(
        message: str, exc: object, *, status: int = 502
    ) -> tuple[dict[str, str], int]:
        app.logger.error(
            "%s: %s",
            message,
            redact(f"{type(exc).__name__}: {exc}")[:500],
        )
        return client_error(f"{message}; retry the request", status)

    def log_redacted_failure(message: str, exc: object, *, level: str = "error") -> None:
        logger = app.logger
        log = getattr(logger, level)
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
        try:
            drain_events(events, chunks, stop)
        finally:
            chat_slots.release()

    def _drain_durable_stream(
        events: Iterator[dict[str, object]],
        chunks: queue.Queue[dict[str, object] | None],
        stop: threading.Event,
        correlation_id: str,
    ) -> None:
        set_correlation_id(correlation_id)
        try:
            drain_durable_events(events, chunks, stop)
        finally:
            chat_slots.release()

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
        chunks: queue.Queue[dict[str, object] | None] = queue.Queue(maxsize=STREAM_QUEUE_MAXSIZE)
        stop = threading.Event()
        chat_pool.submit(_drain_stream, events, chunks, stop, correlation_id)
        g.chat_admission_reserved = False

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

    def stream_engine_events_with_session(
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
        chunks: queue.Queue[dict[str, object] | None] = queue.Queue(maxsize=STREAM_QUEUE_MAXSIZE)
        stop = threading.Event()
        drain = _drain_durable_stream if disconnect_safe else _drain_stream
        chat_pool.submit(drain, events, chunks, stop, correlation_id)
        g.chat_admission_reserved = False

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

    def open_db() -> sqlite3.Connection:
        return connect_sqlite(
            resolved_db_path,
            role=SQLiteConnectionRole.WRITER,
            schema_preflight=True,
        )

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

    def get_operations_read_db() -> sqlite3.Connection:
        """Bounded diagnostic reads must survive application schema drift."""
        if "request_operations_read_db" not in g:
            conn = connect_sqlite(
                resolved_db_path,
                role=SQLiteConnectionRole.READ_ONLY,
                schema_preflight=False,
            )
            conn.row_factory = sqlite3.Row
            g.request_operations_read_db = conn
        return g.request_operations_read_db

    # ----- DASHBOARD (unified tabbed command-center shell) -----

    def cockpit_rows_snapshot(conn: sqlite3.Connection) -> dict[str, list[CockpitRow]]:
        """The paint-cycle routes' shared cockpit-rows read (M2 F5).

        Returns exactly what ``build_cockpit_rows(conn, repo_root)`` returns,
        served from the shared data memo when a fresh snapshot exists — one
        build per cold paint cycle instead of one per route — while each
        route keeps its own response contract (HTML fragment with ETag versus
        JSON hydration with no-store). Fail-closed: a memo build that raises
        never publishes (the exception propagates unchanged, reservation
        abandoned) and a Busy lookup builds directly without publishing, so
        the memo can only make a response faster, never different.
        """
        lookup = cockpit_memo.get_or_reserve(cockpit_memo_key_value)
        if isinstance(lookup, CockpitDataHit):
            # Record the memo's data clock so the response entry this rows
            # dict feeds cannot outlive it: the pre-memo bound (no served
            # cockpit data older than the 30s TTL at response time) holds
            # exactly, instead of widening to memo-TTL + response-TTL.
            g.cockpit_memo_expires_at = lookup.entry.expires_at
            return {role: list(role_rows) for role, role_rows in lookup.entry.rows.items()}
        if isinstance(lookup, CockpitDataReservation):
            g.cockpit_memo_reservation = lookup
            try:
                rows = build_cockpit_rows(conn, repo_root)
            except BaseException:
                cockpit_memo.abandon(lookup)
                raise
            cockpit_memo.publish(lookup, rows)
            g.pop("cockpit_memo_reservation", None)
            return rows
        assert isinstance(lookup, CockpitDataBusy)
        # Bounded wait expired or the in-flight bound is occupied: degrade to
        # a direct, unpublished build with unchanged semantics.
        return build_cockpit_rows(conn, repo_root)

    def overview_fragment_response() -> Response:
        """Build the legacy Overview fragment for compatible live drill-throughs."""
        read_conn = get_read_db()
        rows = cockpit_rows_snapshot(read_conn)
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
        overview = render_overview_panel(
            rows,
            coverage,
            inbox_html=inbox_html,
            upcoming_html=upcoming_html,
            open_loops_html=open_loops_html,
        )
        return Response(overview, mimetype="text/html")

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

    def bump_activation_count(panel_id: str) -> None:
        """UPSERT +1 for (panel, today); Alembic owns table creation.

        A pre-migration database degrades to an omitted metric. Request paths
        never perform DDL or contend on schema locks.
        """
        try:
            conn = open_db()
            try:
                conn.execute(
                    "INSERT INTO panel_activation_counts (panel_id, day, count)"
                    " VALUES (?, ?, 1)"
                    " ON CONFLICT(panel_id, day) DO UPDATE SET count = count + 1",
                    (panel_id, datetime.now(UTC).strftime("%Y-%m-%d")),
                )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            print(
                json.dumps(
                    {
                        "event": "panel_activation_count_failed",
                        "panel": panel_id,
                        "error": redact(f"{type(exc).__name__}: {exc}")[:500],
                    }
                ),
                file=sys.stderr,
            )

    def parse_ask_turn_with_session() -> tuple[AskTurn | None, _AskSession | None]:
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
        if len(query) > MAX_USER_INPUT_CHARS:
            raise ValueError(f"query exceeds the {MAX_USER_INPUT_CHARS} character limit")
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

    def prepare_durable_ask(
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
            context = canonical_session_context(repo_root, client_context)
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

    def durable_conflict(
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

    def session_to_json(sess: _AskSession) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": sess.id,
            "title": sess.title,
            "created_at": sess.created_at,
            "updated_at": sess.updated_at,
        }
        try:
            context = get_session_context(sess.id, db_path=db_path)
        except Exception as exc:
            log_redacted_failure("Ask session context validation failed", exc)
            context = None
        payload["session_context"] = (
            context.context.model_dump(mode="json") if context is not None else None
        )
        payload["session_revision"] = context.revision if context is not None else 0
        return payload

    # ----- CHAT (Phase 3) -----

    def legacy_chat_migrated(ticker: str) -> dict[str, object]:
        return {
            "schema_version": "chat_migrated.v1",
            "status": "migrated",
            "ticker": ticker.strip().upper(),
            "message": "Report chat moved to Copilot Ask; no legacy history was imported.",
            "replacement_url": (f"/?copilot=1&ticker={ticker.strip().upper()}#screen-workspace"),
        }

    ctx = ServerContext(
        app=app,
        repo_root=repo_root,
        db_path=db_path,
        resolved_db_path=resolved_db_path,
        resolved_code_root=resolved_code_root,
        server_origin=server_origin,
        job_registry=job_registry,
        report_capability=report_capability,
        panel_cache=panel_cache,
        cockpit_memo=cockpit_memo,
        cockpit_memo_key_value=cockpit_memo_key_value,
        declared_operations=declared_operations,
        operations_review_code_identity=operations_review_code_identity,
        operations_job_receipt_roots=operations_job_receipt_roots,
        source_context=source_context,
        source_state_root=source_state_root,
        chat_pool=chat_pool,
        chat_slots=chat_slots,
        panel_metrics=panel_metrics,
        metric_cache_modes=metric_cache_modes,
        activation_cache_modes=activation_cache_modes,
        collect_current_readme_status=collect_current_readme_status,
        start_background_task=start_background_task,
        client_error=client_error,
        internal_failure=internal_failure,
        log_redacted_failure=log_redacted_failure,
        stream_engine_events_with_session=stream_engine_events_with_session,
        open_db=open_db,
        get_read_db=get_read_db,
        get_operations_read_db=get_operations_read_db,
        cockpit_rows_snapshot=cockpit_rows_snapshot,
        overview_fragment_response=overview_fragment_response,
        bump_activation_count=bump_activation_count,
        parse_ask_turn_with_session=parse_ask_turn_with_session,
        prepare_durable_ask=prepare_durable_ask,
        durable_conflict=durable_conflict,
        session_to_json=session_to_json,
        legacy_chat_migrated=legacy_chat_migrated,
    )
    install_middleware(app, ctx)
    return ctx
