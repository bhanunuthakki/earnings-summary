"""src/news/ — validated persistence for the structured ``news`` table.

The ``news`` table (migration 0065) feeds the material_news trigger. Both
ingestion feeds — the primary FMP stock-news fetcher and the FMP-independent
WebSearch+Opus fallback (later PRs) — map their wire shapes into a single
``NewsRow`` and persist through ``upsert_news_rows``, so the table contract —
above all the UTC ``'YYYY-MM-DD HH:MM:SS'`` ``published_at`` shape the trigger's
lexical recency compare depends on — is enforced in exactly one place.

The feeds, dispatcher, and pipeline stage that drive this gate ship in later
PRs; this package is the persistence substrate they consume.
"""

from __future__ import annotations

from news.store import NewsRow, upsert_news_rows

__all__ = [
    "NewsRow",
    "upsert_news_rows",
]
