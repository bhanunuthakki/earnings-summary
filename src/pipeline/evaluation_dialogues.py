"""Bounded, read-only candidates for the Portfolio Copilot evaluation dialog.

This joins only persisted local state.  It never promotes a discovery row,
creates an Ask session, or infers a security type from a ticker.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ask.exchange_store import StoredExchangeDataError, get_session_context
from ask.store import list_sessions
from identity import DEFAULT_USER_ID
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from user_state.notes import AnalystNoteRow, list_notes

__all__ = ["EvaluationDialogue", "EvaluationDialogueItem", "load_evaluation_dialogues"]

_MAX_ITEMS = 40
_MAX_SESSIONS = 200
_Instrument = Literal["stock", "etf", "unknown"]
_Availability = Literal["available", "partial", "unavailable"]


class EvaluationDialogueItem(BaseModel):
    """One local, evaluation-scoped conversation candidate."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    name: str | None = None
    instrument_type: _Instrument
    lifecycle: str
    discovery_candidate_id: int | None = None
    discovery_status: str | None = None
    open_note_count: int = 0
    latest_note_at: str | None = None
    workup_readiness: _Availability
    ask_session_id: str | None = None
    ask_session_updated_at: str | None = None
    ask_session_link_state: Literal["linked", "unlinked"]
    freshness: _Availability
    reason_codes: tuple[str, ...] = ()


class EvaluationDialogue(BaseModel):
    """Fail-closed bounded read model for the evaluation-dialogue launcher."""

    model_config = ConfigDict(frozen=True)

    state: _Availability
    items: tuple[EvaluationDialogueItem, ...]
    reason_codes: tuple[str, ...] = ()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _instrument(raw: object) -> _Instrument:
    value = str(raw or "").strip().lower()
    if value == "etf":
        return "etf"
    if value in {"equity", "adr", "stock"}:
        return "stock"
    return "unknown"


def _linked_sessions(db_path: Path) -> dict[tuple[str, int | None], tuple[str, str]]:
    """Newest explicitly matching session per ticker/candidate, never heuristic."""

    linked: dict[tuple[str, int | None], tuple[str, str]] = {}
    try:
        sessions = list_sessions(scope="portfolio", limit=_MAX_SESSIONS, db_path=db_path)
    except (OSError, sqlite3.Error):
        return linked
    for session in sessions:
        try:
            record = get_session_context(session.id, db_path=db_path)
        except (OSError, sqlite3.Error, StoredExchangeDataError, ValueError):
            continue
        if record is None or record.context.company_ticker is None:
            continue
        context = record.context
        ticker = str(context.company_ticker)
        key = (ticker, context.evaluation_candidate_id)
        linked.setdefault(key, (session.id, session.updated_at))
    return linked


def load_evaluation_dialogues(
    db_path: Path | str,
    *,
    user_id: str = DEFAULT_USER_ID,
    limit: int = _MAX_ITEMS,
) -> EvaluationDialogue:
    """Return deterministic evaluation rows and explicit incomplete state.

    A tracked evaluation company is the admission boundary. Discovery, notes,
    workup and session data enrich it independently and never manufacture rows.
    """

    safe_limit = max(1, min(int(limit), _MAX_ITEMS))
    path = Path(db_path)
    if not path.is_file():
        return EvaluationDialogue(
            state="unavailable", items=(), reason_codes=("evaluation_source_unavailable",)
        )
    try:
        conn = connect_sqlite(path, role=SQLiteConnectionRole.READ_ONLY)
    except (OSError, sqlite3.Error):
        return EvaluationDialogue(
            state="unavailable", items=(), reason_codes=("database_unavailable",)
        )
    try:
        if not _table_exists(conn, "tracked_companies"):
            return EvaluationDialogue(
                state="unavailable", items=(), reason_codes=("evaluation_source_unavailable",)
            )
        try:
            rows = conn.execute(
                "SELECT ticker, name, instrument_type FROM tracked_companies "
                "WHERE user_id = ? AND list_type = 'evaluation' AND archived_at IS NULL "
                "ORDER BY UPPER(ticker) LIMIT ?",
                (user_id, safe_limit),
            ).fetchall()
        except sqlite3.Error:
            return EvaluationDialogue(
                state="unavailable", items=(), reason_codes=("evaluation_source_unavailable",)
            )
        candidates: dict[str, tuple[int | None, str | None]] = {}
        candidate_available = _table_exists(conn, "discovery_candidates")
        if candidate_available:
            try:
                for row in conn.execute(
                    "SELECT id, ticker, status FROM discovery_candidates WHERE user_id = ?",
                    (user_id,),
                ):
                    candidates[str(row["ticker"]).upper()] = (int(row["id"]), str(row["status"]))
            except sqlite3.Error:
                candidate_available = False
        sessions = _linked_sessions(path)
        notes: dict[str, list[AnalystNoteRow]] = defaultdict(list)
        try:
            for note in list_notes(user_id=user_id, db_path=path, limit=200):
                if note.ticker:
                    notes[note.ticker.upper()].append(note)
            notes_available = True
        except (OSError, sqlite3.Error):
            notes_available = False
        items: list[EvaluationDialogueItem] = []
        for row in rows:
            ticker = str(row["ticker"]).upper()
            candidate_id, candidate_status = candidates.get(ticker, (None, None))
            session = sessions.get((ticker, candidate_id))
            if session is None:
                # A context without candidate identity is an explicit ticker link,
                # useful for existing sessions but not mistaken for a candidate.
                session = sessions.get((ticker, None))
            reasons: list[str] = []
            instrument = _instrument(row["instrument_type"])
            if instrument == "unknown":
                reasons.append("instrument_type_unavailable")
            if not candidate_available:
                reasons.append("discovery_source_unavailable")
            if not notes_available:
                reasons.append("notes_source_unavailable")
            workup: _Availability = "available" if instrument == "etf" else "partial"
            if instrument == "unknown":
                workup = "unavailable"
            if instrument == "etf":
                reasons.append("etf_workup_route_available")
            else:
                reasons.append("company_workup_route_available")
            freshness: _Availability = (
                "available" if notes_available and candidate_available else "partial"
            )
            if not notes_available and not candidate_available:
                freshness = "unavailable"
            ticker_notes = notes.get(ticker, [])
            items.append(
                EvaluationDialogueItem(
                    ticker=ticker,
                    name=str(row["name"]) if row["name"] else None,
                    instrument_type=instrument,
                    lifecycle="evaluation",
                    discovery_candidate_id=candidate_id,
                    discovery_status=candidate_status,
                    open_note_count=len(ticker_notes),
                    latest_note_at=(str(ticker_notes[0].created_at) if ticker_notes else None),
                    workup_readiness=workup,
                    ask_session_id=session[0] if session else None,
                    ask_session_updated_at=session[1] if session else None,
                    ask_session_link_state="linked" if session else "unlinked",
                    freshness=freshness,
                    reason_codes=tuple(reasons),
                )
            )
        state: _Availability = "available"
        reasons = []
        if (
            not candidate_available
            or not notes_available
            or any(x.instrument_type == "unknown" for x in items)
        ):
            state = "partial"
            reasons.append("enrichment_partial")
        return EvaluationDialogue(state=state, items=tuple(items), reason_codes=tuple(reasons))
    finally:
        conn.close()
