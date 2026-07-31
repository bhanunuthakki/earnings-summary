"""Per-ticker dashboard-owned settings (migration 0067, widened by 0260).

Two knobs today:

* ``bypass_budget`` — the persistent "always ignore LLM budget caps for this
  ticker" flag, set from the dashboard's per-ticker Refresh panel and read by
  ``build_artifacts`` when resolving ``force_budget_bypass``.
* ``auto_pre_earnings_brief`` (0260) — the owner's sticky opt-in marking an
  EVALUATION name for scheduled pre-earnings brief generation
  (``earnings_brief``). Portfolio names are always in scope read-side; this
  flag exists only to pick the evaluation subset.

Best-effort: every helper degrades to a safe default (``False`` / no-op) when
the DB or table / column is missing so a fresh repo / pre-migration DB never
breaks the build or the server.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from db_paths import resolve_db_path
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

log = logging.getLogger(__name__)

# Boolean flag columns on ticker_settings. A closed vocabulary — the generic
# helpers interpolate the column name into SQL, so it must never come from
# request data without passing through this set.
FLAG_COLUMNS: frozenset[str] = frozenset({"bypass_budget", "auto_pre_earnings_brief"})


def get_flag(ticker: str, column: str, *, db_path: Path | str | None = None) -> bool:
    """True when `ticker`'s boolean `column` is set. Returns False on an
    unknown column, missing DB / table / column / row, or any read error
    (fail safe: the conservative default for every flag here is off)."""
    if column not in FLAG_COLUMNS:
        raise ValueError(f"unknown ticker_settings flag: {column!r}")
    path = resolve_db_path(db_path)
    if path is None or not Path(path).exists():
        return False
    try:
        conn = connect_sqlite(path, role=SQLiteConnectionRole.READ_ONLY)
        try:
            row = conn.execute(
                f"SELECT {column} FROM ticker_settings WHERE ticker = ?",  # nosec B608 -- column from closed FLAG_COLUMNS vocabulary
                (ticker.upper(),),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.debug(
            {
                "event": "get_ticker_flag_failed",
                "ticker": ticker,
                "column": column,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return False
    return bool(row[0]) if row is not None else False


def set_flag(
    ticker: str,
    column: str,
    value: bool,
    *,
    db_path: Path | str | None = None,
    now: datetime | None = None,
) -> bool:
    """Upsert one boolean flag for `ticker`. Returns True on write, False when
    the DB / table / column is unavailable."""
    if column not in FLAG_COLUMNS:
        raise ValueError(f"unknown ticker_settings flag: {column!r}")
    path = resolve_db_path(db_path)
    if path is None or not Path(path).exists():
        return False
    ts = (now or datetime.now(UTC)).isoformat()
    try:
        conn = connect_sqlite(path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        try:
            conn.execute(
                f"""
                INSERT INTO ticker_settings (ticker, {column}, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    {column} = excluded.{column},
                    updated_at = excluded.updated_at
                """,  # nosec B608 -- column from closed FLAG_COLUMNS vocabulary
                (ticker.upper(), 1 if value else 0, ts, ts),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning(
            {
                "event": "set_ticker_flag_failed",
                "ticker": ticker,
                "column": column,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return False


def get_bypass_budget(ticker: str, *, db_path: Path | str | None = None) -> bool:
    """True when `ticker` is set to always ignore LLM budget caps."""
    return get_flag(ticker, "bypass_budget", db_path=db_path)


def set_bypass_budget(
    ticker: str,
    value: bool,
    *,
    db_path: Path | str | None = None,
    now: datetime | None = None,
) -> bool:
    """Upsert the per-ticker `bypass_budget` flag."""
    return set_flag(ticker, "bypass_budget", value, db_path=db_path, now=now)


def get_auto_pre_earnings_brief(ticker: str, *, db_path: Path | str | None = None) -> bool:
    """True when this (evaluation) name is opted in to scheduled pre-earnings
    brief generation. Portfolio names never need this — they are always in
    scope by list_type (see ``earnings_brief.eligible_tickers``)."""
    return get_flag(ticker, "auto_pre_earnings_brief", db_path=db_path)


def set_auto_pre_earnings_brief(
    ticker: str,
    value: bool,
    *,
    db_path: Path | str | None = None,
    now: datetime | None = None,
) -> bool:
    """Upsert the per-ticker pre-earnings-brief opt-in."""
    return set_flag(ticker, "auto_pre_earnings_brief", value, db_path=db_path, now=now)
