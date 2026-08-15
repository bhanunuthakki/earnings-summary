"""Unit Tests: Canonical News Ladder and Retired Source Policy (BHA-61)."""

from __future__ import annotations

import pytest

from news.news_ladder import (
    NewsLadderPolicy,
    NewsSourceFeed,
    NormalizedNewsItem,
    RetiredNewsSource,
)


def test_authorized_news_sources() -> None:
    assert NewsLadderPolicy.is_source_authorized("edgar_8k") is True
    assert NewsLadderPolicy.is_source_authorized("edgar_13d") is True
    assert NewsLadderPolicy.is_source_authorized("yf_grades") is True
    assert NewsLadderPolicy.is_source_authorized("fmp_stock_news") is True
    assert NewsLadderPolicy.is_source_authorized("websearch_fallback") is True
    assert NewsLadderPolicy.is_source_authorized("random_untrusted_feed") is False


def test_retired_sources_disposition() -> None:
    # Explicitly retired candidate legs
    assert NewsLadderPolicy.is_source_retired(RetiredNewsSource.FINNHUB) is True
    assert NewsLadderPolicy.is_source_retired(RetiredNewsSource.GENERIC_RSS_SCRAPER) is True
    assert NewsLadderPolicy.is_source_retired(RetiredNewsSource.ALPHAVANTAGE_SENTIMENT) is True
    assert NewsLadderPolicy.is_source_authorized("finnhub") is False


def test_url_sanitization_and_hash_deduplication() -> None:
    url1 = "https://www.sec.gov/ix?doc=/Archives/edgar/data/320193/000032019324000106/aapl-20240928.htm?utm_source=feed"
    url2 = "https://www.sec.gov/ix?doc=/Archives/edgar/data/320193/000032019324000106/aapl-20240928.htm"

    hash1 = NormalizedNewsItem.compute_hash("AAPL", url1)
    hash2 = NormalizedNewsItem.compute_hash("AAPL", url2)
    assert hash1 == hash2

    item1 = NormalizedNewsItem(
        ticker="AAPL",
        title="Apple 8-K Item 2.02",
        url=url1,
        source_feed=NewsSourceFeed.EDGAR_8K,
        published_at_utc="2026-08-15T12:00:00Z",
        article_hash=hash1,
    )
    item2 = NormalizedNewsItem(
        ticker="AAPL",
        title="Apple 8-K Duplicate",
        url=url2,
        source_feed=NewsSourceFeed.EDGAR_8K,
        published_at_utc="2026-08-15T12:05:00Z",
        article_hash=hash2,
    )

    deduped = NewsLadderPolicy.deduplicate_items([item1, item2])
    assert len(deduped) == 1
    assert deduped[0].title == "Apple 8-K Item 2.02"


def test_invalid_url_rejection() -> None:
    with pytest.raises(ValueError, match="Invalid URL structure"):
        NewsLadderPolicy.sanitize_url("not_a_url")
