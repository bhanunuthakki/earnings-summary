"""Per-claim citation grounding for Ask answers (fund-grade build S8).

``ask.grounding`` numbers the evidence and the answer carries inline ``[n]``
markers, but until S8 the citations event was ANSWER-level: a regex collected
every marker that appeared anywhere in the final text. A claim could drift
from its source unnoticed — the answer cites [1] for sentence A while
sentence B's figure rides along uncited, and nothing flags it.

This module restructures the contract to CLAIM level, with two channels that
get reconciled:

* **inline markers** — the prompt contract (``grounding.build_evidence_block``)
  now demands a ``[n]`` on every claim-bearing sentence. The markers live in
  the visible text, so they are authoritative for what a sentence cites.
* **structured claim map** — after the answer streams, one fast-model call
  (purpose ``ask_claim_grounding``: Haiku pin in ``llm.cli.LLM_MODELS``,
  budget row seeded by alembic 0090, ``llm.structured`` parse-retry) re-reads
  the answer against the numbered evidence and emits
  ``{"claims": [{"quote", "cites", "supported"}]}`` — which sentences are
  factual claims, which evidence backs each, and which claims NO listed
  evidence supports.

Reconciliation (``_reconcile``) anchors each mapped quote to a real sentence
of the answer and resolves cites conservatively: a sentence's inline markers
win when present (the user sees them); the map's cites are accepted only for
sentences the model left unmarked (recovered cites); cites outside the
evidence numbering are dropped; quotes that anchor to no sentence are
dropped. A claim with no surviving cites is ``supported=False`` regardless
of what the map said.

Failure policy — fail CLOSED to the pre-S8 behavior. On any non-ok path
(budget skip, transport failure, unusable JSON after the retry, zero
anchored claims paired with a degenerate map) ``build_citations_payload``
falls back to the answer-level regex: the same ``items`` the event carried
before S8, ``grounding: "answer_level"``, no ``claims`` key. The citations
SSE event is therefore a strict superset of the legacy shape and old
clients keep working.

Streaming: the inline ``[n]`` markers ride the ``delta`` frames as plain
text (they survive SSE chunking trivially — they're just characters); the
claim map arrives in the trailing ``citations`` event after ``final``, and
the render path upgrades the already-displayed text in place
(``ui.cite_marks``). No delta buffering, no trailer stripping.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ask.grounding import EvidenceItem, used_citation_items
from llm.structured import call_llm_structured
from llm_budget import should_skip_for_budget

log = logging.getLogger(__name__)

PURPOSE = "ask_claim_grounding"

# A degenerate map (every quote dropped) reads as model failure, not as "the
# answer made no claims" — only an explicitly empty claims list means that.
_MAX_CLAIMS = 40
_CLAIM_TEXT_CHARS = 240
_MIN_QUOTE_CHARS = 12

_MARKER_RX = re.compile(r"\[(\d{1,2})\]")
# Sentence boundary: terminal punctuation followed by whitespace, or a
# newline (markdown bullets/paragraphs break sentences too).
_SENTENCE_SPLIT_RX = re.compile(r"(?<=[.!?])\s+|\n+")
_NORM_STRIP_RX = re.compile(r"\[\d{1,2}\]|[*_`#]+")


@dataclass(frozen=True, slots=True)
class Claim:
    """One claim-bearing sentence of the answer, mapped to its evidence."""

    text: str  # the anchored sentence (trimmed), markers included
    cites: tuple[int, ...]  # evidence numbers, ascending
    supported: bool  # False = no listed evidence backs this claim

    def payload(self) -> dict[str, object]:
        return {"text": self.text, "cites": list(self.cites), "supported": self.supported}


def _normalize(text: str) -> str:
    """Matching form: markers + markdown tokens out, whitespace collapsed,
    case folded — so a quote anchors whether or not the model copied the
    ``[n]`` markers along with the words."""
    return " ".join(_NORM_STRIP_RX.sub(" ", text).lower().split())


def split_sentences(text: str) -> list[str]:
    """The answer's sentences (trimmed, empties dropped) — the unit a claim
    anchors to, shared with the eval harness so graders segment identically."""
    return [s.strip() for s in _SENTENCE_SPLIT_RX.split(text) if s.strip()]


def _anchor_sentence(quote: str, sentences: list[str], normed: list[str]) -> int | None:
    """Index of the sentence the quote was copied from; None when it anchors
    nowhere (a paraphrase or hallucinated quote — dropped by contract)."""
    q = _normalize(quote)
    if len(q) < _MIN_QUOTE_CHARS:
        return None
    for i, s in enumerate(normed):
        if q in s or (len(s) >= _MIN_QUOTE_CHARS and s in q):
            return i
    return None


def _inline_cites(sentence: str, valid: frozenset[int]) -> tuple[int, ...]:
    found = {int(m.group(1)) for m in _MARKER_RX.finditer(sentence)}
    return tuple(sorted(found & valid))


def _reconcile(
    raw_claims: list[object],
    final_text: str,
    items: list[EvidenceItem],
) -> list[Claim] | None:
    """Anchor + reconcile the model's map against the answer text.

    Returns None (fail closed) when the map is structurally unusable; an
    empty list is a VALID outcome (the model judged no sentence a claim).
    """
    valid = frozenset(item.n for item in items)
    sentences = split_sentences(final_text)
    normed = [_normalize(s) for s in sentences]

    by_sentence: dict[int, tuple[set[int], bool]] = {}
    dropped = 0
    for entry in raw_claims[:_MAX_CLAIMS]:
        if not isinstance(entry, dict):
            dropped += 1
            continue
        entry_map = cast("dict[str, object]", entry)
        quote = entry_map.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            dropped += 1
            continue
        idx = _anchor_sentence(quote, sentences, normed)
        if idx is None:
            dropped += 1
            continue
        raw_cites = entry_map.get("cites")
        map_cites: set[int] = set()
        if isinstance(raw_cites, list):
            for c in cast("list[object]", raw_cites):
                if isinstance(c, int) and not isinstance(c, bool) and c in valid:
                    map_cites.add(c)
        inline = set(_inline_cites(sentences[idx], valid))
        # Inline markers are authoritative — the map only RECOVERS cites for
        # sentences the answer left unmarked, never overrides visible ones.
        cites = inline if inline else map_cites
        supported = bool(cites) and (entry_map.get("supported") is not False)
        prev = by_sentence.get(idx)
        if prev is None:
            by_sentence[idx] = (cites, supported)
        else:
            prev[0].update(cites)
            by_sentence[idx] = (prev[0], (prev[1] or supported) and bool(prev[0]))

    if raw_claims and not by_sentence:
        # Every entry was unusable — that's a failed extraction, not a
        # clean "no claims" answer.
        log.warning({"event": "ask_claim_grounding_map_unusable", "dropped": dropped})
        return None
    return [
        Claim(
            text=sentences[idx][:_CLAIM_TEXT_CHARS],
            cites=tuple(sorted(cites)),
            supported=supported,
        )
        for idx, (cites, supported) in sorted(by_sentence.items())
    ]


def _build_prompt(final_text: str, items: list[EvidenceItem]) -> str:
    evidence_lines = "\n".join(f"[{item.n}] {item.text}" for item in items)
    return f"""You audit one finished answer from a portfolio research assistant for
citation grounding. You get the ANSWER and the NUMBERED EVIDENCE it was
allowed to cite. Output ONLY a JSON object — no prose, no markdown fences:

{{"claims": [{{"quote": "<the claim sentence, copied verbatim from the answer,
[n] markers included>", "cites": [evidence numbers that directly support it],
"supported": true|false}}]}}

Rules:
- One entry per sentence in the answer that states a checkable fact: a
  figure, a quote, a dated event, a comparison. Skip greetings, hedges,
  questions, pure opinion, and meta commentary. An answer with no factual
  claims gets {{"claims": []}}.
- "quote" must be copied from the answer text (you may shorten to the first
  ~15 words). Never paraphrase.
- "cites" may only contain numbers from the evidence list, and only when
  that evidence actually states what the claim says. Keep the answer's own
  [n] markers when they are correct; add a number the answer omitted only
  when the evidence clearly supports the sentence; never cite evidence that
  does not support the claim.
- "supported": false when no listed evidence backs the claim (then "cites"
  MUST be []).

EVIDENCE:
{evidence_lines}

ANSWER:
{final_text}

JSON:"""


def extract_claim_map(
    final_text: str,
    items: list[EvidenceItem],
    *,
    db_path: Path,
) -> list[Claim] | None:
    """The claims→cites map for one finished answer. Never raises.

    None means fail-closed (budget skip / transport failure / unusable map)
    — the caller degrades to answer-level citations. An empty list is a
    real result: the model audited the answer and found no factual claims.
    """
    text = final_text.strip()
    if not text or not items:
        return None
    if should_skip_for_budget(PURPOSE, db_path=db_path) is not None:
        log.info({"event": "ask_claim_grounding_budget_skipped"})
        return None
    try:
        payload = call_llm_structured(
            _build_prompt(text, items),
            purpose=PURPOSE,
            scope="ask",
            expect="object",
            required_keys=("claims",),
        )
    except Exception as exc:
        # Interactive surface: a transport failure, hard stop, or doubly-bad
        # JSON must degrade to answer-level citations, never break the turn.
        log.warning(
            {"event": "ask_claim_grounding_failed", "error": f"{type(exc).__name__}: {exc}"}
        )
        return None
    raw = cast("dict[str, object]", payload).get("claims")
    if not isinstance(raw, list):
        return None
    return _reconcile(cast("list[object]", raw), text, items)


def build_citations_payload(
    final_text: str,
    items: list[EvidenceItem],
    *,
    db_path: Path,
) -> dict[str, object] | None:
    """The citations event body for one finished narrative answer.

    Per-claim shape (a strict superset of the legacy event — old clients
    read ``items`` and ignore the rest)::

        {"items": [chip payloads], "claims": [{text, cites, supported}],
         "grounding": "per_claim"}

    ``items`` covers every evidence item cited inline anywhere in the answer
    PLUS items recovered by the claim map for unmarked sentences. On any
    claim-map failure the payload is exactly the pre-S8 answer-level event
    (plus ``grounding: "answer_level"``); None when nothing was cited at
    all — the caller then emits no citations event, as before.
    """
    if not items:
        return None
    inline_used = used_citation_items(final_text, items)
    claims = extract_claim_map(final_text, items, db_path=db_path)
    if claims is None:
        if not inline_used:
            return None
        return {
            "items": [item.chip_payload() for item in inline_used],
            "grounding": "answer_level",
        }
    used_ns = {item.n for item in inline_used} | {n for c in claims for n in c.cites}
    used = [item for item in items if item.n in used_ns]
    if not used and not claims:
        return None
    return {
        "items": [item.chip_payload() for item in used],
        "claims": [c.payload() for c in claims],
        "grounding": "per_claim",
    }


__all__ = [
    "PURPOSE",
    "Claim",
    "build_citations_payload",
    "extract_claim_map",
    "split_sentences",
]
