"""Tier-aware due-list queries for the scalability runner.

The fresh-review memo flagged that today every tracked ticker gets the same
processing cadence regardless of how actively the analyst follows it. With
~11 portfolio names, ~63 watchlist+evaluation names, and ~2.3k index members
in the universe, treating them uniformly doesn't scale.

`tracked_companies.processing_tier` (added by migration 0044 OR 0051,
whichever lands first) carries the tier. The mapping:

    P1   portfolio                         daily refresh
    P2   watchlist | evaluation            weekly refresh
    P3   index_member | etf | none         monthly refresh

This module is the read-only oracle for "who is due right now?" — it does NOT
write to the DB and does NOT trigger any pipeline. Callers (the daily worker,
the new weekly/monthly cron entries, the dashboard tier-coverage strip) take
the returned list and route it into their own work.

Two helpers, same shape:

    tickers_due_for_refresh(repo_root, 'daily' | 'weekly' | 'monthly')
        Reads tracked_companies.last_built_at against the tier's cadence rule.

    tickers_due_for_lens_regen(repo_root, lens_name, 'daily' | 'weekly' | 'monthly')
        Reads llm_artifacts.generated_at scoped to the lens purpose.

Cadence rule (per tier, applied uniformly to either refresh or lens regen):

    cadence='daily'     P1: always due
                        P2: due if last_event > 7d ago
                        P3: due if last_event > 30d ago
    cadence='weekly'    P1: due if last_event > 7d ago
                        P2: due if last_event > 30d ago     (monthly)
                        P3: due if last_event > 90d ago     (quarterly)
    cadence='monthly'   P1: due if last_event > 30d ago
                        P2: due if last_event > 90d ago
                        P3: due if last_event > 365d ago

"daily / weekly / monthly" here names *which cron tick* is calling. The
function returns the universe of tickers that SHOULD be touched by that
tick — the daily cron always picks up every P1 and any P2/P3 that drifted
past their cadence. Tier rules are encoded once in `_DUE_THRESHOLDS_DAYS`
so a future "P0 = real-time" or "P4 = annual" tier is a single-row edit.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

Cadence = Literal["daily", "weekly", "monthly"]


# Per-tier age thresholds in days. None on the diagonal means "always due"
# (P1 + daily cron — no last_built_at age check).
_DUE_THRESHOLDS_DAYS: dict[Cadence, dict[str, int | None]] = {
    "daily": {"P1": None, "P2": 7, "P3": 30},
    "weekly": {"P1": 7, "P2": 30, "P3": 90},
    "monthly": {"P1": 30, "P2": 90, "P3": 365},
}


def tickers_due_for_refresh(
    repo_root: Path,
    cadence: Cadence,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Tickers whose (processing_tier, last_built_at) make them due for the cadence tick.

    Returns an alphabetically sorted list of UPPERCASE tickers. Archived rows
    are excluded. A ticker with last_built_at IS NULL is always due (never
    been built). Read-only — does not mutate any table.

    `now` is injectable for testing; defaults to wall-clock UTC-naive (matches
    how `last_built_at` is stored — see daily_fetch_and_brief._record_skip).
    """
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return []
    now = now if now is not None else datetime.now()
    thresholds = _DUE_THRESHOLDS_DAYS[cadence]

    with connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY) as conn:
        conn.row_factory = sqlite3.Row
        if not _has_processing_tier_column(conn):
            return []
        rows = conn.execute(
            "SELECT ticker, processing_tier, last_built_at "
            "FROM tracked_companies "
            "WHERE archived_at IS NULL "
            "ORDER BY ticker"
        ).fetchall()

    return _filter_due(rows, thresholds, now, "last_built_at")


def tickers_due_for_lens_regen(
    repo_root: Path,
    lens_name: str,
    cadence: Cadence,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Tickers whose lens artifact (purpose='lens:<lens_name>') is due for regen.

    Joins tracked_companies → llm_artifacts on (ticker, purpose, current row).
    A ticker with no cached artifact for this lens is always due. Returns a
    sorted list of UPPERCASE tickers.
    """
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return []
    now = now if now is not None else datetime.now()
    thresholds = _DUE_THRESHOLDS_DAYS[cadence]
    purpose = f"lens:{lens_name}"

    with connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY) as conn:
        conn.row_factory = sqlite3.Row
        if not _has_processing_tier_column(conn):
            return []
        if not _has_table(conn, "llm_artifacts"):
            # No artifact store yet — every tracked ticker is "due" since
            # nothing has been generated.
            rows = conn.execute(
                "SELECT ticker, processing_tier, NULL AS last_generated "
                "FROM tracked_companies WHERE archived_at IS NULL "
                "ORDER BY ticker"
            ).fetchall()
            return _filter_due(rows, thresholds, now, "last_generated")

        rows = conn.execute(
            """
            SELECT tc.ticker,
                   tc.processing_tier,
                   (
                     SELECT MAX(a.generated_at)
                     FROM llm_artifacts a
                     WHERE a.ticker = tc.ticker
                       AND a.purpose = ?
                       AND a.superseded_by_id IS NULL
                   ) AS last_generated
            FROM tracked_companies tc
            WHERE tc.archived_at IS NULL
            ORDER BY tc.ticker
            """,
            (purpose,),
        ).fetchall()

    return _filter_due(rows, thresholds, now, "last_generated")


def _filter_due(
    rows: list[sqlite3.Row],
    thresholds: dict[str, int | None],
    now: datetime,
    age_col: str,
) -> list[str]:
    """Apply the per-tier threshold to a (ticker, processing_tier, <age_col>) row set."""
    out: list[str] = []
    for r in rows:
        tier = (r["processing_tier"] or "P3").upper()
        threshold_days = thresholds.get(tier)
        # P1 + daily → always due (threshold is None).
        if threshold_days is None:
            out.append(str(r["ticker"]).upper())
            continue
        age_str = r[age_col]
        if not age_str:
            # Never built / never generated — always due.
            out.append(str(r["ticker"]).upper())
            continue
        last_event = _parse_dt(str(age_str))
        if last_event is None:
            out.append(str(r["ticker"]).upper())
            continue
        if now - last_event >= timedelta(days=threshold_days):
            out.append(str(r["ticker"]).upper())
    return sorted(set(out))


def _parse_dt(s: str) -> datetime | None:
    """Tolerant ISO-8601 parse — strips 'Z' and microseconds/timezone if present."""
    s = s.strip()
    if not s:
        return None
    # SQLite CURRENT_TIMESTAMP / our own writers use 'YYYY-MM-DD HH:MM:SS'
    # or ISO with 'T'. fromisoformat handles both since Python 3.11+.
    if s.endswith("Z"):
        s = s[:-1]
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    # Strip tzinfo if any — last_built_at is naive in this codebase.
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


def _has_processing_tier_column(conn: sqlite3.Connection) -> bool:
    cur = conn.execute("PRAGMA table_info(tracked_companies)")
    return any(row[1] == "processing_tier" for row in cur.fetchall())


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Coverage summary for the dashboard tier-coverage strip
# ---------------------------------------------------------------------------


def tier_coverage_summary(
    repo_root: Path,
    *,
    now: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, dict[str, int]]:
    """Return per-tier (fresh, stale, total) counts for dashboard display.

    "Fresh" means within the tier's daily-cadence window (P1 always, P2 ≤ 7d,
    P3 ≤ 30d). This is the same threshold the daily worker uses, so the
    coverage strip is showing exactly the universe the daily tick already
    handles plus the staleness that's accumulated since.

    Output:
      {"P1": {"fresh": 11, "stale": 0, "total": 11}, ...}
    """
    db_path = repo_root / "data" / "portfolio.db"
    out: dict[str, dict[str, int]] = {
        "P1": {"fresh": 0, "stale": 0, "total": 0},
        "P2": {"fresh": 0, "stale": 0, "total": 0},
        "P3": {"fresh": 0, "stale": 0, "total": 0},
    }
    if not db_path.exists():
        return out
    now = now if now is not None else datetime.now()
    thresholds = _DUE_THRESHOLDS_DAYS["daily"]

    db_conn = conn or connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        db_conn.row_factory = sqlite3.Row
        if not _has_processing_tier_column(db_conn):
            return out
        rows = db_conn.execute(
            "SELECT processing_tier, last_built_at FROM tracked_companies WHERE archived_at IS NULL"
        ).fetchall()
    finally:
        if conn is None:
            db_conn.close()

    for r in rows:
        tier = (r["processing_tier"] or "P3").upper()
        if tier not in out:
            tier = "P3"
        out[tier]["total"] += 1
        threshold_days = thresholds.get(tier)
        last_str = r["last_built_at"]
        if threshold_days is None:
            # P1: a daily-cadence row is "fresh" iff it has any last_built_at
            # within the last 24h. Anything older is "stale" by P1 standards
            # (it should have been touched today).
            if last_str:
                last_dt = _parse_dt(str(last_str))
                if last_dt is not None and (now - last_dt) < timedelta(days=1):
                    out[tier]["fresh"] += 1
                else:
                    out[tier]["stale"] += 1
            else:
                out[tier]["stale"] += 1
            continue
        if not last_str:
            out[tier]["stale"] += 1
            continue
        last_dt = _parse_dt(str(last_str))
        if last_dt is None:
            out[tier]["stale"] += 1
            continue
        if (now - last_dt) < timedelta(days=threshold_days):
            out[tier]["fresh"] += 1
        else:
            out[tier]["stale"] += 1

    return out
