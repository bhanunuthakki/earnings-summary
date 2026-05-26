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
    """Aggregated quality view for one (purpose, prompt_version) tuple."""

    purpose: str
    prompt_version: str
    score_count: int
    avg_score: float
    min_score: float
    max_score: float


def _open(db_path: Path | str) -> sqlite3.Connection | None:
    p = Path(db_path)
    if not p.exists():
        return None
    try:
        conn = sqlite3.connect(str(p), timeout=5.0)
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


def summarize_by_prompt_version(
    *,
    db_path: Path | str,
    purpose: str | None = None,
    since: datetime | None = None,
) -> list[VersionSummary]:
    """Aggregate calibration scores by (purpose, prompt_version).

    `purpose` filters to one purpose; `since` filters to scores recorded
    after a given timestamp (use to compare "last 30 days under v3" vs
    "all-time average").

    Returns a list sorted purpose ASC, prompt_version DESC so the most
    recent version of each purpose floats to the top of its group.
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
        if since is not None:
            where.append("scored_at >= ?")
            params.append(since.isoformat())
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        rows = conn.execute(
            f"""
            SELECT purpose, prompt_version,
                   COUNT(*) AS score_count,
                   AVG(score) AS avg_score,
                   MIN(score) AS min_score,
                   MAX(score) AS max_score
            FROM prompt_calibration_scores
            {where_sql}
            GROUP BY purpose, prompt_version
            ORDER BY purpose ASC, prompt_version DESC
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    return [
        VersionSummary(
            purpose=str(r["purpose"]),
            prompt_version=str(r["prompt_version"]),
            score_count=int(r["score_count"]),
            avg_score=float(r["avg_score"] or 0.0),
            min_score=float(r["min_score"] or 0.0),
            max_score=float(r["max_score"] or 0.0),
        )
        for r in rows
    ]
