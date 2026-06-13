"""Shared helpers for the `signals` substrate tests (alembic 0095).

The fast path: stamp a tmp DB at the prior head, hand-create the minimal `news`
table (verbatim 0065 schema) so the backfill has something to mirror, then run
only 0095. Keeps the store / migration / panel tests hermetic and quick without
the full migration chain (the diet-guard test needs the whole inbox schema and
builds it separately).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0094_validation_issue_resolution"
SIGNALS_HEAD = "0095_signals"

# Verbatim 0065_news schema — the backfill SELECTs these columns.
_NEWS_DDL = """
CREATE TABLE news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    headline TEXT NOT NULL,
    url TEXT NOT NULL,
    published_at TEXT NOT NULL,
    snippet TEXT,
    source TEXT,
    source_feed TEXT NOT NULL DEFAULT 'fmp_stock_news',
    fetched_at TEXT NOT NULL
)
"""


def build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def make_news_then_signals(db_path: Path, news_rows: Sequence[tuple[str | None, ...]]) -> None:
    """Create `news` with ``news_rows`` at the prior-head stamp, then run 0095
    so the migration backfills them. Each row is
    (ticker, headline, url, published_at, snippet, source, source_feed, fetched_at).
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(_NEWS_DDL)
        conn.executemany(
            "INSERT INTO news "
            "(ticker, headline, url, published_at, snippet, source, source_feed, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            news_rows,
        )
        conn.commit()
    finally:
        conn.close()
    cfg = build_config(db_path)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, SIGNALS_HEAD)


def signals_only(db_path: Path) -> None:
    """Stamp prior head + run 0095 with NO `news` table — an empty `signals`."""
    cfg = build_config(db_path)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, SIGNALS_HEAD)
