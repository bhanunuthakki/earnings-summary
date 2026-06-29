"""insight_notes store (alembic 0118) — the Ledger synthesis layer's CRUD.

One ``insight_notes`` row is a consolidated, source-grounded artifact (theme /
stance / digest). Writes are supersede-chained: recording a new insight for a
``(scope_key, kind)`` flips the prior 'current' row to 'superseded' (invalidate,
never delete — the bi-temporal posture of the spine). ``watermark_id`` drives
incremental synthesis: a scope whose musings haven't advanced past its watermark
is skipped with zero LLM cost.

``search_musings`` uses the FTS5 index when present (0118), else a LIKE fallback —
retrieval-failure beats hallucination, and the reader never 500s on a DB without
FTS5 compiled in.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from user_state._db import now_iso, open_conn

INSIGHT_KINDS: tuple[str, ...] = ("theme", "stance", "digest")


@dataclass(frozen=True, slots=True)
class InsightRow:
    id: int
    scope_key: str
    kind: str
    body_md: str
    source_note_ids: list[int]
    meta: dict[str, object]
    as_of: str
    watermark_id: int | None
    status: str
    esi: float | None
    provenance: str


def _decode(row: sqlite3.Row) -> InsightRow:
    raw_ids = row["source_note_ids"] or "[]"
    try:
        ids = [int(x) for x in json.loads(raw_ids)]
    except (ValueError, TypeError):
        ids = []
    meta_raw = row["meta_json"]
    meta: dict[str, object] = {}
    if meta_raw:
        try:
            parsed: object = json.loads(meta_raw)
            if isinstance(parsed, dict):
                meta = cast("dict[str, object]", parsed)
        except ValueError:
            meta = {}
    return InsightRow(
        id=int(row["id"]),
        scope_key=str(row["scope_key"]),
        kind=str(row["kind"]),
        body_md=str(row["body_md"]),
        source_note_ids=ids,
        meta=meta,
        as_of=str(row["as_of"]),
        watermark_id=None if row["watermark_id"] is None else int(row["watermark_id"]),
        status=str(row["status"]),
        esi=None if row["esi"] is None else float(row["esi"]),
        provenance=str(row["provenance"]),
    )


def current_watermark(
    scope_key: str, kind: str, *, db_path: Path | str | None = None
) -> int | None:
    """The watermark_id of the current insight for this scope+kind, or None — the
    incremental-synthesis gate (skip when no musing advanced past it)."""
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT watermark_id FROM insight_notes "
            "WHERE scope_key = ? AND kind = ? AND status = 'current' "
            "ORDER BY id DESC LIMIT 1",
            (scope_key, kind),
        ).fetchone()
        return None if row is None or row[0] is None else int(row[0])
    finally:
        conn.close()


def record_insight(
    *,
    scope_key: str,
    kind: str,
    body_md: str,
    source_note_ids: list[int],
    watermark_id: int | None,
    meta: dict[str, object] | None = None,
    esi: float | None = None,
    provenance: str = "derived",
    db_path: Path | str | None = None,
) -> int:
    """Record a new insight, superseding the prior 'current' one for this
    (scope_key, kind). Returns the new row id."""
    if kind not in INSIGHT_KINDS:
        raise ValueError(f"unknown insight kind {kind!r}; expected one of {INSIGHT_KINDS}")
    conn = open_conn(db_path)
    try:
        now = now_iso()
        prior = conn.execute(
            "SELECT id FROM insight_notes WHERE scope_key = ? AND kind = ? AND status = 'current'",
            (scope_key, kind),
        ).fetchone()
        cur = conn.execute(
            """
            INSERT INTO insight_notes
                (scope_key, kind, body_md, source_note_ids, meta_json, as_of,
                 watermark_id, supersedes_id, status, esi, provenance, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'current', ?, ?, ?, ?)
            """,
            (
                scope_key,
                kind,
                body_md,
                json.dumps([int(x) for x in source_note_ids]),
                json.dumps(meta) if meta else None,
                now,
                watermark_id,
                int(prior[0]) if prior else None,
                esi,
                provenance,
                now,
                now,
            ),
        )
        if prior:
            conn.execute(
                "UPDATE insight_notes SET status = 'superseded', updated_at = ? WHERE id = ?",
                (now, int(prior[0])),
            )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def list_insights(
    *,
    kind: str | None = None,
    scope_key: str | None = None,
    status: str = "current",
    db_path: Path | str | None = None,
) -> list[InsightRow]:
    conn = open_conn(db_path)
    try:
        clauses = ["status = ?"]
        params: list[object] = [status]
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if scope_key is not None:
            clauses.append("scope_key = ?")
            params.append(scope_key)
        rows = conn.execute(
            f"SELECT * FROM insight_notes WHERE {' AND '.join(clauses)} ORDER BY scope_key, id DESC",
            params,
        ).fetchall()
        return [_decode(r) for r in rows]
    finally:
        conn.close()


def _has_fts(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='analyst_notes_fts'"
    ).fetchone()
    return row is not None


def search_musings(
    query: str, *, user_id: str = "bhanu", limit: int = 50, db_path: Path | str | None = None
) -> list[int]:
    """Return musing note ids matching ``query`` (BM25 via FTS5 when present, else
    a LIKE fallback). Empty query → []."""
    q = query.strip()
    if not q:
        return []
    conn = open_conn(db_path)
    try:
        if _has_fts(conn):
            try:
                rows = conn.execute(
                    "SELECT n.id FROM analyst_notes_fts f "
                    "JOIN analyst_notes n ON n.id = f.rowid "
                    "WHERE f.analyst_notes_fts MATCH ? AND n.kind = 'musing' "
                    "AND n.user_id = ? ORDER BY bm25(analyst_notes_fts) LIMIT ?",
                    (q, user_id, int(limit)),
                ).fetchall()
                return [int(r[0]) for r in rows]
            except sqlite3.Error:
                pass  # malformed FTS query → fall back to LIKE
        rows = conn.execute(
            "SELECT id FROM analyst_notes WHERE kind = 'musing' AND user_id = ? "
            "AND body LIKE ? ORDER BY id DESC LIMIT ?",
            (user_id, f"%{q}%", int(limit)),
        ).fetchall()
        return [int(r[0]) for r in rows]
    finally:
        conn.close()
