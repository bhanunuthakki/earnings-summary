"""Deterministic, injection-safe query planning for the lexical retrieval lanes.

Both narrative lexical lanes compile untrusted natural-language questions into
inert FTS5 MATCH expressions through this one planner, replacing the earlier
OR-of-all-tokens compilers:

* **Stopword handling.** Question filler never becomes an operand, so a
  stopword-heavy question can neither flood the candidate pool with
  incidental single-word hits nor dilute bm25 ranking. A question that is
  stopword-only plans to ``None`` and the lanes return no lexical candidates.
* **Phrase queries.** Quoted spans in the question compile to phrase operands
  (adjacent, in order) instead of being broken into independent OR tokens.
* **KPI alias expansion.** When the legacy ``kpi_aliases`` table is present,
  query terms that match an alias row expand to the counterpart spelling as an
  OR alternative inside their AND group. The table is dropped at the checkout's
  ``0002_drop_dead_tables`` migration, so current-head deployments resolve an
  empty alias map (never an error): alias help degrades to a no-op while sealed
  retrieval stays fail-closed on its corpus seals. Pre-0002 deployments and
  hermetic fixtures that carry the table get the expansion.
* **AND-biased matching with OR fallback.** Lanes first match all content
  terms; only an empty result falls back to any-term matching. This intentionally
  prioritizes high-precision conjunctive hits when available over broader partial
  matches; when conjunctive hits exist, broader disjunctive hits are omitted
  in favor of higher precision.

The compiled expression is a pure function of ``(query_text, alias map)``.
The sealed trace verifier re-runs the same planner on the same committed
question and must reproduce the exact candidates and scores, so the planner
performs no I/O, reads no clock, and never randomizes. Every operand is
double-quoted with embedded quotes doubled, preserving the injection-safety
contract of the original compilers: untrusted text can only become inert
quoted strings, never FTS5 operators.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "about",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "but",
        "by",
        "can",
        "could",
        "d",
        "did",
        "do",
        "does",
        "doing",
        "explain",
        "for",
        "from",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "ll",
        "m",
        "me",
        "my",
        "no",
        "nor",
        "not",
        "of",
        "on",
        "or",
        "our",
        "ours",
        "out",
        "over",
        "own",
        "re",
        "s",
        "she",
        "so",
        "some",
        "such",
        "t",
        "tell",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "too",
        "up",
        "us",
        "ve",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "you",
        "your",
        "yours",
    }
)
"""Narrative-lane stopword set: filler that never discriminates documents."""

MAX_OPERANDS: int = 24
"""Upper bound on total operands per compiled expression (bounded token cap)."""

MAX_ALIAS_ROWS: int = 500
"""Upper bound on alias rows read per plan; protects the query-time read."""

_TOKEN_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]+")
"""Free-text query tokens: two or more characters, as the original
OR-of-all-tokens compilers tokenized (single stray characters in free text are
noise and never matched before the planner either)."""

_WORD_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
"""Single-or-more-character words: quoted-phrase spans and curated alias
names keep every word, including one-letter tokens (``"R&D"``-style spans)."""

_PHRASE_RX = re.compile(r'"([^"]*)"')


@dataclass(frozen=True)
class LexicalQueryPlan:
    """Inert FTS5 plan for one untrusted narrative question.

    ``groups`` carries one AND group per content term: the term operand plus
    any alias expansions of it, ORed inside the group. ``operands`` is every
    distinct operand across the groups, query-derived terms first, then
    alias-derived variants in discovery order.
    """

    groups: tuple[tuple[str, ...], ...]
    operands: tuple[str, ...]

    @property
    def match_all(self) -> str:
        """AND-biased expression: every content term (or an alias of it) matches."""
        return " AND ".join(_group_expression(group) for group in self.groups)

    @property
    def match_any(self) -> str:
        """Recall-preserving fallback expression: any operand matches."""
        return " OR ".join(_quoted(operand) for operand in self.operands)


def plan_lexical_query(
    query_text: str,
    *,
    aliases: Mapping[str, tuple[str, ...]] | None = None,
) -> LexicalQueryPlan | None:
    """Plan one untrusted question into inert quoted FTS5 operands.

    Returns ``None`` when the question carries no content term, so the lanes
    fail closed to an empty lexical candidate list rather than matching
    incidental stopword hits.
    """

    alias_map: Mapping[str, tuple[str, ...]] = {} if aliases is None else aliases
    phrases: list[str] = []
    tokens: list[str] = []
    seen: set[str] = set()

    def _add_operand(text: str, *, sink: list[str]) -> None:
        key = _normalize_alias(text)
        if not key or key in seen:
            return
        seen.add(key)
        sink.append(text)

    remainder_parts: list[str] = []
    cursor = 0
    for match in _PHRASE_RX.finditer(query_text):
        remainder_parts.append(query_text[cursor : match.start()])
        cursor = match.end()
        words = _WORD_RX.findall(match.group(1))
        if not words:
            continue
        if len(words) == 1:
            _add_operand(words[0], sink=tokens)
        else:
            _add_operand(" ".join(words), sink=phrases)
    remainder_parts.append(query_text[cursor:])

    for match in _TOKEN_RX.finditer(" ".join(remainder_parts)):
        _add_operand(match.group(0), sink=tokens)

    content_phrases = [
        phrase
        for phrase in phrases
        if any(word.casefold() not in STOPWORDS for word in phrase.split())
    ]
    content_tokens = [token for token in tokens if token.casefold() not in STOPWORDS]
    content = content_phrases + content_tokens
    if not content:
        return None

    groups: list[tuple[str, ...]] = []
    operands: list[str] = []
    used = {_normalize_alias(term) for term in content}
    for term in content:
        if len(operands) >= MAX_OPERANDS:
            break
        group = [term]
        operands.append(term)
        for variant in alias_map.get(_normalize_alias(term), ()):
            if len(operands) >= MAX_OPERANDS:
                break
            if not variant or variant in used:
                continue
            used.add(variant)
            group.append(variant)
            operands.append(variant)
        groups.append(tuple(group))
    return LexicalQueryPlan(groups=tuple(groups), operands=tuple(operands))


def kpi_alias_expansions(conn: sqlite3.Connection) -> dict[str, tuple[str, ...]]:
    """Bidirectional alias variants from current definition revisions and legacy alias rows.

    Reads admitted, active reported labels and counterpart canonical KPI names from
    ``kpi_definition_revisions`` joined with ``kpi_definitions`` when present at current schema head,
    as well as active rows from legacy ``kpi_aliases`` when present in older snapshots or fixtures.
    Absence of either table yields an empty map rather than an error.
    Keys and variants are normalized (casefolded, token-joined).
    Deterministic: variants are sorted alphabetically per key.
    """

    variants: dict[str, set[str]] = {}

    has_revisions = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kpi_definition_revisions'"
        ).fetchone()
        is not None
    )
    has_definitions = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kpi_definitions'"
        ).fetchone()
        is not None
    )

    if has_revisions and has_definitions:
        rows = conn.execute(
            "SELECT d.name, r.reported_label FROM kpi_definition_revisions AS r "
            "JOIN kpi_definitions AS d ON d.id=r.kpi_definition_id "
            "WHERE r.status='admitted' AND r.lifecycle='active' "
            "ORDER BY r.kpi_definition_revision_id LIMIT ?",
            (MAX_ALIAS_ROWS,),
        ).fetchall()
        for canonical_raw, reported_raw in rows:
            canonical = _normalize_alias(str(canonical_raw))
            reported = _normalize_alias(str(reported_raw))
            if not canonical or not reported or canonical == reported:
                continue
            variants.setdefault(reported, set()).add(canonical)
            variants.setdefault(canonical, set()).add(reported)

    has_legacy = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kpi_aliases'"
        ).fetchone()
        is not None
    )

    if has_legacy:
        remaining_rows = max(0, MAX_ALIAS_ROWS - sum(len(v) for v in variants.values()))
        if remaining_rows > 0:
            rows = conn.execute(
                "SELECT canonical_name,reported_name FROM kpi_aliases "
                "WHERE effective_to IS NULL ORDER BY id LIMIT ?",
                (remaining_rows,),
            ).fetchall()
            for canonical_raw, reported_raw in rows:
                canonical = _normalize_alias(str(canonical_raw))
                reported = _normalize_alias(str(reported_raw))
                if not canonical or not reported or canonical == reported:
                    continue
                variants.setdefault(reported, set()).add(canonical)
                variants.setdefault(canonical, set()).add(reported)

    return {key: tuple(sorted(values)) for key, values in variants.items()}


def fact_alias_terms(
    terms: Sequence[str], aliases: Mapping[str, tuple[str, ...]]
) -> tuple[str, ...]:
    """Alias-derived instr terms for the canonical fact lane.

    The fact lane scores with exact ``instr`` substring probes, so each
    normalized alias variant joins as one full term (multi-word variants stay
    one precise phrase probe rather than exploding into per-word noise).
    Deterministic: variants follow term order, deduped against the terms and
    each other.
    """

    seen = {_normalize_alias(term) for term in terms}
    expanded: list[str] = []
    for term in terms:
        for variant in aliases.get(_normalize_alias(term), ()):
            if not variant or variant in seen:
                continue
            seen.add(variant)
            expanded.append(variant)
    return tuple(expanded)


def _normalize_alias(text: str) -> str:
    return " ".join(_WORD_RX.findall(text.casefold()))


def _quoted(operand: str) -> str:
    """Inert quoted operand: embedded quotes doubled, nothing else escapes."""

    return '"' + operand.replace('"', '""') + '"'


def _group_expression(group: tuple[str, ...]) -> str:
    expression = " OR ".join(_quoted(operand) for operand in group)
    if len(group) > 1:
        return "(" + expression + ")"
    return expression
