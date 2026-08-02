"""The canonical "latest DCF run" reader — ONE query shape for every surface
that asks "what is the current DCF valuation for this ticker".

Before this module, two query shapes coexisted for the exact same question.
FILTERED (correct): ``WHERE COALESCE(is_latest, 1) = 1 AND
COALESCE(segment_name, '') = ''`` — used by ``allocation.eligibility``,
``bear_lint`` and ``model_provenance.basis``. UNFILTERED (wrong): everywhere
else simply ordered ``dcf_runs`` by ``ticker, created_at DESC, id DESC`` and
took the first row per ticker with no predicate at all — so a SEGMENT row
(``segment_name`` set, e.g. a bank/holdco per-segment sub-model) or a
SUPERSEDED row (``is_latest = 0``, migration 0137 versioning) could win as
"the" DCF for a ticker whenever it happened to have the newest
``created_at``. Both bugs produce a valuation that looks normal and is wrong.

Every caller here reads the same LATEST, TOP-LEVEL (unsegmented) row — the
one row a decision, a ranking leg, or a dashboard cell should ever call "the"
DCF. ``COALESCE`` tolerates a pre-migration / hand-rolled test schema that
lacks ``is_latest``/``segment_name``/``sanity_flag`` entirely (every row then
reads as latest and unsegmented, sanity_flag reads as NULL) — the same
degrade-gracefully rule every caller already followed individually.

``sanity_flag`` (migration 0182, 'outlier' past
``dcf.persist.SANITY_OVER_UNDER_LIMIT``) rides along on every row this module
returns; it is NOT filtered out here — the row still exists and a caller may
legitimately want to show it (badged). Whether a sanity-flagged row should be
EXCLUDED from a ranking/opportunity leg or merely have its gap-signal nulled
is a per-caller policy decision (see each call site's docstring), not this
module's to make.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

__all__ = [
    "LatestDcfRow",
    "latest_dcf_row",
    "latest_dcf_row_from_db",
    "latest_dcf_rows",
    "latest_dcf_rows_from_db",
]

# Columns selected unconditionally (present on every dcf_runs schema this
# module has to read, including the oldest hand-rolled test fixtures: ticker
# identifies the row, id is the primary key, created_at orders "latest") vs.
# columns probed for presence (added by a later migration, or simply absent
# on a hand-rolled test schema that never needed that field — e.g.
# bear_lint's own test fixture has no ``valuation_date``/``npv_per_share``
# columns at all, since the original ``_latest_top_level_rows`` only ever
# read ``live_price``/``assumption_snapshot_json``). Every field a caller
# might read must be probed here, never assumed core, or a narrower
# hand-rolled schema fails the whole query (caught by ``sqlite3.Error``,
# degrading SILENTLY to "no row" — exactly the bug a missing-column probe
# exists to prevent).
_CORE_COLUMNS = ("ticker", "id", "created_at")
_OPTIONAL_COLUMNS = (
    "valuation_date",
    "npv_per_share",
    "live_price",
    "live_price_at",
    "over_under_pct",
    "sanity_flag",
    "assumption_snapshot_json",
)


@dataclass(frozen=True, slots=True)
class LatestDcfRow:
    """One ticker's latest, top-level (unsegmented, current-version)
    ``dcf_runs`` row. Optional columns read ``None`` on a schema that
    predates them."""

    ticker: str
    id: int
    created_at: str | None
    valuation_date: str | None
    npv_per_share: float | None
    live_price: float | None
    live_price_at: str | None
    over_under_pct: float | None
    sanity_flag: str | None
    assumption_snapshot_json: str | None


def _dcf_columns(conn: sqlite3.Connection) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute("PRAGMA table_info(dcf_runs)")}
    except sqlite3.Error:
        return set()


def _select_clause(cols: set[str]) -> str:
    selects: list[str] = list(_CORE_COLUMNS)
    selects.extend(c if c in cols else f"NULL AS {c}" for c in _OPTIONAL_COLUMNS)
    return ", ".join(selects)


def _where_clause(cols: set[str]) -> str:
    is_latest_pred = "COALESCE(is_latest, 1) = 1" if "is_latest" in cols else "1 = 1"
    seg_pred = "COALESCE(segment_name, '') = ''" if "segment_name" in cols else "1 = 1"
    return f"{is_latest_pred} AND {seg_pred}"


def _row_from(r: sqlite3.Row) -> LatestDcfRow:
    return LatestDcfRow(
        ticker=str(r["ticker"]).upper(),
        id=int(r["id"]),
        created_at=str(r["created_at"]) if r["created_at"] is not None else None,
        valuation_date=str(r["valuation_date"]) if r["valuation_date"] is not None else None,
        npv_per_share=float(r["npv_per_share"]) if r["npv_per_share"] is not None else None,
        live_price=float(r["live_price"]) if r["live_price"] is not None else None,
        live_price_at=str(r["live_price_at"]) if r["live_price_at"] is not None else None,
        over_under_pct=float(r["over_under_pct"]) if r["over_under_pct"] is not None else None,
        sanity_flag=str(r["sanity_flag"]) if r["sanity_flag"] is not None else None,
        assumption_snapshot_json=(
            str(r["assumption_snapshot_json"])
            if r["assumption_snapshot_json"] is not None
            else None
        ),
    )


def latest_dcf_row(conn: sqlite3.Connection, ticker: str) -> LatestDcfRow | None:
    """The latest top-level ``dcf_runs`` row for one ticker, or ``None`` when
    there is no such row (or ``dcf_runs`` doesn't exist / errors)."""
    cols = _dcf_columns(conn)
    if not cols:
        return None
    sql = (
        f"SELECT {_select_clause(cols)} FROM dcf_runs "
        f"WHERE UPPER(ticker) = ? AND {_where_clause(cols)} "
        "ORDER BY created_at DESC, id DESC LIMIT 1"
    )
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(sql, (ticker.upper(),)).fetchone()
    except sqlite3.Error:
        return None
    return _row_from(row) if row is not None else None


def latest_dcf_rows(conn: sqlite3.Connection) -> dict[str, LatestDcfRow]:
    """ticker -> latest top-level ``dcf_runs`` row, for every ticker on
    file. ``{}`` on a missing table / query error."""
    cols = _dcf_columns(conn)
    if not cols:
        return {}
    sql = (
        f"SELECT {_select_clause(cols)} FROM dcf_runs "
        f"WHERE {_where_clause(cols)} "
        "ORDER BY ticker, created_at DESC, id DESC"
    )
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql).fetchall()
    except sqlite3.Error:
        return {}
    out: dict[str, LatestDcfRow] = {}
    for r in rows:
        parsed = _row_from(r)
        if parsed.ticker not in out:
            out[parsed.ticker] = parsed
    return out


def latest_dcf_row_from_db(db_path: Path, ticker: str) -> LatestDcfRow | None:
    """File-path convenience wrapper: opens ``db_path`` read-only, degrades to
    ``None`` on a missing file / connection error (never raises)."""
    if not db_path.exists():
        return None
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return None
    try:
        return latest_dcf_row(conn, ticker)
    finally:
        conn.close()


def latest_dcf_rows_from_db(db_path: Path) -> dict[str, LatestDcfRow]:
    """File-path convenience wrapper: opens ``db_path`` read-only, degrades to
    ``{}`` on a missing file / connection error (never raises)."""
    if not db_path.exists():
        return {}
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return {}
    try:
        return latest_dcf_rows(conn)
    finally:
        conn.close()
