"""The DCF-eligible ticker universe — the names a DCF is maintained for.

A full brief carries a §Valuation DCF card, so the names that auto-produce briefs
— the portfolio and evaluation tracked-company lists — are exactly the names a DCF
should exist for. Both batch DCF drivers resolve their default set through here:

  * ``execution/build_all_redesigned_dcf.py`` (the redesign workbook builder), and
  * ``execution/refresh_dcf.py --all-named`` (the canonical refresh),

so evaluation-list names are first-class DCF citizens alongside portfolio names.
The redesigned builder computes its own WACC (CAPM, market-value weights), so an
eval name needs no hand-seeded ``wacc`` to qualify; names an FCFF DCF can't value
(banks/insurers/asset-managers flagged ``dcf_applicable=false``) self-skip in the
per-name pipeline, not here.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pipeline.queries import BRIEFED_LIST_TYPE_VALUES
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

# The lists that auto-produce a full brief (and thus a §Valuation DCF card).
# Mirror of ``db.BRIEFED_LIST_TYPES`` / ``pipeline.queries.BRIEFED_LIST_TYPES``,
# duplicated here on purpose so this lightweight helper does not load the
# broader legacy DB facade merely to resolve list semantics.
BRIEFED_LIST_TYPES = BRIEFED_LIST_TYPE_VALUES


def dcf_universe(repo_root: Path) -> list[str]:
    """Uppercased, de-duplicated, sorted tickers on the briefed lists (portfolio +
    evaluation) in ``<repo_root>/data/portfolio.db``.

    Returns ``[]`` when the DB or the ``tracked_companies`` table is absent (e.g. a
    checkout that carries no ``data/``) or unreadable, so callers can union the
    result with a filesystem fallback without special-casing the empty DB.
    """
    db_path = Path(repo_root) / "data" / "portfolio.db"
    if not db_path.exists():
        return []
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return []
    # ETFs on the evaluation list are analyzed via the published-data lane
    # (directives/etf_data.md) — an FCFF DCF over a fund's non-existent income
    # statement is nonsense, so they are excluded at the universe. The
    # column-less retry keeps pre-0044 substrates (older test DBs) working.
    try:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tracked_companies)")}
        has_archived = "archived_at" in columns
        has_instrument_type = "instrument_type" in columns
        if has_archived and has_instrument_type:
            rows = conn.execute(
                "SELECT DISTINCT UPPER(ticker) FROM tracked_companies "
                "WHERE list_type IN (?, ?) AND archived_at IS NULL "
                "AND (instrument_type IS NULL OR LOWER(instrument_type) <> 'etf')",
                BRIEFED_LIST_TYPES,
            ).fetchall()
        elif has_instrument_type:
            rows = conn.execute(
                "SELECT DISTINCT UPPER(ticker) FROM tracked_companies "
                "WHERE list_type IN (?, ?) "
                "AND (instrument_type IS NULL OR LOWER(instrument_type) <> 'etf')",
                BRIEFED_LIST_TYPES,
            ).fetchall()
        elif has_archived:
            rows = conn.execute(
                "SELECT DISTINCT UPPER(ticker) FROM tracked_companies "
                "WHERE list_type IN (?, ?) AND archived_at IS NULL",
                BRIEFED_LIST_TYPES,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT UPPER(ticker) FROM tracked_companies WHERE list_type IN (?, ?)",
                BRIEFED_LIST_TYPES,
            ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return sorted({str(row[0]) for row in rows if row and row[0]})
