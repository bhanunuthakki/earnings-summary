"""§8 Recent developments — WebSearch-driven news brief with on-disk cache.

When `enable_llm` is False (default for dev runs), returns LLM_PENDING with the
fix command embedded. When True, checks `.tmp/news_cache/<TICKER>.json` for a
fresh cached payload; if absent or older than `cache_ttl_days`, calls
`llm_client.generate_recent_developments` (which routes through Claude
WebSearch+WebFetch with the standard fallback chain) and writes a new cache
entry.

`force_refresh=True` bypasses the cache lookup. Used by the planned
`refresh_news` CLI in Phase 4.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from llm_client import generate_recent_developments
from report.models import (
    RecentDevelopmentsSection,
    SectionStatus,
)
from report.sections._common import missing

NEWS_CACHE_DIRNAME = ".tmp/news_cache"
DEFAULT_TTL_DAYS = 7
DEFAULT_NEWS_DAYS_WINDOW = 7


def build(
    ticker: str,
    repo_root: Path,
    enable_llm: bool,
    news_days: int = DEFAULT_NEWS_DAYS_WINDOW,
    cache_ttl_days: int = DEFAULT_TTL_DAYS,
    force_refresh: bool = False,
) -> RecentDevelopmentsSection:
    cache_path = repo_root / NEWS_CACHE_DIRNAME / f"{ticker.upper()}.json"

    if not force_refresh:
        cached = _read_cache(cache_path, cache_ttl_days)
        if cached is not None:
            return cached

    if not enable_llm:
        return RecentDevelopmentsSection(
            status=SectionStatus.LLM_PENDING,
            missing=missing(
                stage="SYNTHESIZE(recent_developments_websearch)",
                fix_command=(
                    f"python execution/build_artifacts.py --ticker {ticker.upper()} --enable-llm"
                ),
                detail=(
                    f"Pass --enable-llm to populate. Routes through Claude WebSearch+WebFetch "
                    f"(subscription billing) with Gemini fallback. Cache TTL {cache_ttl_days}d "
                    f"at {NEWS_CACHE_DIRNAME}/{ticker.upper()}.json."
                ),
            ),
            news_days_window=news_days,
        )

    content_md = generate_recent_developments(ticker, news_days=news_days)
    section = RecentDevelopmentsSection(
        status=SectionStatus.OK,
        cached_at=datetime.now(UTC),
        news_days_window=news_days,
        content_md=content_md,
    )
    _write_cache(cache_path, section)
    return section


def _read_cache(path: Path, ttl_days: int) -> RecentDevelopmentsSection | None:
    """Return a cached section if present and within TTL; else None.

    Treats any parse error or missing field as a cache miss — the caller
    falls back to a fresh LLM call.
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cached_at_raw = data["cached_at"]
        cached_at = datetime.fromisoformat(cached_at_raw)
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None
    if cached_at.tzinfo is None:
        cached_at = cached_at.replace(tzinfo=UTC)
    age = datetime.now(UTC) - cached_at
    if age > timedelta(days=ttl_days):
        return None
    return RecentDevelopmentsSection(
        status=SectionStatus.OK,
        cached_at=cached_at,
        news_days_window=int(data.get("news_days_window", DEFAULT_NEWS_DAYS_WINDOW)),
        content_md=str(data.get("content_md", "")),
    )


def _write_cache(path: Path, section: RecentDevelopmentsSection) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cached_at": section.cached_at.isoformat() if section.cached_at else "",
        "news_days_window": section.news_days_window,
        "content_md": section.content_md or "",
    }
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
