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

from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

# The lists that auto-produce a full brief (and thus a §Valuation DCF card).
# Mirror of ``db.BRIEFED_LIST_TYPES`` / ``pipeline.queries.BRIEFED_LIST_TYPES``,
# duplicated here on purpose: importing ``db`` runs ``init_db()`` against the
# default DB at import time, a side effect this lightweight helper must not have.
BRIEFED_LIST_TYPES: tuple[str, ...] = ("portfolio", "evaluation")


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
    placeholders = ", ".join("?" for _ in BRIEFED_LIST_TYPES)
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return []
    # ETFs on the evaluation list are analyzed via the published-data lane
    # (directives/etf_data.md) — an FCFF DCF over a fund's non-existent income
    # statement is nonsense, so they are excluded at the universe. The
    # column-less retry keeps pre-0044 substrates (older test DBs) working.
    query = (
        "SELECT DISTINCT UPPER(ticker) FROM tracked_companies "
        f"WHERE list_type IN ({placeholders}) "
        "AND (instrument_type IS NULL OR LOWER(instrument_type) <> 'etf')"
    )
    try:
        try:
            rows = conn.execute(query, BRIEFED_LIST_TYPES).fetchall()
        except sqlite3.OperationalError:
            rows = conn.execute(
                "SELECT DISTINCT UPPER(ticker) FROM tracked_companies "
                f"WHERE list_type IN ({placeholders})",
                BRIEFED_LIST_TYPES,
            ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return sorted({str(row[0]) for row in rows if row and row[0]})
