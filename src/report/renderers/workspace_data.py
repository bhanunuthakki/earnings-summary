"""Data shapers for the workspace renderer.

Four jobs:

1. **Saydo parser** — pulls a "print vs guide" row list out of the
   ``cards[].saydo_md`` markdown the LLM emits. The design has a structured
   table slot; this parser bridges from the existing markdown contract
   without requiring a new compute phase.
2. **Saydo LLM filter** — when enable_llm is True, takes the parsed
   print-vs-guide rows and asks Claude to pick the strategically important
   ones (skipping FX trivia, tax-rate noise, etc.). Cached on disk.
3. **News structurer** — re-shapes the free-markdown
   ``RecentDevelopmentsSection.content_md`` into the design's tile layout
   (date / tag / source / headline / gloss / tone).
4. **KPI-strip picker** — selects up to N tier-1 KPI ledger rows that have
   enough history for a sparkline, and normalizes their formatting.

Everything degrades to an empty list when the source doesn't parse, so the
renderer can fall through to a stub panel instead of crashing.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import cast

from llm_client import (
    compose_anchor_block,
    generate_saydo_filter,
    load_bear_anchor,
    load_ir_anchor,
    load_priors_anchor,
    load_thesis_anchor,
    load_worldview_anchor,
)
from report.models import (
    KpiLedgerRow,
    RecentDevelopmentsSection,
    SayDoCard,
)
from report.renderers.charts_v2 import fmt_compact

# Editorial-typography characters hoisted to module constants so the call
# sites don't trip ruff's RUF001 (ambiguous unicode in code). Built via chr()
# so the source text itself stays ASCII-only.
_RSQUO = chr(0x2019)  # right single quote, short year prefix
_TIMES = chr(0x00D7)  # multiplication sign, ratio multiplier
_EMDASH = chr(0x2014)
_ENDASH = chr(0x2013)
_DASH_CHARS = _EMDASH + _ENDASH + "-"


# ---------------------------------------------------------------------------
# Print-vs-guide rows (Say-Do)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrintVsGuideRow:
    metric: str
    guide: str
    actual: str
    verdict: str  # EXCEEDED / MET / MISSED / unknown


# Header column we accept as the leading column of the print-vs-guide table.
# LLM emits sometimes "Metric", sometimes "Commitment", sometimes "Item" —
# all map to the same row shape (label first, then prior, then current,
# then a verdict-ish trailing column).
_HEADER_LEADERS = ("Metric", "Commitment", "Item", "Promise", "Topic")
_DO_TABLE_HEADER_RX = re.compile(
    r"^\|\s*(?P<leader>" + "|".join(_HEADER_LEADERS) + r")\s*\|(?P<rest>.+)\|\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_TABLE_ROW_RX = re.compile(r"^\|(.+)\|\s*$", re.MULTILINE)


def parse_print_vs_guide(card: SayDoCard) -> list[PrintVsGuideRow]:
    """Extract the most "print-vs-guide"-shaped pipe table from the SayDo md.

    The LLM contract is loose: tables vary across cards
    (Commitment / From Call / Checkable / Gap; Metric / Prior / Current / Direction;
    Item / Guide / Actual / Verdict; etc). We accept any 4+ column pipe table
    whose first header column is one of ``_HEADER_LEADERS`` and map the cells
    to (label, prior-context, current-context, verdict-ish) row by row.
    Returns an empty list on format mismatch so the renderer falls back to a
    stub.
    """
    md = card.saydo_md or ""
    if not md:
        return []
    header_match = _DO_TABLE_HEADER_RX.search(md)
    if header_match is None:
        return []
    rest = md[header_match.end() :]
    rows: list[PrintVsGuideRow] = []
    skipped_separator = False
    for row_match in _TABLE_ROW_RX.finditer(rest):
        cells = [c.strip() for c in row_match.group(1).split("|")]
        cells = [c for c in cells if c != ""]
        if not skipped_separator:
            if all(set(c) <= {"-", ":"} for c in cells):
                skipped_separator = True
                continue
            break
        if len(cells) < 3:
            break
        # Permissive mapping: label is always cells[0]; the verdict-like
        # signal is always the LAST cell; "guide" and "actual" share the
        # middle cells (collapse if there's only one).
        label = cells[0]
        verdict_cell = cells[-1]
        if len(cells) >= 4:
            guide = cells[1]
            actual = " · ".join(cells[2:-1])
        else:
            guide = ""
            actual = cells[1]
        verdict = _direction_to_verdict(verdict_cell)
        rows.append(PrintVsGuideRow(metric=label, guide=guide, actual=actual, verdict=verdict))
    return rows


def filter_important_print_vs_guide(
    ticker: str,
    repo_root: Path,
    card: SayDoCard,
    rows: list[PrintVsGuideRow],
    enable_llm: bool,
) -> list[PrintVsGuideRow]:
    """LLM-judged filter: keep only the strategically important commitments.

    Falls back to the full list (just truncated to 8) when ``enable_llm`` is
    False or the LLM call fails. Cached under ``data/saydo_filter/<TICKER>.json``
    keyed by sha256 of the payload + card identity.
    """
    if not rows:
        return rows
    if not enable_llm:
        return rows[:8]
    payload = [
        {
            "id": str(i),
            "metric": r.metric,
            "guide": r.guide,
            "actual": r.actual,
            "verdict": r.verdict,
        }
        for i, r in enumerate(rows)
    ]
    # Anchor block must be in the cache key so a fresh thesis edit, new bear
    # case, new IR-deck cache, or a changed open analyst note invalidates
    # stale filter decisions.
    anchor_block = compose_anchor_block(
        load_thesis_anchor(repo_root, ticker),
        load_bear_anchor(repo_root, ticker),
        load_ir_anchor(repo_root, ticker),
        load_priors_anchor(repo_root, ticker),
        load_worldview_anchor(repo_root),  # inert until LEDGER_WORLDVIEW_ANCHOR
    )
    cache_key = _saydo_filter_cache_key(card, payload, anchor_block)
    cached = _load_saydo_filter_cache(repo_root, ticker, cache_key)
    if cached is None:
        try:
            quarter_label = f"{card.current_quarter} {card.current_year}"
            raw = generate_saydo_filter(ticker, quarter_label, payload, anchor_block=anchor_block)
            cached = _parse_saydo_filter_response(raw)
            _save_saydo_filter_cache(repo_root, ticker, cache_key, cached)
        except Exception:
            # Soft-fail: show all rows (truncated) so the panel still renders.
            return rows[:8]
    kept_ids = set(cached)
    kept = [r for i, r in enumerate(rows) if str(i) in kept_ids]
    if not kept:
        # LLM dropped everything (shouldn't happen, but be defensive).
        return rows[:8]
    return kept


def _saydo_filter_cache_key(
    card: SayDoCard, payload: list[dict[str, str]], anchor_block: str = ""
) -> str:
    h = hashlib.sha256()
    h.update(f"{card.current_quarter}{card.current_year}".encode())
    h.update(b"\x00")
    h.update(json.dumps(payload, sort_keys=True).encode("utf-8"))
    h.update(b"\x00")
    h.update(anchor_block.encode("utf-8"))
    return h.hexdigest()


def _saydo_filter_cache_path(repo_root: Path, ticker: str) -> Path:
    return repo_root / "data" / "saydo_filter" / f"{ticker.upper()}.json"


def _load_saydo_filter_cache(repo_root: Path, ticker: str, cache_key: str) -> list[str] | None:
    path = _saydo_filter_cache_path(repo_root, ticker)
    if not path.exists():
        return None
    try:
        payload = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None
    by_key_raw = payload.get("by_key")
    if not isinstance(by_key_raw, dict):
        return None
    by_key = cast("dict[str, object]", by_key_raw)
    entry_raw = by_key.get(cache_key)
    if not isinstance(entry_raw, list):
        return None
    entry = cast("list[object]", entry_raw)
    return [str(i) for i in entry]


def _save_saydo_filter_cache(repo_root: Path, ticker: str, cache_key: str, ids: list[str]) -> None:
    path = _saydo_filter_cache_path(repo_root, ticker)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, object] = {"by_key": {}}
    if path.exists():
        try:
            loaded = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
            if isinstance(loaded.get("by_key"), dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            pass
    by_key = cast("dict[str, object]", existing["by_key"])
    by_key[cache_key] = ids
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


_JSON_ARRAY_RX = re.compile(r"\[[^\[\]]*\]", re.DOTALL)


def _parse_saydo_filter_response(raw: str) -> list[str]:
    """LLM sometimes appends a markdown rationale after the JSON array — extract
    just the first top-level array. Tolerant of leading/trailing prose."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Find the first JSON-array-shaped substring and try again.
        m = _JSON_ARRAY_RX.search(raw)
        if m is None:
            raise ValueError("saydo_filter response had no JSON array") from None
        parsed = json.loads(m.group(0))
    if not isinstance(parsed, list):
        raise ValueError("saydo_filter response was not a JSON array")
    return [str(x) for x in parsed]  # type: ignore[arg-type]


def _direction_to_verdict(direction: str) -> str:
    """Heuristic mapping of the 'Direction vs. Trajectory' cell to a verdict.

    The LLM uses arrows and free text. We classify by symbol first, then by
    a small keyword set. Unknown directions stay 'unknown' so the renderer
    pill shows the muted/neutral variant.
    """
    text = direction.lower()
    if "↑" in direction or "acceleration" in text or "expansion" in text or "beat" in text:
        return "EXCEEDED"
    if "↓" in direction or "miss" in text or "decel" in text or "contract" in text:
        return "MISSED"
    if "→" in direction or "in-line" in text or "in line" in text or "clean delivery" in text:
        return "MET"
    if "exceeded" in text:
        return "EXCEEDED"
    if "met" in text:
        return "MET"
    return "unknown"


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


# ---------------------------------------------------------------------------
# P3 panel bundle (P4-A1)
# ---------------------------------------------------------------------------
#
# The P3 accessors in src/report/sections/p3_data.py return typed rows for
# six "captured but not surfaced" subsystems. The workspace renderer needs
# them spread across multiple tabs (Thesis / Company / Decisions / SayDo /
# Eval) so the cleanest wiring is: load every panel's rows up-front, hand
# the bundle to the tab dispatcher, let each tab pull what it needs.
#
# Why dataclass not Pydantic: same convention as ``KpiStripTile`` / ``NewsTile``
# in this module — render-side shaping, not a section boundary. Pydantic
# at the section boundary (ReportSpec); dataclasses for renderer-internal
# data movement.


from report.sections.p3_data import (  # noqa: E402  (kept near use site)
    CustomerConcentrationRow,
    DecisionHistorySummary,
    LeaseLadderRow,
    MacroSensitivityRow,
    PeerCompRow,
    SayDoVerdictRow,
    StrategicTargetRow,
    load_customer_concentrations,
    load_decision_history,
    load_lease_ladder,
    load_macro_sensitivities,
    load_peer_comp,
    load_saydo_verdicts,
    load_strategic_targets,
)
from user_state.notes import AnalystNoteRow, list_notes  # noqa: E402


def _new_open_notes() -> list[AnalystNoteRow]:
    return []


@dataclass(frozen=True)
class WorkspaceP3Panels:
    """All P3 accessor rows for one (ticker, repo) render, pre-loaded.

    Empty lists / zero-row summary when the source tables are absent or the
    ticker has no rows — the renderer's panel functions render an empty-state
    callout in that case so the workspace stays visually complete.
    """

    macro_sensitivities: list[MacroSensitivityRow]
    strategic_targets: list[StrategicTargetRow]
    customer_concentrations: list[CustomerConcentrationRow]
    lease_ladder: list[LeaseLadderRow]
    decision_history: DecisionHistorySummary
    saydo_verdicts: list[SayDoVerdictRow]
    peer_comp: list[PeerCompRow]
    # P4.4: the owner's open analyst notes for this name — new builds lead
    # with the standing watch-items (the strip under the thesis lede).
    open_notes: list[AnalystNoteRow] = field(default_factory=_new_open_notes)
    # Position-tab coaching line (REQ-3/REQ-6): count of advisor_memos rows
    # with kind='position_review' for this ticker — "Guard: never run on this
    # name · N position reviews". 0 when the table/DB is absent (best-effort,
    # same degrade contract as every other P3 accessor).
    position_review_count: int = 0

    @classmethod
    def empty(cls) -> WorkspaceP3Panels:
        return cls(
            macro_sensitivities=[],
            strategic_targets=[],
            customer_concentrations=[],
            lease_ladder=[],
            decision_history=DecisionHistorySummary(
                total=0,
                by_kind={},
                by_conviction={},
                win_rate_overall=None,
                rows=[],
            ),
            saydo_verdicts=[],
            peer_comp=[],
            open_notes=[],
            position_review_count=0,
        )


def _load_open_notes_safe(ticker: str, db_path: Path) -> list[AnalystNoteRow]:
    """Open analyst notes for the report strip — watch items + questions lead.

    Best-effort like the P3 accessors: missing DB / pre-0074 schema → []."""
    if not db_path.exists():
        return []
    try:
        rows = list_notes(ticker=ticker, status="open", limit=12, db_path=db_path)
    except sqlite3.Error:
        return []
    kind_rank = {"watch": 0, "question": 1}
    return sorted(rows, key=lambda n: kind_rank.get(n.kind, 9))


def _load_position_review_count_safe(ticker: str, db_path: Path) -> int:
    """Count of ``advisor_memos`` rows with kind='position_review' for this
    ticker — the Position-tab guard line's "N position reviews" figure.

    Best-effort like ``_load_open_notes_safe``: missing DB / pre-0140 schema
    (the kind predates migration 0140) both degrade to 0 rather than raising,
    so the coaching line still renders (as "0 position reviews") instead of
    crashing the build."""
    if not db_path.exists():
        return 0
    try:
        from advisor.store import list_memos

        return len(list_memos(kind="position_review", ticker=ticker, limit=10_000, db_path=db_path))
    except sqlite3.Error:
        return 0


def load_graded_sell_base_rate(ticker: str, db_path: Path) -> str | None:
    """The graded-sells base-rate line for the Position-tab coaching block, or
    None when unavailable.

    ``advisor.position_review.graded_sell_record`` is being built on a
    parallel branch (not yet merged as of this PR) — a guarded import so this
    renderer never hard-depends on a module that may not exist yet. Any
    failure (missing module, missing DB, missing table) degrades to None and
    the caller skips the line silently (spec: "do not fail if it's missing")."""
    if not db_path.exists():
        return None
    try:
        from advisor.position_review import graded_sell_record  # type: ignore[attr-defined]
    except ImportError:
        return None
    try:
        result = cast("object", graded_sell_record(ticker, db_path=db_path))
    except sqlite3.Error:
        return None
    return cast("str | None", result)


def load_workspace_p3_panels(ticker: str, repo_root: Path) -> WorkspaceP3Panels:
    """Call every P3 accessor once and return the bundle.

    Best-effort: missing portfolio.db / missing tables / cold ticker all
    funnel through the accessors' empty-list contract.
    """
    db_path = repo_root / "data" / "portfolio.db"
    return WorkspaceP3Panels(
        macro_sensitivities=load_macro_sensitivities(ticker, db_path=db_path),
        strategic_targets=load_strategic_targets(ticker, db_path=db_path),
        customer_concentrations=load_customer_concentrations(ticker, db_path=db_path),
        lease_ladder=load_lease_ladder(ticker, db_path=db_path),
        decision_history=load_decision_history(ticker, db_path=db_path),
        saydo_verdicts=load_saydo_verdicts(ticker, db_path=db_path),
        peer_comp=load_peer_comp(ticker, repo_root=repo_root),
        open_notes=_load_open_notes_safe(ticker, db_path),
        position_review_count=_load_position_review_count_safe(ticker, db_path),
    )


__all__ = [
    "KpiStripTile",
    "NewsTile",
    "PrintVsGuideRow",
    "WorkspaceP3Panels",
    "format_ledger_value",
    "load_workspace_p3_panels",
    "parse_print_vs_guide",
    "quarter_short",
    "select_kpi_strip",
    "structure_news",
]
