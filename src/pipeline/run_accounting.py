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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import TypeAlias

from models.runs import StageName, StageStatus
from schema_compat import require_current_for_write

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | Sequence["JsonValue"] | Mapping[str, "JsonValue"]

DEFAULT_STALE_AFTER = timedelta(hours=6)
DEFAULT_REAPER_LIMIT = 100


@dataclass(frozen=True)
class PipelineRunSuppressedError(RuntimeError):
    """Raised when the same logical invocation is already running or complete."""

    pipeline_key: str
    attempt_id: str
    status: StageStatus

    def __str__(self) -> str:
        return (
            f"pipeline invocation {self.pipeline_key} suppressed: attempt "
            f"{self.attempt_id} is {self.status.value}; pass force=True to supersede it"
        )


def suppression_payload(exc: PipelineRunSuppressedError) -> dict[str, JsonValue]:
    """Return the stable CLI/scheduler response for an intentional no-op."""
    status = "already_running" if exc.status is StageStatus.IN_PROGRESS else "already_done"
    return {
        "status": status,
        "pipeline_key": exc.pipeline_key,
        "attempt_id": exc.attempt_id,
    }


def _canonical_json_value(value: JsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"pipeline invocation input is not JSON-compatible: {type(value).__name__}")


def make_pipeline_key(
    directive: str,
    ticker_scope: list[str],
    invocation_inputs: Mapping[str, JsonValue] | None = None,
) -> str:
    """Stable identity for one logical invocation, independent of retry attempts.

    ``invocation_inputs`` must contain every material input that can change the
    result (for example an as-of date, source-document ids, or mode flags).
    Values are canonicalized before hashing so mapping and ticker order do not
    change the key.
    """
    canonical = json.dumps(
        {
            "directive": directive.strip(),
            "tickers": sorted({t.strip().upper() for t in ticker_scope if t.strip()}),
            "inputs": _canonical_json_value(invocation_inputs or {}),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"pipeline_{sha256(canonical.encode('utf-8')).hexdigest()[:24]}"


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def make_run_id(directive: str, ticker_scope: list[str]) -> str:
    """Construct a deterministic-ish run_id; uniqueness via uuid suffix."""
    scope = "_".join(sorted(ticker_scope)) if ticker_scope else "ALL"
    short = uuid.uuid4().hex[:8]
    return f"{directive}_{scope}_{datetime.now().strftime('%Y%m%dT%H%M%S')}_{short}"


def _parse_started_at(raw: object) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _iso_datetime(value: datetime | None) -> str | None:
    """Serialize SQLite timestamps explicitly (Python 3.12 removed implicit adapters)."""
    return value.isoformat(timespec="microseconds") if value is not None else None


def _abandon_attempts(
    conn: sqlite3.Connection,
    *,
    attempt_ids: Sequence[str],
    now: datetime,
    reason: str,
) -> list[str]:
    abandoned: list[str] = []
    for attempt_id in attempt_ids:
        changed = conn.execute(
            "UPDATE pipeline_attempts SET ended_at = ?, status = ?, error_summary = ? "
            "WHERE attempt_id = ? AND status = ?",
            (
                _iso_datetime(now),
                StageStatus.ABANDONED.value,
                reason,
                attempt_id,
                StageStatus.IN_PROGRESS.value,
            ),
        ).rowcount
        if not changed:
            continue
        conn.execute(
            "UPDATE ingestion_runs SET ended_at = ?, status = ?, error_summary = ? "
            "WHERE attempt_id = ? AND status = ?",
            (
                _iso_datetime(now),
                StageStatus.ABANDONED.value,
                reason,
                attempt_id,
                StageStatus.IN_PROGRESS.value,
            ),
        )
        abandoned.append(attempt_id)
    return abandoned


def abandon_stale_runs(
    conn: sqlite3.Connection,
    *,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
    limit: int = DEFAULT_REAPER_LIMIT,
    now: datetime | None = None,
) -> list[str]:
    """Mark at most ``limit`` orphaned in-progress attempts as abandoned."""
    require_current_for_write(conn)
    if stale_after <= timedelta(0):
        raise ValueError("stale_after must be positive")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if not {"pipeline_key", "attempt_id"}.issubset(_columns(conn, "ingestion_runs")):
        return []

    current = now or datetime.now()
    cutoff = current - stale_after
    rows = conn.execute(
        "SELECT attempt_id, started_at FROM pipeline_attempts "
        "WHERE status = ? ORDER BY started_at, attempt_id LIMIT ?",
        (StageStatus.IN_PROGRESS.value, limit),
    ).fetchall()
    stale_ids = [
        str(row[0])
        for row in rows
        if (started_at := _parse_started_at(row[1])) is not None and started_at <= cutoff
    ]
    reason = (
        "abandoned stale pipeline attempt; "
        f"exceeded {int(stale_after.total_seconds())}s at {current.isoformat(timespec='seconds')}"
    )
    abandoned = _abandon_attempts(conn, attempt_ids=stale_ids, now=current, reason=reason)
    conn.commit()
    return abandoned


def start_run(
    conn: sqlite3.Connection,
    directive: str,
    ticker_scope: list[str],
    *,
    invocation_inputs: Mapping[str, JsonValue] | None = None,
    force: bool = False,
    deduplicate_completed: bool = False,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
    now: datetime | None = None,
) -> str:
    """Atomically start an attempt, suppressing duplicate logical invocations.

    A fresh in-progress attempt with the same pipeline key is always
    suppressed. Stale attempts are first transitioned to ``abandoned``.
    Callers that provide a complete material-input fingerprint may also set
    ``deduplicate_completed`` to suppress a previously successful invocation.
    ``force`` explicitly abandons a live duplicate and starts a new attempt.
    """
    require_current_for_write(conn)
    run_id = make_run_id(directive, ticker_scope)  # legacy alias for the attempt id
    current = now or datetime.now()
    scope = json.dumps(sorted({ticker.upper() for ticker in ticker_scope}))
    columns = _columns(conn, "ingestion_runs")
    if {"pipeline_key", "attempt_id"}.issubset(columns):
        if stale_after <= timedelta(0):
            raise ValueError("stale_after must be positive")
        pipeline_key = make_pipeline_key(directive, ticker_scope, invocation_inputs)
        try:
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            active_rows = conn.execute(
                "SELECT attempt_id, started_at FROM pipeline_attempts "
                "WHERE pipeline_key = ? AND status = ? ORDER BY started_at, attempt_id",
                (pipeline_key, StageStatus.IN_PROGRESS.value),
            ).fetchall()
            cutoff = current - stale_after
            stale_ids = [
                str(row[0])
                for row in active_rows
                if (started_at := _parse_started_at(row[1])) is not None and started_at <= cutoff
            ]
            if stale_ids:
                _abandon_attempts(
                    conn,
                    attempt_ids=stale_ids[:DEFAULT_REAPER_LIMIT],
                    now=current,
                    reason=(
                        "abandoned stale pipeline attempt before retry; "
                        f"exceeded {int(stale_after.total_seconds())}s"
                    ),
                )

            active = conn.execute(
                "SELECT attempt_id FROM pipeline_attempts "
                "WHERE pipeline_key = ? AND status = ? ORDER BY started_at DESC LIMIT 1",
                (pipeline_key, StageStatus.IN_PROGRESS.value),
            ).fetchone()
            if active is not None and not force:
                conn.rollback()
                raise PipelineRunSuppressedError(
                    pipeline_key, str(active[0]), StageStatus.IN_PROGRESS
                )
            if active is not None:
                _abandon_attempts(
                    conn,
                    attempt_ids=[str(active[0])],
                    now=current,
                    reason=f"superseded by forced attempt {run_id}",
                )

            if deduplicate_completed and not force:
                completed = conn.execute(
                    "SELECT attempt_id FROM pipeline_attempts "
                    "WHERE pipeline_key = ? AND status = ? ORDER BY ended_at DESC LIMIT 1",
                    (pipeline_key, StageStatus.OK.value),
                ).fetchone()
                if completed is not None:
                    conn.rollback()
                    raise PipelineRunSuppressedError(
                        pipeline_key, str(completed[0]), StageStatus.OK
                    )

            # The normalized tables are additive. Keep the legacy ledger populated
            # for readers that have not yet dual-read the new records.
            conn.execute(
                "INSERT OR IGNORE INTO pipeline_runs "
                "(pipeline_key, directive, ticker_scope, first_started_at) VALUES (?, ?, ?, ?)",
                (pipeline_key, directive, scope, _iso_datetime(current)),
            )
            conn.execute(
                "INSERT INTO pipeline_attempts "
                "(attempt_id, pipeline_key, started_at, ended_at, status, error_summary) "
                "VALUES (?, ?, ?, NULL, ?, NULL)",
                (run_id, pipeline_key, _iso_datetime(current), StageStatus.IN_PROGRESS.value),
            )
            conn.execute(
                "INSERT INTO ingestion_runs "
                "(run_id, attempt_id, pipeline_key, started_at, ended_at, directive, "
                "ticker_scope, status, error_summary) "
                "VALUES (?, ?, ?, ?, NULL, ?, ?, ?, NULL)",
                (
                    run_id,
                    run_id,
                    pipeline_key,
                    _iso_datetime(current),
                    directive,
                    scope,
                    StageStatus.IN_PROGRESS.value,
                ),
            )
            conn.commit()
        except PipelineRunSuppressedError:
            raise
        except Exception:
            conn.rollback()
            raise
    else:
        conn.execute(
            "INSERT INTO ingestion_runs "
            "(run_id, started_at, ended_at, directive, ticker_scope, status, error_summary) "
            "VALUES (?, ?, NULL, ?, ?, ?, NULL)",
            (run_id, _iso_datetime(current), directive, scope, StageStatus.IN_PROGRESS.value),
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
            _iso_datetime(period_end),
            stage.value,
            status.value,
            _iso_datetime(started_at if started_at is not None else now),
            _iso_datetime(now if is_terminal else None),
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
                _iso_datetime(period_end),
                stage.value,
                status.value,
                _iso_datetime(started_at if started_at is not None else now),
                _iso_datetime(now if is_terminal else None),
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
    """Finish a live attempt without reviving one already marked abandoned."""
    require_current_for_write(conn)
    conn.execute(
        "UPDATE ingestion_runs SET ended_at = ?, status = ?, error_summary = ? "
        "WHERE run_id = ? AND status = ?",
        (
            _iso_datetime(datetime.now()),
            status.value,
            error_summary,
            run_id,
            StageStatus.IN_PROGRESS.value,
        ),
    )
    if {"pipeline_key", "attempt_id"}.issubset(_columns(conn, "ingestion_runs")):
        conn.execute(
            "UPDATE pipeline_attempts SET ended_at = ?, status = ?, error_summary = ? "
            "WHERE attempt_id = ? AND status = ?",
            (
                _iso_datetime(datetime.now()),
                status.value,
                error_summary,
                run_id,
                StageStatus.IN_PROGRESS.value,
            ),
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
            (run_id, ticker.upper(), _iso_datetime(period_end)),
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
