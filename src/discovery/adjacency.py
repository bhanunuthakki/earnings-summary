"""Adjacency miner (master build P5.3): companies repeatedly named NEAR the
portfolio — in the holdings' competitive watchlists, in the holdings' own
earnings-call transcripts, and in their news rows — resolved against the
tracked universe and surfaced as discovery candidates.

Deterministic and LLM-free. The matcher is the load-bearing piece: each
index-member company becomes a normalized MATCH PHRASE (legal suffixes
stripped — "Blackstone Inc." → "blackstone"; "Expedia Group" → "expedia"),
and a document matches only on the FULL phrase at WORD BOUNDARIES.
Substring matching is banned by construction: probing the real universe
showed `%KLA%` happily matching "kirKLAnd's" and `%Rio %` matching "Bravo
bRIO" — word-boundary full-phrase matching kills that class. Two further
guards:

  * single-token phrases must be >= 5 chars for transcript/news scanning
    (a 3-letter phrase like "kla" false-positives endlessly in prose) but
    only >= 2 for the competitive-watchlist strings, which are short,
    intentional lists where "KLA" really means KLA Corp;
  * a multi-token name ALSO matches on its distinctive first token alone
    ("Marvell Technology" matches a bare "Marvell") — but only when that
    token is unique across the lexicon's first tokens, >= 6 chars, not a
    generic English opener ("american", "applied", ...), AND appears
    Capitalized in the raw document (prose says "we applied"; companies
    are written "Applied Materials");
  * documents are tokenized ONCE into word/bigram sets, so scanning ~2.3k
    phrases over a transcript is set lookups, not 2.3k regex passes.

Sources mined (each hit carries the holding it was found near and a
human-readable detail line for the queue's "why surfaced" column):

  watchlist  — micro_thesis/holdings/<T>.json ``competitive_watchlist``
  transcript — the last N processed transcripts per portfolio holding
               (transcripts/processed/<T>_Q<q>_<year>.txt), counted;
               below ``min_mentions`` total is noise, not adjacency
  news       — the news table rows of portfolio holdings (headline +
               snippet)
"""

from __future__ import annotations

import itertools
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from identity import DEFAULT_USER_ID
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

log = logging.getLogger(__name__)

# Legal/structural suffixes stripped from company names before matching.
_NAME_SUFFIXES: frozenset[str] = frozenset(
    {
        "inc",
        "inc.",
        "incorporated",
        "corp",
        "corp.",
        "corporation",
        "co",
        "co.",
        "company",
        "ltd",
        "ltd.",
        "limited",
        "plc",
        "group",
        "holdings",
        "holding",
        "sa",
        "s.a.",
        "nv",
        "n.v.",
        "ag",
        "se",
        "lp",
        "l.p.",
        "trust",
        "international",
    }
)

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'\-]*")
_CAP_WORD_RE = re.compile(r"\b[A-Z][A-Za-z0-9'\-]*")

_TRANSCRIPT_RE = re.compile(r"^(?P<ticker>[A-Z][A-Z0-9.\-]*)_Q(?P<q>[1-4])_(?P<y>\d{4})\.txt$")

# Generic name openers that read as ordinary prose even when unique in the
# lexicon — never used as a standalone first-token match.
_GENERIC_FIRST_TOKENS: frozenset[str] = frozenset(
    {
        "american",
        "applied",
        "advanced",
        "central",
        "consolidated",
        "digital",
        "eastern",
        "federal",
        "first",
        "general",
        "global",
        "international",
        "national",
        "northern",
        "pacific",
        "public",
        "southern",
        "standard",
        "united",
        "universal",
        "western",
    }
)


@dataclass(slots=True)
class AdjacencyHit:
    """One adjacency observation: ``ticker`` surfaced near ``holding``."""

    ticker: str
    name: str | None
    source: str  # "watchlist" | "transcript" | "news"
    holding: str
    detail: str


@dataclass(slots=True)
class _Phrase:
    ticker: str
    name: str
    tokens: tuple[str, ...]
    # The distinctive-first-token shortcut ("marvell" for Marvell Technology)
    # — set by build_lexicon only when unique/long/non-generic; matching it
    # additionally requires the token Capitalized in the raw document.
    alt_token: str | None = None

    @property
    def text(self) -> str:
        return " ".join(self.tokens)


def normalize_phrase(name: str) -> tuple[str, ...]:
    """Company name → matchable token phrase (lowercased, suffix-stripped).
    Trailing suffix tokens drop until a non-suffix token remains; a name
    that is ALL suffixes (degenerate) keeps its first token."""
    tokens = _WORD_RE.findall(name.lower())
    while len(tokens) > 1 and tokens[-1] in _NAME_SUFFIXES:
        tokens.pop()
    return tuple(tokens)


def build_lexicon(
    db_path: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    fmp_dir: Path | None = None,
) -> list[_Phrase]:
    """Match phrases for every index-member name (the discoverable set).
    With ``fmp_dir`` set, names whose profile says not-actively-trading are
    dropped — a delisted ghost (acquired, merged) is not a candidate even
    when a holding's watchlist still names it."""
    if not db_path.exists():
        return []
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT ticker, name FROM tracked_companies "
            "WHERE user_id = ? AND list_type = 'index_member'",
            (user_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out: list[_Phrase] = []
    for r in rows:
        ticker = str(r[0]).upper()
        if fmp_dir is not None:
            from discovery.screens import is_actively_trading

            if not is_actively_trading(fmp_dir, ticker):
                continue
        name = str(r[1])
        tokens = normalize_phrase(name)
        if tokens:
            out.append(_Phrase(ticker=ticker, name=name, tokens=tokens))
    first_counts: dict[str, int] = {}
    for p in out:
        first_counts[p.tokens[0]] = first_counts.get(p.tokens[0], 0) + 1
    for p in out:
        first = p.tokens[0]
        if (
            len(p.tokens) > 1
            and first_counts[first] == 1
            and len(first) >= 6
            and first not in _GENERIC_FIRST_TOKENS
        ):
            p.alt_token = first
    return out


@dataclass(slots=True)
class _DocIndex:
    """One document tokenized once — phrase matching is set lookups."""

    words: set[str]
    bigrams: set[str]
    spaced: str
    cap_words: set[str]  # tokens that appeared Capitalized in the raw text


def _doc_index(text: str) -> _DocIndex:
    words = _WORD_RE.findall(text.lower())
    bigrams = {f"{a} {b}" for a, b in itertools.pairwise(words)}
    cap_words = {w.lower() for w in _CAP_WORD_RE.findall(text)}
    return _DocIndex(words=set(words), bigrams=bigrams, spaced=" ".join(words), cap_words=cap_words)


def _phrase_in_doc(phrase: _Phrase, doc: _DocIndex, *, min_single: int) -> bool:
    toks = phrase.tokens
    if len(toks) == 1:
        if len(toks[0]) >= min_single and toks[0] in doc.words:
            return True
    elif len(toks) == 2:
        if phrase.text in doc.bigrams:
            return True
    elif all(t in doc.words for t in toks) and f" {phrase.text} " in f" {doc.spaced} ":
        return True
    # Distinctive-first-token shortcut: "Marvell" alone means Marvell
    # Technology — but only Capitalized in the source (prose filter).
    return (
        phrase.alt_token is not None
        and phrase.alt_token in doc.words
        and phrase.alt_token in doc.cap_words
    )


def _count_phrase(phrase: _Phrase, doc: _DocIndex) -> int:
    padded = f" {doc.spaced} "
    full = padded.count(f" {phrase.text} ")
    if phrase.alt_token is not None:
        # The alt-token count subsumes full-phrase occurrences.
        return max(full, padded.count(f" {phrase.alt_token} "))
    return full


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def portfolio_tickers(db_path: Path, *, user_id: str = DEFAULT_USER_ID) -> list[str]:
    """The holdings whose surroundings get mined."""
    if not db_path.exists():
        return []
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT ticker FROM tracked_companies "
            "WHERE user_id = ? AND list_type = 'portfolio' ORDER BY ticker",
            (user_id,),
        ).fetchall()
        return [str(r[0]).upper() for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def mine_watchlists(repo_root: Path, lexicon: list[_Phrase]) -> list[AdjacencyHit]:
    """competitive_watchlist entries across micro_thesis/holdings/*.json,
    resolved against the lexicon (single-token phrases allowed from 2 chars
    here — the entries are short, intentional rival lists)."""
    hits: list[AdjacencyHit] = []
    holdings_dir = repo_root / "micro_thesis" / "holdings"
    if not holdings_dir.exists():
        return hits
    for path in sorted(holdings_dir.glob("*.json")):
        try:
            raw: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        doc = cast("dict[str, object]", raw)
        watchlist = doc.get("competitive_watchlist")
        if not isinstance(watchlist, list):
            continue
        holding = path.stem.upper()
        for entry in cast("list[object]", watchlist):
            if not isinstance(entry, str) or not entry.strip():
                continue
            doc = _doc_index(entry)
            for phrase in lexicon:
                if _phrase_in_doc(phrase, doc, min_single=2):
                    hits.append(
                        AdjacencyHit(
                            ticker=phrase.ticker,
                            name=phrase.name,
                            source="watchlist",
                            holding=holding,
                            detail=f"on {holding}'s competitive watchlist ({entry.strip()!r})",
                        )
                    )
    return hits


def _latest_transcripts(repo_root: Path, ticker: str, n: int) -> list[Path]:
    """Up to ``n`` most recent processed transcripts for one holding."""
    d = repo_root / "transcripts" / "processed"
    if not d.exists():
        return []
    upper = ticker.upper()
    dated: list[tuple[int, int, Path]] = []
    for p in d.iterdir():
        m = _TRANSCRIPT_RE.match(p.name)
        if m and m.group("ticker").upper() == upper:
            dated.append((int(m.group("y")), int(m.group("q")), p))
    dated.sort(reverse=True)
    return [p for _y, _q, p in dated[:n]]


def mine_transcripts(
    repo_root: Path,
    lexicon: list[_Phrase],
    holdings: list[str],
    *,
    per_holding: int = 4,
    min_mentions: int = 3,
) -> list[AdjacencyHit]:
    """Companies named repeatedly in the holdings' own earnings calls.
    Counts the FULL phrase at word boundaries across the holding's last
    ``per_holding`` transcripts; fewer than ``min_mentions`` total is
    treated as passing noise, not adjacency."""
    hits: list[AdjacencyHit] = []
    for holding in holdings:
        paths = _latest_transcripts(repo_root, holding, per_holding)
        if not paths:
            continue
        counts: dict[str, int] = {}
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            doc = _doc_index(text)
            for phrase in lexicon:
                if phrase.ticker == holding:
                    continue
                if _phrase_in_doc(phrase, doc, min_single=5):
                    counts[phrase.ticker] = counts.get(phrase.ticker, 0) + _count_phrase(
                        phrase, doc
                    )
        by_ticker = {p.ticker: p for p in lexicon}
        for ticker, count in sorted(counts.items()):
            if count < min_mentions:
                continue
            phrase = by_ticker[ticker]
            hits.append(
                AdjacencyHit(
                    ticker=ticker,
                    name=phrase.name,
                    source="transcript",
                    holding=holding,
                    detail=(
                        f"named {count}x across {holding}'s last {len(paths)} earnings call(s)"
                    ),
                )
            )
    return hits


def mine_news(
    db_path: Path,
    lexicon: list[_Phrase],
    holdings: list[str],
    *,
    user_id: str = DEFAULT_USER_ID,
) -> list[AdjacencyHit]:
    """Companies named in the holdings' news rows (headline + snippet)."""
    _ = user_id  # news rows are ticker-keyed, not user-keyed
    hits: list[AdjacencyHit] = []
    if not db_path.exists() or not holdings:
        return hits
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return hits
    marks = ",".join("?" * len(holdings))
    try:
        rows = conn.execute(
            f"SELECT ticker, headline, COALESCE(snippet, '') FROM news WHERE ticker IN ({marks})",
            holdings,
        ).fetchall()
    except sqlite3.Error:
        return hits
    finally:
        conn.close()
    seen: set[tuple[str, str]] = set()
    for r in rows:
        holding = str(r[0]).upper()
        headline = str(r[1])
        doc = _doc_index(f"{headline} {r[2]}")
        for phrase in lexicon:
            if phrase.ticker == holding or (phrase.ticker, holding) in seen:
                continue
            if _phrase_in_doc(phrase, doc, min_single=5):
                seen.add((phrase.ticker, holding))
                hits.append(
                    AdjacencyHit(
                        ticker=phrase.ticker,
                        name=phrase.name,
                        source="news",
                        holding=holding,
                        detail=f"in {holding} news: {headline.strip()!r}",
                    )
                )
    return hits


def mine_adjacency(
    repo_root: Path,
    db_path: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    fmp_dir: Path | None = None,
    per_holding_transcripts: int = 4,
    min_transcript_mentions: int = 3,
) -> list[AdjacencyHit]:
    """All three sources over the current portfolio. Best-effort everywhere —
    a missing dir/table just contributes nothing."""
    lexicon = build_lexicon(db_path, user_id=user_id, fmp_dir=fmp_dir)
    if not lexicon:
        return []
    holdings = portfolio_tickers(db_path, user_id=user_id)
    hits = mine_watchlists(repo_root, lexicon)
    hits += mine_transcripts(
        repo_root,
        lexicon,
        holdings,
        per_holding=per_holding_transcripts,
        min_mentions=min_transcript_mentions,
    )
    hits += mine_news(db_path, lexicon, holdings, user_id=user_id)
    log.info(
        {
            "event": "discovery_adjacency_done",
            "lexicon": len(lexicon),
            "holdings": len(holdings),
            "hits": len(hits),
        }
    )
    return hits
