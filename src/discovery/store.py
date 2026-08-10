"""Read/write API for discovery_candidates + discovery_signals (alembic
0081 / 0094 / 0095).

The persistence half of the discovery pipelines (master build P5.3): one
row per (user, ticker) surfaced, carrying the "why surfaced" evidence the
queue UI renders. Re-running discovery is an UPSERT that refreshes
evidence / score / score_json / name / last_seen_at but NEVER touches status
— the lifecycle (new → queued → building → built, or dismissed) belongs to
the owner via the P5.4 queue, and a dismissed name must stay dismissed across
every future run.

``score`` is now a weighted sum over the candidate's ``discovery_signals``
rows (``scoring.py``), and ``score_json`` holds that score's ``score_why``
breakdown. Each signal producer (the screen+adjacency run, the 13F miner)
fully replaces ITS signal classes each run via :func:`replace_signals`, so a
name that stops firing a screen loses that signal without disturbing another
producer's classes.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import cast

from identity import DEFAULT_USER_ID
from user_state._db import now_iso, open_conn, parse_dt

CANDIDATE_STATUSES: tuple[str, ...] = ("new", "queued", "building", "built", "dismissed")

# Candidates a build may start from. Anything else is either already done
# (built), in flight (building), or explicitly rejected (dismissed). Shared
# by the /discovery chat command (ask.commands) and the REST build routes.
BUILDABLE_STATUSES: frozenset[str] = frozenset({"new", "queued"})


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
    score_json: dict[str, object] | None = None


@dataclass(slots=True)
class SignalRow:
    """One row of discovery_signals, fully decoded (``meta`` parsed)."""

    id: int
    user_id: str
    ticker: str
    signal_class: str
    source_key: str
    weight: float
    raw_strength: float
    observed_at: str
    detail: str | None
    meta: dict[str, object]
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class SignalWrite:
    """The fields :func:`replace_signals` persists — a producer's view of one
    typed signal before it gets an id/timestamps."""

    ticker: str
    signal_class: str
    source_key: str
    weight: float
    raw_strength: float
    observed_at: str
    detail: str | None = None
    meta: dict[str, object] = field(default_factory=dict[str, object])


def _user_in(user_id: str) -> tuple[str, list[object]]:
    """A tenant clause tolerant of the historical user-id drift (TEXT 'bhanu',
    TEXT '1', and the no-affinity integer 1 all coexist in prod — see the
    tenant-userid-drift incident). Writes go under ``user_id``; reads match the
    tolerant set."""
    return "user_id IN (?, ?, ?)", [user_id, "1", 1]


def upsert_candidate(
    *,
    ticker: str,
    name: str | None,
    score: float,
    evidence: list[dict[str, object]],
    score_json: dict[str, object] | None = None,
    user_id: str = DEFAULT_USER_ID,
    db_path: Path | str | None = None,
) -> CandidateRow:
    """INSERT a fresh candidate, or refresh an existing (user, ticker) row's
    evidence/score/score_json/name/last_seen_at. Status is never written on
    conflict."""
    symbol = ticker.strip().upper()
    if not symbol:
        raise ValueError("candidate ticker must be non-empty")
    now = now_iso()
    score_blob = None if score_json is None else json.dumps(score_json)
    conn = open_conn(db_path)
    try:
        conn.execute(
            """
            INSERT INTO discovery_candidates
                (user_id, ticker, name, status, score, evidence_json, score_json,
                 first_seen_at, last_seen_at, updated_at)
            VALUES (?, ?, ?, 'new', ?, ?, ?, ?, ?, ?)
            ON CONFLICT (user_id, ticker) DO UPDATE SET
              name = excluded.name,
              score = excluded.score,
              evidence_json = excluded.evidence_json,
              score_json = excluded.score_json,
              last_seen_at = excluded.last_seen_at,
              updated_at = excluded.updated_at
            """,
            (user_id, symbol, name, score, json.dumps(evidence), score_blob, now, now, now),
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


def existing_candidate_tickers(
    *, user_id: str = DEFAULT_USER_ID, db_path: Path | str | None = None
) -> set[str]:
    """Every (user) ticker already in discovery_candidates, any status. The
    entry-threshold gate consults this: a NEW name must clear the bar to be
    inserted, but an EXISTING candidate is always refreshed (so its score and
    lifecycle stay current even when it slips below the bar)."""
    clause, params = _user_in(user_id)
    conn = open_conn(db_path)
    try:
        rows = conn.execute(
            f"SELECT ticker FROM discovery_candidates WHERE {clause}", params
        ).fetchall()
        return {str(r[0]).upper() for r in rows}
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


def get_candidate_by_ticker(
    ticker: str, *, user_id: str = DEFAULT_USER_ID, db_path: Path | str | None = None
) -> CandidateRow | None:
    """One candidate by ticker (any status, including dismissed) — the
    Compare peek's need_rank lookup, where a name might not be in the live
    inbox at all. ``None`` on an unknown ticker or a missing DB (never a
    crash — the peek degrades to "not computed" for that column)."""
    symbol = ticker.strip().upper()
    clause, params = _user_in(user_id)
    try:
        conn = open_conn(db_path)
    except (FileNotFoundError, RuntimeError):
        return None
    try:
        row = conn.execute(
            f"SELECT * FROM discovery_candidates WHERE {clause} AND ticker = ?",
            [*params, symbol],
        ).fetchone()
        return None if row is None else _row_to_dc(row)
    except sqlite3.Error:
        return None
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


def promote_to_watchlist(
    *,
    ticker: str,
    name: str | None,
    user_id: str = DEFAULT_USER_ID,
    db_path: Path | str | None = None,
) -> bool:
    """The Watch action (PRD §8.2, P1-B): index_member/none -> watchlist.

    Mirrors ``discovery_build._promote_to_evaluation``'s shape (a direct
    ``tracked_companies`` UPDATE, not ``db.track_company`` — Watch is a
    lightweight "keep an eye on this", not a full onboard) with one addition:
    a raw Discovery name that has never been tracked (investor-only 13F
    surface, no screen/adjacency hit) gets a minimal INSERT rather than a
    no-op UPDATE. Evaluation coverage is downgraded to monitored watchlist;
    a portfolio holding remains portfolio-authoritative.
    The discovery candidate's own status/lifecycle is NEVER touched here;
    the queue disposition and the tracked-universe membership are separate
    concerns. Returns False only on a missing/unreadable DB."""
    symbol = ticker.strip().upper()
    try:
        conn = open_conn(db_path)
    except (FileNotFoundError, RuntimeError):
        return False
    try:
        row = conn.execute(
            "SELECT list_type, archived_at FROM tracked_companies WHERE user_id = ? AND ticker = ?",
            (user_id, symbol),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO tracked_companies "
                "(user_id, ticker, name, list_type, processing_tier, brief_dirty) "
                "VALUES (?, ?, ?, 'watchlist', 'P2', 0)",
                (user_id, symbol, name or symbol),
            )
        elif str(row[0]) != "portfolio":
            conn.execute(
                "UPDATE tracked_companies SET list_type = 'watchlist', "
                "processing_tier = 'P2', brief_dirty = 0, archived_at = NULL "
                "WHERE user_id = ? AND ticker = ?",
                (user_id, symbol),
            )
        elif row[1] is not None:
            conn.execute(
                "UPDATE tracked_companies SET archived_at = NULL, processing_tier = 'P1', "
                "brief_dirty = 1 "
                "WHERE user_id = ? AND ticker = ?",
                (user_id, symbol),
            )
        conn.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _decode_json_obj(raw: object) -> dict[str, object] | None:
    if raw is None:
        return None
    try:
        parsed: object = json.loads(str(raw))
    except ValueError:  # pragma: no cover - json_valid CHECK forbids this
        return None
    return cast("dict[str, object]", parsed) if isinstance(parsed, dict) else None


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
    # score_json predates only pre-0095 DBs; tolerate its absence. (`in row`
    # would scan VALUES, not column names — sqlite3.Row iterates its values — so
    # probe the explicit key list.)
    columns = row.keys()
    score_json = _decode_json_obj(row["score_json"]) if "score_json" in columns else None
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
        score_json=score_json,
    )


# ---------------------------------------------------------------------------
# discovery_signals (alembic 0094): the typed, weighted child rows
# ---------------------------------------------------------------------------


def _signal_row_to_dc(row: sqlite3.Row) -> SignalRow:
    return SignalRow(
        id=int(row["id"]),
        user_id=str(row["user_id"]),
        ticker=str(row["ticker"]),
        signal_class=str(row["signal_class"]),
        source_key=str(row["source_key"]),
        weight=float(row["weight"]),
        raw_strength=float(row["raw_strength"]),
        observed_at=str(row["observed_at"]),
        detail=None if row["detail"] is None else str(row["detail"]),
        meta=_decode_json_obj(row["meta_json"]) or {},
        created_at=parse_dt(row["created_at"]),
        updated_at=parse_dt(row["updated_at"]),
    )


def replace_signals(
    signals: Iterable[SignalWrite],
    *,
    classes: Iterable[str],
    user_id: str = DEFAULT_USER_ID,
    db_path: Path | str | None = None,
) -> int:
    """Replace this user's signals for the given ``classes`` with ``signals``,
    in one transaction. Each producer owns its classes and fully refreshes them
    each run: the screen+adjacency run replaces ``{'screen','adjacency'}``, the
    13F miner replaces ``{'investor_13f'}`` — so a name that stops firing a
    screen loses that row without touching the investor rows (and vice versa).
    Returns the number of rows inserted."""
    class_set = sorted(set(classes))
    if not class_set:
        return 0
    rows = list(signals)
    now = now_iso()
    conn = open_conn(db_path)
    try:
        marks = ",".join("?" * len(class_set))
        conn.execute(
            f"DELETE FROM discovery_signals WHERE user_id = ? AND signal_class IN ({marks})",
            [user_id, *class_set],
        )
        conn.executemany(
            """
            INSERT INTO discovery_signals
                (user_id, ticker, signal_class, source_key, weight, raw_strength,
                 observed_at, detail, meta_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    user_id,
                    s.ticker.strip().upper(),
                    s.signal_class,
                    s.source_key,
                    float(s.weight),
                    float(s.raw_strength),
                    s.observed_at,
                    s.detail,
                    json.dumps(s.meta) if s.meta else None,
                    now,
                    now,
                )
                for s in rows
                if s.signal_class in class_set
            ],
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def list_signals(
    ticker: str, *, user_id: str = DEFAULT_USER_ID, db_path: Path | str | None = None
) -> list[SignalRow]:
    """Every typed signal behind one candidate (the panel's evidence peek),
    newest event first. Tolerant of the tenant user-id drift."""
    clause, params = _user_in(user_id)
    conn = open_conn(db_path)
    try:
        rows = conn.execute(
            f"SELECT * FROM discovery_signals WHERE {clause} AND ticker = ? "
            "ORDER BY observed_at DESC, signal_class, source_key",
            [*params, ticker.strip().upper()],
        ).fetchall()
        return [_signal_row_to_dc(r) for r in rows]
    finally:
        conn.close()


def signals_by_ticker(
    signal_class: str, *, user_id: str = DEFAULT_USER_ID, db_path: Path | str | None = None
) -> dict[str, list[SignalRow]]:
    """All signals of one class, grouped by ticker — the scorer (run_discovery)
    loads another producer's class (e.g. the 13F miner's ``investor_13f`` rows)
    to score them alongside the fundamentals it just computed. Degrades to ``{}``
    on a missing DB / pre-0096 schema."""
    clause, params = _user_in(user_id)
    try:
        conn = open_conn(db_path)
    except (sqlite3.Error, FileNotFoundError, RuntimeError):
        return {}
    try:
        rows = conn.execute(
            f"SELECT * FROM discovery_signals WHERE {clause} AND signal_class = ?",
            [*params, signal_class],
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    out: dict[str, list[SignalRow]] = {}
    for r in rows:
        out.setdefault(str(r["ticker"]).upper(), []).append(_signal_row_to_dc(r))
    return out
