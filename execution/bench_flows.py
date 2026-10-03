"""Named-flow benchmark harness for the mission's performance evidence.

Contract (services.yaml ``bench-baseline`` / ``bench-run``):

    .venv/bin/python execution/bench_flows.py --db .tmp/portfolio_bench.db --label LABEL

Runs the named flows against the migrated bench copy and writes a
machine-readable artifact to ``.tmp/bench/LABEL.json``. Every artifact
records, per flow: wall-clock samples (default 7 timed runs after 1 warmup)
with median and coefficient of variation, a statement-count summary from one
SQLite trace pass, and ``EXPLAIN QUERY PLAN`` output for the hot statement
shapes. The artifact also records git HEAD plus a worktree digest, the bench
DB identity (sha256, alembic revision, resolved-spine row counts), the frozen
ticker parameterization, and the original-snapshot integrity check, so any
before/after comparison can name exactly what was measured.

Named flows:

* ``kpi_roster_resolution``       -- ``tier1_kpi_deltas`` over the full
  portfolio roster: the canonical resolution path every cockpit KPI read rides.
* ``cockpit_overview_build``     -- the body of ``_overview_fragment_response``
  (comments_server.py): cockpit rows, tier coverage, inbox stream, upcoming
  strip, open-loops band, and the overview panel render on one shared
  connection.
* ``dashboard_rows_build``       -- ``build_dashboard_rows`` (the roster builder
  behind ``/api/dashboard`` and ``build_cockpit_rows``).
* ``report_financials_section``  -- ``financials.build`` for each frozen ticker
  on one shared connection.
* ``cockpit_paint_cycle``        -- the cold ``/api/panel/overview`` ->
  ``/api/work-os/portfolio`` -> ``/api/work-os/evaluation`` three-request
  sequence. Each sample runs in a fresh child process so panel caches and any
  future data memos cannot leak across samples on either side of a
  before/after comparison.

Offline by construction: no network (the tracker base URL is pinned to a dead
loopback so tracker fetches fail closed instantly; the client's :8000 default is
a live user service on this machine and is never used) and no LLM calls.

Never point ``--db`` at the checkout-default ``data/portfolio.db`` or at the
approved snapshot ``.tmp/portfolio_local.db``; the harness refuses both and
fails closed. The bench DB must sit at the checkout's alembic head.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ParamSpec, TypedDict, cast
from unittest.mock import patch

import sqlite_bootstrap

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BENCH_DIR = PROJECT_ROOT / ".tmp" / "bench"

FLOW_NAMES: tuple[str, ...] = (
    "kpi_roster_resolution",
    "cockpit_overview_build",
    "dashboard_rows_build",
    "report_financials_section",
    "cockpit_paint_cycle",
)
_PAINT_CYCLE_ROUTES: tuple[tuple[str, str], ...] = (
    ("overview", "/api/panel/overview"),
    ("portfolio", "/api/work-os/portfolio"),
    ("evaluation", "/api/work-os/evaluation"),
)
# Dead loopback base URL for the portfolio tracker (the mission-contract pin):
# nothing serves the discard port on loopback, so connects are refused in well
# under a millisecond and the render paths take their documented fail-closed
# degradation. NEVER fall back to the client's :8000 default (a live user
# service, off-limits for this mission) and never use ports 8000/8088/8765.
_DEAD_TRACKER_URL = "http://127.0.0.1:9"
_LABEL_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REPRESENTATIVE_PREVIEW_CHARS = 4000
_EQP_SHAPES_PER_FLOW = 5
_CHILD_TIMEOUT_SECONDS = 900.0

# The managed-SQLite bootstrap must complete before the first sqlite3 import:
# it verifies the pinned runtime (3.53.4), registers the governed datetime
# adapters, and adds src/ to sys.path. Keep dependent imports in the success
# branch so no inline lint suppressions are needed for this ordering constraint.
if sqlite_bootstrap.preload_sqlite() == sqlite_bootstrap.EXPECTED_SQLITE_VERSION:
    import sqlite3

    from dashboard.inbox import collect_inbox, render_inbox_stream, schema_drift_notice
    from dashboard.upcoming import render_upcoming_strip
    from models.companies import ListType
    from pipeline.dashboard_status import build_dashboard_rows
    from pipeline.open_loops import render_open_loops_band
    from pipeline.queries import BRIEFED_LIST_TYPES, tracked_companies_for_user
    from pipeline.research_cockpit import build_cockpit_rows, tier1_kpi_deltas
    from pipeline.tier_runner import tier_coverage_summary
    from pipeline.work_os_overview import render_overview_panel
    from report.sections.financials import build as build_financials_section
    from schema_compat import SchemaRevisionMismatch, expected_head
    from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
else:
    raise RuntimeError("managed SQLite bootstrap did not return the expected runtime")


class ShapeSummary(TypedDict):
    """One normalized statement shape observed during a trace pass."""

    shape: str
    count: int
    cumulative_seconds: float
    representative_sql: str


class StatementSummary(TypedDict):
    """Aggregated statement evidence for one flow."""

    total_statements: int
    distinct_statement_shapes: int
    top_shapes_by_time: list[ShapeSummary]


class ExplainPlan(TypedDict):
    """EXPLAIN QUERY PLAN output for one hot statement shape."""

    title: str
    statement_shape: str
    statement_sha256: str
    plan: list[str]


@dataclass(frozen=True)
class FlowContext:
    """Per-run flow parameterization frozen from the bench DB."""

    db_path: Path
    portfolio_tickers: frozenset[str]
    frozen_tickers: tuple[str, ...]


@dataclass
class _TraceEntry:
    sql: str
    start: float
    seconds: float


_P = ParamSpec("_P")


# ---------------------------------------------------------------------------
# Statement tracing (SQL trace + EXPLAIN QUERY PLAN evidence)
# ---------------------------------------------------------------------------


class StatementCollector:
    """sqlite3 trace-callback sink recording each executed statement.

    The callback fires when a statement starts; the delta between consecutive
    callbacks therefore attributes execution-plus-fetch cost to the preceding
    statement. ``finish()`` closes the attribution for the last statement.
    """

    def __init__(self) -> None:
        self._entries: list[_TraceEntry] = []

    def __call__(self, sql: str) -> None:
        now = time.perf_counter()
        if self._entries:
            self._entries[-1].seconds = now - self._entries[-1].start
        self._entries.append(_TraceEntry(sql=sql, start=now, seconds=0.0))

    def finish(self) -> None:
        end = time.perf_counter()
        if self._entries:
            self._entries[-1].seconds = end - self._entries[-1].start

    def summarize(self) -> StatementSummary:
        shapes: dict[str, ShapeSummary] = {}
        for entry in self._entries:
            shape = _statement_shape(entry.sql)
            slot = shapes.get(shape)
            if slot is None:
                slot = ShapeSummary(
                    shape=shape,
                    count=0,
                    cumulative_seconds=0.0,
                    representative_sql=entry.sql,
                )
                shapes[shape] = slot
            slot["count"] += 1
            slot["cumulative_seconds"] += entry.seconds
        ranked = sorted(shapes.values(), key=lambda slot: slot["cumulative_seconds"], reverse=True)
        return StatementSummary(
            total_statements=len(self._entries),
            distinct_statement_shapes=len(shapes),
            top_shapes_by_time=ranked,
        )


_STRING_LITERAL_RX = re.compile(r"'(?:[^']|'')*'")
_NUMBER_RX = re.compile(r"\b\d+(?:\.\d+)?\b")
_WHITESPACE_RX = re.compile(r"\s+")


def _statement_shape(sql: str) -> str:
    """Normalize literals out of a trace-expanded statement for shape grouping.

    Inlining ``LIMIT 1`` vs ``LIMIT 14`` and every bound value into one shape
    is the point: it collapses per-ticker replicas of the same query into a
    single countable shape (exactly what the N+1 findings need to measure).
    """
    collapsed = _WHITESPACE_RX.sub(" ", sql).strip()
    without_strings = _STRING_LITERAL_RX.sub("?", collapsed)
    return _NUMBER_RX.sub("?", without_strings)[:320]


def _traced_connect(
    connect: Callable[_P, sqlite3.Connection],
    collector: StatementCollector,
) -> Callable[_P, sqlite3.Connection]:
    """Wrap ``sqlite3.connect`` so connections opened in a patch window trace.

    Patching the sqlite3 module attribute (the repo's own
    ``sqlite_overview_benchmark`` pattern) intercepts every connection factory
    regardless of import style, because ``src/sqlite_runtime.py`` resolves
    ``sqlite3.connect`` through the module attribute at call time.
    """

    def traced(*args: _P.args, **kwargs: _P.kwargs) -> sqlite3.Connection:
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(collector)
        return connection

    return traced


def _truncate_shape_previews(summary: StatementSummary) -> None:
    for slot in summary["top_shapes_by_time"]:
        if len(slot["representative_sql"]) > _REPRESENTATIVE_PREVIEW_CHARS:
            slot["representative_sql"] = (
                slot["representative_sql"][:_REPRESENTATIVE_PREVIEW_CHARS] + "...<truncated>"
            )


def _capture_eqp(db_path: Path, shapes: list[ShapeSummary]) -> list[ExplainPlan]:
    """EXPLAIN QUERY PLAN the hottest statement shapes on a governed connection."""
    if not shapes:
        return []
    conn = _open_read_connection(db_path)
    plans: list[ExplainPlan] = []
    try:
        for rank, slot in enumerate(shapes[:_EQP_SHAPES_PER_FLOW], start=1):
            plan_lines: list[str]
            try:
                rows = conn.execute("EXPLAIN QUERY PLAN " + slot["representative_sql"]).fetchall()
                plan_lines = [str(row[3]) for row in rows]
            except sqlite3.Error as exc:
                plan_lines = [f"<EXPLAIN QUERY PLAN failed: {type(exc).__name__}: {exc}>"]
            plans.append(
                ExplainPlan(
                    title=(
                        f"rank-{rank} count={slot['count']} "
                        f"cumulative={slot['cumulative_seconds']:.4f}s"
                    ),
                    statement_shape=slot["shape"],
                    statement_sha256=hashlib.sha256(slot["shape"].encode("utf-8")).hexdigest(),
                    plan=plan_lines,
                )
            )
    finally:
        conn.close()
    return plans


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _open_read_connection(db_path: Path) -> sqlite3.Connection:
    """Open the same governed read connection the cockpit's request path uses."""
    return connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY, schema_preflight=True)


def _prepare_environment(db_path: Path) -> None:
    """Pin the benchmark environment offline and DB-consistent."""
    os.environ["PORTFOLIO_TRACKER_API_URL"] = _DEAD_TRACKER_URL
    # Align every implicit DB resolution (components that consult
    # configured_db_path) with the explicit --db target.
    os.environ["EARNINGS_SUMMARY_DB_PATH"] = str(db_path)


def _validate_db(db_path: Path) -> None:
    if not db_path.is_file():
        raise SystemExit(f"bench database not found: {db_path}")
    checkout_default = (PROJECT_ROOT / "data" / "portfolio.db").resolve()
    snapshot = (PROJECT_ROOT / ".tmp" / "portfolio_local.db").resolve()
    if db_path == checkout_default:
        raise SystemExit(
            "refusing to run against the checkout-default database "
            "(data/portfolio.db must never exist); use .tmp/portfolio_bench.db"
        )
    if db_path == snapshot:
        raise SystemExit(
            "refusing to run against the approved snapshot; "
            "benchmark the migrated copy (.tmp/portfolio_bench.db) only"
        )


def _sample_stats(samples: list[float]) -> dict[str, Any]:
    if not samples:
        raise RuntimeError("no timed samples recorded")
    mean = statistics.fmean(samples)
    stdev = statistics.stdev(samples) if len(samples) > 1 else 0.0
    return {
        "runs": len(samples),
        "samples_seconds": [round(sample, 6) for sample in samples],
        "median_seconds": round(statistics.median(samples), 6),
        "mean_seconds": round(mean, 6),
        "stdev_seconds": round(stdev, 6),
        "cov": round(stdev / mean, 4) if mean > 0 else 0.0,
        "min_seconds": round(min(samples), 6),
        "max_seconds": round(max(samples), 6),
    }


def _sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


# ---------------------------------------------------------------------------
# Flow implementations (component flows)
# ---------------------------------------------------------------------------


def _run_kpi_roster_resolution(
    context: FlowContext, conn: sqlite3.Connection | None
) -> dict[str, Any]:
    """``tier1_kpi_deltas`` over the full portfolio roster (production call shape)."""
    connection = _open_read_connection(context.db_path) if conn is None else conn
    try:
        deltas = tier1_kpi_deltas(
            connection,
            set(context.portfolio_tickers),
            as_of=datetime.now(UTC).date(),
        )
        return {
            "portfolio_tickers": len(context.portfolio_tickers),
            "tickers_with_deltas": len(deltas),
            "kpi_delta_rows": sum(len(rows) for rows in deltas.values()),
        }
    finally:
        if conn is None:
            connection.close()


def _run_cockpit_overview_build(
    context: FlowContext, conn: sqlite3.Connection | None
) -> dict[str, Any]:
    """The body of ``_overview_fragment_response`` (comments_server.py)."""
    connection = _open_read_connection(context.db_path) if conn is None else conn
    try:
        rows = build_cockpit_rows(connection, PROJECT_ROOT)
        coverage = tier_coverage_summary(PROJECT_ROOT, conn=connection)
        try:
            inbox_html = render_inbox_stream(
                collect_inbox(context.db_path, limit=14, conn=connection),
                db_path=context.db_path,
                compact=True,
                surface="home",
                show_filters=True,
            )
        except SchemaRevisionMismatch as exc:
            inbox_html = schema_drift_notice(exc)
        upcoming_html = render_upcoming_strip(
            context.db_path, datetime.now(UTC).date(), conn=connection
        )
        open_loops_html = render_open_loops_band(context.db_path, conn=connection)
        overview = render_overview_panel(
            rows,
            coverage,
            inbox_html=inbox_html,
            upcoming_html=upcoming_html,
            open_loops_html=open_loops_html,
        )
        return {
            "cockpit_rows": {name: len(group) for name, group in rows.items()},
            "overview_html_bytes": len(overview.encode("utf-8")),
        }
    finally:
        if conn is None:
            connection.close()


def _run_dashboard_rows_build(db_path: Path, conn: sqlite3.Connection | None) -> dict[str, Any]:
    """``build_dashboard_rows`` — the roster builder with the per-ticker loop."""
    connection = _open_read_connection(db_path) if conn is None else conn
    try:
        grouped = build_dashboard_rows(connection, PROJECT_ROOT)
        return {
            "portfolio_rows": len(grouped.get("portfolio", [])),
            "evaluation_rows": len(grouped.get("evaluation", [])),
        }
    finally:
        if conn is None:
            connection.close()


def _run_report_financials_section(
    context: FlowContext, conn: sqlite3.Connection | None
) -> dict[str, Any]:
    """``financials.build`` for each frozen ticker on one shared connection."""
    connection = _open_read_connection(context.db_path) if conn is None else conn
    try:
        per_ticker: dict[str, dict[str, Any]] = {}
        for ticker in context.frozen_tickers:
            started = time.perf_counter()
            section = build_financials_section(ticker, PROJECT_ROOT, conn=connection)
            per_ticker[ticker] = {
                "seconds": round(time.perf_counter() - started, 6),
                "status": str(section.status.value),
            }
        return {
            "frozen_tickers": list(context.frozen_tickers),
            "per_ticker": per_ticker,
        }
    finally:
        if conn is None:
            connection.close()


def _flow_runners(
    context: FlowContext,
) -> dict[str, Callable[[sqlite3.Connection | None], dict[str, Any]]]:
    return {
        "kpi_roster_resolution": lambda conn: _run_kpi_roster_resolution(context, conn),
        "cockpit_overview_build": lambda conn: _run_cockpit_overview_build(context, conn),
        "dashboard_rows_build": lambda conn: _run_dashboard_rows_build(context.db_path, conn),
        "report_financials_section": lambda conn: _run_report_financials_section(context, conn),
    }


def _measure_component_flow(
    runner: Callable[[sqlite3.Connection | None], dict[str, Any]],
    db_path: Path,
    *,
    runs: int,
    warmup: int,
) -> dict[str, Any]:
    for _ in range(warmup):
        runner(None)
    samples: list[float] = []
    units: dict[str, Any] = {}
    for _ in range(runs):
        gc.collect()
        started = time.perf_counter()
        units = runner(None)
        samples.append(time.perf_counter() - started)
    flow: dict[str, Any] = _sample_stats(samples)
    flow["units"] = units

    # One traced pass for statement counts + hot-shape EQP. The flow connection
    # is opened BEFORE tracing so its connection-setup statements stay out of
    # the count (the count is about the build, not the open); auxiliary
    # connections opened inside the patch window are counted from open.
    collector = StatementCollector()
    conn = _open_read_connection(db_path)
    try:
        with patch.object(sqlite3, "connect", _traced_connect(sqlite3.connect, collector)):
            conn.set_trace_callback(collector)
            runner(conn)
        collector.finish()
    finally:
        conn.set_trace_callback(None)
        conn.close()
    statements = collector.summarize()
    flow["eqp"] = _capture_eqp(db_path, statements["top_shapes_by_time"])
    _truncate_shape_previews(statements)
    flow["statements"] = statements
    flow["statement_count_protocol"] = (
        "one traced pass after the timed runs; the flow connection is opened before tracing so "
        "its setup statements are excluded; auxiliary connections opened inside the patch window "
        "are counted from open"
    )
    return flow


# ---------------------------------------------------------------------------
# Flow implementation (cockpit_paint_cycle — request sequence per child process)
# ---------------------------------------------------------------------------


def _paint_cycle_sample(db_path: Path, *, trace: bool) -> dict[str, Any]:
    """One cold paint cycle: fresh app, three sequential GETs, one process."""
    import comments_server

    comments_server.configure_runtime_db(PROJECT_ROOT)
    app = comments_server.create_app(
        PROJECT_ROOT,
        db_path=db_path,
        server_origin="http://127.0.0.1:7421",
    )
    client = app.test_client()

    def run_sequence() -> dict[str, Any]:
        per_request: dict[str, float] = {}
        response_bytes: dict[str, int] = {}
        status_codes: dict[str, int] = {}
        cache_states: dict[str, str] = {}
        sequence_started = time.perf_counter()
        for name, route in _PAINT_CYCLE_ROUTES:
            request_started = time.perf_counter()
            response = client.get(route)
            per_request[name] = time.perf_counter() - request_started
            body = response.get_data()
            if response.status_code != 200:
                raise RuntimeError(
                    f"cockpit_paint_cycle: GET {route} returned {response.status_code}: "
                    f"{body[:300]!r}"
                )
            status_codes[name] = int(response.status_code)
            response_bytes[name] = len(body)
            cache_header = response.headers.get("X-Panel-Cache")
            cache_states[name] = str(cache_header) if cache_header is not None else ""
        return {
            "total_seconds": time.perf_counter() - sequence_started,
            "per_request_seconds": per_request,
            "response_bytes": response_bytes,
            "status_codes": status_codes,
            "panel_cache_states": cache_states,
        }

    collector = StatementCollector()
    if trace:
        with patch.object(sqlite3, "connect", _traced_connect(sqlite3.connect, collector)):
            result = run_sequence()
        collector.finish()
        statements = collector.summarize()
        result["statement_count_protocol"] = (
            "one traced child process after the timed children; every connection opened during "
            "the request sequence is traced from open, so per-request connection setup "
            "statements are included"
        )
        result["eqp"] = _capture_eqp(db_path, statements["top_shapes_by_time"])
        _truncate_shape_previews(statements)
        result["statements"] = statements
    else:
        result = run_sequence()
    return result


def _spawn_paint_cycle_child(db_path: Path, *, trace: bool) -> dict[str, Any]:
    """Run one paint-cycle sample in a fresh child process.

    A child per sample guarantees genuinely cold state (empty panel cache, no
    module-level memos) at the start of every sequence, on both sides of any
    before/after comparison: post-M2 data memos cannot leak across samples.
    """
    payload = json.dumps({"db_path": str(db_path), "trace": trace})
    completed = subprocess.run(
        [sys.executable, "execution/bench_flows.py", "--paint-cycle-sample", payload],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=_CHILD_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"cockpit_paint_cycle child failed (exit {completed.returncode}); "
            f"stderr tail: {completed.stderr[-2000:]}"
        )
    lines = [line for line in completed.stdout.strip().splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("cockpit_paint_cycle child produced no output")
    parsed: Any = json.loads(lines[-1])
    if not isinstance(parsed, dict):
        raise RuntimeError("cockpit_paint_cycle child output was not a JSON object")
    return cast("dict[str, Any]", parsed)


def _measure_paint_cycle(db_path: Path, *, runs: int, warmup: int) -> dict[str, Any]:
    per_request: dict[str, list[float]] = {name: [] for name, _route in _PAINT_CYCLE_ROUTES}
    samples: list[float] = []
    last: dict[str, Any] = {}
    for index in range(warmup + runs):
        sample = _spawn_paint_cycle_child(db_path, trace=False)
        if index >= warmup:
            samples.append(float(sample["total_seconds"]))
            for name, _route in _PAINT_CYCLE_ROUTES:
                per_request[name].append(float(sample["per_request_seconds"][name]))
        last = sample
    flow: dict[str, Any] = _sample_stats(samples)
    flow["units"] = {
        "routes": dict(_PAINT_CYCLE_ROUTES),
        "status_codes": last["status_codes"],
        "response_bytes": last["response_bytes"],
        "panel_cache_states": last["panel_cache_states"],
        "per_request_median_seconds": {
            name: round(statistics.median(values), 6) for name, values in per_request.items()
        },
        "per_request_samples_seconds": {
            name: [round(value, 6) for value in values] for name, values in per_request.items()
        },
    }
    traced = _spawn_paint_cycle_child(db_path, trace=True)
    flow["statement_count_protocol"] = str(traced["statement_count_protocol"])
    flow["statements"] = traced["statements"]
    flow["eqp"] = traced["eqp"]
    return flow


# ---------------------------------------------------------------------------
# Provenance blocks
# ---------------------------------------------------------------------------


def _git_identity() -> dict[str, Any]:
    def git_output(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        return completed.stdout if completed.returncode == 0 else ""

    porcelain = git_output("status", "--porcelain")
    return {
        "head": git_output("rev-parse", "HEAD").strip(),
        "branch": git_output("rev-parse", "--abbrev-ref", "HEAD").strip(),
        "status_porcelain": porcelain.strip(),
        "status_porcelain_sha256": hashlib.sha256(porcelain.encode("utf-8")).hexdigest(),
    }


def _database_identity(db_path: Path) -> dict[str, Any]:
    conn = _open_read_connection(db_path)
    try:
        version_row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        spine_kpi = conn.execute("SELECT COUNT(*) FROM v_kpi_facts_resolved_current").fetchone()
        spine_financial = conn.execute(
            "SELECT COUNT(*) FROM v_financial_facts_resolved_current"
        ).fetchone()
    finally:
        conn.close()
    version = str(version_row[0]) if version_row is not None else None
    head = expected_head(PROJECT_ROOT)
    return {
        "path": str(db_path),
        "sha256": _sha256_file(db_path),
        "size_bytes": db_path.stat().st_size,
        "alembic_version": version,
        "checkout_expected_head": head,
        "schema_at_checkout_head": version == head,
        "resolved_spine_rows": {
            "v_kpi_facts_resolved_current": int(spine_kpi[0]) if spine_kpi is not None else 0,
            "v_financial_facts_resolved_current": (
                int(spine_financial[0]) if spine_financial is not None else 0
            ),
        },
    }


def _snapshot_integrity() -> dict[str, Any]:
    """Record that the approved snapshot is byte-unchanged (VAL-FACT-001 evidence)."""
    snapshot = PROJECT_ROOT / ".tmp" / "portfolio_local.db"
    record = BENCH_DIR / "original-snapshot.sha256"
    recorded = record.read_text(encoding="utf-8").strip() if record.is_file() else None
    current = _sha256_file(snapshot) if snapshot.is_file() else None
    return {
        "path": str(snapshot),
        "recorded_sha256": recorded,
        "current_sha256": current,
        "matches_record": bool(recorded is not None and recorded == current),
    }


def _resolve_parameterization(db_path: Path) -> dict[str, Any]:
    """Freeze the comparison parameterization from the bench DB.

    Frozen ticker set (validation contract M2 preamble): the first three
    portfolio tickers plus the first evaluation ticker, with per-ticker tier-1
    KPI definition counts as the coverage check. The production roster for the
    KPI flow mirrors ``build_cockpit_rows`` (tracked_companies_for_user over
    BRIEFED_LIST_TYPES), not the raw SQL.
    """
    conn = _open_read_connection(db_path)
    try:
        companies = tracked_companies_for_user(conn, list_types=BRIEFED_LIST_TYPES)
        portfolio_roster = sorted(
            str(company.ticker) for company in companies if company.list_type is ListType.PORTFOLIO
        )
        evaluation_roster = sorted(
            str(company.ticker) for company in companies if company.list_type is ListType.EVALUATION
        )
        frozen = [
            str(row["ticker"])
            for row in conn.execute(
                "SELECT ticker FROM tracked_companies WHERE list_type='portfolio' "
                "AND archived_at IS NULL ORDER BY ticker LIMIT 3"
            )
        ]
        frozen.extend(
            str(row["ticker"])
            for row in conn.execute(
                "SELECT ticker FROM tracked_companies WHERE list_type='evaluation' "
                "AND archived_at IS NULL ORDER BY ticker LIMIT 1"
            )
        )
        coverage: dict[str, int] = {}
        if frozen:
            marks = ",".join("?" for _ in frozen)
            for row in conn.execute(
                "SELECT ticker, COUNT(*) AS n FROM kpi_definitions "
                f"WHERE threshold_tier='tier_1_break' AND ticker IN ({marks})",  # nosec B608 -- fixed shape; values stay bound
                frozen,
            ):
                coverage[str(row["ticker"])] = int(row["n"])
        return {
            "portfolio_roster": portfolio_roster,
            "evaluation_roster": evaluation_roster,
            "frozen_tickers": frozen,
            "frozen_tickers_tier1_kpi_definitions": coverage,
            "frozen_tickers_tier1_covered": any(count > 0 for count in coverage.values()),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Artifact assembly
# ---------------------------------------------------------------------------


def _build_artifact(
    *,
    label: str,
    db_info: dict[str, Any],
    git_info: dict[str, Any],
    snapshot_integrity: dict[str, Any],
    parameterization: dict[str, Any],
    flows: dict[str, dict[str, Any]],
    runs: int,
    warmup: int,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "label": label,
        "generated_utc": datetime.now(UTC).isoformat(),
        "harness": {
            "entrypoint": "execution/bench_flows.py",
            "interface": ("--db DB_PATH --label LABEL [--runs N] [--warmup N] [--flows a,b,...]"),
            "python_version": sys.version.split()[0],
            "sqlite_version": sqlite3.sqlite_version,
            "offline": True,
        },
        "measurement": {
            "timed_runs_per_flow": runs,
            "warmup_runs_per_flow": warmup,
            "median": "median of the timed samples per flow",
            "dispersion": "cov (stdev/mean) recorded per flow",
            "comparison_rule": (
                "timed comparisons use the recorded medians; a side whose cov exceeds 0.10 is "
                "inconclusive (validation contract measurement rule 5)"
            ),
            "rerun_noise_band": (
                "a re-run against the same bench DB should reproduce each flow median within "
                "+/-10 percent when cov <= 0.10; each flow's cov is recorded above"
            ),
            "statement_count_protocol": "one traced pass per flow (see per-flow statement_count_protocol)",
            "eqp_protocol": (
                "EXPLAIN QUERY PLAN captured per flow for the top statement shapes by cumulative "
                "trace time, on a governed read-only connection"
            ),
            "tracker_url": (
                f"{_DEAD_TRACKER_URL} (dead loopback so tracker fetches fail closed instantly; "
                "the client's :8000 default is never used)"
            ),
        },
        "git": git_info,
        "database": db_info,
        "original_snapshot_integrity": snapshot_integrity,
        "parameterization": parameterization,
        "flows": flows,
    }


def _write_artifact(output: Path, artifact: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, output)


def _print_summary(label: str, flows: dict[str, dict[str, Any]], output: Path) -> None:
    print(f"bench label: {label}")
    for name in FLOW_NAMES:
        flow = flows.get(name)
        if flow is None:
            continue
        statements = flow.get("statements")
        statement_total = int(statements["total_statements"]) if statements else 0
        print(
            f"  {name:<26} median={flow['median_seconds']:.4f}s "
            f"cov={flow['cov']:.3f} statements={statement_total}"
        )
    paint = flows.get("cockpit_paint_cycle")
    if paint is not None:
        for name, value in paint["units"]["per_request_median_seconds"].items():
            print(f"  paint_cycle.{name:<15} median={value:.4f}s")
    print(f"wrote {output}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="bench database path (the migrated copy)")
    parser.add_argument("--label", help="artifact label; writes .tmp/bench/LABEL.json")
    parser.add_argument("--runs", type=int, default=7, help="timed runs per flow (default 7)")
    parser.add_argument("--warmup", type=int, default=1, help="warmup runs per flow (default 1)")
    parser.add_argument(
        "--flows",
        default=",".join(FLOW_NAMES),
        help=f"comma-separated subset of {FLOW_NAMES}",
    )
    parser.add_argument("--paint-cycle-sample", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.paint_cycle_sample is not None:
        payload: object = json.loads(args.paint_cycle_sample)
        if not isinstance(payload, dict) or "db_path" not in payload:
            print("invalid paint-cycle-sample payload", file=sys.stderr)
            return 2
        typed_payload = cast("dict[str, object]", payload)
        child_db = Path(str(typed_payload["db_path"])).expanduser().resolve()
        _validate_db(child_db)
        _prepare_environment(child_db)
        print(json.dumps(_paint_cycle_sample(child_db, trace=bool(typed_payload.get("trace")))))
        return 0

    if args.db is None or args.label is None:
        parser.error("--db and --label are required")
    label = args.label.strip()
    if not _LABEL_RX.match(label):
        print(
            f"invalid label {label!r}: allowed [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}", file=sys.stderr
        )
        return 2
    if args.runs < 1 or args.runs > 50:
        parser.error("--runs must be between 1 and 50")
    if args.warmup < 0 or args.warmup > 10:
        parser.error("--warmup must be between 0 and 10")
    requested = [name.strip() for name in args.flows.split(",") if name.strip()]
    unknown = [name for name in requested if name not in FLOW_NAMES]
    if unknown:
        print(f"unknown flows {unknown}; known flows: {list(FLOW_NAMES)}", file=sys.stderr)
        return 2

    db_path = args.db.expanduser().resolve()
    _validate_db(db_path)
    _prepare_environment(db_path)

    db_info = _database_identity(db_path)
    if not db_info["schema_at_checkout_head"]:
        print(
            f"bench DB is not at checkout head: db={db_info['alembic_version']} "
            f"code={db_info['checkout_expected_head']}; re-run provisioning",
            file=sys.stderr,
        )
        return 2
    parameterization = _resolve_parameterization(db_path)
    if not parameterization["frozen_tickers_tier1_covered"]:
        print(
            "warning: no frozen ticker carries tier-1 KPI definitions; "
            "the canonical resolution path is not exercised",
            file=sys.stderr,
        )
    context = FlowContext(
        db_path=db_path,
        portfolio_tickers=frozenset(parameterization["portfolio_roster"]),
        frozen_tickers=tuple(parameterization["frozen_tickers"]),
    )
    runners = _flow_runners(context)

    flows: dict[str, dict[str, Any]] = {}
    for name in FLOW_NAMES:
        if name not in requested:
            continue
        print(f"flow: {name} ...", flush=True)
        if name == "cockpit_paint_cycle":
            flows[name] = _measure_paint_cycle(db_path, runs=args.runs, warmup=args.warmup)
        else:
            flows[name] = _measure_component_flow(
                runners[name], db_path, runs=args.runs, warmup=args.warmup
            )

    artifact = _build_artifact(
        label=label,
        db_info=db_info,
        git_info=_git_identity(),
        snapshot_integrity=_snapshot_integrity(),
        parameterization=parameterization,
        flows=flows,
        runs=args.runs,
        warmup=args.warmup,
    )
    output = BENCH_DIR / f"{label}.json"
    _write_artifact(output, artifact)
    _print_summary(label, flows, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
