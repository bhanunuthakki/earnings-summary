"""
src/news/ — the validated persistence gate for the structured `news` table.

The material_news trigger reads `news`; the ingestion feeds — the primary FMP
stock-news fetcher, the FMP-independent WebSearch+Opus fallback, and the
additive EDGAR / yfinance-grades feeds (directives/news_sources_plan.md) —
write it. All map their source payloads into the one canonical ``NewsRow`` and
persist via ``upsert_news_rows`` — so the table contract, above all the UTC
``published_at`` format the trigger's recency compare depends on, is enforced
in exactly one place.

The store module exposes:
  * NewsRow — the validated row every feed maps into (UTC-format validator).
  * upsert_news_rows — INSERT OR IGNORE writer, idempotent on UNIQUE (ticker, url).
  * drop_duplicate_stories — cross-feed (ticker, normalized headline, date)
    dedup guard the additive feeds run through.
  * SOURCE_FEED_* — `source_feed` provenance tags.
"""

from __future__ import annotations

from news.store import (
    SOURCE_FEED_EDGAR_8K,
    SOURCE_FEED_EDGAR_13D,
    SOURCE_FEED_EDGAR_13G,
    SOURCE_FEED_FMP,
    SOURCE_FEED_WEBSEARCH,
    SOURCE_FEED_YF_GRADES,
    NewsRow,
    drop_duplicate_stories,
    normalize_headline,
    upsert_news_rows,
)

__all__ = [
    "SOURCE_FEED_EDGAR_8K",
    "SOURCE_FEED_EDGAR_13D",
    "SOURCE_FEED_EDGAR_13G",
    "SOURCE_FEED_FMP",
    "SOURCE_FEED_WEBSEARCH",
    "SOURCE_FEED_YF_GRADES",
    "NewsRow",
    "drop_duplicate_stories",
    "normalize_headline",
    "upsert_news_rows",
]
