"""Bounded, read-only candidates for the Portfolio Copilot evaluation dialog.

This joins only persisted local state.  It never promotes a discovery row,
creates an Ask session, or infers a security type from a ticker.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from ask.exchange_store import SessionContextV1
from identity import DEFAULT_USER_ID
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from user_state.notes import AnalystNoteRow, list_notes

__all__ = [
    "ALLOWED_FILTERS",
    "ALLOWED_LIMITS",
    "ALLOWED_SORTS",
    "MAX_NOTES_FETCH",
    "MAX_SESSIONS_FETCH",
    "EvaluationDialogue",
    "EvaluationDialogueItem",
    "ReasonCode",
    "load_evaluation_dialogues",
]

ALLOWED_LIMITS: tuple[int, ...] = (3, 5, 10)
ALLOWED_SORTS: tuple[str, ...] = ("relevance", "ticker_asc")
ALLOWED_FILTERS: tuple[str, ...] = ("all", "has_dialogue", "has_notes", "ready")

MAX_SESSIONS_FETCH: int = 5_000
MAX_NOTES_FETCH: int = 50_000
_Instrument = Literal["stock", "etf", "unknown"]
_Availability = Literal["available", "partial", "unavailable"]

ReasonCode = Literal[
    "evaluation_source_unavailable",
    "sessions_source_unavailable",
    "notes_source_unavailable",
    "discovery_source_unavailable",
    "instrument_type_unavailable",
    "relevance_partial",
]


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
    ask_session_link_state: Literal["linked", "unlinked", "unknown"]
    freshness: _Availability
    reason_codes: tuple[str, ...] = ()


class EvaluationDialogue(BaseModel):
    """Fail-closed bounded read model for the evaluation-dialogue launcher."""

    model_config = ConfigDict(frozen=True)

    state: _Availability
    items: tuple[EvaluationDialogueItem, ...]
    total_active: int | None = None
    total_matching: int | None = None
    matching_state: Literal["complete", "indeterminate"] = "complete"
    reason_codes: tuple[ReasonCode, ...] = ()

    @model_validator(mode="after")
    def _enforce_invariants(self) -> EvaluationDialogue:
        if (self.state == "unavailable") != (self.total_active is None):
            raise ValueError("state is 'unavailable' if and only if total_active is None")
        if self.state == "unavailable":
            if self.items != ():
                raise ValueError("state 'unavailable' requires empty items")
            if self.matching_state != "indeterminate":
                raise ValueError("state 'unavailable' requires matching_state 'indeterminate'")
            if self.total_matching is not None:
                raise ValueError("state 'unavailable' requires total_matching is None")
        if (self.matching_state == "indeterminate") != (self.total_matching is None):
            raise ValueError(
                "matching_state is 'indeterminate' if and only if total_matching is None"
            )
        return self


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _instrument(raw: object) -> _Instrument:
    value = str(raw or "").strip().lower()
    if value == "etf":
        return "etf"
    if value in {"equity", "adr", "stock"}:
        return "stock"
    return "unknown"


def _parse_iso_timestamp(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _latest_note_timestamp(ticker_notes: Sequence[object]) -> str | None:
    """Return raw timestamp string of the note with the newest UTC datetime, or None if none parse."""
    if not ticker_notes:
        return None
    valid_notes: list[tuple[datetime, str]] = []
    for note in ticker_notes:
        raw_val = getattr(note, "created_at", None)
        if raw_val is None:
            continue
        if isinstance(raw_val, datetime):
            dt = raw_val if raw_val.tzinfo is not None else raw_val.replace(tzinfo=UTC)
            dt = dt.astimezone(UTC)
            formatted = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            valid_notes.append((dt, formatted))
        else:
            raw = str(raw_val)
            dt = _parse_iso_timestamp(raw)
            if dt is not None:
                valid_notes.append((dt, raw))
    if valid_notes:
        return max(valid_notes, key=lambda x: x[0])[1]
    return None


def load_evaluation_dialogues(
    db_path: Path | str,
    *,
    user_id: str = DEFAULT_USER_ID,
    limit: int = 3,
    sort: Literal["relevance", "ticker_asc"] = "relevance",
    filter_state: Literal["all", "has_dialogue", "has_notes", "ready"] = "all",
) -> EvaluationDialogue:
    """Return deterministic evaluation rows and explicit incomplete state.

    A tracked evaluation company is the admission boundary. Discovery, notes,
    workup and session data enrich it independently and never manufacture rows.
    """
    if type(limit) is not int or limit not in ALLOWED_LIMITS:
        raise ValueError(f"limit must be an integer in {ALLOWED_LIMITS}, got {limit!r}")
    if sort not in ALLOWED_SORTS:
        raise ValueError(f"sort must be one of {ALLOWED_SORTS}, got {sort!r}")
    if filter_state not in ALLOWED_FILTERS:
        raise ValueError(f"filter_state must be one of {ALLOWED_FILTERS}, got {filter_state!r}")

    path = Path(db_path)
    if not path.is_file():
        return EvaluationDialogue(
            state="unavailable",
            items=(),
            total_active=None,
            total_matching=None,
            matching_state="indeterminate",
            reason_codes=("evaluation_source_unavailable",),
        )
    try:
        conn = connect_sqlite(path, role=SQLiteConnectionRole.READ_ONLY)
        conn.execute("PRAGMA query_only = ON")
    except (OSError, sqlite3.Error):
        return EvaluationDialogue(
            state="unavailable",
            items=(),
            total_active=None,
            total_matching=None,
            matching_state="indeterminate",
            reason_codes=("evaluation_source_unavailable",),
        )
    try:
        # Stage 1: Active Evaluation Membership & Normalized Collision Defense
        try:
            if not _table_exists(conn, "tracked_companies"):
                return EvaluationDialogue(
                    state="unavailable",
                    items=(),
                    total_active=None,
                    total_matching=None,
                    matching_state="indeterminate",
                    reason_codes=("evaluation_source_unavailable",),
                )
            rows = conn.execute(
                "SELECT ticker, name, instrument_type FROM tracked_companies "
                "WHERE user_id = ? AND list_type = 'evaluation' AND archived_at IS NULL "
                "ORDER BY UPPER(ticker), ticker",
                (user_id,),
            ).fetchall()
        except sqlite3.Error:
            return EvaluationDialogue(
                state="unavailable",
                items=(),
                total_active=None,
                total_matching=None,
                matching_state="indeterminate",
                reason_codes=("evaluation_source_unavailable",),
            )

        canonical_rows: dict[str, sqlite3.Row] = {}
        collision_detected = False
        for row in rows:
            raw_ticker = str(row["ticker"] or "").strip()
            if not raw_ticker:
                continue
            norm_ticker = raw_ticker.upper()
            if norm_ticker in canonical_rows:
                collision_detected = True
                break
            canonical_rows[norm_ticker] = row

        if collision_detected:
            return EvaluationDialogue(
                state="unavailable",
                items=(),
                total_active=None,
                total_matching=None,
                matching_state="indeterminate",
                reason_codes=("evaluation_source_unavailable",),
            )

        total_active = len(canonical_rows)
        if total_active == 0:
            return EvaluationDialogue(
                state="available",
                items=(),
                total_active=0,
                total_matching=0,
                matching_state="complete",
                reason_codes=(),
            )

        # Stage 2: Safe Source Probing, Truncation-Guarded Fetch, & Enrichment
        invalid_session_timestamp_encountered: bool = False
        invalid_note_timestamp_encountered: bool = False
        unreadable_session_context_encountered: bool = False
        missing_context_encountered: bool = False
        sessions_complete: bool = True
        notes_complete: bool = True

        discovery_available = _table_exists(conn, "discovery_candidates")
        sessions_available = _table_exists(conn, "ask_sessions") and _table_exists(
            conn, "ask_session_contexts"
        )
        notes_available = _table_exists(conn, "analyst_notes")

        user_candidates_by_ticker: dict[str, set[int]] = defaultdict(set)
        user_candidate_status: dict[str, tuple[int, str]] = {}
        if discovery_available:
            try:
                for row in conn.execute(
                    "SELECT id, ticker, status FROM discovery_candidates WHERE user_id = ?",
                    (user_id,),
                ):
                    raw_t = str(row["ticker"] or "").strip()
                    if raw_t:
                        nt = raw_t.upper()
                        cid = int(row["id"])
                        user_candidates_by_ticker[nt].add(cid)
                        user_candidate_status[nt] = (cid, str(row["status"]))
            except sqlite3.Error:
                discovery_available = False

        session_rows: list[sqlite3.Row] = []
        if sessions_available:
            try:
                sql = """
                    WITH recent AS MATERIALIZED (
                      SELECT id, updated_at
                      FROM ask_sessions
                      WHERE scope = ?
                      ORDER BY updated_at DESC, id DESC
                      LIMIT ?
                    )
                    SELECT r.id, r.updated_at, c.context_json, c.context_sha256
                    FROM recent r
                    LEFT JOIN ask_session_contexts c ON c.session_id = r.id
                    ORDER BY r.updated_at DESC, r.id DESC
                """
                fetched = conn.execute(sql, ("portfolio", MAX_SESSIONS_FETCH + 1)).fetchall()
                if len(fetched) > MAX_SESSIONS_FETCH:
                    sessions_complete = False
                    session_rows = fetched[:MAX_SESSIONS_FETCH]
                else:
                    session_rows = fetched
            except sqlite3.Error:
                sessions_available = False

        eligible_sessions: dict[str, list[tuple[str, str, datetime | None]]] = defaultdict(list)
        if sessions_available:
            for row in session_rows:
                raw_json = row["context_json"]
                if raw_json is None:
                    missing_context_encountered = True
                    continue
                raw_json_str = str(raw_json)
                committed_sha256 = str(row["context_sha256"])
                computed_sha256 = sha256(raw_json_str.encode("utf-8")).hexdigest()
                if computed_sha256 != committed_sha256:
                    unreadable_session_context_encountered = True
                    continue
                try:
                    context = SessionContextV1.model_validate_json(raw_json_str)
                except (ValidationError, ValueError):
                    unreadable_session_context_encountered = True
                    continue
                if context.company_ticker is None:
                    continue
                norm_ticker = str(context.company_ticker).strip().upper()
                if norm_ticker not in canonical_rows:
                    continue
                cand_id = context.evaluation_candidate_id
                if cand_id is not None and cand_id not in user_candidates_by_ticker.get(
                    norm_ticker, set()
                ):
                    continue
                sess_id = str(row["id"])
                raw_updated_at = str(row["updated_at"])
                dt = _parse_iso_timestamp(raw_updated_at)
                if dt is None:
                    invalid_session_timestamp_encountered = True
                eligible_sessions[norm_ticker].append((sess_id, raw_updated_at, dt))

        session_linkage_complete = (
            sessions_available
            and sessions_complete
            and not unreadable_session_context_encountered
            and not missing_context_encountered
        )
        dialogue_linkage_authoritative = session_linkage_complete and discovery_available

        all_notes_by_ticker: dict[str, list[AnalystNoteRow]] = defaultdict(list)
        open_notes_by_ticker: dict[str, list[AnalystNoteRow]] = defaultdict(list)
        if notes_available:
            try:
                fetched_notes = list_notes(user_id=user_id, conn=conn, limit=MAX_NOTES_FETCH + 1)
                if len(fetched_notes) > MAX_NOTES_FETCH:
                    notes_complete = False
                    notes_rows = fetched_notes[:MAX_NOTES_FETCH]
                else:
                    notes_rows = fetched_notes
                for note in notes_rows:
                    if note.ticker:
                        nt = note.ticker.upper()
                        all_notes_by_ticker[nt].append(note)
                        raw_created = getattr(note, "created_at", None)
                        if raw_created is not None:
                            if isinstance(raw_created, datetime):
                                pass
                            elif _parse_iso_timestamp(str(raw_created)) is None:
                                invalid_note_timestamp_encountered = True
                        status_val = getattr(note, "status", None)
                        if status_val is None or str(status_val).strip().lower() == "open":
                            open_notes_by_ticker[nt].append(note)
            except (OSError, sqlite3.Error, ValueError, IndexError, KeyError):
                notes_available = False

        any_instrument_unknown = False
        all_items: list[EvaluationDialogueItem] = []

        for norm_ticker, row in canonical_rows.items():
            cid_status = user_candidate_status.get(norm_ticker)
            candidate_id = cid_status[0] if cid_status else None
            candidate_status = cid_status[1] if cid_status else None

            ticker_sessions = eligible_sessions.get(norm_ticker, [])
            has_linked_session = len(ticker_sessions) > 0
            valid_sessions: list[tuple[str, str, datetime]] = []
            for session_id, updated_at, parsed_updated_at in ticker_sessions:
                if parsed_updated_at is not None:
                    valid_sessions.append((session_id, updated_at, parsed_updated_at))
            malformed_sessions = [s for s in ticker_sessions if s[2] is None]

            if valid_sessions:
                latest_valid = max(valid_sessions, key=lambda s: (s[2], s[0]))
                selected_session_id: str | None = latest_valid[0]
                selected_updated_at: str | None = latest_valid[1]
            elif malformed_sessions:
                selected_session = max(malformed_sessions, key=lambda s: s[0])
                selected_session_id = selected_session[0]
                selected_updated_at = selected_session[1]
            else:
                selected_session_id = None
                selected_updated_at = None

            if has_linked_session:
                link_state: Literal["linked", "unlinked", "unknown"] = "linked"
            elif dialogue_linkage_authoritative:
                link_state = "unlinked"
            else:
                link_state = "unknown"

            ticker_all_notes = all_notes_by_ticker.get(norm_ticker, [])
            ticker_open_notes = open_notes_by_ticker.get(norm_ticker, [])
            latest_note_raw = _latest_note_timestamp(ticker_all_notes)
            open_count = len(ticker_open_notes)

            instrument = _instrument(row["instrument_type"])
            if instrument == "unknown":
                any_instrument_unknown = True
                workup: _Availability = "unavailable"
            elif instrument == "etf":
                workup = "available"
            else:
                workup = "partial"

            item_reasons: list[str] = []
            if instrument == "unknown":
                item_reasons.append("instrument_type_unavailable")
            if not discovery_available:
                item_reasons.append("discovery_source_unavailable")
            if not notes_available or not notes_complete:
                item_reasons.append("notes_source_unavailable")
            if instrument == "etf":
                item_reasons.append("etf_workup_route_available")
            else:
                item_reasons.append("company_workup_route_available")

            freshness: _Availability = (
                "available"
                if (notes_available and notes_complete and discovery_available)
                else "partial"
            )
            if not (notes_available and notes_complete) and not discovery_available:
                freshness = "unavailable"

            all_items.append(
                EvaluationDialogueItem(
                    ticker=norm_ticker,
                    name=str(row["name"]) if row["name"] else None,
                    instrument_type=instrument,
                    lifecycle="evaluation",
                    discovery_candidate_id=candidate_id,
                    discovery_status=candidate_status,
                    open_note_count=open_count,
                    latest_note_at=latest_note_raw,
                    workup_readiness=workup,
                    ask_session_id=selected_session_id,
                    ask_session_updated_at=selected_updated_at,
                    ask_session_link_state=link_state,
                    freshness=freshness,
                    reason_codes=tuple(item_reasons),
                )
            )

        total_active = len(all_items)

        # Stage 3: Availability & Complete Reason-Code Assessment
        reason_codes_list: list[ReasonCode] = []
        if not discovery_available:
            reason_codes_list.append("discovery_source_unavailable")
        if not sessions_available or not sessions_complete:
            reason_codes_list.append("sessions_source_unavailable")
        if not notes_available or not notes_complete:
            reason_codes_list.append("notes_source_unavailable")
        if any_instrument_unknown:
            reason_codes_list.append("instrument_type_unavailable")
        if (
            not discovery_available
            or not sessions_available
            or not sessions_complete
            or not notes_available
            or not notes_complete
            or not session_linkage_complete
            or invalid_session_timestamp_encountered
            or invalid_note_timestamp_encountered
        ):
            reason_codes_list.append("relevance_partial")

        frozen_reason_codes: tuple[ReasonCode, ...] = tuple(sorted(set(reason_codes_list)))

        # Stage 4: Filter Matching Set
        matching_state: Literal["complete", "indeterminate"] = "complete"
        total_matching: int | None = None
        filtered_items: list[EvaluationDialogueItem] = []

        if filter_state == "all":
            matching_state = "complete"
            filtered_items = list(all_items)
            total_matching = total_active
        elif filter_state == "has_dialogue":
            if not dialogue_linkage_authoritative:
                matching_state = "indeterminate"
                total_matching = None
                filtered_items = []
            else:
                matching_state = "complete"
                filtered_items = [x for x in all_items if x.ask_session_link_state == "linked"]
                total_matching = len(filtered_items)
        elif filter_state == "has_notes":
            if not notes_available or not notes_complete:
                matching_state = "indeterminate"
                total_matching = None
                filtered_items = []
            else:
                matching_state = "complete"
                filtered_items = [x for x in all_items if x.open_note_count > 0]
                total_matching = len(filtered_items)
        elif filter_state == "ready":
            if any_instrument_unknown:
                matching_state = "indeterminate"
                total_matching = None
                filtered_items = []
            else:
                matching_state = "complete"
                filtered_items = [x for x in all_items if x.workup_readiness == "available"]
                total_matching = len(filtered_items)

        # Stage 5: Sort Matching Set
        def _relevance_sort_key(item: EvaluationDialogueItem) -> tuple[int, float, int, str, str]:
            ask_dt = _parse_iso_timestamp(item.ask_session_updated_at)
            note_dt = _parse_iso_timestamp(item.latest_note_at)
            valid_dts = [d for d in (ask_dt, note_dt) if d is not None]
            has_activity = len(valid_dts) > 0
            activity_ts = max(valid_dts).timestamp() if has_activity else 0.0
            readiness_rank = (
                0
                if item.workup_readiness == "available"
                else (1 if item.workup_readiness == "partial" else 2)
            )
            return (
                0 if has_activity else 1,
                -activity_ts,
                readiness_rank,
                item.ticker.upper(),
                item.ticker,
            )

        if sort == "relevance":
            sorted_items = sorted(filtered_items, key=_relevance_sort_key)
        else:
            sorted_items = sorted(filtered_items, key=lambda x: (x.ticker.upper(), x.ticker))

        # Stage 6 & 7: Apply Display Limit & Return Fail-Closed Projection
        items_slice = tuple(sorted_items[:limit])
        state: _Availability = "partial" if frozen_reason_codes else "available"

        return EvaluationDialogue(
            state=state,
            items=items_slice,
            total_active=total_active,
            total_matching=total_matching,
            matching_state=matching_state,
            reason_codes=frozen_reason_codes,
        )
    finally:
        conn.close()
