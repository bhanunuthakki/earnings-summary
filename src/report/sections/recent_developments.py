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
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from llm_client import (
    compose_anchor_block,
    generate_recent_developments,
    is_hard_stop,
    load_bear_anchor,
    load_ir_anchor,
    load_priors_anchor,
    load_thesis_anchor,
)
from report.models import (
    RecentDevelopmentsSection,
    SectionStatus,
)
from report.render_clock import render_now
from report.sections._common import budget_gate, budget_skip_missing, missing

log = logging.getLogger(__name__)

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
    force_budget_bypass: bool = False,
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
                    f"with Gemini fallback. Cache TTL {cache_ttl_days}d "
                    f"at {NEWS_CACHE_DIRNAME}/{ticker.upper()}.json."
                ),
            ),
            news_days_window=news_days,
        )

    skip = budget_gate(
        "recent_developments", "Recent developments (§9)", repo_root, bypass=force_budget_bypass
    )
    if skip is not None:
        return RecentDevelopmentsSection(
            status=SectionStatus.BUDGET_SKIPPED,
            budget_skip=skip,
            missing=budget_skip_missing(ticker, "recent_developments", skip),
            news_days_window=news_days,
        )

    anchor_block = compose_anchor_block(
        load_thesis_anchor(repo_root, ticker),
        load_bear_anchor(repo_root, ticker),
        load_ir_anchor(repo_root, ticker),
        load_priors_anchor(repo_root, ticker),
    )
    try:
        content_md = generate_recent_developments(
            ticker, news_days=news_days, anchor_block=anchor_block
        )
    except Exception as exc:
        # Hard LLM CALL exception. Propagate genuine hard stops (monthly budget
        # cap, CLI not installed); degrade transient operational failures
        # (timeout, non-zero exit, both Claude + Gemini momentarily down) to a
        # loud MISSING_DATA banner so one flaky §8 call can't abort the whole
        # multi-section build. We do NOT write the cache on failure, so a
        # re-run retries the call instead of serving a degraded stub.
        if is_hard_stop(exc):
            raise
        log.error(
            "recent_developments LLM call failed for %s: %s: %s",
            ticker,
            type(exc).__name__,
            exc,
        )
        return RecentDevelopmentsSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="SYNTHESIZE(recent_developments_websearch)",
                fix_command=(
                    f"python execution/build_artifacts.py --ticker {ticker.upper()} --enable-llm"
                ),
                detail=(
                    "The recent-developments LLM call failed operationally "
                    "(timeout, or both the Claude CLI and the Gemini fallback "
                    "were momentarily unavailable). Re-run to retry."
                ),
            ),
            news_days_window=news_days,
        )
    section = RecentDevelopmentsSection(
        status=SectionStatus.OK,
        cached_at=render_now(),
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
    age = render_now() - cached_at
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
