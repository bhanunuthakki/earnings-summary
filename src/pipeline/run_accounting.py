"""Pipeline run accounting — write ingestion_runs and stage_transitions rows.

Every CLI invocation that ingests, parses, validates, persists, computes,
synthesizes, or publishes calls `start_run` once at the top, `record_stage`
per (ticker, period, stage) transition, and `end_run` at the bottom. On
failure, `end_run` is called with status=FAILED and the error_summary; the
run_id can be resumed via `latest_stage_for`.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from hashlib import sha256

from models.runs import StageName, StageStatus
from schema_compat import require_current_for_write


def make_pipeline_key(directive: str, ticker_scope: list[str]) -> str:
    """Stable identity for the logical work, independent of retry attempts."""
    canonical = json.dumps(
        {"directive": directive.strip(), "tickers": sorted({t.upper() for t in ticker_scope})},
        separators=(",", ":"),
    )
    return f"pipeline_{sha256(canonical.encode('utf-8')).hexdigest()[:24]}"


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def make_run_id(directive: str, ticker_scope: list[str]) -> str:
    """Construct a deterministic-ish run_id; uniqueness via uuid suffix."""
    scope = "_".join(sorted(ticker_scope)) if ticker_scope else "ALL"
    short = uuid.uuid4().hex[:8]
    return f"{directive}_{scope}_{datetime.now().strftime('%Y%m%dT%H%M%S')}_{short}"


def start_run(
    conn: sqlite3.Connection,
    directive: str,
    ticker_scope: list[str],
) -> str:
    """Insert an ingestion_runs row with status=in_progress; return the run_id."""
    require_current_for_write(conn)
    run_id = make_run_id(directive, ticker_scope)  # legacy alias for the attempt id
    now = datetime.now()
    scope = json.dumps(sorted({ticker.upper() for ticker in ticker_scope}))
    columns = _columns(conn, "ingestion_runs")
    if {"pipeline_key", "attempt_id"}.issubset(columns):
        pipeline_key = make_pipeline_key(directive, ticker_scope)
        # The normalized tables are additive. Keep the legacy ledger populated
        # for readers that have not yet dual-read the new records.
        conn.execute(
            "INSERT OR IGNORE INTO pipeline_runs (pipeline_key, directive, ticker_scope, first_started_at) "
            "VALUES (?, ?, ?, ?)",
            (pipeline_key, directive, scope, now),
        )
        conn.execute(
            "INSERT INTO pipeline_attempts (attempt_id, pipeline_key, started_at, ended_at, status, error_summary) "
            "VALUES (?, ?, ?, NULL, ?, NULL)",
            (run_id, pipeline_key, now, StageStatus.IN_PROGRESS.value),
        )
        conn.execute(
            "INSERT INTO ingestion_runs "
            "(run_id, attempt_id, pipeline_key, started_at, ended_at, directive, ticker_scope, status, error_summary) "
            "VALUES (?, ?, ?, ?, NULL, ?, ?, ?, NULL)",
            (run_id, run_id, pipeline_key, now, directive, scope, StageStatus.IN_PROGRESS.value),
        )
    else:
        conn.execute(
            "INSERT INTO ingestion_runs "
            "(run_id, started_at, ended_at, directive, ticker_scope, status, error_summary) "
            "VALUES (?, ?, NULL, ?, ?, ?, NULL)",
            (run_id, now, directive, scope, StageStatus.IN_PROGRESS.value),
        )
    conn.commit()
    return run_id


def record_stage(
    conn: sqlite3.Connection,
    run_id: str,
    ticker: str,
    stage: StageName,
    status: StageStatus,
    period_end: datetime | None = None,
    error_msg: str | None = None,
    started_at: datetime | None = None,
) -> None:
    """Insert one stage_transitions row.

    For OK / SKIPPED / NEEDS_REVIEW / FAILED: sets ended_at = now.
    For IN_PROGRESS: sets ended_at = NULL.
    """
    require_current_for_write(conn)
    now = datetime.now()
    is_terminal = status is not StageStatus.IN_PROGRESS and status is not StageStatus.NOT_STARTED
    conn.execute(
        "INSERT INTO stage_transitions "
        "(run_id, ticker, period_end, stage, status, started_at, ended_at, error_msg) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            ticker.upper(),
            period_end,
            stage.value,
            status.value,
            started_at if started_at is not None else now,
            now if is_terminal else None,
            error_msg,
        ),
    )
    if _columns(conn, "stage_transitions") and _columns(conn, "ingestion_runs") >= {
        "pipeline_key",
        "attempt_id",
    }:
        # This FK-backed journal is the durable source for new writers; the
        # original stage_transitions row remains the compatibility projection.
        conn.execute(
            "INSERT INTO pipeline_stage_transitions "
            "(attempt_id, ticker, period_end, stage, status, started_at, ended_at, error_msg) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                ticker.upper(),
                period_end,
                stage.value,
                status.value,
                started_at if started_at is not None else now,
                now if is_terminal else None,
                error_msg,
            ),
        )
    conn.commit()


def end_run(
    conn: sqlite3.Connection,
    run_id: str,
    status: StageStatus,
    error_summary: str | None = None,
) -> None:
    """Update ingestion_runs.ended_at + status for the given run_id."""
    require_current_for_write(conn)
    conn.execute(
        "UPDATE ingestion_runs SET ended_at = ?, status = ?, error_summary = ? WHERE run_id = ?",
        (datetime.now(), status.value, error_summary, run_id),
    )
    if {"pipeline_key", "attempt_id"}.issubset(_columns(conn, "ingestion_runs")):
        conn.execute(
            "UPDATE pipeline_attempts SET ended_at = ?, status = ?, error_summary = ? WHERE attempt_id = ?",
            (datetime.now(), status.value, error_summary, run_id),
        )
    conn.commit()


def latest_stage_for(
    conn: sqlite3.Connection,
    run_id: str,
    ticker: str,
    period_end: datetime | None = None,
) -> tuple[StageName, StageStatus] | None:
    """Return the most-recent (stage, status) for (run_id, ticker, period_end), or None."""
    cur = conn.cursor()
    if period_end is None:
        cur.execute(
            "SELECT stage, status FROM stage_transitions "
            "WHERE run_id = ? AND ticker = ? AND period_end IS NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (run_id, ticker.upper()),
        )
    else:
        cur.execute(
            "SELECT stage, status FROM stage_transitions "
            "WHERE run_id = ? AND ticker = ? AND period_end = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (run_id, ticker.upper(), period_end),
        )
    row = cur.fetchone()
    if row is None:
        return None
    return (StageName(row["stage"]), StageStatus(row["status"]))


def stages_not_ok_for(
    conn: sqlite3.Connection,
    run_id: str,
) -> list[tuple[str, datetime | None, StageName, StageStatus]]:
    """Return all stage transitions in this run whose status != OK.

    Used by `--resume` to identify where a failed run left off.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT ticker, period_end, stage, status FROM stage_transitions "
        "WHERE run_id = ? AND status != ? "
        "ORDER BY started_at",
        (run_id, StageStatus.OK.value),
    )
    out: list[tuple[str, datetime | None, StageName, StageStatus]] = []
    for row in cur.fetchall():
        out.append(
            (
                row["ticker"],
                row["period_end"],
                StageName(row["stage"]),
                StageStatus(row["status"]),
            )
        )
    return out
