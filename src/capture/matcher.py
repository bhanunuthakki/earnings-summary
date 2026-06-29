"""Deterministic roster ticker matching for captured musings — NO LLM.

A musing's ticker is matched over the tracked roster with plain string rules, not
a model: a voice transcription that mishears a ticker ("Nu" vs "new") must never
be *wrong-attributed*; it falls through to ``needs_ticker`` (NULL ticker) instead,
which the SOURCES band surfaces for one-click disposition. The matcher is biased
hard toward NOT guessing.

Two match lanes, both word-boundary:
  * SYMBOLS — case-SENSITIVE uppercase (``MELI``, ``GOOGL``). Lowercase prose
    never matches a bare symbol, so the common words "now"/"nu" can't be mistaken
    for the NOW/NU tickers; a typed uppercase symbol still resolves. A tiny
    stopword set (``SYMBOL_STOPWORDS`` = {"I", "A"}) is exempt so the capitalized
    pronoun/article in ordinary prose can't match a single-letter roster ticker.
  * PHRASES — case-INsensitive distinctive names/aliases (``Nubank``,
    ``MercadoLibre``, ``ServiceNow``). Curated to be distinctive enough that a
    casual mention attributes safely; anything ambiguous is left unmatched.

``build_roster_index`` / ``match_ticker`` are pure and unit-tested; ``load_roster``
reads ``tracked_companies`` and degrades to the built-in alias seed when absent.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from alias_manager import resolve_ticker
from identity import DEFAULT_USER_ID
from user_state._db import open_conn

# Distinctive name aliases for the tracked roster — case-insensitive phrase
# matches. Curated CONSERVATIVELY: only names distinctive enough that a casual
# investing-context mention is unambiguous. Common English words that happen to
# be tickers (NOW, META, UBER, ...) are deliberately NOT phrase-matched — the
# uppercase SYMBOL lane catches a typed "META" while lowercase "meta" stays
# unmatched, so a "meta realization" is never attributed to the stock. Extend as
# the roster grows; ambiguity is a feature (it routes to needs_ticker).
DISTINCTIVE_ALIASES: dict[str, str] = {
    "nubank": "NU",
    "nu holdings": "NU",
    "mercadolibre": "MELI",
    "mercado libre": "MELI",
    "servicenow": "NOW",
    "veeva": "VEEV",
    "rubrik": "RBRK",
    "novo nordisk": "NVO",
    "alphabet": "GOOG",
    "brookfield": "BN",
    "booking holdings": "BKNG",
    "sofi": "SOFI",
}

# Uppercase tokens the SYMBOL lane refuses to match even when a roster ticker
# shares the spelling. The capitalized pronoun "I" — and the sentence-initial
# article "A" — appear in nearly every typed/voice musing, so a single-letter
# roster ticker like "I" (or "A") would otherwise be picked up as a spurious
# candidate and flag every note ``needs_ticker``. This is the lane's ONLY
# case-folding exception; it's deliberately tiny. Other single-letter symbols
# (F, T, V, ...) are NOT excluded — they almost never appear capitalized as a
# standalone English word, so a typed single-letter ticker still resolves.
SYMBOL_STOPWORDS: frozenset[str] = frozenset({"I", "A"})


@dataclass(frozen=True, slots=True)
class RosterIndex:
    """Pre-built, canonicalized lookup tables. ``symbol_to_ticker`` keys are
    exact-case UPPER symbols; ``phrase_to_ticker`` keys are lowercased phrases."""

    symbol_to_ticker: Mapping[str, str]
    phrase_to_ticker: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class MatchResult:
    """``ticker`` is the single confident match or None. ``needs_ticker`` is True
    only when ≥2 distinct roster tickers appear (never guess between them)."""

    ticker: str | None
    candidates: tuple[str, ...]
    needs_ticker: bool


def build_roster_index(
    *, symbols: Iterable[str], phrases: Mapping[str, str] | None = None
) -> RosterIndex:
    """Canonicalize symbols (GOOGL→GOOG via alias_manager) and phrase targets."""
    sym: dict[str, str] = {}
    for s in symbols:
        key = str(s).strip().upper()
        if key:
            sym[key] = resolve_ticker(key)
    ph: dict[str, str] = {}
    if phrases:
        for k, v in phrases.items():
            key = " ".join(str(k).split()).lower()
            if key:
                ph[key] = resolve_ticker(str(v).strip().upper())
    return RosterIndex(symbol_to_ticker=sym, phrase_to_ticker=ph)


def _word_present(token: str, text: str) -> bool:
    """Whole-token presence with non-word boundaries (handles multi-word phrases
    and ticker symbols alike). ``text`` is searched verbatim — callers lowercase
    it for the case-insensitive phrase lane."""
    return re.search(rf"(?<!\w){re.escape(token)}(?!\w)", text) is not None


def match_ticker(text: str, roster: RosterIndex) -> MatchResult:
    """Deterministic match: 0 → (None, not-needed); 1 → that ticker; ≥2 → (None,
    needs_ticker). Symbols match case-sensitively; phrases case-insensitively."""
    if not text:
        return MatchResult(None, (), False)
    found: set[str] = set()
    for sym, ticker in roster.symbol_to_ticker.items():
        if sym in SYMBOL_STOPWORDS:
            continue
        if _word_present(sym, text):
            found.add(ticker)
    low = text.lower()
    for phrase, ticker in roster.phrase_to_ticker.items():
        if _word_present(phrase, low):
            found.add(ticker)
    candidates = tuple(sorted(found))
    if len(candidates) == 1:
        return MatchResult(candidates[0], candidates, False)
    if len(candidates) >= 2:
        return MatchResult(None, candidates, True)
    return MatchResult(None, (), False)


def load_roster(
    db_path: Path | str | None = None, *, user_id: str = DEFAULT_USER_ID
) -> RosterIndex:
    """Build the index from ``tracked_companies`` (ticker + name) merged with the
    distinctive-alias seed. Degrades to the seed alone when the DB / table is
    absent — the matcher must never crash the capture write path."""
    symbols: list[str] = []
    phrases: dict[str, str] = dict(DISTINCTIVE_ALIASES)
    try:
        conn = open_conn(db_path)
    except (FileNotFoundError, RuntimeError):
        return build_roster_index(symbols=symbols, phrases=phrases)
    try:
        try:
            rows = conn.execute(
                "SELECT ticker, name FROM tracked_companies "
                "WHERE user_id = ? AND archived_at IS NULL",
                (user_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for row in rows:
            ticker = str(row[0]).strip().upper()
            if ticker:
                symbols.append(ticker)
            name = ("" if row[1] is None else str(row[1])).strip().lower()
            # A multi-word company name is distinctive; a 1-2 char name is not.
            if len(name) >= 3 and " " in name:
                phrases.setdefault(name, ticker)
    finally:
        conn.close()
    return build_roster_index(symbols=symbols, phrases=phrases)
