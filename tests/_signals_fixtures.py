"""Shared helpers for the minimal `signals` substrate tests."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

from signals.store import sync_news_to_signals

# Verbatim 0065_news schema — sync_news_to_signals SELECTs these columns.
_NEWS_DDL = """
CREATE TABLE IF NOT EXISTS news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    headline TEXT NOT NULL,
    url TEXT NOT NULL,
    published_at TEXT NOT NULL,
    snippet TEXT,
    source TEXT,
    source_feed TEXT NOT NULL DEFAULT 'fmp_stock_news',
    fetched_at TEXT NOT NULL
);
"""

# Matches the consolidated 0001_initial_schema signals table and indexes without
# pulling unrelated tables (such as tracked_companies or ir_event_revisions).
_SIGNALS_DDL = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    url TEXT,
    firm TEXT,
    event_date TEXT(10),
    published_at TEXT NOT NULL,
    weight FLOAT DEFAULT (1.0) NOT NULL,
    cadence TEXT DEFAULT 'event' NOT NULL,
    source_feed TEXT,
    news_id INTEGER,
    created_at TEXT NOT NULL,
    quality_score FLOAT,
    PRIMARY KEY (id),
    CONSTRAINT ck_signals_signal_type CHECK (signal_type IN (
        'buyside_rating',
        'consensus_rating',
        'estimate_revision',
        'general_news',
        'investor_day',
        'media_appearance'
    )),
    CONSTRAINT ck_signals_cadence CHECK (cadence IN ('event', 'quarterly', 'scheduled'))
);
CREATE INDEX IF NOT EXISTS ix_signals_event_date
    ON signals (event_date) WHERE event_date IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_signals_ticker_type_date
    ON signals (ticker, signal_type, published_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS ux_signals_event
    ON signals (ticker, signal_type, event_date) WHERE event_date IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_signals_media_url
    ON signals (ticker, url) WHERE signal_type = 'media_appearance' AND url IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_signals_news_id
    ON signals (news_id) WHERE news_id IS NOT NULL;
"""


def make_news_then_signals(db_path: Path, news_rows: Sequence[tuple[str | None, ...]]) -> None:
    """Create minimal `news` and `signals` schemas, insert rows, and mirror them."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_NEWS_DDL + "\n" + _SIGNALS_DDL)
        conn.executemany(
            "INSERT INTO news "
            "(ticker, headline, url, published_at, snippet, source, source_feed, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            news_rows,
        )
        conn.commit()
        sync_news_to_signals(conn)
    finally:
        conn.close()


def signals_only(db_path: Path) -> None:
    """Create minimal `signals` schema with no `news` table."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_SIGNALS_DDL)
        conn.commit()
    finally:
        conn.close()
