"""Read/write API for discovery_candidates (alembic 0081).

The persistence half of the discovery pipelines (master build P5.3): one
row per (user, ticker) surfaced, carrying the "why surfaced" evidence the
queue UI renders. Re-running discovery is an UPSERT that refreshes
evidence / score / name / last_seen_at but NEVER touches status — the
lifecycle (new → queued → building → built, or dismissed) belongs to the
owner via the P5.4 queue, and a dismissed name must stay dismissed across
every future run.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from identity import DEFAULT_USER_ID
from user_state._db import now_iso, open_conn, parse_dt

CANDIDATE_STATUSES: tuple[str, ...] = ("new", "queued", "building", "built", "dismissed")


@dataclass(slots=True)
class CandidateRow:
    """One row of discovery_candidates, fully decoded."""

    id: int
    user_id: str
    ticker: str
    name: str | None
    status: str
    score: float
    evidence: list[dict[str, object]]
    first_seen_at: datetime
    last_seen_at: datetime
    updated_at: datetime


def upsert_candidate(
    *,
    ticker: str,
    name: str | None,
    score: float,
    evidence: list[dict[str, object]],
    user_id: str = DEFAULT_USER_ID,
    db_path: Path | str | None = None,
) -> CandidateRow:
    """INSERT a fresh candidate, or refresh an existing (user, ticker) row's
    evidence/score/name/last_seen_at. Status is never written on conflict."""
    symbol = ticker.strip().upper()
    if not symbol:
        raise ValueError("candidate ticker must be non-empty")
    now = now_iso()
    conn = open_conn(db_path)
    try:
        conn.execute(
            """
            INSERT INTO discovery_candidates
                (user_id, ticker, name, status, score, evidence_json,
                 first_seen_at, last_seen_at, updated_at)
            VALUES (?, ?, ?, 'new', ?, ?, ?, ?, ?)
            ON CONFLICT (user_id, ticker) DO UPDATE SET
              name = excluded.name,
              score = excluded.score,
              evidence_json = excluded.evidence_json,
              last_seen_at = excluded.last_seen_at,
              updated_at = excluded.updated_at
            """,
            (user_id, symbol, name, score, json.dumps(evidence), now, now, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM discovery_candidates WHERE user_id = ? AND ticker = ?",
            (user_id, symbol),
        ).fetchone()
        if row is None:  # pragma: no cover - upsert guarantees presence
            raise LookupError(f"discovery_candidates ({user_id!r}, {symbol!r}) missing")
        return _row_to_dc(row)
    finally:
        conn.close()


def list_candidates(
    *,
    user_id: str = DEFAULT_USER_ID,
    status: str | None = None,
    limit: int = 500,
    db_path: Path | str | None = None,
) -> list[CandidateRow]:
    """Candidates ranked by score (then recency). ``status=None`` returns the
    live inbox — everything except dismissed; pass a status for one bucket."""
    if status is not None and status not in CANDIDATE_STATUSES:
        raise ValueError(f"status must be one of {CANDIDATE_STATUSES}, got {status!r}")
    clauses = ["user_id = ?"]
    params: list[object] = [user_id]
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    else:
        clauses.append("status != 'dismissed'")
    params.append(int(limit))
    conn = open_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM discovery_candidates WHERE "
            + " AND ".join(clauses)
            + " ORDER BY score DESC, last_seen_at DESC, ticker ASC LIMIT ?",
            params,
        ).fetchall()
        return [_row_to_dc(r) for r in rows]
    finally:
        conn.close()


def get_candidate(candidate_id: int, *, db_path: Path | str | None = None) -> CandidateRow | None:
    """One candidate by id, or None."""
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM discovery_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        return None if row is None else _row_to_dc(row)
    finally:
        conn.close()


def set_status(
    candidate_id: int,
    status: str,
    *,
    db_path: Path | str | None = None,
) -> CandidateRow | None:
    """Move a candidate through its lifecycle. Returns the row, or None when
    the id is unknown. Raises ValueError on a status outside the CHECK set."""
    if status not in CANDIDATE_STATUSES:
        raise ValueError(f"status must be one of {CANDIDATE_STATUSES}, got {status!r}")
    conn = open_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE discovery_candidates SET status = ?, updated_at = ? WHERE id = ?",
            (status, now_iso(), candidate_id),
        )
        if cur.rowcount == 0:
            return None
        conn.commit()
        row = conn.execute(
            "SELECT * FROM discovery_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        return None if row is None else _row_to_dc(row)
    finally:
        conn.close()


def _row_to_dc(row: sqlite3.Row) -> CandidateRow:
    evidence: list[dict[str, object]] = []
    try:
        parsed: object = json.loads(str(row["evidence_json"]))
    except ValueError:  # pragma: no cover - json_valid CHECK forbids this
        parsed = None
    if isinstance(parsed, list):
        evidence = [
            cast("dict[str, object]", e)
            for e in cast("list[object]", parsed)
            if isinstance(e, dict)
        ]
    raw_name = row["name"]
    return CandidateRow(
        id=int(row["id"]),
        user_id=str(row["user_id"]),
        ticker=str(row["ticker"]),
        name=None if raw_name is None else str(raw_name),
        status=str(row["status"]),
        score=float(row["score"]),
        evidence=evidence,
        first_seen_at=parse_dt(row["first_seen_at"]),
        last_seen_at=parse_dt(row["last_seen_at"]),
        updated_at=parse_dt(row["updated_at"]),
    )
