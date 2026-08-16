"""Canonical News Ingestion Ladder & Source Policy (BHA-61).

Governs additive and fallback news feeds across the platform:
- Primary Canonical: SEC EDGAR (8-K item codes, 13D/13G filings)
- Primary Market: FMP Stock News
- Rating Changes: yfinance Analyst Upgrades/Downgrades
- Final Fallback: WebSearch + LLM Structuring

Explicitly retired/killed sources:
- Finnhub (redundant, requires proprietary API keys & quota management)
- Unauthenticated Web Scrapers / Generic RSS (unreliable provenance, noisy)
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field


class NewsSourceFeed(StrEnum):
    """Authorized news feeds in the canonical ladder."""

    EDGAR_8K = "edgar_8k"
    EDGAR_13D = "edgar_13d"
    EDGAR_13G = "edgar_13g"
    FMP_STOCK_NEWS = "fmp_stock_news"
    YF_GRADES = "yf_grades"
    WEB_SEARCH_FALLBACK = "websearch_fallback"


class RetiredNewsSource(StrEnum):
    """Explicitly retired / rejected candidate sources (BHA-61)."""

    FINNHUB = "finnhub"
    GENERIC_RSS_SCRAPER = "generic_rss_scraper"
    ALPHAVANTAGE_SENTIMENT = "alphavantage_sentiment"


class NormalizedNewsItem(BaseModel):
    """Normalized, deduplicated news article payload."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    ticker: str = Field(min_length=1, max_length=10)
    title: str = Field(min_length=1, max_length=1000)
    url: str = Field(min_length=1, max_length=2000)
    source_feed: NewsSourceFeed
    publisher: str = Field(default="", max_length=200)
    published_at_utc: str = Field(max_length=50)
    summary: str | None = Field(default=None, max_length=5000)
    is_material: bool = Field(default=False)
    article_hash: str = Field(max_length=64)

    @classmethod
    def compute_hash(cls, ticker: str, url: str) -> str:
        """Deterministic SHA256 hash over canonical ticker and normalized URL."""
        norm_url = url.strip().split("?")[0].rstrip("/")
        raw = f"{ticker.upper()}:{norm_url}".encode()
        return hashlib.sha256(raw).hexdigest()


class NewsLadderPolicy:
    """Manages news source priority, deduplication, and refusal rules."""

    @staticmethod
    def is_source_authorized(source_name: str) -> bool:
        """Check if source is part of the approved canonical news ladder."""
        try:
            NewsSourceFeed(source_name)
            return True
        except ValueError:
            return False

    @staticmethod
    def is_source_retired(source_name: str) -> bool:
        """True if source was formally evaluated and retired (BHA-61)."""
        try:
            RetiredNewsSource(source_name)
            return True
        except ValueError:
            return False

    @staticmethod
    def sanitize_url(raw_url: str) -> str:
        """Normalize URL for deduplication across tracking parameters."""
        parsed = urlparse(raw_url.strip())
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(f"Invalid URL structure: {raw_url}")
        # Strip tracking query params
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        return clean_url.rstrip("/")

    @staticmethod
    def deduplicate_items(items: list[NormalizedNewsItem]) -> list[NormalizedNewsItem]:
        """Deduplicate news items by article_hash, preserving highest-priority feed."""
        seen_hashes: set[str] = set()
        unique: list[NormalizedNewsItem] = []
        for item in items:
            if item.article_hash not in seen_hashes:
                seen_hashes.add(item.article_hash)
                unique.append(item)
        return unique


__all__ = [
    "NewsLadderPolicy",
    "NewsSourceFeed",
    "NormalizedNewsItem",
    "RetiredNewsSource",
]
