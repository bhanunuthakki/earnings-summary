"""Inbox categorization + transparent ranking (Inbox v2).

Every stream item gets ONE category facet — the filter chips on the Home
rail and the feed — and a transparent score that orders the WHOLE stream
flat: score descending, newest-first on ties. There are no recency-bucket
tiers — each card's relative stamp says "when", the score says "how much
it matters":

    score = severity (category x status)
          x recency decay (30h half-life)
          x position weight (live tracker % of book, equal-weight fallback)
          x thesis relevance (ticker currently WARN / BREACH)
          x signal strength (the alert's OWN evidence magnitude — |z|,
            relevance, restatement delta, owner-falsifier breach — so a strong
            alert outranks a marginal one WITHIN its category; 1.0 when absent)

No ML: each factor is a small lookup table, and the factor breakdown ships
on the card as the "why ranked here" tooltip (``InboxItem.score_why``).

Category derivation:
  * alerts map by ``trigger_kind`` (earnings_tone → Earnings, kpi_inflection /
    thesis_drift → Thesis changes, saydo_due → Watch items);
  * ``material_news`` alerts refine via the ``news`` row behind the alert
    (alembic 0065): a grades feed or an upgrade/downgrade-shaped headline →
    Rating changes; an ``edgar_8k`` row → Press releases when the filing is
    disclosure-only (items ⊆ {7.01, 8.01, 9.01}), else News; a press-wire
    ``source`` → Press releases; else News;
  * drafts → Drafts, ledger entries → Thesis changes, journal notes → Watch
    items, synthesis sections → Synthesis.

Rating changes arrive two ways: the additive yfinance grades feed
(``source_feed='yf_grades'``, execution/fetch_yf_grades.py) and headline
patterns on the stock-news feed. The dedicated FMP per-event grades endpoint
(``/stable/grades``) could not be confirmed available on FMP_TIER=free (docs
are auth-gated; the live probe hit the free tier's daily quota: 429
``{"Error Message": "Limit Reach …"}``), so ``fmp_grades`` stays recognized as
a future feed tag without needing changes here.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from signals.store import SIGNAL_CONSENSUS_RATING

if TYPE_CHECKING:
    from dashboard.inbox import InboxItem

__all__ = [
    "ADVISOR_MEMO_TITLE",
    "CATEGORY_LABELS",
    "CATEGORY_ORDER",
    "SEMANTIC_ADVISOR_MEMO",
    "annotate_and_rank",
    "inbox_label",
    "note_semantic_kind",
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
    # A falsifiable "what would change my mind" condition being met is a
    # thesis-relevant event — the decision it resurfaces was thesis-driven.
    "decision_condition": CATEGORY_THESIS,
    # A held-name figure being restated is thesis-relevant: the numbers the
    # thesis rests on changed under it (L10).
    "restatement": CATEGORY_THESIS,
}

_KIND_CATEGORIES: dict[str, str] = {
    "draft": CATEGORY_DRAFTS,
    "ledger": CATEGORY_THESIS,
    "note": CATEGORY_WATCH,
    "synthesis": CATEGORY_SYNTHESIS,
}

# The advisor's persist_memo ledger echo renders under this title
# (dashboard.inbox keys its ledger-kind label off this constant — one source
# of truth). Those cards are memo commentary, not thesis changes, so they
# ride the synthesis weight (owner feedback 2026-06-11: the advisor memo
# must not sit on top of the stream).
ADVISOR_MEMO_TITLE = "Advisor memo"

# ----------------------------------------------------------------------------
# Semantic identity (Law 1 — "identity over source")
# ----------------------------------------------------------------------------
#
# A stream item's category, label, and actions derive from WHAT it is — a
# discriminator stored at write time — not from the source table it was
# UNION-ed out of. The advisor's memory-everywhere write echoes ONE memo
# through BOTH analyst_notes (source='advisor', source_ref='advisor_memo:<id>',
# context={'memo_id', 'kind'}) and thesis_ledger_entries (entry_kind=
# 'advisor_memo'); collect_inbox stamps both echoes with this identity so they
# rank + label as machine-authored reading regardless of which table surfaced
# them. This is the deliberate unified-item-model seed the diet substrate and
# the S12 spine generalize — extend the discriminator vocabulary here, never
# re-sniff the source table at render time.

SEMANTIC_ADVISOR_MEMO = "advisor_memo"

# Human captions for the analyst-note kinds. The raw enum (the source-table
# value: ``observation`` / ``watch`` / …) never reaches a card label —
# inbox_label() maps it through here.
_NOTE_KIND_LABELS: dict[str, str] = {
    "question": "Open question",
    "decision": "Decision",
    "watch": "Watch item",
    "assumption": "Assumption",
    "observation": "Note",
}


def note_semantic_kind(
    source: str, source_ref: str | None, context: Mapping[str, object] | None
) -> str | None:
    """Semantic identity for an ``analyst_notes``-backed stream item, read from
    its provenance columns. An advisor/synthesis memo — written by the
    advisor's memory-everywhere path (``source='advisor'``,
    ``source_ref='advisor_memo:<id>'``, ``context={'memo_id', 'kind'}``) —
    reads as machine-authored reading whatever its raw note ``kind``. Returns
    ``None`` for an ordinary analyst note (no refinement; it keeps its kind).
    Any one of the three signals is sufficient (a legacy row may carry only
    some)."""
    if source == "advisor":
        return SEMANTIC_ADVISOR_MEMO
    if (source_ref or "").startswith("advisor_memo:"):
        return SEMANTIC_ADVISOR_MEMO
    if context is not None and "memo_id" in context:
        return SEMANTIC_ADVISOR_MEMO
    return None


def _is_advisor_memo(it: InboxItem) -> bool:
    """Identity test, source-table-independent. The primary signal is the
    stored ``semantic_kind`` (set on both the note and the ledger echo in
    collect_inbox); the ledger-title check is the back-compat fallback for
    items built before ``semantic_kind`` existed (and for direct-construction
    unit tests)."""
    if it.semantic_kind == SEMANTIC_ADVISOR_MEMO:
        return True
    return it.kind == "ledger" and it.title == ADVISOR_MEMO_TITLE


def inbox_label(it: InboxItem) -> str:
    """The ONE human-facing kind label for a stream card (design_language
    §Streams). Resolved from semantic identity — never a source-table string
    or a raw enum:

    * machine-authored reading (advisor/synthesis memos) → "Advisor memo",
      whichever table it echoed through;
    * an analyst note → its kind's human caption (the raw ``observation`` /
      ``watch`` / … enum never shows), with a "Reconcile · " prefix while it
      awaits reconciliation (status 'pending', set by collect_inbox);
    * everything else (alerts, drafts, ledger entries, synthesis sections)
      already carries a composed title from collect_inbox and passes through.

    ``dashboard.inbox._title_for`` delegates here fully — this is the single
    label resolver.
    """
    if _is_advisor_memo(it):
        return ADVISOR_MEMO_TITLE
    if it.kind == "note":
        label = _NOTE_KIND_LABELS.get(it.title, it.title)
        return f"Reconcile · {label}" if (it.status or "") == "pending" else label
    return it.title


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

# Dedicated grades feeds: yf_grades is live (execution/fetch_yf_grades.py);
# fmp_grades remains forward-compat for the still-unverified FMP endpoint.
_GRADES_SOURCE_FEEDS: tuple[str, ...] = ("fmp_grades", "yf_grades")

# EDGAR 8-K rows (execution/fetch_edgar_news.py) carry their Reg-S-K item codes
# in the headline prefix: "8-K 2.01, 9.01: completed acquisition — Acme, Inc.".
_EDGAR_8K_FEED = "edgar_8k"
_EDGAR_8K_ITEMS_RX = re.compile(r"^8-K(?:/A)?\s+([0-9][0-9., ]*):")

# Items that are company-published disclosure rather than a hard corporate
# event: 7.01 Reg-FD, 8.01 other events, 9.01 exhibits boilerplate. A filing
# whose items all sit in this set reads as a press release; ANY other item
# (2.01 acquisition, 5.02 exec change, ...) keeps it in News.
_DISCLOSURE_ONLY_8K_ITEMS = frozenset({"7.01", "8.01", "9.01"})


# ----------------------------------------------------------------------------
# Severity / decay tables (the transparent part)
# ----------------------------------------------------------------------------

# Owner-set priorities (2026-06-11 feedback): earnings carry the single
# highest weight, and synthesis — the advisor-memo / synthesis-memo cards —
# ranks below plain news: memos are background reading, not events.
_CATEGORY_SEVERITY: dict[str, float] = {
    CATEGORY_EARNINGS: 3.2,
    CATEGORY_THESIS: 2.8,
    CATEGORY_DRAFTS: 2.2,
    CATEGORY_NEWS: 1.8,
    CATEGORY_RATING: 1.6,
    CATEGORY_WATCH: 1.4,
    CATEGORY_PRESS: 1.2,
    CATEGORY_SYNTHESIS: 1.0,
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

# 30h half-life (was 48h): with the bucket tiers gone, decay alone keeps the
# stream fresh-first — short enough that today dominates, long enough that
# yesterday's earnings still outrank this morning's wire noise.
_RECENCY_HALF_LIFE_HOURS = 30.0
_RECENCY_FLOOR = 0.05

# Position factor: 1 + slope x weight-fraction, capped so a mega-position
# can't drown everything else (40% of book → x1.6).
_POSITION_SLOPE = 1.5
_POSITION_WEIGHT_CAP = 0.4

_THESIS_TONE_FACTORS: dict[str, float] = {"ok": 1.0, "warn": 1.25, "breach": 1.5}


# ----------------------------------------------------------------------------
# DB lookups (best-effort, off the render hot path)
# ----------------------------------------------------------------------------
#
# The render must not block on the network or re-scan growing tables every
# build (directive §4 S12). Two moves:
#   * position weights come from the morning-pipeline-materialized disk cache
#     (``portfolio_weights.read_materialized_weights``), NOT the live tracker;
#   * ``_thesis_tones`` (a full ``thesis_evaluations`` scan that grows with
#     every evaluation) and ``_news_meta`` are memoized per (resolved path, file
#     mtime): an unchanged DB serves repeated renders for free, and any committed
#     write busts the memo by bumping the mtime — no time-based staleness window,
#     no schema. A scoped two-read cache, NOT a universal spine.

_TONES_MEMO: dict[str, tuple[int, dict[str, str]]] = {}
_NEWS_META_MEMO: dict[tuple[str, tuple[int, ...]], tuple[int, dict[int, tuple[str, str]]]] = {}


def _db_mtime_ns(db_path: Path) -> int | None:
    """File mtime in ns — the memo invalidation key. ``None`` when the file is
    gone (a vanished DB busts the memo, which is the safe default)."""
    try:
        return db_path.stat().st_mtime_ns
    except OSError:
        return None


def _materialized_weights(db_path: Path | None) -> dict[str, float]:
    """Position weights for the ranking factor, read from the morning-pipeline
    disk cache (``data/portfolio_weights.json`` beside the DB). Never touches
    the network; ``{}`` when no DB or no cache → equal weighting. ``db_path`` is
    ``<repo_root>/data/portfolio.db``, so the repo root is two parents up."""
    if db_path is None:
        return {}
    try:
        from portfolio_weights import read_materialized_weights

        return read_materialized_weights(Path(db_path).resolve().parent.parent)
    except Exception:  # pragma: no cover - a cache read must never fail a render
        return {}


def _thesis_tones(db_path: Path | None) -> dict[str, str]:
    """Latest thesis-evaluation tone per ticker: 'ok' | 'warn' | 'breach'.
    Missing DB/table → {} (factor 1.0 everywhere). Memoized per (path, mtime)."""
    if db_path is None:
        return {}
    path = Path(db_path)
    if not path.exists():
        return {}
    key = str(path.resolve())
    mtime = _db_mtime_ns(path)
    cached = _TONES_MEMO.get(key)
    if cached is not None and mtime is not None and cached[0] == mtime:
        return cached[1]
    tones = _compute_thesis_tones(path)
    if mtime is not None:
        _TONES_MEMO[key] = (mtime, tones)
    return tones


def _compute_thesis_tones(db_path: Path) -> dict[str, str]:
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
    """news.id → (source, source_feed), lowercased, for material_news refinement.
    Memoized per (path, mtime, requested-id-set) — the same stream's
    material-news ids repeat across re-renders of an unchanged DB."""
    if db_path is None or not news_ids:
        return {}
    path = Path(db_path)
    if not path.exists():
        return {}
    key = (str(path.resolve()), tuple(sorted(set(int(i) for i in news_ids))))
    mtime = _db_mtime_ns(path)
    cached = _NEWS_META_MEMO.get(key)
    if cached is not None and mtime is not None and cached[0] == mtime:
        return cached[1]
    meta = _compute_news_meta(path, list(key[1]))
    if mtime is not None:
        _NEWS_META_MEMO[key] = (mtime, meta)
    return meta


def _compute_news_meta(db_path: Path, news_ids: list[int]) -> dict[int, tuple[str, str]]:
    if not news_ids:
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


def _signal_types_by_news(db_path: Path | None, news_ids: list[int]) -> dict[int, str]:
    """news.id → signals.signal_type for the mirrored diet rows (alembic 0095).

    The diet substrate mirrors every `news` row into `signals` with a TYPE
    stored at INGEST. ``_categorize`` reads that stored type so a rating change
    is identified by IDENTITY (Law 1 — identity over source), not by re-sniffing
    the headline at render time. Best-effort: a pre-0095 DB (no `signals` table)
    → ``{}``, and the source-feed / headline-regex legs stay the fallback for
    un-mirrored rows."""
    if db_path is None or not news_ids or not Path(db_path).exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        marks = ",".join("?" for _ in news_ids)
        rows = conn.execute(
            f"SELECT news_id, signal_type FROM signals WHERE news_id IN ({marks})",
            [int(i) for i in news_ids],
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {int(r[0]): str(r[1]) for r in rows if r[0] is not None}


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
    signal_types = _signal_types_by_news(db_path, news_ids)

    out: list[str] = []
    for idx, it in enumerate(items):
        if it.kind == "alert":
            category = _TRIGGER_CATEGORIES.get(it.title, CATEGORY_NEWS)
            if it.title == "material_news":
                news_id, headline = evidence.get(idx, (None, ""))
                source, source_feed = meta.get(news_id or -1, ("", ""))
                # Identity over source (Law 1): the ``consensus_rating``
                # signal_type the diet substrate STORED at ingest is
                # authoritative — it types the alert as a rating change without
                # the render-time headline regex. The ``source_feed`` grades
                # leg is the fallback for un-mirrored (pre-0095) rows; both land
                # in Rating changes, so they share the branch.
                if (
                    signal_types.get(news_id or -1) == SIGNAL_CONSENSUS_RATING
                    or source_feed in _GRADES_SOURCE_FEEDS
                ):
                    category = CATEGORY_RATING
                elif source_feed == _EDGAR_8K_FEED:
                    category = _edgar_8k_category(headline)
                elif _RATING_HEADLINE_RX.search(headline):
                    category = CATEGORY_RATING
                elif any(wire in source for wire in _PRESS_WIRE_SOURCES):
                    category = CATEGORY_PRESS
        else:
            category = _KIND_CATEGORIES.get(it.kind, CATEGORY_WATCH)
            # Demote machine-authored reading by IDENTITY (not source table):
            # an advisor memo rides the synthesis weight whether it reached the
            # stream as a thesis-ledger echo OR as an analyst note — so a
            # just-generated portfolio memo can't outrank real events.
            if _is_advisor_memo(it):
                category = CATEGORY_SYNTHESIS
        out.append(category)
    return out


def _edgar_8k_category(headline: str) -> str:
    """Press releases for disclosure-only 8-Ks (every item in 7.01/8.01/9.01),
    News for anything carrying a material item — read from the item codes the
    EDGAR feed embeds in the headline. No codes parseable → News (8-K alone is
    a corporate event, and 13D/G rows never reach here)."""
    match = _EDGAR_8K_ITEMS_RX.match(headline)
    if match is None:
        return CATEGORY_NEWS
    codes = {code.strip() for code in match.group(1).split(",") if code.strip()}
    if codes and codes <= _DISCLOSURE_ONLY_8K_ITEMS:
        return CATEGORY_PRESS
    return CATEGORY_NEWS


def _age_text(hours: float) -> str:
    if hours < 1.0:
        return "<1h old"
    if hours < 48.0:
        return f"{hours:.0f}h old"
    return f"{hours / 24.0:.0f}d old"


# Signal-strength factor (owner feedback 2026-07-14: pending-alert ordering felt
# "random and low" because a marginal alert and a screaming one scored identically
# within a category). Reads the alert's OWN evidence magnitude so a strong signal
# outranks a weak one in the same category. Bounded [_STRENGTH_MIN, _STRENGTH_MAX]
# so it refines order WITHIN a category without overriding the owner-set category
# severity ordering.
_STRENGTH_MIN = 0.75
_STRENGTH_MAX = 1.5
# Advisor-authored decision-condition breaches: a modest lift, never the
# owner-falsifier ceiling (see _strength_factor).
_ADVISOR_CONDITION_STRENGTH = 1.15


def _clampf(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _is_num(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _strength_factor(it: InboxItem) -> tuple[float, str]:
    """A bounded multiplier from the alert's own evidence magnitude. Returns
    ``(1.0, "n/a")`` for non-alert items or evidence with no recognised
    magnitude, so it is inert unless a real strength signal is present."""
    alert = it.alert
    if alert is None or not alert.evidence_json:
        return 1.0, "n/a"
    try:
        ev = json.loads(alert.evidence_json)
    except (ValueError, TypeError):
        return 1.0, "n/a"
    if not isinstance(ev, dict):
        return 1.0, "n/a"

    # An OWNER-authored falsifier breach is the highest-quality signal there
    # is — but only the owner's own words earn the ceiling. Advisor-authored
    # condition breaches (the 2026-07-01 sweep class) are informative, not
    # screaming; evidence rows written before decided_by was threaded through
    # lack the key and are treated as advisor, never boosted to the ceiling.
    if (alert.trigger_kind or "").lower() == "decision_condition":
        if ev.get("decided_by") == "owner":
            return _STRENGTH_MAX, "owner falsifier breach"
        return _ADVISOR_CONDITION_STRENGTH, "advisor condition breach"
    # A KPI that crossed a registered threshold / is a thesis-breaker is decisive.
    if ev.get("threshold_crossed") or ev.get("is_thesis_breaker"):
        return _STRENGTH_MAX, "threshold crossed"
    # KPI inflection: scale by statistical surprise (|z|); z=2 → ~0.85, z=6 → 1.5.
    z = ev.get("zscore")
    if _is_num(z):
        az = abs(float(z))
        return _clampf(0.85 + 0.16 * (az - 2.0), _STRENGTH_MIN, _STRENGTH_MAX), f"|z| {az:.1f}"
    # Material news: LLM relevance 0..1 → 0.7..1.5.
    rel = ev.get("relevance_score")
    if _is_num(rel):
        return _clampf(
            0.7 + 0.8 * float(rel), _STRENGTH_MIN, _STRENGTH_MAX
        ), f"relevance {float(rel):.2f}"
    # Restatement: bigger revision, stronger signal.
    d = ev.get("delta_pct")
    if _is_num(d):
        ad = abs(float(d))
        return _clampf(0.9 + 0.06 * ad, _STRENGTH_MIN, _STRENGTH_MAX), f"Δ {ad:.1f}%"
    return 1.0, "n/a"


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

    strength, strength_text = _strength_factor(it)

    score = severity * recency * position * thesis * strength
    why = (
        f"severity {severity:.2f} ({sev_text}) x recency {recency:.2f} ({_age_text(age_hours)}) "
        f"x position {position:.2f} ({pos_text}) x thesis {thesis:.2f} ({tone_text}) "
        f"x strength {strength:.2f} ({strength_text}) "
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
    """Assign category/score/score_why to every item and order the stream
    flat: score descending, newest-first on ties (recency lives inside the
    score as decay, not as a sort tier). ``position_weights`` is ticker →
    fraction-of-book; ``None`` reads the morning-pipeline-materialized weight
    cache (``portfolio_weights.read_materialized_weights`` — a disk read, never
    the live tracker; empty → equal weighting), ``{}`` forces equal weighting."""
    if not items:
        return []
    weights = (
        dict(position_weights) if position_weights is not None else _materialized_weights(db_path)
    )
    tones = _thesis_tones(db_path)
    categories = _categorize(items, db_path)
    annotated: list[InboxItem] = []
    for it, category in zip(items, categories, strict=True):
        score, why = _score_one(it, category, now=now, weights=weights, tones=tones)
        annotated.append(replace(it, category=category, score=score, score_why=why))

    def _sort_key(x: InboxItem) -> tuple[float, float]:
        aware = x.when if x.when.tzinfo else x.when.replace(tzinfo=UTC)
        return (-x.score, -aware.timestamp())

    annotated.sort(key=_sort_key)
    return annotated
