"""
src/news/ — the validated persistence gate for the structured `news` table.

The material_news trigger reads `news`; two ingestion feeds (the primary FMP
stock-news fetcher and the FMP-independent WebSearch+Opus fallback) write it.
Both map their source payloads into the one canonical ``NewsRow`` and persist
via ``upsert_news_rows`` — so the table contract, above all the UTC
``published_at`` format the trigger's recency compare depends on, is enforced
in exactly one place.

The store module exposes:
  * NewsRow — the validated row both feeds map into (UTC-format validator).
  * upsert_news_rows — INSERT OR IGNORE writer, idempotent on UNIQUE (ticker, url).
  * SOURCE_FEED_FMP / SOURCE_FEED_WEBSEARCH — `source_feed` provenance tags.
"""

from __future__ import annotations

from news.store import (
    SOURCE_FEED_FMP,
    SOURCE_FEED_WEBSEARCH,
    NewsRow,
    upsert_news_rows,
)

__all__ = [
    "SOURCE_FEED_FMP",
    "SOURCE_FEED_WEBSEARCH",
    "NewsRow",
    "upsert_news_rows",
]
