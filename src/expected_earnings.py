"""expected_earnings store — the canonical next-earnings calendar (0082).

One row per (ticker, expected_date) observed by the daily refresher
(execution/refresh_expected_earnings.py), which resolves each active ticker
through the ``sources.earnings_calendar`` preference stack (FMP on-disk cache
first, yfinance fallback). Readers:

  * ``dashboard.upcoming`` — the Home rail's earnings strip (the +91d
    estimate survives only as a fallback for tickers with no calendar row)
  * ``pipeline.research_cockpit`` — the Next ER column
  * portfolio-tracker's read-only bridge (``services/earnings_summary.py``) —
    SELECTs ``ticker, MIN(expected_date)``; revived unchanged by 0082

Write helpers take an open connection (the refresher owns one transaction per
run); the reader degrades to ``{}`` on a pre-0082 DB so render paths never
break on a missing table.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

__all__ = ["last_reported_by_ticker", "record_next_earnings", "upcoming_by_ticker"]


def _now_stamp(now: datetime | None) -> str:
    """Naive-UTC ``YYYY-MM-DD HH:MM:SS`` — matches the column server_default."""
    dt = (now or datetime.now(UTC)).replace(tzinfo=None)
    return dt.isoformat(sep=" ", timespec="seconds")


def record_next_earnings(
    conn: sqlite3.Connection,
    ticker: str,
    expected_date: date,
    detected_source: str,
    *,
    today: date,
    now: datetime | None = None,
) -> None:
    """Upsert one ticker's freshly-observed next earnings date.

    On conflict the row keeps ``first_seen_at`` and refreshes ``last_seen_at``
    + ``detected_source``. Other FUTURE-dated rows for the ticker are pruned —
    a reschedule self-heals instead of leaving a stale earlier date for
    ``MIN(expected_date)`` readers to pick up. Past rows are kept as history.
    Caller commits.
    """
    stamp = _now_stamp(now)
    iso = expected_date.isoformat()
    conn.execute(
        """
        INSERT INTO expected_earnings
            (ticker, expected_date, detected_source, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(ticker, expected_date) DO UPDATE SET
            detected_source = excluded.detected_source,
            last_seen_at = excluded.last_seen_at
        """,
        (ticker.upper(), iso, detected_source, stamp, stamp),
    )
    conn.execute(
        "DELETE FROM expected_earnings "
        "WHERE ticker = ? AND expected_date >= ? AND expected_date != ?",
        (ticker.upper(), today.isoformat(), iso),
    )


def last_reported_by_ticker(conn: sqlite3.Connection, before: date) -> dict[str, date]:
    """Latest expected_date < ``before`` per ticker — the most recent PAST
    report date. Past rows are kept as history by :func:`record_next_earnings`
    (only future-dated reschedules are pruned), which is exactly what makes
    this read possible. Same tolerance contract as :func:`upcoming_by_ticker`:
    a pre-0082 DB or an unparseable date degrades to the ticker being absent.
    """
    try:
        rows = conn.execute(
            "SELECT ticker, MAX(expected_date) FROM expected_earnings "
            "WHERE expected_date < ? GROUP BY ticker",
            (before.isoformat(),),
        ).fetchall()
    except sqlite3.Error:
        return {}
    out: dict[str, date] = {}
    for ticker, iso in rows:
        try:
            out[str(ticker).upper()] = date.fromisoformat(str(iso)[:10])
        except ValueError:
            continue
    return out


def upcoming_by_ticker(conn: sqlite3.Connection, on_or_after: date) -> dict[str, date]:
    """Earliest expected_date >= ``on_or_after`` per ticker.

    Tolerant: a pre-0082 DB (no table) or an unparseable date degrades to the
    ticker being absent, so strip/cockpit renders fall back rather than raise.
    """
    try:
        rows = conn.execute(
            "SELECT ticker, MIN(expected_date) FROM expected_earnings "
            "WHERE expected_date >= ? GROUP BY ticker",
            (on_or_after.isoformat(),),
        ).fetchall()
    except sqlite3.Error:
        return {}
    out: dict[str, date] = {}
    for ticker, iso in rows:
        try:
            out[str(ticker).upper()] = date.fromisoformat(str(iso)[:10])
        except ValueError:
            continue
    return out
