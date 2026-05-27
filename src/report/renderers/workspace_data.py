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
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from llm_client import (
    compose_anchor_block,
    generate_saydo_filter,
    load_bear_anchor,
    load_thesis_anchor,
)
from report.models import (
    KpiLedgerRow,
    RecentDevelopmentsSection,
    SayDoCard,
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
    # Anchor block must be in the cache key so a fresh thesis edit or new
    # bear case invalidates stale filter decisions.
    anchor_block = compose_anchor_block(
        load_thesis_anchor(repo_root, ticker),
        load_bear_anchor(repo_root, ticker),
    )
    cache_key = _saydo_filter_cache_key(card, payload, anchor_block)
    cached = _load_saydo_filter_cache(repo_root, ticker, cache_key)
    if cached is None:
        try:
            quarter_label = f"{card.current_quarter} {card.current_year}"
            raw = generate_saydo_filter(
                ticker, quarter_label, payload, anchor_block=anchor_block
            )
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


_PCT_HINT_RX = re.compile(r"%|margin|growth|rate|share", re.IGNORECASE)
_RATIO_HINT_RX = re.compile(r"ratio|multiple|coverage", re.IGNORECASE)


def select_kpi_strip(rows: list[KpiLedgerRow], n: int = 4) -> list[KpiStripTile]:
    """Pick up-to-N tier-1 KPIs with enough history for a sparkline.

    Drops rows that have ``None`` in every history slot. Formatting heuristic:
    percent for KPIs whose name mentions margin/growth/rate, ratio multiplier
    for KPIs containing ratio/multiple, otherwise raw with one decimal.
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
        is_pct = bool(_PCT_HINT_RX.search(row.name)) and not _RATIO_HINT_RX.search(row.name)
        is_ratio = bool(_RATIO_HINT_RX.search(row.name))

        def _fmt(v: float, *, _pct: bool = is_pct, _ratio: bool = is_ratio) -> str:
            if _ratio:
                return f"{v:.2f}{_TIMES}"
            if _pct:
                return f"{v:.0f}%"
            return f"{v:.1f}"

        latest = values[-1]
        prev = values[-2] if len(values) >= 2 else None
        latest_display = _fmt(latest)
        delta_display: str | None = None
        delta_sign = ""
        if prev is not None:
            d = latest - prev
            if abs(d) > 1e-9:
                delta_sign = "pos" if d > 0 else "neg"
                arrow = "↑" if d > 0 else "↓"
                delta_display = f"{arrow} {abs(d):.1f}"
        tiles.append(
            KpiStripTile(
                name=row.name.split("(")[0].strip(),
                values=values,
                labels=labels,
                latest_label=labels[-1],
                latest_display=latest_display,
                delta_display=delta_display,
                delta_sign=delta_sign,
            )
        )
        if len(tiles) >= n:
            break
    return tiles


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
        )


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
    )


__all__ = [
    "KpiStripTile",
    "NewsTile",
    "PrintVsGuideRow",
    "WorkspaceP3Panels",
    "load_workspace_p3_panels",
    "parse_print_vs_guide",
    "quarter_short",
    "select_kpi_strip",
    "structure_news",
]
