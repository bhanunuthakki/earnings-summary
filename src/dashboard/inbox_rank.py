"""Inbox categorization + transparent ranking (Inbox v2).

Every stream item gets ONE category facet — the filter chips on the Home
rail and the feed — and a transparent score that orders cards WITHIN their
recency bucket:

    score = severity (category x status)
          x recency decay (48h half-life)
          x position weight (live tracker % of book, equal-weight fallback)
          x thesis relevance (ticker currently WARN / BREACH)

No ML: each factor is a small lookup table, and the factor breakdown ships
on the card as the "why ranked here" tooltip (``InboxItem.score_why``).

Category derivation:
  * alerts map by ``trigger_kind`` (earnings_tone → Earnings, kpi_inflection /
    thesis_drift → Thesis changes, saydo_due → Watch items);
  * ``material_news`` alerts refine via the ``news`` row behind the alert
    (alembic 0065): a press-wire ``source`` → Press releases, a grades feed
    or an upgrade/downgrade-shaped headline → Rating changes, else News;
  * drafts → Drafts, ledger entries → Thesis changes, journal notes → Watch
    items, synthesis sections → Synthesis.

Rating changes today come from headline patterns on the stock-news feed —
the dedicated FMP per-event grades endpoint (``/stable/grades``) could not be
confirmed available on FMP_TIER=free (docs are auth-gated; the live probe hit
the free tier's daily quota: 429 ``{"Error Message": "Limit Reach …"}``), so
the categorizer also recognizes a future ``source_feed='fmp_grades'`` without
needing changes here.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from dashboard.inbox import InboxItem

__all__ = [
    "CATEGORY_LABELS",
    "CATEGORY_ORDER",
    "annotate_and_rank",
    "bucket_label",
    "live_position_weights",
]

# ----------------------------------------------------------------------------
# Categories
# ----------------------------------------------------------------------------

CATEGORY_NEWS = "news"
CATEGORY_EARNINGS = "earnings"
CATEGORY_PRESS = "press"
CATEGORY_RATING = "rating"
CATEGORY_THESIS = "thesis"
CATEGORY_DRAFTS = "drafts"
CATEGORY_WATCH = "watch"
CATEGORY_SYNTHESIS = "synthesis"

CATEGORY_ORDER: tuple[str, ...] = (
    CATEGORY_NEWS,
    CATEGORY_EARNINGS,
    CATEGORY_PRESS,
    CATEGORY_RATING,
    CATEGORY_THESIS,
    CATEGORY_DRAFTS,
    CATEGORY_WATCH,
    CATEGORY_SYNTHESIS,
)

CATEGORY_LABELS: dict[str, str] = {
    CATEGORY_NEWS: "News",
    CATEGORY_EARNINGS: "Earnings",
    CATEGORY_PRESS: "Press releases",
    CATEGORY_RATING: "Rating changes",
    CATEGORY_THESIS: "Thesis changes",
    CATEGORY_DRAFTS: "Drafts",
    CATEGORY_WATCH: "Watch items",
    CATEGORY_SYNTHESIS: "Synthesis",
}

_TRIGGER_CATEGORIES: dict[str, str] = {
    "material_news": CATEGORY_NEWS,  # refined below via the news row / headline
    "earnings_tone": CATEGORY_EARNINGS,
    "kpi_inflection": CATEGORY_THESIS,
    "thesis_drift": CATEGORY_THESIS,
    "saydo_due": CATEGORY_WATCH,
}

_KIND_CATEGORIES: dict[str, str] = {
    "draft": CATEGORY_DRAFTS,
    "ledger": CATEGORY_THESIS,
    "note": CATEGORY_WATCH,
    "synthesis": CATEGORY_SYNTHESIS,
}

# news.source substrings (lowercased) that mark a story as issuer PR-wire
# distribution rather than journalism.
_PRESS_WIRE_SOURCES: tuple[str, ...] = (
    "prnewswire",
    "pr newswire",
    "globenewswire",
    "globe newswire",
    "businesswire",
    "business wire",
    "accesswire",
    "newsfile",
    "prweb",
)

# Upgrade/downgrade-shaped headlines on the regular stock-news feed — the
# live leg of the Rating-changes category while the per-event grades feed
# stays unverified on the free tier.
_RATING_HEADLINE_RX = re.compile(
    r"\b(upgrade[sd]?|downgrade[sd]?|initiat\w* coverage|reiterat\w*|"
    r"price target|overweight|underweight|outperform|underperform|"
    r"maintains? (?:a )?(?:buy|sell|hold|neutral)|"
    r"(?:buy|sell|hold|neutral) rating)\b",
    re.IGNORECASE,
)

_GRADES_SOURCE_FEED = "fmp_grades"  # forward-compat: the stubbed dedicated feed


# ----------------------------------------------------------------------------
# Severity / decay tables (the transparent part)
# ----------------------------------------------------------------------------

_CATEGORY_SEVERITY: dict[str, float] = {
    CATEGORY_THESIS: 3.0,
    CATEGORY_EARNINGS: 2.6,
    CATEGORY_DRAFTS: 2.4,
    CATEGORY_SYNTHESIS: 2.0,
    CATEGORY_NEWS: 1.8,
    CATEGORY_RATING: 1.6,
    CATEGORY_WATCH: 1.4,
    CATEGORY_PRESS: 1.2,
}

_STATUS_MULTIPLIER: dict[str, float] = {
    "pending": 1.5,  # needs the owner
    "open": 1.0,
    "approved": 0.8,
    "applied": 0.8,
    "dismissed": 0.5,
    "cancelled": 0.5,
    "expired": 0.4,
}

_RECENCY_HALF_LIFE_HOURS = 48.0
_RECENCY_FLOOR = 0.05

# Position factor: 1 + slope x weight-fraction, capped so a mega-position
# can't drown everything else (40% of book → x1.6).
_POSITION_SLOPE = 1.5
_POSITION_WEIGHT_CAP = 0.4

_THESIS_TONE_FACTORS: dict[str, float] = {"ok": 1.0, "warn": 1.25, "breach": 1.5}

# ----------------------------------------------------------------------------
# Recency buckets — single source of truth for sort + render grouping
# ----------------------------------------------------------------------------

_BUCKET_RANK: dict[str, int] = {"Today": 0, "Yesterday": 1, "This week": 2, "Earlier": 3}


def bucket_label(when: datetime, now: datetime) -> str:
    days = (now.date() - when.date()).days
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    if days <= 7:
        return "This week"
    return "Earlier"


# ----------------------------------------------------------------------------
# Live position weights (best-effort, TTL-cached)
# ----------------------------------------------------------------------------

_WEIGHTS_TTL_SECONDS = 300.0
_weights_cache: tuple[float, dict[str, float]] | None = None


def live_position_weights() -> dict[str, float]:
    """Ticker → fraction-of-book from the companion tracker, ``{}`` when the
    tracker is unreachable (the scorer then falls back to equal weighting).
    Failures are cached for the same TTL as successes so an offline tracker
    costs one ~1s connect-refusal per 5 minutes, not one per render."""
    global _weights_cache
    now = time.monotonic()
    if _weights_cache is not None and now - _weights_cache[0] < _WEIGHTS_TTL_SECONDS:
        return _weights_cache[1]
    weights: dict[str, float] = {}
    try:
        from integrations.portfolio_tracker_client import fetch_live_portfolio

        live = fetch_live_portfolio()
        if live.available:
            for p in live.positions:
                if p.ticker and p.percent_of_portfolio is not None:
                    weights[p.ticker.upper()] = max(p.percent_of_portfolio, 0.0) / 100.0
    except Exception:  # pragma: no cover - any import/transport failure → equal-weight
        weights = {}
    _weights_cache = (now, weights)
    return weights


# ----------------------------------------------------------------------------
# DB lookups (best-effort)
# ----------------------------------------------------------------------------


def _thesis_tones(db_path: Path | None) -> dict[str, str]:
    """Latest thesis-evaluation tone per ticker: 'ok' | 'warn' | 'breach'.
    Missing DB/table → {} (factor 1.0 everywhere)."""
    if db_path is None or not Path(db_path).exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            "SELECT ticker, overall_status FROM thesis_evaluations "
            "ORDER BY ticker, evaluated_at DESC"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    out: dict[str, str] = {}
    for raw_ticker, raw_status in rows:
        ticker = str(raw_ticker).upper()
        if ticker in out or raw_status is None:
            continue
        status = str(raw_status).lower()
        if status in ("ok", "intact"):
            out[ticker] = "ok"
        elif status in ("watch", "warn"):
            out[ticker] = "warn"
        else:
            out[ticker] = "breach"
    return out


def _news_meta(db_path: Path | None, news_ids: list[int]) -> dict[int, tuple[str, str]]:
    """news.id → (source, source_feed), lowercased, for material_news refinement."""
    if db_path is None or not news_ids or not Path(db_path).exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        marks = ",".join("?" for _ in news_ids)
        rows = conn.execute(
            f"SELECT id, source, source_feed FROM news WHERE id IN ({marks})",
            [int(i) for i in news_ids],
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {int(r[0]): (str(r[1] or "").lower(), str(r[2] or "").lower()) for r in rows}


def _alert_news_evidence(item: InboxItem) -> tuple[int | None, str]:
    """(news_id, headline) from a material_news alert's evidence_json."""
    if item.alert is None or not item.alert.evidence_json:
        return None, ""
    try:
        parsed: object = json.loads(item.alert.evidence_json)
    except (ValueError, TypeError):
        return None, ""
    if not isinstance(parsed, dict):
        return None, ""
    evidence = cast("Mapping[str, object]", parsed)
    raw_id = evidence.get("news_id")
    news_id = int(raw_id) if isinstance(raw_id, (int, float)) else None
    headline = str(evidence.get("headline") or "")
    return news_id, headline


# ----------------------------------------------------------------------------
# Categorize + score
# ----------------------------------------------------------------------------


def _categorize(items: list[InboxItem], db_path: Path | None) -> list[str]:
    """One category slug per item, parallel to ``items``."""
    news_ids: list[int] = []
    evidence: dict[int, tuple[int | None, str]] = {}
    for idx, it in enumerate(items):
        if it.kind == "alert" and it.title == "material_news":
            news_id, headline = _alert_news_evidence(it)
            evidence[idx] = (news_id, headline)
            if news_id is not None:
                news_ids.append(news_id)
    meta = _news_meta(db_path, news_ids)

    out: list[str] = []
    for idx, it in enumerate(items):
        if it.kind == "alert":
            category = _TRIGGER_CATEGORIES.get(it.title, CATEGORY_NEWS)
            if it.title == "material_news":
                news_id, headline = evidence.get(idx, (None, ""))
                source, source_feed = meta.get(news_id or -1, ("", ""))
                if source_feed == _GRADES_SOURCE_FEED or _RATING_HEADLINE_RX.search(headline):
                    category = CATEGORY_RATING
                elif any(wire in source for wire in _PRESS_WIRE_SOURCES):
                    category = CATEGORY_PRESS
        else:
            category = _KIND_CATEGORIES.get(it.kind, CATEGORY_WATCH)
        out.append(category)
    return out


def _age_text(hours: float) -> str:
    if hours < 1.0:
        return "<1h old"
    if hours < 48.0:
        return f"{hours:.0f}h old"
    return f"{hours / 24.0:.0f}d old"


def _score_one(
    it: InboxItem,
    category: str,
    *,
    now: datetime,
    weights: Mapping[str, float],
    tones: Mapping[str, str],
) -> tuple[float, str]:
    """(score, why) for one item — every factor named in the why string."""
    label = CATEGORY_LABELS.get(category, category)
    status = (it.status or "").lower()
    severity = _CATEGORY_SEVERITY.get(category, 1.0) * _STATUS_MULTIPLIER.get(status, 1.0)
    sev_text = f"{label} · {status}" if status else label

    age_hours = max((now - it.when).total_seconds() / 3600.0, 0.0)
    recency = max(0.5 ** (age_hours / _RECENCY_HALF_LIFE_HOURS), _RECENCY_FLOOR)

    ticker = (it.ticker or "").upper()
    if not ticker:
        position, pos_text = 1.0, "portfolio-wide"
    elif not weights:
        position, pos_text = 1.0, "equal-weight"
    else:
        w = min(weights.get(ticker, 0.0), _POSITION_WEIGHT_CAP)
        position = 1.0 + _POSITION_SLOPE * w
        pos_text = f"{weights[ticker] * 100:.1f}% of book" if ticker in weights else "not held"

    tone = tones.get(ticker, "") if ticker else ""
    thesis = _THESIS_TONE_FACTORS.get(tone, 1.0)
    tone_text = tone or "n/a"

    score = severity * recency * position * thesis
    why = (
        f"severity {severity:.2f} ({sev_text}) x recency {recency:.2f} ({_age_text(age_hours)}) "
        f"x position {position:.2f} ({pos_text}) x thesis {thesis:.2f} ({tone_text}) "
        f"= {score:.2f}"
    )
    return score, why


def annotate_and_rank(
    items: list[InboxItem],
    *,
    db_path: Path | None,
    now: datetime,
    position_weights: Mapping[str, float] | None = None,
) -> list[InboxItem]:
    """Assign category/score/score_why to every item and order the stream:
    recency bucket first (Today → Earlier), then score descending, then
    newest-first. ``position_weights`` is ticker → fraction-of-book; ``None``
    means "try the live tracker" (TTL-cached, equal-weight when offline) —
    pass ``{}`` to force equal weighting."""
    if not items:
        return []
    weights = dict(position_weights) if position_weights is not None else live_position_weights()
    tones = _thesis_tones(db_path)
    categories = _categorize(items, db_path)
    annotated: list[InboxItem] = []
    for it, category in zip(items, categories, strict=True):
        score, why = _score_one(it, category, now=now, weights=weights, tones=tones)
        annotated.append(replace(it, category=category, score=score, score_why=why))

    def _sort_key(x: InboxItem) -> tuple[int, float, float]:
        aware = x.when if x.when.tzinfo else x.when.replace(tzinfo=UTC)
        return (_BUCKET_RANK.get(bucket_label(x.when, now), 9), -x.score, -aware.timestamp())

    annotated.sort(key=_sort_key)
    return annotated
