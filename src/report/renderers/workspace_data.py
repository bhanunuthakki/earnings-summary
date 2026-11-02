"""Data shapers for the workspace renderer.

Two jobs:

1. **News structurer** — parses the existing
   ``RecentDevelopmentsSection.content_md`` into the design's tile layout
   (date / tag / source / headline / gloss / tone).
2. **KPI-strip picker** — selects up to N tier-1 KPI ledger rows that have
   enough history for a sparkline, and normalizes their formatting.

Everything degrades to an empty list when the source doesn't parse, so the
renderer can fall through to a stub panel instead of crashing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from report.models import (
    KpiLedgerRow,
    RecentDevelopmentsSection,
)
from report.renderers.charts_v2 import fmt_compact
from report.sections.p3_data import (
    PanelAvailability,
    StandingRuleRow,
    StandingRulesPanel,
    WorkspaceP3Panels,
    load_standing_rules,
    load_workspace_p3_panels,
)

# Editorial-typography characters hoisted to module constants so the call
# sites don't trip ruff's RUF001 (ambiguous unicode in code). Built via chr()
# so the source text itself stays ASCII-only.
_RSQUO = chr(0x2019)  # right single quote, short year prefix
_TIMES = chr(0x00D7)  # multiplication sign, ratio multiplier
_EMDASH = chr(0x2014)
_ENDASH = chr(0x2013)
_DASH_CHARS = _EMDASH + _ENDASH + "-"


# ---------------------------------------------------------------------------
# News tiles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NewsTile:
    tag: str
    date: str
    source: str
    headline: str
    gloss: str
    tone: str  # pos / opt / neu / neg
    url: str | None = None


# Bullet shape:
#   - **Headline text** — Gloss text. [Source: Publisher, YYYY-MM-DD, https://url]
# Tolerant of em / en / hyphen dashes and missing URL.
_NEWS_BULLET_RX = re.compile(
    rf"""
    ^\s*[-*]\s+               # bullet marker
    \*\*(?P<headline>[^*]+)\*\*  # bold headline
    \s*[{_DASH_CHARS}]+\s*    # em / en / hyphen separator
    (?P<gloss>.+?)            # gloss text (lazy)
    (?:\s*\[Source:\s*
        (?P<source>[^,\]]+?)
        (?:\s*,\s*(?P<date>\d{{4}}-\d{{2}}-\d{{2}}))?
        (?:\s*,\s*(?P<url>https?://[^\]\s]+))?
    \s*\])?                   # optional [Source: ...] tail
    \s*$
    """,
    re.MULTILINE | re.VERBOSE,
)


# Lightweight tone classifier — only material/tags keywords. Sentence-level
# parsing not worth the complexity for a 1-line gloss.
_POS_WORDS = {
    "rally",
    "growth",
    "wins",
    "beat",
    "exceed",
    "accelerat",
    "upgrade",
    "expansion",
    "record",
    "all-time",
    "tailwind",
    "strong",
}
_NEG_WORDS = {
    "damages",
    "fine",
    "suit",
    "antitrust",
    "lawsuit",
    "downgrade",
    "miss",
    "decel",
    "warn",
    "outage",
    "breach",
    "risk",
    "regulator",
}
_OPT_WORDS = {
    "plan",
    "intends",
    "expects",
    "guide",
    "raised",
    "yen bond",
    "issuance",
    "intent",
    "deploy",
    "preview",
    "unveil",
}

# Tag inference from headline keywords. Same idea as tone but coarser.
_TAG_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bcapex|capital|bond|fund(ing|s)?\b", re.IGNORECASE), "CAPITAL"),
    (re.compile(r"\bantitrust|suit|fine|regulator|court|FTC|EU|UK\b", re.IGNORECASE), "LEGAL"),
    (re.compile(r"\bcloud|backlog|data center|infrastructure|TPU|GPU\b", re.IGNORECASE), "INFRA"),
    (re.compile(r"\bgemini|model|chip|launch|product|release|app\b", re.IGNORECASE), "PRODUCT"),
    (re.compile(r"\bearnings|revenue|guidance|forecast\b", re.IGNORECASE), "EARNINGS"),
    (re.compile(r"\bcap|valuation|stock|market|rally\b", re.IGNORECASE), "NARRATIVE"),
]


def _classify_tone(headline: str, gloss: str) -> str:
    text = (headline + " " + gloss).lower()
    if any(w in text for w in _NEG_WORDS):
        return "neg"
    if any(w in text for w in _POS_WORDS):
        return "pos"
    if any(w in text for w in _OPT_WORDS):
        return "opt"
    return "neu"


def _classify_tag(headline: str) -> str:
    for pattern, tag in _TAG_RULES:
        if pattern.search(headline):
            return tag
    return "NEWS"


_NEWS_SECTION_HEADER_RX = re.compile(r"^###\s+(?P<title>.+?)\s*$", re.MULTILINE)


def structure_news(section: RecentDevelopmentsSection, limit: int = 5) -> list[NewsTile]:
    """Back-compat single-list parse of the first section of ``content_md``."""
    by_section = structure_news_by_section(section, limit_per_section=limit)
    if not by_section:
        return []
    return next(iter(by_section.values()))


def structure_news_by_section(
    section: RecentDevelopmentsSection, limit_per_section: int = 8
) -> dict[str, list[NewsTile]]:
    """Parse ``content_md`` into ``{section_title: [NewsTile, ...]}`` mapping.

    Splits the markdown on H3 (``### title``) headers and runs the bullet
    parser against each chunk's body. Sections with no recognisable bullets
    are dropped. Section title order is preserved from the source.
    """
    md = section.content_md or ""
    if not md:
        return {}
    headers = list(_NEWS_SECTION_HEADER_RX.finditer(md))
    if not headers:
        # Source has no H3 sections — fall back to a single "Material events"
        # bucket so the renderer still has something to show.
        tiles = _parse_news_bullets(md, limit_per_section)
        return {"Material events": tiles} if tiles else {}
    out: dict[str, list[NewsTile]] = {}
    for i, h in enumerate(headers):
        title = h.group("title").strip()
        body_start = h.end()
        body_end = headers[i + 1].start() if i + 1 < len(headers) else len(md)
        chunk = md[body_start:body_end]
        tiles = _parse_news_bullets(chunk, limit_per_section)
        if tiles:
            out[title] = tiles
    return out


def _parse_news_bullets(chunk: str, limit: int) -> list[NewsTile]:
    tiles: list[NewsTile] = []
    for m in _NEWS_BULLET_RX.finditer(chunk):
        headline = m.group("headline").strip()
        gloss = m.group("gloss").strip().rstrip(".")
        gloss = re.sub(r"\*\*([^*]+)\*\*", r"\1", gloss)
        gloss = re.sub(r"\*([^*]+)\*", r"\1", gloss)
        date = (m.group("date") or "").strip()
        source = (m.group("source") or "").strip()
        url = (m.group("url") or "").strip() or None
        tiles.append(
            NewsTile(
                tag=_classify_tag(headline),
                date=date,
                source=source,
                headline=headline,
                gloss=gloss,
                tone=_classify_tone(headline, gloss),
                url=url,
            )
        )
        if len(tiles) >= limit:
            break
    return tiles


# ---------------------------------------------------------------------------
# KPI strip
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KpiStripTile:
    name: str
    values: list[float]
    labels: list[str]
    latest_label: str
    latest_display: str
    delta_display: str | None
    delta_sign: str  # 'pos' / 'neg' / ''
    # Stable PK handle (kpi_definitions.id), passed through from KpiLedgerRow so
    # the tile can emit a fact_ref doorway (Law 2) — None degrades to the plain
    # inert tile (never a dead button; see workspace_sections/chrome._kpi_tile).
    kpi_definition_id: int | None = None


_PCT_HINT_RX = re.compile(r"%|margin|growth|rate|share", re.IGNORECASE)
_RATIO_HINT_RX = re.compile(r"ratio|multiple|coverage", re.IGNORECASE)
# Names denoting a *count* of things (members, not money) so the strip
# humanizes them without a "$" prefix. Checked only after the percent/ratio
# hints, so "customer growth rate" still reads as a percentage.
_COUNT_HINT_RX = re.compile(
    r"\b(?:customers?|subscribers?|members?|users?|accounts?|merchants?|"
    r"stores?|seats?|employees?|headcount|units?)\b",
    re.IGNORECASE,
)
# Unit strings (post-`_unit_label` normalization, plus raw holdings-JSON units)
# decisive enough to classify a value ahead of the name hints.
_STRIP_PCT_UNITS = frozenset({"%", "percent", "pct", "pp"})
_STRIP_RATIO_UNITS = frozenset({"ratio", "x", _TIMES, "multiple"})
_STRIP_MONEY_UNITS = frozenset({"usd", "$", "dollar", "dollars"})


def _kpi_strip_kind(unit: str | None, name: str) -> str:
    """Classify a strip KPI's value: ``'pct'`` | ``'bps'`` | ``'ratio'`` |
    ``'count'`` | ``'money'``.

    A decisive unit wins; otherwise the name hints decide. Anything that isn't
    a percent, ratio, or count falls through to ``'money'`` — the common case
    for a tier-1 headline KPI (revenue, FCF, RPO, backlog), so a raw level like
    ``364000000000.0`` humanizes to ``$364.0B`` instead of rendering as a bare
    twelve-digit float."""
    u = (unit or "").strip().lower()
    if u == "bps":
        return "bps"
    if u in _STRIP_PCT_UNITS or "%" in u or "margin" in u:
        return "pct"
    if u in _STRIP_RATIO_UNITS:
        return "ratio"
    if u == "count":
        return "count"
    if u in _STRIP_MONEY_UNITS:
        return "money"
    # No decisive unit — read the name. Percent/ratio hints first so a
    # "customer growth rate" reads as a percentage, not a head count.
    if _PCT_HINT_RX.search(name) and not _RATIO_HINT_RX.search(name):
        return "pct"
    if _RATIO_HINT_RX.search(name):
        return "ratio"
    if _COUNT_HINT_RX.search(name):
        return "count"
    return "money"


def _fmt_strip_value(v: float, kind: str) -> str:
    """Humanize a strip KPI's latest level for its kind."""
    if kind == "pct":
        return f"{v:.0f}%"
    if kind == "bps":
        return f"{v:.0f} bps"
    if kind == "ratio":
        return f"{v:.2f}{_TIMES}"
    body = fmt_compact(v)
    return f"${body}" if kind == "money" else body


def _fmt_strip_delta(magnitude: float, kind: str) -> str:
    """Humanize the absolute quarter-over-quarter move (the caller renders the
    arrow). Levels show the compact absolute change (``$120.0B``); rates show
    points; ratios show the bare absolute move."""
    if kind == "pct":
        return f"{magnitude:.1f}pp"
    if kind == "bps":
        return f"{magnitude:.0f} bps"
    if kind == "ratio":
        return f"{magnitude:.2f}"
    body = fmt_compact(magnitude)
    return f"${body}" if kind == "money" else body


def format_ledger_value(value: float, unit: str | None, name: str) -> str:
    """Latest-value cell for the §2 KPI ledger table.

    Money / count levels humanize to a compact figure (``$364.0B`` / ``118.0M``)
    so a raw level like ``364000000000`` no longer renders as a bare integer.
    Percent / ratio / bps keep a trimmed number — the ledger's own Unit column
    already carries their suffix, so re-adding it here would double it
    (``42.3 %``, ``1.23 ratio``)."""
    kind = _kpi_strip_kind(unit, name)
    if kind in ("pct", "bps", "ratio"):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    body = fmt_compact(value)
    return f"${body}" if kind == "money" else body


def select_kpi_strip(rows: list[KpiLedgerRow], n: int = 4) -> list[KpiStripTile]:
    """Pick up-to-N tier-1 KPIs with enough history for a sparkline.

    Drops rows whose history is entirely ``None``. Values are humanized by kind
    (see ``_kpi_strip_kind``): percent (``42%``), ratio (``1.23×``), or a
    compact currency / count level (``$364.0B``, ``118.0M``) — never a bare
    multi-digit float. The delta is the absolute quarter-over-quarter move,
    formatted in the same kind.
    """
    tiles: list[KpiStripTile] = []
    for row in rows:
        if row.tier != "tier_1":
            continue
        history = [(d, v) for d, v in row.history if v is not None]
        if len(history) < 2:
            continue
        values = [v for _, v in history]
        labels = [d for d, _ in history]
        kind = _kpi_strip_kind(row.unit, row.name)

        latest = values[-1]
        prev = values[-2] if len(values) >= 2 else None
        latest_display = _fmt_strip_value(latest, kind)
        delta_display: str | None = None
        delta_sign = ""
        if prev is not None:
            d = latest - prev
            if abs(d) > 1e-9:
                delta_sign = "pos" if d > 0 else "neg"
                arrow = "↑" if d > 0 else "↓"
                delta_display = f"{arrow} {_fmt_strip_delta(abs(d), kind)}"
        tiles.append(
            KpiStripTile(
                name=row.name.split("(")[0].strip(),
                values=values,
                labels=labels,
                latest_label=labels[-1],
                latest_display=latest_display,
                delta_display=delta_display,
                delta_sign=delta_sign,
                kpi_definition_id=row.kpi_definition_id,
            )
        )
        if len(tiles) >= n:
            break
    return tiles


# ---------------------------------------------------------------------------
# KPI ledger row enrichment (staleness + trend delta)
# ---------------------------------------------------------------------------
#
# The §2 ledger table renders one row per tracked KPI. These pure helpers turn
# the already-loaded ``KpiLedgerRow.history`` into the two signals the bare
# "latest value" cell can't carry: whether that value is current, and how it's
# moved. The sparkline itself reuses ``workspace_charts.sparkline``.

# A ledger fact older than ~2 quarters from the report date is stale: the issuer
# has reported newer quarters since, so the value shouldn't read as current. The
# 200-day cut ≈ two quarterly cadences plus reporting-lag headroom, so the most
# recently reported quarter (≤ ~135 days old at report time) never false-flags.
KPI_STALE_AFTER_DAYS = 200


def _parse_period(period: str) -> date | None:
    try:
        return date.fromisoformat(period[:10])
    except (ValueError, TypeError):
        return None


def kpi_is_stale(latest_period: str, report_date: date) -> bool:
    """True when a KPI's most recent fact predates ``report_date`` by more than
    ~2 quarters (see ``KPI_STALE_AFTER_DAYS``). False for an unparseable period
    so a malformed date never flags a row."""
    latest = _parse_period(latest_period)
    if latest is None:
        return False
    return (report_date - latest).days > KPI_STALE_AFTER_DAYS


# Units whose change reads as percentage *points* vs. a percent change of a
# level. ``_RATIO_HINT_RX`` / ``_PCT_HINT_RX`` (defined above for the strip)
# back the name-based fallback when the unit is absent.
_PP_UNITS = frozenset({"%", "percent", "pp"})
_LEVEL_UNITS = frozenset(
    {"count", "actual", "millions", "thousands", "billions", "m", "k", "b", "usd", "$", "x"}
)


def _delta_mode(unit: str | None, name: str) -> str:
    """Classify how a KPI's change should read: ``'pp'`` (percentage points),
    ``'bps'``, ``'ratio'`` (absolute, unitless) or ``'pct'`` (percent change of
    a level)."""
    u = (unit or "").strip().lower()
    if u == "bps":
        return "bps"
    if u == "ratio":
        return "ratio"
    if u in _PP_UNITS or "%" in u or "margin" in u:
        return "pp"
    if u in _LEVEL_UNITS:
        return "pct"
    # No decisive unit — reuse the same name heuristic the KPI strip uses.
    if _PCT_HINT_RX.search(name) and not _RATIO_HINT_RX.search(name):
        return "pp"
    return "pct"


def _year_ago_value(points: list[tuple[str, float]], latest_period: str) -> float | None:
    """Value ~1 year before ``latest_period``, matched by date within ±50 days
    (robust to quarter-end drift and irregular spacing). None when no earlier
    point falls in that window."""
    target = _parse_period(latest_period)
    if target is None:
        return None
    want = target - timedelta(days=365)
    best: float | None = None
    best_gap = 51
    for period, value in points[:-1]:
        d = _parse_period(period)
        if d is None:
            continue
        gap = abs((d - want).days)
        if gap < best_gap:
            best_gap = gap
            best = value
    return best


def _trim(x: float) -> str:
    """One-decimal float with a redundant trailing ``.0`` removed ("4.0"→"4",
    "0.3"→"0.3", "10.0"→"10")."""
    s = f"{x:.1f}"
    return s[:-2] if s.endswith(".0") else s


def _format_delta(
    current: float, base: float, unit: str | None, name: str, tag: str
) -> tuple[str | None, str]:
    mode = _delta_mode(unit, name)
    if mode == "pct":
        if base == 0:
            return None, ""
        change = (current / base - 1.0) * 100.0
        if abs(change) < 0.5:  # rounds to 0%
            return None, ""
        arrow, direction = ("↑", "pos") if change > 0 else ("↓", "neg")
        return f"{arrow} {abs(change):.0f}% {tag}", direction
    delta = current - base
    if mode == "bps":
        if abs(delta) < 0.5:
            return None, ""
        arrow, direction = ("↑", "pos") if delta > 0 else ("↓", "neg")
        return f"{arrow} {abs(delta):.0f}bps {tag}", direction
    # 'pp' (percentage points) and 'ratio' (absolute, no suffix) share the
    # absolute-difference formatting; only the suffix differs.
    if abs(delta) < 0.05:
        return None, ""
    arrow, direction = ("↑", "pos") if delta > 0 else ("↓", "neg")
    suffix = "pp" if mode == "pp" else ""
    return f"{arrow} {_trim(abs(delta))}{suffix} {tag}", direction


def kpi_trend_delta(
    history: list[tuple[str, float | None]], unit: str | None, name: str
) -> tuple[str | None, str]:
    """Return ``(label, direction)`` for a ledger row's recent change, e.g.
    ``("↑ 4pp YoY", "pos")``. Prefers a year-over-year delta (date-matched ~1y
    back); falls back to quarter-over-quarter when there's no year-ago point.
    ``(None, "")`` when there are fewer than two real observations or the change
    rounds to zero.

    ``direction`` ('pos'/'neg') reflects only which way the metric moved —
    whether that move is good or bad is the Status column's job (a rising NPL is
    'pos' here but red there)."""
    points = [(p, v) for p, v in history if v is not None]
    if len(points) < 2:
        return None, ""
    latest_period, latest_value = points[-1]
    base = _year_ago_value(points, latest_period)
    tag = "YoY"
    if base is None:
        base = points[-2][1]
        tag = "QoQ"
    return _format_delta(latest_value, base, unit, name, tag)


# ---------------------------------------------------------------------------
# Quarter label helpers
# ---------------------------------------------------------------------------


def quarter_short(label: str) -> str:
    """``2026 Q1`` -> ``Q1 'YY``. Pass-through if format unrecognized."""
    parts = label.split()
    if len(parts) == 2 and parts[0].isdigit() and len(parts[0]) == 4:
        yr = parts[0][2:]
        return f"{parts[1]} {_RSQUO}{yr}"
    return label


__all__ = [
    "KpiStripTile",
    "NewsTile",
    "PanelAvailability",
    "StandingRuleRow",
    "StandingRulesPanel",
    "WorkspaceP3Panels",
    "format_ledger_value",
    "load_standing_rules",
    "load_workspace_p3_panels",
    "quarter_short",
    "select_kpi_strip",
    "structure_news",
]
