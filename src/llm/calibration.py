"""Prompt-quality calibration tracker.

Graders (`grade_bear_cases`, `grade_decisions`, future `grade_qa_topics`)
write to this module when they produce a quality score for an LLM artifact.
Aggregation queries group by (purpose, prompt_version) so the analyst can
answer "is the v3 bear_case prompt actually better than v2?" without
spelunking the per-call ledger.

The audit foundation lives in alembic 0058_prompt_calibration_scores;
this module is the read/write API.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CalibrationScore:
    """One scored output. `score` is the analyst-decided value (convention:
    0.0..1.0 where 1.0 is "exactly what we wanted")."""

    purpose: str
    prompt_version: str
    score: float
    ticker: str | None = None
    reason: str | None = None
    scored_by: str | None = None
    artifact_id: int | None = None


@dataclass(frozen=True)
class VersionSummary:
    """Aggregated quality view for one (purpose, prompt_version) tuple.

    Percentiles use linear interpolation. ``last_scored_at`` is the ISO
    timestamp of the most recent row in the group (None for empty groups,
    which the caller never sees because aggregation drops them).
    """

    purpose: str
    prompt_version: str
    score_count: int
    avg_score: float
    min_score: float
    max_score: float
    p25: float
    p50: float
    p75: float
    last_scored_at: str | None


def _open(db_path: Path | str) -> sqlite3.Connection | None:
    p = Path(db_path)
    if not p.exists():
        return None
    try:
        conn = connect_sqlite(
            p,
            role=SQLiteConnectionRole.WRITER,
            # Calibration is an optional compatibility bridge introduced long
            # before the current head; _table_exists gates the narrow write.
            schema_preflight=False,
        )
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as exc:
        log.warning({"event": "calibration_open_failed", "error": str(exc)})
        return None


def _table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='prompt_calibration_scores' LIMIT 1"
    ).fetchone()
    return row is not None


def record_score(score: CalibrationScore, *, db_path: Path | str) -> int | None:
    """Insert one calibration row. Returns the new row id, or None when the
    DB or table is missing (synthetic envs without migration 0058).

    Best-effort: a write failure must never break the grader's main job
    (producing the score itself). Failures land in the log.
    """
    conn = _open(db_path)
    if conn is None:
        return None
    try:
        if not _table_exists(conn):
            return None
        cur = conn.execute(
            """
            INSERT INTO prompt_calibration_scores
              (purpose, prompt_version, ticker, score, reason,
               scored_at, scored_by, artifact_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                score.purpose,
                score.prompt_version,
                score.ticker,
                float(score.score),
                score.reason,
                datetime.utcnow().isoformat(),
                score.scored_by,
                score.artifact_id,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    except sqlite3.Error as exc:
        log.warning({"event": "calibration_record_failed", "error": str(exc)})
        return None
    finally:
        conn.close()


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile. ``pct`` is 0..100. Caller guarantees
    ``values`` is non-empty (every group SUM into here has score_count >= 1)."""
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def summarize_by_prompt_version(
    *,
    db_path: Path | str,
    purpose: str | None = None,
    ticker: str | None = None,
    since: datetime | None = None,
) -> list[VersionSummary]:
    """Aggregate calibration scores by (purpose, prompt_version).

    `purpose` / `ticker` filter to one slice; `since` filters to scores
    recorded after a given timestamp (use to compare "last 30 days under
    v3" vs "all-time average").

    Returns a list sorted purpose ASC, prompt_version DESC so the most
    recent version of each purpose floats to the top of its group. Each
    summary carries count/avg/min/max plus p25/p50/p75 and the latest
    ``scored_at`` ISO timestamp in the group.
    """
    conn = _open(db_path)
    if conn is None or not _table_exists(conn):
        return []
    try:
        where: list[str] = []
        params: list[object] = []
        if purpose is not None:
            where.append("purpose = ?")
            params.append(purpose)
        if ticker is not None:
            where.append("ticker = ?")
            params.append(ticker)
        if since is not None:
            where.append("scored_at >= ?")
            params.append(since.isoformat())
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        rows = conn.execute(
            f"""
            SELECT purpose, prompt_version, score, scored_at
            FROM prompt_calibration_scores
            {where_sql}
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    groups: dict[tuple[str, str], list[tuple[float, str | None]]] = {}
    for r in rows:
        key = (str(r["purpose"]), str(r["prompt_version"]))
        scored_at = r["scored_at"]
        groups.setdefault(key, []).append(
            (float(r["score"]), str(scored_at) if scored_at is not None else None)
        )

    summaries: list[VersionSummary] = []
    for (g_purpose, g_version), entries in groups.items():
        scores = [s for s, _ in entries]
        timestamps = [t for _, t in entries if t is not None]
        summaries.append(
            VersionSummary(
                purpose=g_purpose,
                prompt_version=g_version,
                score_count=len(scores),
                avg_score=sum(scores) / len(scores),
                min_score=min(scores),
                max_score=max(scores),
                p25=_percentile(scores, 25.0),
                p50=_percentile(scores, 50.0),
                p75=_percentile(scores, 75.0),
                last_scored_at=max(timestamps) if timestamps else None,
            )
        )

    # Two-pass stable sort: prompt_version DESC first, then purpose ASC.
    # Python's sort is stable so the second sort preserves the first's order
    # inside each purpose group. The DESC ordering inside a purpose lets the
    # current (highest-named) version sit at the top of its group.
    summaries.sort(key=lambda s: s.prompt_version, reverse=True)
    summaries.sort(key=lambda s: s.purpose)
    return summaries


def daily_avg_scores(
    *,
    db_path: Path | str,
    since: datetime,
    purpose: str | None = None,
    ticker: str | None = None,
) -> dict[tuple[str, str], list[tuple[str, float]]]:
    """Per-(purpose, prompt_version) list of (date_iso, avg_score) for the window.

    Used by the dashboard sparkline. Returns an empty dict when the DB / table
    is missing so the consumer can degrade gracefully.
    """
    conn = _open(db_path)
    if conn is None or not _table_exists(conn):
        return {}
    try:
        where: list[str] = ["scored_at >= ?"]
        params: list[object] = [since.isoformat()]
        if purpose is not None:
            where.append("purpose = ?")
            params.append(purpose)
        if ticker is not None:
            where.append("ticker = ?")
            params.append(ticker)
        where_sql = "WHERE " + " AND ".join(where)
        rows = conn.execute(
            f"""
            SELECT purpose, prompt_version, substr(scored_at, 1, 10) AS day,
                   AVG(score) AS avg_score
            FROM prompt_calibration_scores
            {where_sql}
            GROUP BY purpose, prompt_version, day
            ORDER BY purpose, prompt_version, day
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    out: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for r in rows:
        key = (str(r["purpose"]), str(r["prompt_version"]))
        out.setdefault(key, []).append((str(r["day"]), float(r["avg_score"])))
    return out
