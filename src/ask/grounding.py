"""Grounded evidence retrieval for narrative answers (Ask v3).

Before a narrative turn reaches the LLM, this module pulls the rows and
passages most relevant to the question from the citeable stores:

  packs       — portfolio evidence (S4): live holdings, stated conviction,
                DCF fair values, the decision ledger, open journal notes,
                performance vs benchmarks. ``ask.router`` decides per
                question which packs load (a budget-gated fast-model call
                that fails closed to none); ``ask.packs`` loads them.
  facts       — kpi_facts + financial_facts series whose metric the
                question names (matched against kpi_definitions names and
                financial_facts line items), with the latest fact's
                source_doc_id as provenance. The model-read text of a fact
                also carries a caution tag (``_fact_item`` →
                ``_provenance_annotation``) when the newest row is shaky — a
                sub-par scored confidence and/or a cross-source ⚠ disagreement
                (S2's ``pipeline.confidence``), e.g.
                ``[conf 79%; ⚠ SEC says $101M, 0.99% delta]`` — so the answer
                can discount it rather than cite a contested figure as fact.
                The ⚠ strings are ``display_issues_for_fact``'s verbatim, the
                ONE enriched-fact-text format the credibility engine (L10) and
                static-prose citations (L12) extend.
  filings     — the parsed 10-K/10-Q section JSONs
                (data/historical/fmp/<T>_form_10k_<YEAR>.json), sections
                scored by keyword overlap, doc_id resolved through the
                documents.file_path natural key
  transcripts — earnings-call transcript FILES (the /source viewer numbers
                file lines, so hits cite /source/<doc_id>#L<line>), located
                through the transcripts table's document_id

Each hit becomes a numbered ``EvidenceItem`` ([1], [2], …); packs lead the
numbering (the book is the primary context for the questions that select
them). The engine injects ``build_evidence_block`` into the prompt under a
cite-or-don't-claim contract and, after the answer streams, emits a
``citations`` event with ``used_citation_items`` — only the markers the
answer actually used.

Everything is best-effort and read-only: a missing DB, table, or file —
or any pack-channel failure — silently contributes nothing. The one LLM
call here is the pack router (see ``ask.router`` for its budget gate and
fail-closed contract); document retrieval itself never calls a model.

The agentic follow-up loop (``ask.followup``, S7) re-enters through
``gather_requested_evidence``: the model names channel/ticker/period
explicitly (``EvidenceNeed``), which reaches transcripts and filings OLDER
than the latest ones the one-shot pass is limited to; new items continue
the [n] numbering so round-2 evidence cites like round-1 evidence.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from ask import turn_cache
from ask.packs import PACK_KEYS, load_packs
from ask.router import route_packs
from pipeline.confidence import (
    IssuesByTicker,
    display_issues_for_fact,
    load_unresolved_issues,
)
from provenance.overrides import (
    FINANCIAL_FACT,
    KPI,
    FactOverride,
    OverrideAction,
    active_scalar_override_map,
)
from provenance.selection import selected_transcripts_relation
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from timeseries.loaders import reader_tier_join_sql, reader_tier_rank_sql

log = logging.getLogger(__name__)

# Tier-aware winner ordering for the fact-series queries. The queries LEFT JOIN
# documents as ``d`` and ORDER BY (period_end DESC, tier_rank DESC, id DESC) so
# that, per (period_end, fiscal_period_type), the deterministic SEC XBRL row
# leads the FMP row — the SAME (source_quality_tier rank, id) contract the
# Series loaders enforce. ``_dedupe_series`` then keeps the FIRST row per
# period, i.e. the tier-winner. The prior insertion-order pick (period_end DESC,
# id DESC only) was source-agnostic and, on the ~162k FMP+SEC duplicated keys,
# surfaced whichever row had the higher id (usually the later FMP ingest).
# NULL/absent tier ranks 0 and loses only to a real tiered row. The join is
# ORDER-BY-only — no extra SELECT column — so the positional row shape
# (period_end, fiscal_period_type, value, unit, source_doc_id[, confidence])
# every downstream consumer unpacks is unchanged. Computed per-connection
# (``_tier_bits``) so a legacy DB without the tier column/documents degrades to
# the id-only pick.


def _tier_bits(conn: sqlite3.Connection, fact_alias: str) -> tuple[str, str]:
    """(tier_rank_fragment, doc_join_clause) for a fact-series query over
    ``fact_alias`` (``kf`` or ``ff``). Both empty-safe on legacy DBs."""
    return (
        reader_tier_rank_sql(conn),
        reader_tier_join_sql(conn, fact_alias=fact_alias),
    )


# Below this scored confidence a grounded fact carries a "conf NN%" caution in
# the text the MODEL reads (not just the UI popover): it mirrors the UI's
# low-confidence affordance (``ui.source_chip.LOW_CONFIDENCE_THRESHOLD`` = 0.8,
# the point one unresolved cross-source disagreement drops an FMP fact below).
# Duplicated as a plain constant so the ask layer does not import the ui/report
# layer; at or above it a clean fact stays un-annotated (no prompt noise).
_LOW_CONFIDENCE_THRESHOLD = 0.8

# Caps keep the evidence block interactive-sized: the model reads it on
# every narrative turn, so it must stay a few KB, not a filing.
_MAX_FACT_ITEMS_PER_TICKER = 3
_MAX_FACT_ITEMS = 6
_MAX_FILING_ITEMS = 2
_MAX_TRANSCRIPT_ITEMS = 3
# Document channels self-cap at 11 (6+2+3); packs at one item each (≤6).
_MAX_ITEMS = 18
_MAX_TICKERS = 4
_SERIES_POINTS = 8
_FILING_SNIPPET_CHARS = 700
_TRANSCRIPT_SNIPPET_CHARS = 400

_TICKERISH_RX = re.compile(r"\b[A-Z][A-Z0-9.\-]{0,5}\b")

# fact_ref handles (S12, Instrument Paradigm Law 2): the stable identity a
# clickable datum carries so Ask resolves the EXACT series by PK instead of
# re-phrase-matching the display name. Two grammars:
#   kpi:{ticker}:{def_id}                     → kpi_facts by kpi_definition_id PK
#   fin:{ticker}:{line_item}:{fiscal_period}  → financial_facts by (line_item, fpt)
# A token may sit anywhere in the question (the doorway appends it to a readable
# label); the NL name-match (`_fact_evidence`) is the FALLBACK, not the primary.
_FACT_REF_KPI_RX = re.compile(r"\bkpi:([A-Za-z][A-Za-z0-9.\-]{0,9}):(\d{1,9})\b")
_FACT_REF_FIN_RX = re.compile(
    r"\bfin:([A-Za-z][A-Za-z0-9.\-]{0,9}):([a-z0-9_]{1,60}):([A-Za-z0-9]{1,8})\b"
)
# fiscal_period_type tokens that name a single cadence (else default to the
# quarterly series, matching `_fact_evidence`'s financial-facts filter).
_FIN_SPECIFIC_FPT = frozenset({"Q1", "Q2", "Q3", "Q4", "FY", "TTM", "H1", "H2"})

# Question words + glue that carry no retrieval signal. Domain words
# ("margin", "deposit", "guidance") deliberately stay in.
_STOPWORDS = frozenset(
    [
        "a",
        "about",
        "all",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "between",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "down",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "last",
        "latest",
        "less",
        "me",
        "more",
        "most",
        "much",
        "my",
        "of",
        "on",
        "or",
        "our",
        "out",
        "over",
        "per",
        "recent",
        "should",
        "since",
        "so",
        "some",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "told",
        "up",
        "us",
        "versus",
        "vs",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "tell",
        "explain",
        "describe",
        "summarize",
        "summarise",
        "walk",
        "give",
        "think",
        "say",
        "said",
        "happened",
        "changed",
        "compare",
        "compared",
    ]
)


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One numbered, citeable piece of evidence."""

    n: int
    kind: str  # "fact" | "filing" | "transcript" | a pack key (ask.packs.PACK_KEYS)
    label: str  # chip text, e.g. "NU · Operating Margin (GAAP)"
    text: str  # what the model reads (series values / passage snippet)
    doc_id: int | None
    href: str | None  # /source/<doc_id>[?section=…|#L<n>] — None when unresolved
    source_url: str | None = None
    # Scored confidence of the newest fact row backing the item (S2's
    # pipeline.confidence formula, [0, 1]). Only the fact channel carries
    # one — filings/transcripts/packs are passages, not scored facts — and
    # legacy DBs without the column leave it None. The citation popover
    # renders it as a %.
    confidence: float | None = None
    # The stable handle that resolved this item via the PK fast-path
    # (``kpi:{ticker}:{def_id}`` / ``fin:{ticker}:{line_item}:{fpt}``), when it
    # came from a fact_ref token rather than NL name-matching (S12, Law 2).
    # None for NL-matched facts and every non-fact channel. Surfaced in the
    # chip payload so a follow-up can re-pin the exact series.
    fact_ref: str | None = None
    # Structured popover header fields (the auditable "where did this come from"
    # anatomy): the issuer, the backing document's form (``10-K`` / ``10-Q`` /
    # ``transcript``), the newest data point's fiscal period (``Q4'25``), and
    # that point's formatted value (``$20.81B``). Only the fact channel carries
    # a ``value`` (a passage has no single number); legacy items leave them None
    # and render the original label-first popover unchanged.
    ticker: str | None = None
    doc_type: str | None = None
    period: str | None = None
    value: str | None = None

    def chip_payload(self) -> dict[str, object]:
        return {
            "n": self.n,
            "kind": self.kind,
            "label": self.label,
            "href": self.href,
            "source_url": self.source_url,
            "confidence": self.confidence,
            "fact_ref": self.fact_ref,
            "ticker": self.ticker,
            "doc_type": self.doc_type,
            "period": self.period,
            "value": self.value,
        }


@dataclass(frozen=True, slots=True)
class EvidenceNeed:
    """One validated retrieval request from the model (S7 agentic loop).

    ``kind`` is closed over the document channels ("fact" | "filing" |
    "transcript") plus the portfolio pack keys (``ask.packs.PACK_KEYS``).
    ``period`` is free-text ("Q1 2025", "FY2024") parsed best-effort by
    ``_parse_period`` — it is what lets a follow-up round reach transcripts
    and filings OLDER than the latest ones the one-shot pass is limited to.
    """

    kind: str
    ticker: str | None = None
    period: str | None = None
    query: str = ""


NEED_DOC_KINDS = frozenset({"fact", "filing", "transcript"})
NEED_KINDS = NEED_DOC_KINDS | frozenset(PACK_KEYS)


# ---------------------------------------------------------------------------
# Question terms + metric matching
# ---------------------------------------------------------------------------


def _squash_word(w: str) -> str:
    """Light plural folding so "margins" matches "Operating Margin"."""
    return w[:-1] if len(w) >= 4 and w.endswith("s") and not w.endswith("ss") else w


def _squash(text: str) -> str:
    words = re.sub(r"[^a-z0-9&%.\- ]+", " ", text.lower()).split()
    return " ".join(_squash_word(w) for w in words)


def question_terms(question: str) -> list[str]:
    """Distinct content words (squashed), order-preserving."""
    out: dict[str, None] = {}
    for raw in re.findall(r"[a-zA-Z][a-zA-Z0-9\-]{2,}", question.lower()):
        w = _squash_word(raw)
        if w not in _STOPWORDS and raw not in _STOPWORDS:
            out.setdefault(w, None)
    return list(out)


def _label_core(label: str) -> str:
    """A metric label's matchable core: parentheticals stripped, squashed —
    "Operating Margin (GAAP)" → "operating margin"."""
    return _squash(re.sub(r"\([^)]*\)", " ", label))


def _phrase_in(question_squashed: str, phrase: str) -> bool:
    return len(phrase) >= 3 and f" {phrase} " in f" {question_squashed} "


# Captured-KPI names from the capture-all extractor are section-qualified —
# "Net Interest Income (Details) — Net interest margin" (the separator below).
# The LEAF after the last separator is the metric itself, so a typed
# "net interest margin" must match it even though the full qualified core is not
# a substring of the question. We therefore match against the full core OR the
# leaf core. The leaf is used ONLY when it is a distinct ≥2-word phrase, so a
# generic single-word leaf ("Total" / "Net" / "Additions" — the very ambiguity
# the qualification exists to resolve) can never match on its own and flood Ask.
_KPI_QUALIFIER_SEP = " — "


def _label_match_keys(label: str) -> list[str]:
    """Matchable cores for a (possibly section-qualified) KPI label: the full
    parenthetical-stripped core, plus the post-last-separator leaf core when it
    is a distinct ≥2-word phrase. Lets a typed metric name resolve both a clean
    curated definition and a capture-all section-qualified one for the same
    metric (across quarters, qualifiers, and differently-reporting companies)."""
    keys: list[str] = []
    full = _label_core(label)
    if full:
        keys.append(full)
    if _KPI_QUALIFIER_SEP in label:
        leaf = _label_core(label.rsplit(_KPI_QUALIFIER_SEP, 1)[-1])
        if leaf and leaf != full and len(leaf.split()) >= 2:
            keys.append(leaf)
    return keys


_PERIOD_FULL_YEAR_RX = re.compile(r"(20\d{2})")
_PERIOD_SHORT_YEAR_RX = re.compile(r"'(\d{2})")
# (?!\d) so "1Q25" doesn't read as Q2 — the digits after Q are the year there.
_PERIOD_Q_BEFORE_RX = re.compile(r"q\s*([1-4])(?!\d)", re.IGNORECASE)
_PERIOD_Q_AFTER_RX = re.compile(r"([1-4])\s*q", re.IGNORECASE)


def _parse_period(period: str | None) -> tuple[int | None, int | None]:
    """Best-effort (year, quarter) from a free-text period — "Q1 2025",
    "FY2024", "Q1'25", "1Q25" all resolve; anything unparseable is (None,
    None), which downstream means "latest" (the one-shot default)."""
    if not period:
        return (None, None)
    s = period.strip()
    year: int | None = None
    m = _PERIOD_FULL_YEAR_RX.search(s)
    if m:
        year = int(m.group(1))
    else:
        m2 = _PERIOD_SHORT_YEAR_RX.search(s) or re.search(r"[qQ](\d{2})$", s)
        if m2:
            year = 2000 + int(m2.group(1))
    mq = _PERIOD_Q_BEFORE_RX.search(s) or _PERIOD_Q_AFTER_RX.search(s)
    quarter = int(mq.group(1)) if mq else None
    return (year, quarter)


# ---------------------------------------------------------------------------
# Shared DB plumbing
# ---------------------------------------------------------------------------


def _connect(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return None
    # Row factory so the provenance.overrides read API (which indexes rows by
    # column name) works on this connection; every existing consumer here uses
    # integer indexing / unpacking, both of which sqlite3.Row also supports.
    conn.row_factory = sqlite3.Row
    return conn


def _doc_meta(conn: sqlite3.Connection, doc_id: int) -> tuple[str | None, str | None, str | None]:
    """(doc_type, source_url, source_quality_tier) for one documents row.

    The tier picks which side of a cross-source disagreement is "the other
    source" when annotating the fact (``display_issues_for_fact``). Best-effort
    and column-tolerant: a legacy/fixture DB without ``source_quality_tier``
    falls back to the two-column query so the doc_type label never goes missing
    just because the tier column is absent."""
    for cols in ("doc_type, source_url, source_quality_tier", "doc_type, source_url"):
        try:
            row = conn.execute(f"SELECT {cols} FROM documents WHERE id = ?", (doc_id,)).fetchone()
        except sqlite3.Error:
            continue
        if row is None:
            return (None, None, None)
        return (
            str(row[0]) if row[0] is not None else None,
            str(row[1]) if row[1] is not None else None,
            str(row[2]) if len(row) > 2 and row[2] is not None else None,
        )
    return (None, None, None)


def _period_label(period_end: object, fiscal_period_type: object) -> str:
    fpt = str(fiscal_period_type or "").strip() or "?"
    year = str(period_end or "")[:4]
    return f"{fpt}'{year[2:]}" if len(year) == 4 else fpt


def _fmt_value(value: object) -> str:
    try:
        x = float(cast("float", value))
    except (TypeError, ValueError):
        return str(value)
    ax = abs(x)
    if ax >= 1e9:
        return f"{x / 1e9:.2f}B"
    if ax >= 1e6:
        return f"{x / 1e6:.1f}M"
    if ax >= 1e4:
        return f"{x / 1e3:.1f}K"
    return f"{x:g}"


_CURRENCY_UNITS = frozenset({"usd", "$", "dollar", "dollars"})
_PERCENT_UNITS = frozenset({"%", "percent", "pct"})


def _value_display(value: object, unit: str) -> str:
    """Human value for the citation popover: the scaled magnitude with a unit
    affix — ``$20.81B``, ``25.3%``, ``110 millions``. ``actual``/empty units
    carry no affix. This is the chip's NEWEST data point, surfaced as the
    "latest reported" figure (not necessarily the in-sentence number)."""
    base = _fmt_value(value)
    u = (unit or "").strip().lower()
    if u in _PERCENT_UNITS:
        return f"{base}%"
    if u in _CURRENCY_UNITS:
        return f"${base}"
    if u and u != "actual":
        return f"{base} {unit.strip()}"
    return base


def _named_tracked_tickers(question: str, db_path: Path) -> list[str]:
    from ask.context import tracked_tickers  # same package, no cycle

    tracked = tracked_tickers(db_path)
    if not tracked:
        return []
    out: dict[str, None] = {}
    for m in _TICKERISH_RX.finditer(question):
        sym = m.group(0)
        if sym in tracked:
            out.setdefault(sym, None)
    return list(out)


# ---------------------------------------------------------------------------
# Channel 1: facts (kpi_facts + financial_facts)
# ---------------------------------------------------------------------------


def _series_rows(
    conn: sqlite3.Connection, sql: str, params: tuple[object, ...]
) -> list[tuple[object, ...]]:
    try:
        return cast("list[tuple[object, ...]]", conn.execute(sql, params).fetchall())
    except sqlite3.Error:
        return []


def _series_rows_conf(
    conn: sqlite3.Connection, sql_tmpl: str, params: tuple[object, ...]
) -> list[tuple[object, ...]]:
    """``sql_tmpl`` carries a ``{conf}`` slot for the scored-confidence
    column (S2). Tried with it first; a legacy DB without the column fails
    that query, so retry without and pad rows with None — confidence is
    enrichment, never the reason a fact series goes missing."""
    rows = _series_rows(conn, sql_tmpl.format(conf=", confidence"), params)
    if rows:
        return rows
    return [(*r, None) for r in _series_rows(conn, sql_tmpl.format(conf=""), params)]


def _dedupe_series(
    rows: list[tuple[object, ...]],
) -> list[tuple[object, ...]]:
    """Rows ordered (period_end DESC, tier_rank DESC, id DESC) → first per
    (period_end, fiscal_period_type) wins, i.e. the tier-winner (SEC beats FMP).
    Keying on both period_end AND fiscal_period_type keeps the SEC FY/Q4
    dual-write (same period_end, distinct fpt) as two rows rather than
    collapsing them."""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[object, ...]] = []
    for row in rows:
        key = (str(row[0]), str(row[1]))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
        if len(out) >= _SERIES_POINTS:
            break
    return out


def _overlay_fact_rows(
    conn: sqlite3.Connection,
    ticker: str,
    fact_kind: str,
    fact_key: str,
    rows: list[tuple[object, ...]],
) -> tuple[list[tuple[object, ...]], dict[tuple[str, str], FactOverride]]:
    """Apply active company-doc overrides to raw fact-series rows.

    Each row is ``(period_end, fiscal_period_type, value, unit, source_doc_id[,
    confidence])``. A ``replace`` substitutes value (and unit) with the
    company-published figure; a ``drop`` omits the period; ``qualify`` is excluded
    by ``active_scalar_override_map`` (annotation only, no value change). This is
    how a narrative-cited series wins over the stale FMP number, matching the
    report/financials surface — see ``directives/provenance_override_2026_06.md``.

    Returns the overlaid rows AND the ``(period_end, fiscal_period_type)`` override
    map so :func:`_fact_item` can realign the cited chip to the winning row's
    source. A no-op (returns ``rows`` unchanged) when there are no overrides — the
    common case.
    """
    ov_map = active_scalar_override_map(conn, ticker=ticker, fact_kind=fact_kind, fact_key=fact_key)
    if not ov_map:
        return rows, ov_map
    out: list[tuple[object, ...]] = []
    for row in rows:
        ov = ov_map.get((str(row[0])[:10], str(row[1])))
        if ov is None:
            out.append(row)
            continue
        if ov.action == OverrideAction.DROP.value:
            continue
        new_row = list(row)
        if ov.value is not None:
            new_row[2] = ov.value
        if ov.unit is not None:
            new_row[3] = ov.unit
        out.append(tuple(new_row))
    return out, ov_map


def _fact_ref_kpi_item(
    conn: sqlite3.Connection,
    ticker: str,
    def_id: int,
    issues_by_ticker: IssuesByTicker | None = None,
) -> dict[str, object] | None:
    """Resolve a ``kpi:{ticker}:{def_id}`` handle to its series by PK."""
    try:
        drow = conn.execute(
            "SELECT name, unit FROM kpi_definitions WHERE id = ? AND ticker = ?",
            (def_id, ticker),
        ).fetchone()
    except sqlite3.Error:
        return None
    if drow is None:
        return None
    name = str(drow[0])
    unit = str(drow[1] or "")
    tier_rank, doc_join = _tier_bits(conn, "kf")
    rows = _dedupe_series(
        _series_rows_conf(
            conn,
            "SELECT kf.period_end, kf.fiscal_period_type, kf.value, kf.unit, "
            "kf.source_doc_id{conf} "
            f"FROM kpi_facts kf {doc_join} "
            "WHERE kf.ticker = ? AND kf.kpi_definition_id = ? "
            f"ORDER BY kf.period_end DESC, {tier_rank} DESC, kf.id DESC LIMIT 64",
            (ticker, def_id),
        )
    )
    rows, ov_map = _overlay_fact_rows(conn, ticker, KPI, name, rows)
    if not rows:
        return None
    item = _fact_item(
        ticker,
        name,
        unit or str(rows[0][3] or ""),
        rows,
        conn,
        item=name,
        issues_by_ticker=issues_by_ticker,
        override_map=ov_map,
    )
    item["fact_ref"] = f"kpi:{ticker}:{def_id}"
    return item


def _fact_ref_fin_item(
    conn: sqlite3.Connection,
    ticker: str,
    line_item: str,
    fpt: str,
    issues_by_ticker: IssuesByTicker | None = None,
) -> dict[str, object] | None:
    """Resolve a ``fin:{ticker}:{line_item}:{fpt}`` handle to its series."""
    clauses = "ff.ticker = ? AND ff.line_item = ?"
    params: list[object] = [ticker, line_item]
    fpt_u = fpt.upper()
    if fpt_u in _FIN_SPECIFIC_FPT:
        clauses += " AND ff.fiscal_period_type = ?"
        params.append(fpt_u)
    else:
        clauses += " AND ff.fiscal_period_type IN ('Q1','Q2','Q3','Q4')"
    tier_rank, doc_join = _tier_bits(conn, "ff")
    sql = (
        "SELECT ff.period_end, ff.fiscal_period_type, ff.value, ff.unit, "
        "ff.source_doc_id{conf} "
        f"FROM financial_facts ff {doc_join} "
        f"WHERE {clauses} "
        f"ORDER BY ff.period_end DESC, {tier_rank} DESC, ff.id DESC LIMIT 64"
    )
    rows = _dedupe_series(_series_rows_conf(conn, sql, tuple(params)))
    rows, ov_map = _overlay_fact_rows(conn, ticker, FINANCIAL_FACT, line_item, rows)
    if not rows:
        return None
    label = line_item.replace("_", " ").title()
    # ``item`` is the raw line_item (issues match on it), not the title-cased label.
    item = _fact_item(
        ticker,
        label,
        str(rows[0][3] or ""),
        rows,
        conn,
        item=line_item,
        issues_by_ticker=issues_by_ticker,
        override_map=ov_map,
    )
    item["fact_ref"] = f"fin:{ticker}:{line_item}:{fpt}"
    return item


def _fact_ref_evidence(
    conn: sqlite3.Connection,
    question: str,
    issues_by_ticker: IssuesByTicker | None = None,
) -> list[dict[str, object]]:
    """The PK fast-path: every fact_ref token in ``question`` resolves its exact
    series directly, bypassing NL name-matching. The token names its own ticker
    (the authoritative handle), so this is independent of the scope/named-ticker
    heuristics. Resolved series lead the fact channel; ``_fact_evidence`` then
    fills the rest and is deduped against these by label."""
    out: list[dict[str, object]] = []
    seen_kpi: set[int] = set()
    for m in _FACT_REF_KPI_RX.finditer(question):
        def_id = int(m.group(2))
        if def_id in seen_kpi:
            continue
        seen_kpi.add(def_id)
        item = _fact_ref_kpi_item(conn, m.group(1).upper(), def_id, issues_by_ticker)
        if item is not None:
            out.append(item)
        if len(out) >= _MAX_FACT_ITEMS:
            return out
    seen_fin: set[tuple[str, str, str]] = set()
    for m in _FACT_REF_FIN_RX.finditer(question):
        key = (m.group(1).upper(), m.group(2), m.group(3))
        if key in seen_fin:
            continue
        seen_fin.add(key)
        item = _fact_ref_fin_item(conn, key[0], key[1], key[2], issues_by_ticker)
        if item is not None:
            out.append(item)
        if len(out) >= _MAX_FACT_ITEMS:
            return out
    return out


def _fact_evidence(
    conn: sqlite3.Connection,
    question_squashed: str,
    tickers: list[str],
    issues_by_ticker: IssuesByTicker | None = None,
) -> list[dict[str, object]]:
    """Metric series the question names, newest first, with provenance."""
    found: list[dict[str, object]] = []
    for ticker in tickers:
        per_ticker = 0
        # KPI definitions: names matched on their parenthetical-stripped core.
        try:
            kpi_defs = conn.execute(
                "SELECT id, name, unit FROM kpi_definitions WHERE ticker = ?", (ticker,)
            ).fetchall()
        except sqlite3.Error:
            kpi_defs = []
        matched_kpis = [
            (int(d[0]), str(d[1]), str(d[2] or ""))
            for d in kpi_defs
            if any(_phrase_in(question_squashed, key) for key in _label_match_keys(str(d[1])))
        ]
        # Longest (most specific) labels first.
        matched_kpis.sort(key=lambda d: len(d[1]), reverse=True)
        for def_id, name, unit in matched_kpis:
            if per_ticker >= _MAX_FACT_ITEMS_PER_TICKER:
                break
            kpi_tier_rank, kpi_doc_join = _tier_bits(conn, "kf")
            rows = _dedupe_series(
                _series_rows_conf(
                    conn,
                    "SELECT kf.period_end, kf.fiscal_period_type, kf.value, kf.unit, "
                    "kf.source_doc_id{conf} "
                    f"FROM kpi_facts kf {kpi_doc_join} "
                    "WHERE kf.ticker = ? AND kf.kpi_definition_id = ? "
                    f"ORDER BY kf.period_end DESC, {kpi_tier_rank} DESC, kf.id DESC LIMIT 64",
                    (ticker, def_id),
                )
            )
            rows, ov_map = _overlay_fact_rows(conn, ticker, KPI, name, rows)
            if not rows:
                continue
            found.append(
                _fact_item(
                    ticker,
                    name,
                    unit or str(rows[0][3] or ""),
                    rows,
                    conn,
                    item=name,
                    issues_by_ticker=issues_by_ticker,
                    override_map=ov_map,
                )
            )
            per_ticker += 1

        # Financial line items: snake_case keys matched the same way.
        try:
            line_items = conn.execute(
                "SELECT DISTINCT line_item FROM financial_facts WHERE ticker = ?", (ticker,)
            ).fetchall()
        except sqlite3.Error:
            line_items = []
        matched_fins = [
            str(li[0])
            for li in line_items
            if _phrase_in(question_squashed, _squash(str(li[0]).replace("_", " ")))
        ]
        matched_fins.sort(key=len, reverse=True)
        for line_item in matched_fins:
            if per_ticker >= _MAX_FACT_ITEMS_PER_TICKER:
                break
            fin_tier_rank, fin_doc_join = _tier_bits(conn, "ff")
            rows = _dedupe_series(
                _series_rows_conf(
                    conn,
                    "SELECT ff.period_end, ff.fiscal_period_type, ff.value, ff.unit, "
                    "ff.source_doc_id{conf} "
                    f"FROM financial_facts ff {fin_doc_join} "
                    "WHERE ff.ticker = ? AND ff.line_item = ? "
                    "AND ff.fiscal_period_type IN ('Q1','Q2','Q3','Q4') "
                    f"ORDER BY ff.period_end DESC, {fin_tier_rank} DESC, ff.id DESC LIMIT 64",
                    (ticker, line_item),
                )
            )
            rows, ov_map = _overlay_fact_rows(conn, ticker, FINANCIAL_FACT, line_item, rows)
            if not rows:
                continue
            label = line_item.replace("_", " ").title()
            found.append(
                _fact_item(
                    ticker,
                    label,
                    str(rows[0][3] or ""),
                    rows,
                    conn,
                    item=line_item,
                    issues_by_ticker=issues_by_ticker,
                    override_map=ov_map,
                )
            )
            per_ticker += 1

        if len(found) >= _MAX_FACT_ITEMS:
            break
    return found[:_MAX_FACT_ITEMS]


def _row_value(value: object) -> float:
    """The newest row's value as a float for issue matching — 0.0 when the
    stored TEXT value won't parse (value-anchored issues then simply miss,
    matching ``report.sections.financials``'s convention)."""
    try:
        return float(cast("float", value))
    except (TypeError, ValueError):
        return 0.0


def _provenance_annotation(confidence: float | None, issue_strings: list[str]) -> str:
    """The bracketed caution tag appended to a fact's MODEL-read text, e.g.
    `` [conf 79%; ⚠ SEC says $101M, 0.99% delta]``.

    Empty for a clean, high-confidence fact (the common case) so the prompt
    stays noise-free. A "conf NN%" leads only when the score is below
    ``_LOW_CONFIDENCE_THRESHOLD`` or the fact carries an unresolved issue;
    ``issue_strings`` are ``pipeline.confidence.display_issues_for_fact``'s
    pre-formatted (⚠-prefixed) popover strings, kept VERBATIM so this remains
    the one enriched-text format the credibility engine (L10) and static-prose
    citations (L12) extend.
    """
    parts: list[str] = []
    if confidence is not None and (confidence < _LOW_CONFIDENCE_THRESHOLD or issue_strings):
        parts.append(f"conf {round(max(0.0, min(1.0, confidence)) * 100)}%")
    parts.extend(issue_strings)
    return f" [{'; '.join(parts)}]" if parts else ""


def _fact_item(
    ticker: str,
    label: str,
    unit: str,
    rows: list[tuple[object, ...]],
    conn: sqlite3.Connection,
    *,
    item: str | None = None,
    issues_by_ticker: IssuesByTicker | None = None,
    override_map: dict[tuple[str, str], FactOverride] | None = None,
) -> dict[str, object]:
    """One fact-series evidence item. ``item`` is the raw matching name
    (financial_facts.line_item / kpi_definitions.name) used to look up the
    newest row's unresolved validation issues — it differs from the title-cased
    display ``label`` for financial line items, so it must be passed explicitly
    for issue matching; it falls back to ``label`` for KPIs (whose name IS the
    label). ``issues_by_ticker`` is loaded once by the caller and shared.

    ``override_map`` is :func:`_overlay_fact_rows`'s
    ``(period_end, fiscal_period_type) -> FactOverride`` map: the series ``rows``
    already carry the overridden values, so this is used ONLY to realign the
    cited chip's source to the winning company document when the newest row was
    ``replace``-overridden — never a chip pointing at the FMP row beside a
    company-published figure."""
    points = "; ".join(f"{_period_label(r[0], r[1])} {_fmt_value(r[2])}" for r in rows)
    # Bind the newest row's fields before the len()-based confidence check —
    # that check narrows the tuple's arity for the type checker, so any later
    # subscript would read as possibly-out-of-range.
    newest = rows[0]
    period_end_raw, fpt_raw, value_raw, doc_id_raw = newest[0], newest[1], newest[2], newest[4]
    conf_raw = newest[5] if len(newest) > 5 else None
    doc_id = int(cast("int", doc_id_raw)) if doc_id_raw is not None else None
    doc_type, source_url, tier = (
        _doc_meta(conn, doc_id) if doc_id is not None else (None, None, None)
    )
    # Scored confidence (S2) of the newest fact row — the one the chip cites.
    confidence: float | None = None
    if conf_raw is not None:
        try:
            confidence = float(cast("float", conf_raw))
        except (TypeError, ValueError):
            confidence = None
    # Company-doc override on the newest (cited) row: ``_overlay_fact_rows`` has
    # already swapped the VALUE; here we swap the chip's SOURCE so the popover
    # describes the company document, not the superseded FMP row.
    newest_ov = override_map.get((str(period_end_raw)[:10], str(fpt_raw))) if override_map else None
    if newest_ov is not None and newest_ov.action == OverrideAction.REPLACE.value:
        doc_type = newest_ov.source_doc_type or doc_type
        source_url = newest_ov.source_url or source_url
        if newest_ov.source_doc_id is not None:
            doc_id = newest_ov.source_doc_id
        if newest_ov.confidence is not None:
            confidence = newest_ov.confidence
    # Cross-source ⚠ disagreement (and any other unresolved validation issue)
    # on that newest row — pulled from the SAME source the UI popover reads
    # (display_issues_for_fact), so the answer-writing model sees the conflict
    # ("SEC says $101M") instead of citing a contested figure as settled fact.
    issue_strings: list[str] = []
    if issues_by_ticker:
        issue_strings = display_issues_for_fact(
            issues_by_ticker,
            ticker=ticker,
            item=item or label,
            period_end=str(period_end_raw),
            value=_row_value(value_raw),
            displayed_tier=tier,
        )
    unit_part = f", {unit}" if unit and unit != "actual" else ""
    src_part = f" — source: {doc_type}" if doc_type else ""
    annotation = _provenance_annotation(confidence, issue_strings)
    return {
        "kind": "fact",
        "label": f"{ticker} · {label}",
        "text": f"{ticker} {label} (newest first{unit_part}): {points}{src_part}{annotation}",
        "doc_id": doc_id,
        "href": f"/source/{doc_id}" if doc_id is not None else None,
        "source_url": source_url,
        "confidence": confidence,
        # Structured popover fields — the newest row this chip cites.
        "ticker": ticker,
        "doc_type": doc_type,
        "period": _period_label(period_end_raw, fpt_raw),
        "value": _value_display(value_raw, unit),
    }


# ---------------------------------------------------------------------------
# Channel 2: filings (parsed 10-K/10-Q section JSONs)
# ---------------------------------------------------------------------------

_FILING_META_KEYS = frozenset({"symbol", "period", "year"})


def _flatten_section(value: object) -> str:
    """A section's list of {label: [values]} dicts → readable text."""
    if not isinstance(value, list):
        return ""
    lines: list[str] = []
    for entry in cast("list[object]", value):
        if not isinstance(entry, dict):
            continue
        for k, v in cast("dict[str, object]", entry).items():
            vals = (
                " | ".join(str(x) for x in cast("list[object]", v))
                if isinstance(v, list)
                else str(v)
            )
            lines.append(f"{k}: {vals}" if vals else str(k))
    return "\n".join(lines)


# Filing files are named ``{ticker}_form_{form}_{YEAR}.json``. Only that exact
# shape is selected — quarter-suffixed 10-Q files were already excluded by the
# former year-regex, so this preserves the prior selection byte-for-byte.
_FILING_NAME_RX = re.compile(r"^(.+)_form_(10[kq])_(\d{4})\.json$")
_FILING_DOC_TYPES = ("fmp_10k_json", "fmp_10q_json")
# How many fiscal years back the no-DB fallback probe scans (exact-name stat
# calls, never a directory glob).
_FILING_PROBE_BACK_YEARS = 30


def _latest_filing_paths(
    repo_root: Path, ticker: str, conn: sqlite3.Connection | None = None
) -> list[tuple[str, int, Path]]:
    """[(form, year, path)] — the newest 10-K and newest 10-Q on disk for this
    ticker.

    Resolved through the indexed ``documents`` table (``file_path`` natural key)
    rather than globbing ``data/historical/fmp``, which holds ~110k files — the
    documented "never glob fmp" trap, and this runs on the LIVE ask path. The
    fiscal year is parsed from the file name (``documents.period_end`` is NULL
    for these rows on prod). Falls back to a bounded exact-name probe — never a
    directory scan — when no DB is available or it holds no rows for the ticker.
    """
    base = repo_root / "data" / "historical" / "fmp"
    best: dict[str, tuple[int, Path]] = {}
    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT file_path FROM documents WHERE ticker = ? AND doc_type IN (?, ?)",
                (ticker, *_FILING_DOC_TYPES),
            ).fetchall()
        except sqlite3.Error:
            rows = []
        for row in rows:
            m = _FILING_NAME_RX.match(Path(str(row[0])).name)
            if m is None or m.group(1) != ticker:
                continue
            form, year = m.group(2), int(m.group(3))
            path = repo_root / str(row[0])
            if (form not in best or year > best[form][0]) and path.exists():
                best[form] = (year, path)
    if not best:
        best = _probe_latest_filing_paths(base, ticker)
    return [(form, best[form][0], best[form][1]) for form in ("10k", "10q") if form in best]


def _probe_latest_filing_paths(base: Path, ticker: str) -> dict[str, tuple[int, Path]]:
    """Newest 10-K/10-Q for ``ticker`` by probing exact file names over a bounded
    descending fiscal-year window — the no-DB fallback that still never scans the
    ~110k-file directory."""
    this_year = datetime.now(UTC).year
    best: dict[str, tuple[int, Path]] = {}
    for form in ("10k", "10q"):
        for year in range(this_year + 1, this_year - _FILING_PROBE_BACK_YEARS, -1):
            path = base / f"{ticker}_form_{form}_{year}.json"
            if path.exists():
                best[form] = (year, path)
                break
    return best


def _filing_doc_id(conn: sqlite3.Connection | None, repo_root: Path, path: Path) -> int | None:
    """documents.id via the file_path natural key (posix-relative)."""
    if conn is None:
        return None
    try:
        rel = path.relative_to(repo_root).as_posix()
    except ValueError:
        return None
    try:
        row = conn.execute("SELECT id FROM documents WHERE file_path = ?", (rel,)).fetchone()
    except sqlite3.Error:
        return None
    return int(row[0]) if row else None


def _filing_paths_for_year(repo_root: Path, ticker: str, year: int) -> list[tuple[str, int, Path]]:
    """[(form, year, path)] for one specific fiscal year — the follow-up
    loop's reach into filings older than the newest pair on disk."""
    base = repo_root / "data" / "historical" / "fmp"
    out: list[tuple[str, int, Path]] = []
    for form in ("10k", "10q"):
        path = base / f"{ticker}_form_{form}_{year}.json"
        if path.exists():
            out.append((form, year, path))
    return out


def _parse_filing_sections(path: Path) -> list[tuple[str, str, str, str]] | None:
    """Read + flatten + squash one filing JSON into the question-INDEPENDENT
    fields scoring needs: ``(section, snippet, squashed_full, squashed_section)``.

    This is the heavy half of filing retrieval (a multi-hundred-KB JSON parse +
    a regex squash of every section), and it does NOT depend on the question —
    so ``turn_cache.cached_file_parse`` memoizes it by (file, mtime), and only
    the cheap keyword scoring below re-runs per turn. Returns ``None`` when the
    file can't be read/parsed (the caller skips it, exactly as before)."""
    try:
        raw_payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(raw_payload, dict):
        return None
    payload = cast("dict[str, object]", raw_payload)
    out: list[tuple[str, str, str, str]] = []
    for section, value in payload.items():
        if section in _FILING_META_KEYS:
            continue
        text = _flatten_section(value)
        if not text:
            continue
        out.append(
            (
                section,
                " ".join(text.split())[:_FILING_SNIPPET_CHARS],
                _squash(section + " " + text[:8000]),
                _squash(section),
            )
        )
    return out


def _scored_filing_sections(
    conn: sqlite3.Connection | None,
    repo_root: Path,
    terms: list[str],
    ticker: str,
    paths: list[tuple[str, int, Path]],
) -> list[tuple[int, dict[str, object]]]:
    """Keyword-score every section across the given filing files. The per-file
    read+flatten+squash is memoized (``turn_cache``); only the scoring re-runs
    per question."""
    scored: list[tuple[int, dict[str, object]]] = []
    for form, year, path in paths:
        parsed = turn_cache.cached_file_parse(path, _parse_filing_sections)
        if not parsed:
            continue
        doc_id = _filing_doc_id(conn, repo_root, path)
        form_label = "10-K" if form == "10k" else "10-Q"
        for section, snippet, squashed_full, squashed_section in parsed:
            score = sum(1 for t in terms if f" {t} " in f" {squashed_full} ")
            score += 2 * sum(1 for t in terms if f" {t} " in f" {squashed_section} ")
            if score <= 0:
                continue
            href = (
                f"/source/{doc_id}?section={urllib.parse.quote(section)}"
                if doc_id is not None
                else None
            )
            scored.append(
                (
                    score,
                    {
                        "kind": "filing",
                        "label": f"{ticker} {form_label} FY{year} · {section}",
                        "text": f'{ticker} {form_label} FY{year}, section "{section}": {snippet}',
                        "doc_id": doc_id,
                        "href": href,
                        "source_url": None,
                    },
                )
            )
    return scored


def _filing_evidence(
    conn: sqlite3.Connection | None,
    repo_root: Path,
    terms: list[str],
    tickers: list[str],
) -> list[dict[str, object]]:
    if not terms:
        return []
    scored: list[tuple[int, dict[str, object]]] = []
    for ticker in tickers:
        scored.extend(
            _scored_filing_sections(
                conn, repo_root, terms, ticker, _latest_filing_paths(repo_root, ticker, conn)
            )
        )
    scored.sort(key=lambda s: s[0], reverse=True)
    return [item for _, item in scored[:_MAX_FILING_ITEMS]]


# ---------------------------------------------------------------------------
# Channel 3: transcripts (files, cited by viewer line number)
# ---------------------------------------------------------------------------


def _transcript_docs(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    year: int | None = None,
    quarter: int | None = None,
    limit: int = 2,
) -> list[tuple[int, str, str, str]]:
    """[(document_id, file_path, fiscal_period_type, period_end)] newest
    first. ``year``/``quarter`` narrow to a specific call — the follow-up
    loop's reach beyond the latest two (period_end years are calendar)."""
    clauses = ["t.ticker = ?"]
    params: list[object] = [ticker]
    if year is not None:
        clauses.append("substr(t.period_end, 1, 4) = ?")
        params.append(str(year))
    if quarter is not None:
        clauses.append("t.fiscal_period_type = ?")
        params.append(f"Q{quarter}")
    params.append(limit)
    try:
        transcripts = selected_transcripts_relation(conn)
        rows = conn.execute(
            "SELECT t.document_id, d.file_path, t.fiscal_period_type, t.period_end "
            f"FROM {transcripts} t JOIN documents d ON d.id = t.document_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY t.period_end DESC LIMIT ?",
            tuple(params),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [(int(r[0]), str(r[1]), str(r[2] or ""), str(r[3] or "")) for r in rows]


def _parse_transcript_lines(path: Path) -> list[tuple[int, str, str]] | None:
    """Read + squash one transcript file into ``(line_no, snippet, squashed)``
    for the substantive lines (>= 40 chars).

    The read + per-line squash is the heavy half of transcript retrieval and is
    question-INDEPENDENT, so ``turn_cache.cached_file_parse`` memoizes it by
    (file, mtime) and only the keyword scoring below re-runs per turn. Returns
    ``None`` when the file can't be read (the caller skips it, as before)."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    out: list[tuple[int, str, str]] = []
    for line_no, line in enumerate(lines, start=1):
        flat = " ".join(line.split())
        if len(flat) < 40:  # skip headers/blank/speaker-only lines
            continue
        out.append((line_no, flat[:_TRANSCRIPT_SNIPPET_CHARS], _squash(flat)))
    return out


def _scored_transcript_lines(
    repo_root: Path,
    terms: list[str],
    ticker: str,
    docs: list[tuple[int, str, str, str]],
) -> list[tuple[int, dict[str, object]]]:
    """Keyword-score every substantive line across the given transcript docs.
    The per-file read+squash is memoized (``turn_cache``); only the scoring
    re-runs per question."""
    scored: list[tuple[int, dict[str, object]]] = []
    for doc_id, file_path, fpt, period_end in docs:
        path = repo_root / file_path
        parsed = turn_cache.cached_file_parse(path, _parse_transcript_lines)
        if not parsed:
            continue
        call_label = _period_label(period_end, fpt)
        for line_no, snippet, squashed_line in parsed:
            squashed = f" {squashed_line} "
            score = sum(1 for t in terms if f" {t} " in squashed)
            if score <= 0:
                continue
            scored.append(
                (
                    score,
                    {
                        "kind": "transcript",
                        "label": f"{ticker} {call_label} call · L{line_no}",
                        "text": f"{ticker} {call_label} earnings call, line {line_no}: {snippet}",
                        "doc_id": doc_id,
                        "href": f"/source/{doc_id}#L{line_no}",
                        "source_url": None,
                    },
                )
            )
    return scored


def _dedupe_transcript_hits(
    scored: list[tuple[int, dict[str, object]]], limit: int
) -> list[dict[str, object]]:
    """Top hits, one line per (doc, neighborhood) — adjacent lines repeat
    the same point. ``scored`` need not be pre-sorted."""
    ordered = sorted(scored, key=lambda s: s[0], reverse=True)
    out: list[dict[str, object]] = []
    taken: list[tuple[int | None, int]] = []
    for _, item in ordered:
        href = str(item["href"] or "")
        m = re.search(r"#L(\d+)$", href)
        line_no = int(m.group(1)) if m else 0
        key_doc = cast("int | None", item["doc_id"])
        if any(d == key_doc and abs(line_no - ln) <= 2 for d, ln in taken):
            continue
        taken.append((key_doc, line_no))
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _transcript_evidence(
    conn: sqlite3.Connection | None,
    repo_root: Path,
    terms: list[str],
    tickers: list[str],
) -> list[dict[str, object]]:
    if conn is None or not terms:
        return []
    scored: list[tuple[int, dict[str, object]]] = []
    for ticker in tickers:
        scored.extend(
            _scored_transcript_lines(repo_root, terms, ticker, _transcript_docs(conn, ticker))
        )
    return _dedupe_transcript_hits(scored, _MAX_TRANSCRIPT_ITEMS)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def gather_evidence(
    question: str,
    *,
    repo_root: Path,
    db_path: Path,
    scope_tickers: list[str],
    cache_key: str | None = None,
) -> list[EvidenceItem]:
    """Retrieve numbered evidence for one narrative question.

    ``scope_tickers`` is the surface's default universe (the report's ticker,
    or the portfolio); tickers literally named in the question win over it.
    Returns [] whenever there is nothing relevant — the caller then asks the
    ungrounded question exactly as before.

    ``cache_key`` opts this call into the per-thread retrieval memo (L14): when
    set (a session id, or ``ticker:report_date``) and an identical retrieval —
    same thread, scope, normalized question, and DB mtime — recurs within the
    short memo TTL, the whole body is skipped, so the pack-router round-trip and
    every DB/file scan are avoided. ``None`` (the default) disables the memo, so
    existing callers and tests behave exactly as before.
    """
    q = question.strip()
    if not q:
        return []
    memo_key: tuple[object, ...] | None = None
    if cache_key:
        memo_key = turn_cache.gather_key(
            cache_key=cache_key,
            repo_root=repo_root,
            scope_tickers=scope_tickers,
            norm_question=turn_cache.normalize_question(q),
            db_token=turn_cache.db_mtime_token(db_path),
        )
        cached = turn_cache.get_gather(memo_key)
        if cached is not None:
            return cast("list[EvidenceItem]", cached)
    try:
        named = _named_tracked_tickers(q, db_path)
        scope = [t.strip().upper() for t in scope_tickers if t.strip()]
        tickers = (named or scope)[:_MAX_TICKERS]
        if not tickers:
            return []
        terms = question_terms(q)
        question_squashed = _squash(q)

        # Portfolio packs lead the numbering. Focus = the tickers the
        # question literally names; a small scope (the report drawer's one
        # ticker, an explicit ask-box universe) also focuses, while a wide
        # scope (the whole portfolio) means portfolio-wide pack views. The
        # channel is isolated so a pack bug can never cost document evidence.
        raw: list[dict[str, object]] = []
        try:
            focus = named or (scope if len(scope) <= _MAX_TICKERS else [])
            route = route_packs(q, db_path=db_path)
            raw.extend(load_packs(route.packs, db_path=db_path, focus_tickers=focus))
        except Exception:
            log.warning({"event": "ask_pack_channel_failed"}, exc_info=True)

        conn = _connect(db_path)
        try:
            if conn is not None:
                # Unresolved validation issues for the whole DB, parsed once
                # (best-effort: {} when the table is absent) and shared across
                # every fact item so a cross-source ⚠ disagreement reaches the
                # model's text, not only the UI popover.
                issues_by_ticker = load_unresolved_issues(conn)
                # PK fast-path first: fact_ref tokens resolve the EXACT series
                # and lead the fact channel; the NL name-match then fills the
                # rest, deduped by label so a token doesn't double its metric.
                ref_facts = _fact_ref_evidence(conn, q, issues_by_ticker)
                raw.extend(ref_facts)
                pinned = {str(it["label"]) for it in ref_facts}
                raw.extend(
                    it
                    for it in _fact_evidence(conn, question_squashed, tickers, issues_by_ticker)
                    if str(it["label"]) not in pinned
                )
            raw.extend(_filing_evidence(conn, repo_root, terms, tickers))
            if conn is not None:
                raw.extend(_transcript_evidence(conn, repo_root, terms, tickers))
        finally:
            if conn is not None:
                conn.close()

        items: list[EvidenceItem] = []
        for i, item in enumerate(raw[:_MAX_ITEMS], start=1):
            items.append(
                EvidenceItem(
                    n=i,
                    kind=str(item["kind"]),
                    label=str(item["label"]),
                    text=str(item["text"]),
                    doc_id=cast("int | None", item["doc_id"]),
                    href=cast("str | None", item["href"]),
                    source_url=cast("str | None", item["source_url"]),
                    confidence=cast("float | None", item.get("confidence")),
                    fact_ref=cast("str | None", item.get("fact_ref")),
                    ticker=cast("str | None", item.get("ticker")),
                    doc_type=cast("str | None", item.get("doc_type")),
                    period=cast("str | None", item.get("period")),
                    value=cast("str | None", item.get("value")),
                )
            )
        if memo_key is not None:
            turn_cache.put_gather(memo_key, cast("list[object]", items))
        return items
    except Exception:
        # Retrieval must never break the answer path.
        log.warning({"event": "ask_grounding_failed"}, exc_info=True)
        return []


# Follow-up retrieval caps (S7 agentic loop): each round may add at most
# this many items / this much text — the per-round token budget.
_MAX_NEW_ITEMS_PER_ROUND = 6
_MAX_ITEMS_PER_NEED = 2
_NEW_EVIDENCE_CHAR_BUDGET = 6000


def gather_requested_evidence(
    needs: list[EvidenceNeed],
    *,
    question: str,
    repo_root: Path,
    db_path: Path,
    scope_tickers: list[str],
    existing: list[EvidenceItem],
    max_items: int = _MAX_NEW_ITEMS_PER_ROUND,
) -> list[EvidenceItem]:
    """Targeted retrieval for one follow-up round (S7 agentic loop).

    Unlike :func:`gather_evidence` (which guesses channels and periods from
    the question), each ``EvidenceNeed`` names its channel explicitly, and
    ``period`` reaches transcripts/filings OLDER than the latest ones the
    one-shot pass is limited to. New items continue the numbering after
    ``existing`` so they join the same cite-or-don't-claim system; items
    duplicating something already presented are dropped. Best-effort and
    read-only — any failure contributes nothing, never raises.
    """
    try:
        scope = [t.strip().upper() for t in scope_tickers if t.strip()][:_MAX_TICKERS]
        fallback_terms = question_terms(question)

        raw: list[dict[str, object]] = []
        # Portfolio packs: one load_packs call over the distinct pack kinds,
        # focused on the tickers those needs name (empty → portfolio-wide).
        pack_kinds = list(dict.fromkeys(n.kind for n in needs if n.kind in PACK_KEYS))
        if pack_kinds:
            focus = list(dict.fromkeys(n.ticker for n in needs if n.kind in PACK_KEYS and n.ticker))
            try:
                raw.extend(load_packs(tuple(pack_kinds), db_path=db_path, focus_tickers=focus))
            except Exception:
                log.warning({"event": "ask_followup_pack_channel_failed"}, exc_info=True)

        conn = _connect(db_path)
        try:
            # Parsed once, shared across this round's fact needs (see
            # gather_evidence); {} when conn/table is absent.
            issues_by_ticker = load_unresolved_issues(conn) if conn is not None else {}
            for need in needs:
                if need.kind not in NEED_DOC_KINDS:
                    continue
                tickers = [need.ticker] if need.ticker else scope
                if not tickers:
                    continue
                terms = question_terms(need.query) or fallback_terms
                year, quarter = _parse_period(need.period)
                if need.kind == "fact" and conn is not None:
                    query_squashed = (
                        _squash(need.query) if need.query.strip() else _squash(question)
                    )
                    raw.extend(_fact_evidence(conn, query_squashed, tickers, issues_by_ticker))
                elif need.kind == "filing":
                    for ticker in tickers:
                        paths = (
                            _filing_paths_for_year(repo_root, ticker, year)
                            if year is not None
                            else _latest_filing_paths(repo_root, ticker, conn)
                        )
                        scored = _scored_filing_sections(conn, repo_root, terms, ticker, paths)
                        scored.sort(key=lambda s: s[0], reverse=True)
                        raw.extend(item for _, item in scored[:_MAX_ITEMS_PER_NEED])
                elif need.kind == "transcript" and conn is not None:
                    for ticker in tickers:
                        docs = _transcript_docs(
                            conn,
                            ticker,
                            year=year,
                            quarter=quarter,
                            limit=4 if (year is not None or quarter is not None) else 2,
                        )
                        scored = _scored_transcript_lines(repo_root, terms, ticker, docs)
                        raw.extend(_dedupe_transcript_hits(scored, _MAX_ITEMS_PER_NEED))
        finally:
            if conn is not None:
                conn.close()

        seen_keys = {(it.kind, it.label) for it in existing}
        seen_hrefs = {it.href for it in existing if it.href}
        start = max((it.n for it in existing), default=0) + 1
        out: list[EvidenceItem] = []
        budget = _NEW_EVIDENCE_CHAR_BUDGET
        for item in raw:
            kind = str(item["kind"])
            label = str(item["label"])
            href = cast("str | None", item["href"])
            if (kind, label) in seen_keys or (href is not None and href in seen_hrefs):
                continue
            text = str(item["text"])
            if len(text) > budget:
                continue
            budget -= len(text)
            seen_keys.add((kind, label))
            if href is not None:
                seen_hrefs.add(href)
            out.append(
                EvidenceItem(
                    n=start + len(out),
                    kind=kind,
                    label=label,
                    text=text,
                    doc_id=cast("int | None", item["doc_id"]),
                    href=href,
                    source_url=cast("str | None", item["source_url"]),
                    confidence=cast("float | None", item.get("confidence")),
                    fact_ref=cast("str | None", item.get("fact_ref")),
                    ticker=cast("str | None", item.get("ticker")),
                    doc_type=cast("str | None", item.get("doc_type")),
                    period=cast("str | None", item.get("period")),
                    value=cast("str | None", item.get("value")),
                )
            )
            if len(out) >= max_items:
                break
        return out
    except Exception:
        log.warning({"event": "ask_followup_retrieval_failed"}, exc_info=True)
        return []


def build_evidence_block(items: list[EvidenceItem]) -> str:
    """The prompt block: numbered evidence + the per-claim citation contract."""
    if not items:
        return ""
    lines = "\n".join(f"[{item.n}] {item.text}" for item in items)
    return f"""EVIDENCE — numbered sources retrieved for this question. Cite with [n]
immediately after each claim a source supports (e.g. "NPLs rose to 7.2% [1][3]"):

{lines}

CITE-OR-SAY-UNSURE: every figure or quote you take from the evidence above
must carry its [n] marker. Cite PER SENTENCE: each sentence that states a
figure, fact, or quote carries its own [n] marker(s) — never batch the
markers at the end of a paragraph, where a reader can't tell which sentence
they back. A figure the evidence doesn't cover must either come from a file
you actually opened with the Read tool — name that file inline — or be
flagged as unverified. Never invent numbers, and never attach [n] to a
claim the numbered evidence doesn't support.

WEIGH THE PROVENANCE: a fact may carry a bracketed caution tag — a scored
confidence below par and/or a cross-source disagreement, e.g.
"[conf 79%; ⚠ SEC says $101M, 0.99% delta]". When it does, do NOT state that
figure as settled fact: surface the uncertainty (give the range or name the
disagreeing source), discount it in your reasoning, and lean on the
higher-confidence evidence. A clean fact carries no tag — treat it as solid."""


_MARKER_RX = re.compile(r"\[(\d{1,2})\]")


def used_citation_items(final_text: str, items: list[EvidenceItem]) -> list[EvidenceItem]:
    """The subset of evidence the answer actually cited, in evidence order."""
    if not items:
        return []
    used = {int(m.group(1)) for m in _MARKER_RX.finditer(final_text)}
    return [item for item in items if item.n in used]


__all__ = [
    "NEED_DOC_KINDS",
    "NEED_KINDS",
    "EvidenceItem",
    "EvidenceNeed",
    "build_evidence_block",
    "gather_evidence",
    "gather_requested_evidence",
    "question_terms",
    "used_citation_items",
]
