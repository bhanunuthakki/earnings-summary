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
                source_doc_id as provenance
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
from pathlib import Path
from typing import cast

from ask.packs import PACK_KEYS, load_packs
from ask.router import route_packs

log = logging.getLogger(__name__)

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

    def chip_payload(self) -> dict[str, object]:
        return {
            "n": self.n,
            "kind": self.kind,
            "label": self.label,
            "href": self.href,
            "source_url": self.source_url,
            "confidence": self.confidence,
            "fact_ref": self.fact_ref,
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
        return sqlite3.connect(str(db_path), timeout=5.0)
    except sqlite3.Error:
        return None


def _doc_meta(conn: sqlite3.Connection, doc_id: int) -> tuple[str | None, str | None]:
    """(doc_type, source_url) for one documents row — best-effort."""
    try:
        row = conn.execute(
            "SELECT doc_type, source_url FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
    except sqlite3.Error:
        return (None, None)
    if row is None:
        return (None, None)
    return (
        str(row[0]) if row[0] is not None else None,
        str(row[1]) if row[1] is not None else None,
    )


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
    """Rows ordered (period_end DESC, id DESC) → first per period wins."""
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


def _fact_ref_kpi_item(
    conn: sqlite3.Connection, ticker: str, def_id: int
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
    rows = _dedupe_series(
        _series_rows_conf(
            conn,
            "SELECT period_end, fiscal_period_type, value, unit, source_doc_id{conf} "
            "FROM kpi_facts WHERE ticker = ? AND kpi_definition_id = ? "
            "ORDER BY period_end DESC, id DESC LIMIT 64",
            (ticker, def_id),
        )
    )
    if not rows:
        return None
    item = _fact_item(ticker, name, unit or str(rows[0][3] or ""), rows, conn)
    item["fact_ref"] = f"kpi:{ticker}:{def_id}"
    return item


def _fact_ref_fin_item(
    conn: sqlite3.Connection, ticker: str, line_item: str, fpt: str
) -> dict[str, object] | None:
    """Resolve a ``fin:{ticker}:{line_item}:{fpt}`` handle to its series."""
    clauses = "ticker = ? AND line_item = ?"
    params: list[object] = [ticker, line_item]
    fpt_u = fpt.upper()
    if fpt_u in _FIN_SPECIFIC_FPT:
        clauses += " AND fiscal_period_type = ?"
        params.append(fpt_u)
    else:
        clauses += " AND fiscal_period_type IN ('Q1','Q2','Q3','Q4')"
    sql = (
        "SELECT period_end, fiscal_period_type, value, unit, source_doc_id{conf} "
        f"FROM financial_facts WHERE {clauses} ORDER BY period_end DESC, id DESC LIMIT 64"
    )
    rows = _dedupe_series(_series_rows_conf(conn, sql, tuple(params)))
    if not rows:
        return None
    label = line_item.replace("_", " ").title()
    item = _fact_item(ticker, label, str(rows[0][3] or ""), rows, conn)
    item["fact_ref"] = f"fin:{ticker}:{line_item}:{fpt}"
    return item


def _fact_ref_evidence(conn: sqlite3.Connection, question: str) -> list[dict[str, object]]:
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
        item = _fact_ref_kpi_item(conn, m.group(1).upper(), def_id)
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
        item = _fact_ref_fin_item(conn, key[0], key[1], key[2])
        if item is not None:
            out.append(item)
        if len(out) >= _MAX_FACT_ITEMS:
            return out
    return out


def _fact_evidence(
    conn: sqlite3.Connection, question_squashed: str, tickers: list[str]
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
            if _phrase_in(question_squashed, _label_core(str(d[1])))
        ]
        # Longest (most specific) labels first.
        matched_kpis.sort(key=lambda d: len(d[1]), reverse=True)
        for def_id, name, unit in matched_kpis:
            if per_ticker >= _MAX_FACT_ITEMS_PER_TICKER:
                break
            rows = _dedupe_series(
                _series_rows_conf(
                    conn,
                    "SELECT period_end, fiscal_period_type, value, unit, source_doc_id{conf} "
                    "FROM kpi_facts WHERE ticker = ? AND kpi_definition_id = ? "
                    "ORDER BY period_end DESC, id DESC LIMIT 64",
                    (ticker, def_id),
                )
            )
            if not rows:
                continue
            found.append(_fact_item(ticker, name, unit or str(rows[0][3] or ""), rows, conn))
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
            rows = _dedupe_series(
                _series_rows_conf(
                    conn,
                    "SELECT period_end, fiscal_period_type, value, unit, source_doc_id{conf} "
                    "FROM financial_facts WHERE ticker = ? AND line_item = ? "
                    "AND fiscal_period_type IN ('Q1','Q2','Q3','Q4') "
                    "ORDER BY period_end DESC, id DESC LIMIT 64",
                    (ticker, line_item),
                )
            )
            if not rows:
                continue
            label = line_item.replace("_", " ").title()
            found.append(_fact_item(ticker, label, str(rows[0][3] or ""), rows, conn))
            per_ticker += 1

        if len(found) >= _MAX_FACT_ITEMS:
            break
    return found[:_MAX_FACT_ITEMS]


def _fact_item(
    ticker: str,
    label: str,
    unit: str,
    rows: list[tuple[object, ...]],
    conn: sqlite3.Connection,
) -> dict[str, object]:
    points = "; ".join(f"{_period_label(r[0], r[1])} {_fmt_value(r[2])}" for r in rows)
    doc_id_raw = rows[0][4]
    doc_id = int(cast("int", doc_id_raw)) if doc_id_raw is not None else None
    doc_type, source_url = _doc_meta(conn, doc_id) if doc_id is not None else (None, None)
    # Scored confidence (S2) of the newest fact row — the one the chip cites.
    confidence: float | None = None
    if len(rows[0]) > 5 and rows[0][5] is not None:
        try:
            confidence = float(cast("float", rows[0][5]))
        except (TypeError, ValueError):
            confidence = None
    unit_part = f", {unit}" if unit and unit != "actual" else ""
    src_part = f" — source: {doc_type}" if doc_type else ""
    return {
        "kind": "fact",
        "label": f"{ticker} · {label}",
        "text": f"{ticker} {label} (newest first{unit_part}): {points}{src_part}",
        "doc_id": doc_id,
        "href": f"/source/{doc_id}" if doc_id is not None else None,
        "source_url": source_url,
        "confidence": confidence,
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


def _latest_filing_paths(repo_root: Path, ticker: str) -> list[tuple[str, int, Path]]:
    """[(form, year, path)] — the newest 10-K and newest 10-Q on disk."""
    base = repo_root / "data" / "historical" / "fmp"
    out: list[tuple[str, int, Path]] = []
    for form in ("10k", "10q"):
        best: tuple[int, Path] | None = None
        for path in base.glob(f"{ticker}_form_{form}_*.json"):
            m = re.search(r"_(\d{4})\.json$", path.name)
            if not m:
                continue
            year = int(m.group(1))
            if best is None or year > best[0]:
                best = (year, path)
        if best is not None:
            out.append((form, best[0], best[1]))
    return out


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


def _scored_filing_sections(
    conn: sqlite3.Connection | None,
    repo_root: Path,
    terms: list[str],
    ticker: str,
    paths: list[tuple[str, int, Path]],
) -> list[tuple[int, dict[str, object]]]:
    """Keyword-score every section across the given filing files."""
    scored: list[tuple[int, dict[str, object]]] = []
    for form, year, path in paths:
        try:
            raw_payload: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(raw_payload, dict):
            continue
        payload = cast("dict[str, object]", raw_payload)
        doc_id = _filing_doc_id(conn, repo_root, path)
        for section, value in payload.items():
            if section in _FILING_META_KEYS:
                continue
            text = _flatten_section(value)
            if not text:
                continue
            squashed = _squash(section + " " + text[:8000])
            score = sum(1 for t in terms if f" {t} " in f" {squashed} ")
            score += 2 * sum(1 for t in terms if f" {t} " in f" {_squash(section)} ")
            if score <= 0:
                continue
            form_label = "10-K" if form == "10k" else "10-Q"
            snippet = " ".join(text.split())[:_FILING_SNIPPET_CHARS]
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
                conn, repo_root, terms, ticker, _latest_filing_paths(repo_root, ticker)
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
        rows = conn.execute(
            "SELECT t.document_id, d.file_path, t.fiscal_period_type, t.period_end "
            "FROM transcripts t JOIN documents d ON d.id = t.document_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY t.period_end DESC LIMIT ?",
            tuple(params),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [(int(r[0]), str(r[1]), str(r[2] or ""), str(r[3] or "")) for r in rows]


def _scored_transcript_lines(
    repo_root: Path,
    terms: list[str],
    ticker: str,
    docs: list[tuple[int, str, str, str]],
) -> list[tuple[int, dict[str, object]]]:
    """Keyword-score every substantive line across the given transcript docs."""
    scored: list[tuple[int, dict[str, object]]] = []
    for doc_id, file_path, fpt, period_end in docs:
        path = repo_root / file_path
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        call_label = _period_label(period_end, fpt)
        for line_no, line in enumerate(lines, start=1):
            flat = " ".join(line.split())
            if len(flat) < 40:  # skip headers/blank/speaker-only lines
                continue
            squashed = f" {_squash(flat)} "
            score = sum(1 for t in terms if f" {t} " in squashed)
            if score <= 0:
                continue
            snippet = flat[:_TRANSCRIPT_SNIPPET_CHARS]
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
) -> list[EvidenceItem]:
    """Retrieve numbered evidence for one narrative question.

    ``scope_tickers`` is the surface's default universe (the report's ticker,
    or the portfolio); tickers literally named in the question win over it.
    Returns [] whenever there is nothing relevant — the caller then asks the
    ungrounded question exactly as before.
    """
    q = question.strip()
    if not q:
        return []
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
                # PK fast-path first: fact_ref tokens resolve the EXACT series
                # and lead the fact channel; the NL name-match then fills the
                # rest, deduped by label so a token doesn't double its metric.
                ref_facts = _fact_ref_evidence(conn, q)
                raw.extend(ref_facts)
                pinned = {str(it["label"]) for it in ref_facts}
                raw.extend(
                    it
                    for it in _fact_evidence(conn, question_squashed, tickers)
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
                )
            )
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
                    raw.extend(_fact_evidence(conn, query_squashed, tickers))
                elif need.kind == "filing":
                    for ticker in tickers:
                        paths = (
                            _filing_paths_for_year(repo_root, ticker, year)
                            if year is not None
                            else _latest_filing_paths(repo_root, ticker)
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
                    fact_ref=cast("str | None", item.get("fact_ref")),
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
claim the numbered evidence doesn't support."""


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
