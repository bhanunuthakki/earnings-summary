"""execution/fetch_news_websearch.py — FMP-independent WebSearch+Opus fallback feed.

The genuine FMP-independent path for the `news` table: one Opus ``call_llm_with_web``
per ticker that searches recent news and returns structured rows, which are then
normalized to UTC and mapped to NewsRows. Exists so the material_news trigger
keeps eating when FMP refuses (quota/plan/legacy) — see scratch/plans/news_table_plan.md §4.3.

Reuses the trigger's discipline:
  * Opus model via ``purpose="news_structuring"`` (resolved by call_llm_with_web).
  * llm_artifacts cache keyed on (ticker, UTC date, thesis-anchor sha) so a
    same-day re-run is a cache hit and makes NO second Opus call.
  * "Degrade, never fabricate": an item whose publish date can't be confidently
    placed in UTC is DROPPED, and any LLM/web failure returns [] for the ticker.

This module returns NewsRows; the dispatcher (execution/fetch_news.py) owns the
shared DB connection and persists them via upsert_news_rows (source_feed
``websearch_opus``).
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from llm.anchors import load_thesis_anchor  # noqa: E402
from llm.prompt_versions import prompt_version_for  # noqa: E402
from llm.untrusted import spotlight  # noqa: E402
from llm_artifact_store import (  # noqa: E402
    UpsertRequest,
    compute_input_sha256,
    read_current,
    upsert,
)
from llm_client import structure_recent_news_json  # noqa: E402
from news.store import SOURCE_FEED_WEBSEARCH, NewsRow  # noqa: E402

_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_EASTERN = ZoneInfo("America/New_York")
_PURPOSE = "news_structuring"
# Sourced from the registry (not a hardcoded literal) so a prompt bump in
# prompt_versions.py rotates this cache key too — the registry is the single
# bump-point for prompt changes (directives/llm_calls.md).
_PROMPT_VERSION = prompt_version_for(_PURPOSE)
DEFAULT_NEWS_DAYS = 2

# Timezone tags the structuring prompt may emit, normalized upper-case.
_UTC_TZ = frozenset({"", "UTC", "Z", "GMT", "UT", "ZULU"})
_ET_TZ = frozenset({"ET", "EST", "EDT", "EASTERN", "AMERICA/NEW_YORK"})
# Accepted published_at shapes (date-only -> midnight of that day in its tz).
_PARSE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")


def _log(event: str, **kwargs: object) -> None:
    print(json.dumps({"event": event, **kwargs}), file=sys.stderr)


# ---------------------------------------------------------------------------
# Timestamp normalization + mapping
# ---------------------------------------------------------------------------


def _normalize_to_utc(published_at: str, published_tz: str | None) -> str | None:
    """Normalize an extracted timestamp to canonical UTC 'YYYY-MM-DD HH:MM:SS'.

    Returns None (drop the item) when the value can't be parsed or its zone can't
    be confidently resolved — the "never fabricate a timestamp" rule. A trailing
    'Z' forces UTC regardless of the published_tz hint; a date-only value is taken
    at 00:00:00 of that day in its stated zone."""
    raw = published_at.strip()
    force_utc = raw.endswith("Z")
    raw = raw.rstrip("Zz").strip()

    parsed: datetime | None = None
    for fmt in _PARSE_FORMATS:
        try:
            parsed = datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return None

    tz = "UTC" if force_utc else (published_tz or "").strip().upper()
    if tz in _UTC_TZ:
        aware = parsed.replace(tzinfo=UTC)
    elif tz in _ET_TZ:
        aware = parsed.replace(tzinfo=_EASTERN)
    else:
        return None  # unknown zone -> can't place in UTC -> drop
    return aware.astimezone(UTC).strftime(_DATETIME_FORMAT)


def _map_item(item: object, ticker: str) -> NewsRow | None:
    """Map one structured item to a NewsRow, or None to drop it.

    Drops items missing a non-empty headline / url, or whose timestamp can't be
    normalized to UTC. The NewsRow validator is the final backstop."""
    if not isinstance(item, dict):
        return None
    obj = cast("dict[str, object]", item)

    headline = obj.get("headline")
    url = obj.get("url")
    published_at = obj.get("published_at")
    if not isinstance(headline, str) or not headline.strip():
        return None
    if not isinstance(url, str) or not url.strip():
        return None
    if not isinstance(published_at, str) or not published_at.strip():
        return None

    tz_raw = obj.get("published_tz")
    utc = _normalize_to_utc(published_at, tz_raw if isinstance(tz_raw, str) else None)
    if utc is None:
        return None

    snippet_raw = obj.get("snippet")
    snippet = snippet_raw.strip() if isinstance(snippet_raw, str) and snippet_raw.strip() else None
    source_raw = obj.get("source")
    source = source_raw.strip() if isinstance(source_raw, str) and source_raw.strip() else None

    try:
        return NewsRow(
            ticker=ticker,
            headline=headline.strip(),
            url=url.strip(),
            published_at=utc,
            snippet=snippet,
            source=source,
            source_feed=SOURCE_FEED_WEBSEARCH,
        )
    except ValidationError:
        return None


# ---------------------------------------------------------------------------
# Cache (llm_artifacts, purpose 'news_structuring')
# ---------------------------------------------------------------------------


def _load_anchor(ticker: str) -> str:
    """Best-effort thesis anchor for framing + the cache key; "" on any failure.

    Spotlighted before it enters the prompt: this path loads the thesis
    anchor directly (not via ``compose_anchor_block``, which wraps for the
    composed consumers), so it owns the untrusted-data wrap itself. The wrap
    is deterministic, so the same anchor still cache-hits."""
    try:
        return spotlight(
            load_thesis_anchor(PROJECT_ROOT, ticker),
            source="analyst thesis anchor (stored research context)",
        )
    except Exception as exc:  # anchor is optional — never block the fetch
        _log("news_websearch_anchor_load_failed", ticker=ticker, error=str(exc))
        return ""


def _cache_inputs(ticker: str, utc_date: str, anchor_text: str) -> list[bytes | str]:
    """Deterministic cache key: same ticker, same UTC day, same anchor -> hit."""
    anchor_sha = hashlib.sha256(anchor_text.encode("utf-8")).hexdigest()
    return [f"ticker={ticker}", f"date={utc_date}", f"anchor_sha={anchor_sha}"]


def _read_cached(
    ticker: str, db_path: str | Path, cache_inputs: list[bytes | str]
) -> list[object] | None:
    """Return the cached structured items for today, or None on a miss."""
    existing = read_current(ticker=ticker, purpose=_PURPOSE, db_path=db_path)
    if existing is None:
        return None
    new_sha = compute_input_sha256(prompt_version=_PROMPT_VERSION, cache_inputs=cache_inputs)
    if (
        not existing.dirty
        and existing.input_sha256 == new_sha
        and isinstance(existing.content_json, list)
    ):
        _log("news_websearch_cache_hit", ticker=ticker, artifact_id=existing.id)
        return cast("list[object]", existing.content_json)
    return None


def _write_cache(
    ticker: str, db_path: str | Path, cache_inputs: list[bytes | str], items: list[object]
) -> None:
    _ = upsert(
        UpsertRequest(
            ticker=ticker,
            purpose=_PURPOSE,
            content_json=items,
            cache_inputs=cache_inputs,
            prompt_version=_PROMPT_VERSION,
        ),
        db_path=db_path,
    )


# ---------------------------------------------------------------------------
# Public per-ticker entry point
# ---------------------------------------------------------------------------


def fetch_websearch_news_for_ticker(
    ticker: str,
    *,
    news_days: int = DEFAULT_NEWS_DAYS,
    db_path: str | Path,
) -> list[NewsRow]:
    """Fetch (or cache-hit) structured news for one ticker and map to NewsRows.

    A same-day re-run with the same thesis anchor is an llm_artifacts cache hit
    and makes NO Opus call. Any LLM/web failure degrades to [] for this ticker.
    Does NOT persist — the dispatcher owns the shared connection."""
    ticker = ticker.upper()
    anchor = _load_anchor(ticker)
    utc_date = datetime.now(UTC).strftime("%Y-%m-%d")
    cache_inputs = _cache_inputs(ticker, utc_date, anchor)

    items = _read_cached(ticker, db_path, cache_inputs)
    if items is None:
        try:
            items = structure_recent_news_json(ticker, news_days=news_days, anchor_block=anchor)
        except Exception as exc:  # belt-and-suspenders; the generator already degrades
            _log("news_websearch_llm_failed", ticker=ticker, error=str(exc))
            return []
        _write_cache(ticker, db_path, cache_inputs, items)

    rows: list[NewsRow] = []
    for item in items:
        mapped = _map_item(item, ticker)
        if mapped is not None:
            rows.append(mapped)
    return rows
