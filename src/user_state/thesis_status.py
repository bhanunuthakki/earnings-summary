"""Read the ``v_thesis_status`` view (alembic 0143) — the per-ticker research
snapshot the companion portfolio-tracker consumes for its monthly CIO brief.

The tracker itself reads the view directly over its read-only SQLite handle
(``SELECT ... FROM v_thesis_status WHERE ticker IN (...)``); this module is
earnings-summary's own first-class accessor for the same contract, so the view
is exercised by this repo's tests and callers rather than only across the repo
boundary.

Read-only and tolerant: a DB that predates the view (or was built by init_db
without the migration) raises ``OperationalError`` on the missing view, which we
swallow to an empty mapping — the brief degrades to "no earnings-summary
research on file" rather than crashing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from user_state._db import open_conn, parse_dt


@dataclass(slots=True)
class ThesisStatusRow:
    """One ticker's research snapshot as surfaced by ``v_thesis_status``."""

    ticker: str
    has_written_thesis: bool
    breach_status: str | None
    thesis_updated_at: datetime | None
    ledger_entry_count: int
    last_ledger_at: datetime | None
    open_notes_count: int
    open_questions_count: int


def _dt_or_none(raw: object) -> datetime | None:
    if raw is None or raw == "":
        return None
    try:
        return parse_dt(raw)
    except (ValueError, TypeError):
        return None


def _row_to_dc(r: sqlite3.Row) -> ThesisStatusRow:
    return ThesisStatusRow(
        ticker=str(r["ticker"]),
        has_written_thesis=bool(r["has_written_thesis"]),
        breach_status=(r["breach_status"] if r["breach_status"] is not None else None),
        thesis_updated_at=_dt_or_none(r["thesis_updated_at"]),
        ledger_entry_count=int(r["ledger_entry_count"] or 0),
        last_ledger_at=_dt_or_none(r["last_ledger_at"]),
        open_notes_count=int(r["open_notes_count"] or 0),
        open_questions_count=int(r["open_questions_count"] or 0),
    )


def read_thesis_status(
    tickers: list[str] | tuple[str, ...],
    *,
    db_path: Path | str | None = None,
) -> dict[str, ThesisStatusRow]:
    """Return ``{TICKER: ThesisStatusRow}`` for the requested tickers.

    Tickers are matched case-insensitively and keyed by their UPPER-cased form.
    Tickers with no footprint in any source table are simply absent from the
    result (the caller reads absence as "no research on file"). An empty
    ``tickers`` list short-circuits to ``{}`` without touching the DB.

    Never raises on a missing view — a DB without ``v_thesis_status`` yields
    ``{}`` so consumers degrade instead of breaking.
    """
    wanted = [t.strip().upper() for t in tickers if t and t.strip()]
    if not wanted:
        return {}
    conn = open_conn(db_path)
    try:
        placeholders = ",".join("?" for _ in wanted)
        try:
            # placeholders is a run of fixed '?' marks — no user text is interpolated.
            rows = conn.execute(
                f"SELECT * FROM v_thesis_status WHERE ticker IN ({placeholders})",
                wanted,
            ).fetchall()
        except sqlite3.OperationalError:
            # View absent (pre-0143 DB / init_db build) — degrade to empty.
            return {}
        return {str(r["ticker"]): _row_to_dc(r) for r in rows}
    finally:
        conn.close()
