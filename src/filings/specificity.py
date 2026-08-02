"""P3 boilerplate-vs-substance classifier — deterministic specificity proxy.

Template: **Hope, Hu & Lu, *RAST* 21(4), 2016**, "Specificity" — an explicit
measure separating generic risk language from firm-specific disclosure;
market reaction to a 10-K is more positive when specificity is higher. This
is the template the build-stack doc names *instead of* assuming cosine
similarity handles boilerplate implicitly (Brown & Tucker's implicit
argument, which the literature review explicitly rejects as this module's
basis — ``docs/design/disclosure_change_signals.md`` §1.4).

Four deterministic proxies for "how firm-specific is this text," computed
per unit of length so a longer hunk isn't automatically scored more
specific just for having more words:

* **number density** — dollar figures, percentages, counts. A checkable,
  quantified claim is inherently firm-specific.
* **date density** — years and month names. Specific timing is a firm fact,
  not generic risk phrasing.
* **entity density** — a crude named-entity proxy (capitalized words that
  are NOT the first word of their sentence — i.e. not just
  sentence-initial capitalization). Catches named products, geographies,
  competitors, regulators, subsidiaries.
* **boilerplate phrase density** — hits against a curated list of
  near-universal 10-K risk/MD&A phrasing that recurs across filers
  regardless of what the company does. Subtracted from the other three.

**These thresholds are volume-calibrated against the real book, not
correctness-calibrated against labeled data.** P0/P1's thresholds were tuned
against measured FALSE-POSITIVE rates on the real 97-110-filing corpus
(``disclosure_change_signals.md`` §2); no labeled boilerplate/substantive
corpus exists yet for specificity, so there is no equivalent precision check
here. What IS measured: ``LOW_SPECIFICITY_MAX`` originally required a literal
``BOILERPLATE_PHRASES`` hit on top of the score threshold, but running the
scorer over 4,260 real item-level events (40 tickers, risk_factors + mdna)
showed that requirement suppressed the LOW band to ~2% of events — 1,207
events had a near-zero score (<=0.12) with NO literal phrase match, i.e. the
curated phrase list is far narrower than the real breadth of generic legal
prose. Dropping the phrase-hit requirement (score alone gates LOW) raised the
deterministically-resolved share from ~37% to ~65% (LOW 1,281 + HIGH 1,473
of 4,260) without a labeled set to confirm those verdicts are individually
correct — revisit once enough LLM/human verdicts accumulate to spot-check.

The composite score classifies into three bands used by
``filings.boilerplate_classify``:

* ``LOW`` — confidently boilerplate; resolved deterministically, never sent
  to an LLM.
* ``HIGH`` — confidently substantive (dense numbers/entities/dates, minimal
  boilerplate); also resolved deterministically. Hope/Hu/Lu's whole thesis
  is that high specificity IS the substantive signal, so a text this dense
  in firm-specific content does not need LLM judgment to confirm it.
- ``AMBIGUOUS`` — the survivors that actually reach the LLM triage step
  (``filings.boilerplate_triage``), per the "deterministic first, LLM for
  judgment only" cross-cutting rule.

Also implements **diff-hunk extraction**: never send a whole item body to
anything (deterministic scorer or LLM) when only part of it changed —
``extract_diff_hunk`` reduces a reworded item's before/after excerpts to
just the changed spans plus a small context window, on top of the fact that
``disclosure_events`` never stores more than a ~500-char excerpt per side in
the first place.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from enum import StrEnum

_WORD_RX = re.compile(r"[A-Za-z']+")
_NUMBER_RX = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")
_YEAR_RX = re.compile(r"\b(?:19|20)\d{2}\b")
_MONTH_RX = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\b"
)
_SENTENCE_SPLIT_RX = re.compile(r"(?<=[.!?])\s+")
_CAP_WORD_RX = re.compile(r"^[A-Z][a-zA-Z&\-]+$")
_TRIM_RX = re.compile(r"^[\"'(\[]+|[\"'.,;:)\]]+$")

#: Highly generic legal/risk-disclosure phrasing that recurs across nearly
#: every filer's risk factors/MD&A regardless of what the company does — the
#: boilerplate half of Hope/Hu/Lu's specificity axis. Deliberately
#: conservative (only phrases close to universal in 10-K risk language) so a
#: hit is a strong signal, not a coincidental one.
BOILERPLATE_PHRASES: tuple[str, ...] = (
    "could adversely affect our business",
    "financial condition and results of operations",
    "there can be no assurance",
    "may not be able to",
    "could have a material adverse effect",
    "we cannot guarantee",
    "general economic conditions",
    "competitive pressures",
    "forward-looking statements",
    "risks and uncertainties",
    "as a result of the foregoing",
    "may be unable to",
    "could harm our business",
    "adversely affect our operating results",
    "may adversely impact",
    "we may be unable to",
    "our business, financial condition",
    "results of operations and cash flows",
    "we face intense competition",
    "if we fail to",
    "may materially and adversely affect",
    "our future prospects",
)

#: Below this many words, density-based scores are noisy (a single number or
#: capitalized word swings the ratio wildly) — too little text to trust
#: either band confidently.
MIN_WORDS_FOR_CONFIDENT_BAND = 15
#: A score at/below this is confidently generic. ~30th percentile of
#: specificity_score measured across 4,260 real item-level events (see
#: module docstring) — score alone, no boilerplate-phrase-hit requirement.
LOW_SPECIFICITY_MAX = 0.12
#: A score at/above this is confidently firm-specific.
HIGH_SPECIFICITY_MIN = 0.35


class SpecificityBand(StrEnum):
    LOW = "low"
    AMBIGUOUS = "ambiguous"
    HIGH = "high"


@dataclass(slots=True, frozen=True)
class SpecificityMetrics:
    word_count: int
    number_count: int
    date_count: int
    entity_count: int
    boilerplate_hits: int
    number_density: float
    entity_density: float
    date_density: float
    boilerplate_density: float
    specificity_score: float
    band: SpecificityBand


def _tokens(text: str) -> list[str]:
    return _WORD_RX.findall(text)


def _count_boilerplate_hits(lower_text: str) -> int:
    return sum(lower_text.count(phrase) for phrase in BOILERPLATE_PHRASES)


def _count_entities(text: str) -> int:
    """Crude named-entity proxy: capitalized words that are NOT the first
    word of their sentence. Deliberately simple (no real NER dependency) —
    this is a cheap deterministic PROXY for firm-specific naming, not an
    entity extractor; false positives (a mid-sentence "We" after a comma)
    are accepted as noise this coarse a signal tolerates."""
    count = 0
    for sentence in _SENTENCE_SPLIT_RX.split(text):
        words = sentence.split()
        for i, raw in enumerate(words):
            if i == 0:
                continue
            w = _TRIM_RX.sub("", raw)
            if w and _CAP_WORD_RX.match(w):
                count += 1
    return count


#: Numeric share at/above which a hunk reads as tabular rather than narrative.
#: ``numbers / (words + numbers)`` — measured over 24,243 real quoted events,
#: prose sits far below this and scraped table rows far above it.
TABULAR_NUMERIC_SHARE_MIN = 0.30
#: How many "glued" digit/letter boundaries (a cell wall lost in extraction,
#: e.g. ``Total3,380`` or ``(in millions)20242023``) mark a hunk as tabular.
TABULAR_GLUE_HITS_MIN = 2
#: ...and at what RATE per word. An absolute count alone is length-biased: a
#: 9,600-word narrative section accumulates a few incidental hits and would be
#: condemned by them, while a 6-word table row trips this rate immediately.
#: Caught by the one-ticker dry sweep — MELI's 63,738-char Item 1 Business
#: (numeric share 0.018, i.e. almost pure prose) was being suppressed on 5 hits.
TABULAR_GLUE_RATE_MIN = 0.02

#: A digit run welded to a letter with NO separator — the signature of a lost
#: table-cell boundary. Deliberately requires a 3+ digit run (or grouped/decimal
#: digits) so ordinary financial prose tokens are NOT caught: ``Q4``, ``H1``,
#: ``FY2025``, ``10-K``, ``8-K``, ``S-1``, ``COVID-19``, ``5G``, ``CO2`` all
#: carry short or hyphen-separated digit runs and must stay narrative.
#:
#: ``$`` is deliberately NOT in the leading class: ``$612.4`` is how prose
#: writes money, not a collapsed cell wall. Including it condemned any narrative
#: carrying a few dollar figures — the exact false positive the dry sweep found.
#: ``%`` stays, because a percent sign butting the next number (``%73.2``) is a
#: cell boundary, not a way anyone writes a sentence.
_TABLE_GLUE_RX = re.compile(r"[A-Za-z)%][\d][\d,.]{2,}|[\d][\d,.]{2,}[A-Za-z(]")


def looks_tabular(text: str) -> bool:
    """True when a hunk is extracted TABLE content rather than narrative prose.

    Why this exists: ``specificity_metrics`` treats number density as evidence
    of firm-specificity (Hope/Hu/Lu 2016) — a premise that holds for PROSE and
    inverts on a table. A scraped 10-K table row is almost entirely numbers, so
    it scores maximally specific and takes the confident-HIGH fast path
    straight to a ``substantive`` verdict without an LLM ever reading it.

    Measured over the real book (24,243 non-dismissed events carrying a quote):
    table-shaped hunks scored a mean specificity of 0.708 and landed HIGH 66.7%
    of the time, versus 0.268 / 27.8% for prose — and were then written
    ``substantive`` 74.3% of the time versus 50.1% for prose, while being
    called boilerplate only 5.4% of the time versus 44.4%. The classifier was
    not missing table junk; it was certifying it.

    This is a SHAPE test, never a materiality judgment: callers use it to
    withhold a fast path, not to assign a verdict. A false positive costs one
    LLM triage call; it can never suppress a real disclosure change.
    """
    words = _tokens(text)
    numbers = _NUMBER_RX.findall(text)
    denom = len(words) + len(numbers)
    if denom == 0:
        return False
    if len(numbers) / denom >= TABULAR_NUMERIC_SHARE_MIN:
        return True
    glue = len(_TABLE_GLUE_RX.findall(text))
    # Count AND rate: the count keeps a two-cell fragment from qualifying on a
    # single hit, the rate keeps a long narrative from being condemned by a
    # handful of incidental ones.
    return glue >= TABULAR_GLUE_HITS_MIN and glue / max(len(words), 1) >= TABULAR_GLUE_RATE_MIN


def specificity_metrics(text: str) -> SpecificityMetrics:
    """Compute the deterministic specificity proxy for one text hunk.

    Empty/near-empty text is ``AMBIGUOUS`` (never a confident boilerplate
    verdict just because there was nothing to measure — an empty hunk is a
    measurement gap, not evidence of genericness).
    """
    words = _tokens(text)
    word_count = len(words)
    if word_count == 0:
        return SpecificityMetrics(
            word_count=0,
            number_count=0,
            date_count=0,
            entity_count=0,
            boilerplate_hits=0,
            number_density=0.0,
            entity_density=0.0,
            date_density=0.0,
            boilerplate_density=0.0,
            specificity_score=0.0,
            band=SpecificityBand.AMBIGUOUS,
        )

    lower = text.lower()
    number_count = len(_NUMBER_RX.findall(text))
    date_count = len(_YEAR_RX.findall(text)) + len(_MONTH_RX.findall(text))
    entity_count = _count_entities(text)
    boilerplate_hits = _count_boilerplate_hits(lower)

    number_density = number_count / word_count
    entity_density = entity_count / word_count
    date_density = date_count / word_count
    boilerplate_density = boilerplate_hits / word_count

    # Composite: reward concrete, checkable content; penalize generic legal
    # phrasing at 2x weight, since one boilerplate PHRASE match (multi-word)
    # is a stronger, less-ambiguous signal than one bare capitalized word.
    raw = (number_density + entity_density + date_density) - 2.0 * boilerplate_density
    specificity_score = max(0.0, min(1.0, raw))

    if word_count < MIN_WORDS_FOR_CONFIDENT_BAND:
        band = SpecificityBand.AMBIGUOUS
    elif specificity_score <= LOW_SPECIFICITY_MAX:
        band = SpecificityBand.LOW
    elif specificity_score >= HIGH_SPECIFICITY_MIN:
        band = SpecificityBand.HIGH
    else:
        band = SpecificityBand.AMBIGUOUS

    return SpecificityMetrics(
        word_count=word_count,
        number_count=number_count,
        date_count=date_count,
        entity_count=entity_count,
        boilerplate_hits=boilerplate_hits,
        number_density=number_density,
        entity_density=entity_density,
        date_density=date_density,
        boilerplate_density=boilerplate_density,
        specificity_score=specificity_score,
        band=band,
    )


def extract_diff_hunk(
    prior_text: str, current_text: str, *, context_words: int = 6, max_hunks: int = 3
) -> str:
    """Reduce a reworded item's before/after text to just the CHANGED spans,
    each with a small surrounding context window — never the whole excerpt.

    Word-level (not char-level) opcodes via ``difflib.SequenceMatcher``:
    coarser than char-diff, but the output stays human/LLM-readable (whole
    words, not fragments) while still excluding every unchanged run longer
    than the context window. Returns ``""`` when there is no detectable
    change (identical text) — the caller falls back to the stored
    ``evidence_quote`` in that case.
    """
    prior_words = prior_text.split()
    current_words = current_text.split()
    matcher = difflib.SequenceMatcher(a=prior_words, b=current_words, autojunk=False)
    hunks: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        before = " ".join(prior_words[i1:i2]) if i2 > i1 else "(none)"
        after = " ".join(current_words[j1:j2]) if j2 > j1 else "(none)"
        lo_ctx = " ".join(current_words[max(0, j1 - context_words) : j1])
        hi_ctx = " ".join(current_words[j2 : j2 + context_words])
        hunks.append(f"...{lo_ctx} [WAS: {before}] [NOW: {after}] {hi_ctx}...")
        if len(hunks) >= max_hunks:
            break
    return "\n".join(hunks)
