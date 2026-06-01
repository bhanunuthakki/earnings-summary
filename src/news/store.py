"""
src/news/store.py — the single validated persistence gate for the `news` table.

Both ingestion feeds — the primary FMP stock-news fetcher and the
FMP-independent WebSearch+Opus fallback — map their source-specific payloads
into one canonical ``NewsRow`` and write through ``upsert_news_rows``.
Centralizing the write here means the table's contract — above all the UTC
``published_at`` format the material_news trigger's lexical recency compare
depends on — is enforced once, in code, regardless of which feed produced the
row.

The `news` table is created by Alembic migration ``0065_news``; this module
never creates schema (executors own data, migrations own schema). If the table
is absent, ``upsert_news_rows`` raises ``sqlite3.OperationalError`` and the
calling fetcher logs and exits non-zero rather than silently dropping news.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, field_validator

# The exact stored shape of `published_at` (and `fetched_at`): UTC, naive, fixed
# width, space separator. The trigger compares ``published_at >= ?`` LEXICALLY
# (src/triggers/material_news.py _format_threshold), which is chronological only
# if every value is this canonical form — no 'T', no zone suffix, no fractional
# seconds. NewsRow's validator is the code-side enforcer the TEXT column cannot
# be.
_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# `source_feed` provenance tags — which ingester wrote the row. Defined here so
# both feeds and the dedup tests reference one spelling, not scattered literals.
# SOURCE_FEED_FMP matches the column's server_default in migration 0065_news.
SOURCE_FEED_FMP = "fmp_stock_news"
SOURCE_FEED_WEBSEARCH = "websearch_opus"


class NewsRow(BaseModel):
    """A validated news row ready to persist — the single contract gate for the
    `news` table. Both the FMP and WebSearch+Opus feeds map into this.

    ``extra="forbid"`` so a feed that drifts an unexpected field in fails loud at
    construction rather than silently carrying it.
    """

    model_config = ConfigDict(extra="forbid")

    ticker: str
    headline: str
    url: str
    published_at: str  # UTC 'YYYY-MM-DD HH:MM:SS' — enforced below
    snippet: str | None = None
    source: str | None = None
    source_feed: str  # SOURCE_FEED_FMP | SOURCE_FEED_WEBSEARCH

    @field_validator("published_at")
    @classmethod
    def _utc_fixed_format(cls, v: str) -> str:
        """Reject any value that isn't EXACTLY UTC 'YYYY-MM-DD HH:MM:SS'.

        Raises (rejecting the row) rather than coercing — a malformed timestamp
        is a feed bug, and a wrongly-shaped value would silently skew the
        trigger's lexical 24h window. ISO-with-'T', fractional seconds, a zone
        suffix, a date alone, and empty all fail ``strptime`` outright.

        The round-trip equality check then closes the one gap ``strptime`` leaves
        open: it parses non-zero-padded fields (``'2026-5-30 ...'``), which would
        be the wrong *width* and so sort incorrectly under the trigger's lexical
        compare. A value is canonical iff re-emitting it via the same format
        reproduces it byte-for-byte — the airtight form of the §2.3 invariant.
        """
        if datetime.strptime(v, _DATETIME_FORMAT).strftime(_DATETIME_FORMAT) != v:
            raise ValueError(f"published_at must be canonical UTC '{_DATETIME_FORMAT}': {v!r}")
        return v


def upsert_news_rows(conn: sqlite3.Connection, rows: Iterable[NewsRow]) -> tuple[int, int]:
    """INSERT OR IGNORE each row into `news`; return ``(inserted, deduped)``.

    Idempotent on the table's ``UNIQUE (ticker, url)`` constraint: a row whose
    (ticker, url) already exists is ignored (counted in ``deduped``), so both
    feeds — and same-day re-runs — converge without duplicating stories. A story
    seen by both feeds under the same ticker is stored once; a syndicated URL
    under two tickers is two rows (the compound key is the point).

    ``fetched_at`` is stamped here in UTC at write time (one value per call). The
    `news` table is assumed pre-created by migration ``0065_news``; if it is
    absent this raises ``sqlite3.OperationalError``, which the caller surfaces
    (exit non-zero) rather than dropping news silently.
    """
    fetched_at = datetime.now(UTC).strftime(_DATETIME_FORMAT)
    inserted = 0
    total = 0
    for row in rows:
        total += 1
        cur = conn.execute(
            "INSERT OR IGNORE INTO news "
            "(ticker, headline, url, published_at, snippet, source, source_feed, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.ticker,
                row.headline,
                row.url,
                row.published_at,
                row.snippet,
                row.source,
                row.source_feed,
                fetched_at,
            ),
        )
        if cur.rowcount > 0:
            inserted += 1
    conn.commit()
    return inserted, total - inserted
