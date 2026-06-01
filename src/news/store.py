"""Validated persistence gate for the ``news`` table (feeds the material_news trigger).

Both news ingestion feeds — the primary FMP stock-news fetcher and the
FMP-independent WebSearch+Opus fallback (later PRs) — map their wire shapes into
a single ``NewsRow`` and write through ``upsert_news_rows``. Centralizing
persistence here means the table contract is enforced in exactly one place,
regardless of which feed produced the row.

The single most important invariant this module guards is the ``published_at``
format. The material_news sensor computes its 24-hour recency threshold as a
``'YYYY-MM-DD HH:MM:SS'`` string and compares it LEXICALLY (``published_at >=
?``; see ``triggers.material_news._format_threshold``). That compare is only
chronological when every stored timestamp is UTC-naive and EXACTLY that
fixed-width shape — no ``T`` separator, no fractional seconds, no zone suffix.
An ISO-8601 value (``2026-05-30T14:00:00``) sorts AFTER a same-instant space
value (``'T'`` > ``' '``) and silently skews the window. The TEXT column cannot
enforce the shape, so ``NewsRow``'s validator does — and it RAISES (rejecting
the row) rather than coercing, because a malformed timestamp is a feed bug, not
something to paper over.

Dedup is on ``UNIQUE (ticker, url)`` (migration 0065): one row per
(ticker, article), but a syndicated URL may legitimately appear under two
tickers, so url alone is not unique. ``upsert_news_rows`` uses ``INSERT OR
IGNORE`` so re-running a feed over the same window is idempotent. The table is
created by Alembic (executors never create schema); if it is absent the sqlite
error propagates to the caller rather than being masked here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, field_validator


class NewsRow(BaseModel):
    """A validated news row ready to persist.

    The single contract gate for the ``news`` table: both the FMP and
    WebSearch+Opus feeds map into this shape. ``extra="forbid"`` so a feed that
    grows a stray field fails loud here rather than silently dropping data.
    """

    model_config = ConfigDict(extra="forbid")

    ticker: str
    headline: str
    url: str
    published_at: str  # UTC 'YYYY-MM-DD HH:MM:SS' (naive) — validated below
    snippet: str | None = None
    source: str | None = None
    source_feed: str  # 'fmp_stock_news' | 'websearch_opus'

    @field_validator("published_at")
    @classmethod
    def _utc_fixed_format(cls, v: str) -> str:
        """Reject any ``published_at`` that is not UTC ``'YYYY-MM-DD HH:MM:SS'``.

        ``strptime`` is called purely to validate the shape: it raises
        ``ValueError`` on any deviation (ISO ``T`` separator, fractional
        seconds, zone suffix, empty string), which Pydantic surfaces as a
        ``ValidationError``. The parsed datetime is intentionally discarded —
        the stored value stays the original string, UTC-naive by contract
        (no ``tzinfo`` is attached). This must stay in lockstep with
        ``triggers.material_news._format_threshold``, the consumer of the shape.
        """
        _ = datetime.strptime(v, "%Y-%m-%d %H:%M:%S")
        return v


def upsert_news_rows(conn: sqlite3.Connection, rows: Iterable[NewsRow]) -> tuple[int, int]:
    """``INSERT OR IGNORE`` each row into ``news``; return ``(inserted, deduped)``.

    Idempotent on ``UNIQUE (ticker, url)`` — re-running a feed over the same
    window inserts nothing the second time. ``fetched_at`` is stamped here, once
    per call, in UTC at write time; it records WHEN the row was persisted,
    distinct from the article's ``published_at``. ``deduped`` is the count of
    rows that collided with an existing (ticker, url) and were ignored
    (``len(rows) - inserted``).

    The caller owns the connection — both feeds share one so a story seen by
    both is stored once — and owns the table's existence: if ``news`` is absent
    the sqlite error propagates rather than being masked here.
    """
    fetched_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    pending = list(rows)
    inserted = 0
    for r in pending:
        cur = conn.execute(
            "INSERT OR IGNORE INTO news "
            "(ticker, headline, url, published_at, snippet, source, source_feed, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r.ticker,
                r.headline,
                r.url,
                r.published_at,
                r.snippet,
                r.source,
                r.source_feed,
                fetched_at,
            ),
        )
        inserted += cur.rowcount if cur.rowcount > 0 else 0
    conn.commit()
    return inserted, len(pending) - inserted
