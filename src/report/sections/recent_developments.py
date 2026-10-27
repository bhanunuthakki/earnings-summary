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
from typing import cast

from llm_client import (
    compose_anchor_block,
    generate_recent_developments,
    is_hard_stop,
    load_bear_anchor,
    load_ir_anchor,
    load_priors_anchor,
    load_thesis_anchor,
)
from report.file_capture import read_captured_text
from report.models import (
    RecentDevelopmentEventPayload,
    RecentDevelopmentsPayload,
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
        cached = _read_cache(repo_root, cache_path, cache_ttl_days)
        if cached is not None:
            return cached
        if _is_legacy_markdown_cache(cache_path):
            return RecentDevelopmentsSection(
                status=SectionStatus.MISSING_DATA,
                missing=missing(
                    stage="SYNTHESIZE(recent_developments_structured)",
                    fix_command=(
                        f"python execution/build_artifacts.py --ticker {ticker.upper()} "
                        "--enable-llm --refresh-news"
                    ),
                    detail=(
                        "The cached news brief uses the retired Markdown-only format. "
                        "Run an explicit refresh to create a structured report output."
                    ),
                ),
                news_days_window=news_days,
            )

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
        structured = generate_recent_developments(
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
        content_md=_render_content(structured),
        structured_output=structured,
    )
    _write_cache(cache_path, section)
    return section


def _read_cache(repo_root: Path, path: Path, ttl_days: int) -> RecentDevelopmentsSection | None:
    """Return a cached section if present and within TTL; else None.

    Treats any parse error or missing field as a cache miss — the caller
    falls back to a fresh LLM call.
    """
    try:
        raw_text = read_captured_text(repo_root, path)
    except (FileNotFoundError, OSError):
        return None
    try:
        data_obj: object = json.loads(raw_text)
        if not isinstance(data_obj, dict):
            return None
        data = cast("dict[str, object]", data_obj)
        structured_raw = data.get("structured_output")
        if structured_raw is None:
            return None
        structured = RecentDevelopmentsPayload.model_validate(structured_raw)
        cached_at_raw = data["cached_at"]
        if not isinstance(cached_at_raw, str):
            return None
        cached_at = datetime.fromisoformat(cached_at_raw)
        window_raw = data.get("news_days_window", DEFAULT_NEWS_DAYS_WINDOW)
        if not isinstance(window_raw, int) or isinstance(window_raw, bool):
            return None
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
        news_days_window=window_raw,
        content_md=_render_content(structured),
        structured_output=structured,
    )


def _write_cache(path: Path, section: RecentDevelopmentsSection) -> None:
    from report.file_capture import hold_capture_write_set

    repo_root = path.parents[2] if len(path.parents) >= 3 else path.parent
    with hold_capture_write_set(repo_root, "capture-news-cache"):
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cached_at": section.cached_at.isoformat() if section.cached_at else "",
            "news_days_window": section.news_days_window,
            "structured_output": (
                section.structured_output.model_dump(mode="json")
                if section.structured_output is not None
                else None
            ),
        }
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def _is_legacy_markdown_cache(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and "content_md" in data and "structured_output" not in data


def _render_content(payload: RecentDevelopmentsPayload) -> str:
    sections = {
        "material_news": "Material news",
        "sector_context": "Sector / regulatory context",
        "watch_this_week": "Watch this week",
    }
    grouped: dict[str, list[RecentDevelopmentEventPayload]] = {}
    for event in payload.events:
        grouped.setdefault(event.category, []).append(event)
    lines: list[str] = []
    for category, title in sections.items():
        events = grouped.get(category, [])
        if not events:
            continue
        lines.append(f"### {title}")
        for event in events:
            citation_date = event.published_at[:10]
            gloss = f"{event.factual_summary} {event.thesis_implication}"
            lines.append(
                f"- **{event.headline}** — {gloss} "
                f"[Source: {event.publisher}, {citation_date}, {event.url}]"
            )
        lines.append("")
    if payload.search_status == "no_qualifying_events":
        sources = "; ".join(payload.sources_opened)
        queries = "; ".join(payload.queries_run)
        lines.extend(
            [
                "### Material news",
                "*No qualifying material event was identified in the searched window.*",
                f"Searches run: {queries}. Window covered: {payload.window_start} through "
                f"{payload.window_end}. Sources opened: {sources}.",
            ]
        )
    return "\n".join(lines).strip()
