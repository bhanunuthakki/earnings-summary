"""P4 transcript longitudinal tracking — deterministic core.

``docs/design/disclosure_change_build_stack.md`` P4 +
``docs/design/disclosure_change_signals.md`` §1.7. Same architecture as
P0/P1: deterministic candidate generation first, LLM only for the two
genuinely judgment-shaped questions ("is this answer evasive?", "is this
KPI business-meaningful?"), each asked in ONE batched call per
transcript/ticker — never per question, never per KPI. The LLM step lives
in ``transcripts.transcript_judgment``, kept in its own module so this one
stays free of any call boundary a test would need to mock (mirrors
``filings.metric_lifecycle`` / ``filings.metric_triage``).

What the literature establishes, so this module does not re-derive it:

* **Q&A carries more signal than prepared remarks** and must be scored
  separately (Matsumoto, Pronk & Roelofsen, *TAR* 86(4), 2011).
* **>90% of topic-share variation is firm-level**, ~70% of that
  within-firm-over-time — track deltas, never raw levels (Hassan,
  Hollander, van Lent & Tahoun, *QJE* 134(4), 2019).
* **Tone must be residualized against fundamentals** before calling it a
  shift (Huang, Teoh & Zhang, *TAR* 89(3), 2014 — ABTONE). See
  ``fit_tone_residual_model``/``tone_residual`` below for the real fitted
  residual (D2.3); ``tone_delta_heuristic_flag`` is the OLDER transparent
  two-rule proxy, kept only for side-by-side comparison, never the
  production abnormal-tone signal.
* **CEO and CFO language are not interchangeable** — only CFO language was
  tradeable (Larcker & Zakolyukina, *JAR* 50(2), 2012). Speakers are always
  tracked BY NAME below, never pooled into one "management" bucket.
  ``parse_participants_roster`` (D2.4) resolves named speakers to CEO/CFO
  from the transcript's own CORPORATE PARTICIPANTS block where one exists,
  ahead of the (still-empty-book-wide) ``exec_comp_packages`` DEF 14A join.
  This module does NOT implement, and explicitly does not claim, Larcker/
  Zakolyukina's deception-marker language model — the tone score here is a
  different, sentiment-only construct (see ``transcript_judgment.judge_call``).
* **Vocal affect is an audio signal** (Mayew & Venkatachalam, *JF* 67(1),
  2012) — explicitly out of scope for a text-only pipeline. This module
  never proxies it from "um"/"uh" disfluency counts.

DROPPED — P4 non-answer rate (D1.6, owner ruling, 2026-07-25): Gow, Larcker
& Zakolyukina's ~11% non-answer baseline never reproduced against this
book (measured 30.1% after the attribution fix, spread widened rather than
narrowed) and the owner ruled DROP, not repair — no further tuning of the
threshold/baseline. Verified against a copy of prod ``disclosure_events``
on 2026-07-25: zero ``qa_nonanswer_rate_shift`` rows were ever written (the
construct never reached prod), so this was a code-only removal, no data
purge needed. The non-answer verdict is also no longer requested from the
LLM (see ``transcript_judgment.build_qa_judgment_prompt`` — only the tone
question remains in the per-transcript batched call). Do not rebuild a
non-answer scoring path on this substrate without a NEW literature
reproduction, not just a threshold retune.

Real-book note (measured 2026-07-25 against a copy of ``data/portfolio.db``,
546 ``transcripts`` rows / 863 ``transcript_segments`` rows): only ~17
transcripts (~3%) were ingested with real per-turn speaker attribution
(``compute.transcript_ingest``'s PDF path, i.e. >=2 stored segment rows).
Re-measured later the same day (concurrent ingestion in flight elsewhere in
the session): 548 ``transcripts`` / 165 with >=2 segments (~30%) — the
corpus grew, but the qualitative split holds: the majority is still
aggregator-sourced Q&A-only, and the D2.4 CORPORATE PARTICIPANTS heading
itself was found on only 7 of those 165 (NVDA, TSM) — the "inline intro"
shape (see ``parse_participants_roster``) resolves a CEO and/or CFO on a
further ~18 calls across other tickers with real prepared remarks.
The other ~97% (by the original measurement) are ``execution/fetch_qa_transcript.py`` aggregator pulls —
ONE ``transcript_segments`` row holding the ENTIRE call as a single blob.
Critically, that source fetches **Q&A only** ("prepared remarks are
reproducible from the press release + investor deck and are excluded here
to keep the input focused") with ``has_qa_section`` unconditionally true —
so for the large majority of this book there ARE no prepared remarks to
split out at all. ``load_call_turns`` below re-splits that raw blob into
turns at READ time (it never re-ingests or duplicates
``transcript_ingest.py``'s own stored-segment path); the rest of the
pipeline sees one uniform ``Turn`` shape regardless of which path produced
the row, and callers can read ``CallSnapshot.parse_coverage`` to know which
lane fired for absence-is-never-silent reporting.

Speaker-role classification below is a **structural frequency heuristic**,
not a resolved name roster: ``exec_comp_packages`` (DEF 14A NEO names/roles)
is joined when populated, but is EMPTY for the whole book as of this
writing, so today every management speaker resolves to
``exec_role=None`` ("unresolved") rather than a fabricated CEO/CFO label —
this is an honest degrade, not a broken join. Tone and non-answer measures
are computed and tracked BY NAME regardless, so the CEO-vs-CFO
non-interchangeability finding is respected even without a resolved title.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field

from filings.models import HardStopError

log = logging.getLogger(__name__)

EVENTS_TABLE = "disclosure_events"
DETECTOR_VERSION = "transcript_longitudinal_v1"

# ---------------------------------------------------------------------------
# Materiality constants — named and justified, not magic numbers scattered
# through the detection logic below.
# ---------------------------------------------------------------------------

# A KPI must be present in every one of this many consecutive PRIOR calls
# before its absence this quarter counts as a candidate — a KPI mentioned
# only once, then dropped, is far weaker evidence of a deliberate change
# than one mentioned repeatedly and then dropped.
TOPIC_MIN_PRIOR_PRESENCE = 2

# Analyst-roster overlap (Jaccard) below this floor, on calls with at least
# this many analysts on both sides, is treated as material coverage churn.
ANALYST_ROSTER_OVERLAP_FLOOR = 0.25
ANALYST_ROSTER_MIN_N = 5

# Tone score scale is -1.0 (very negative/defensive) .. +1.0 (very
# positive/confident). A delta below this magnitude is noise even before
# the earnings-surprise check.
TONE_MATERIALITY = 0.35
# "Small surprise" floor (absolute eps_surprise_pct) used by the
# disproportion check in `tone_delta_heuristic_flag` — see that function's
# docstring for what this proxy is and, more importantly, is NOT.
TONE_SMALL_SURPRISE_PCT = 2.0

# Minimum pooled (ticker, period, speaker) tone observations before
# `fit_tone_residual_model` will even attempt a fit — below this an OLS fit
# is fitting noise, not signal (with an intercept + 1 predictor, 3 points is
# the bare mathematical minimum for a residual to exist at all; this floor
# is deliberately well above that). See that function's docstring for the
# book's own measured power (2026-07-25: n=48, R^2=0.06) and why this stays
# a within-book deviation flag, never a claim of statistical significance.
ABTONE_MIN_OBSERVATIONS = 10
# Same materiality floor as `tone_delta_heuristic_flag`'s TONE_MATERIALITY —
# the residual lives on the same -1..1 tone scale, so reusing the
# already-justified threshold avoids inventing a second, unvalidated magic
# number derived from this small sample's own residual variance.
ABTONE_RESIDUAL_MATERIALITY = TONE_MATERIALITY

# Frequency heuristic for management-vs-analyst classification (see
# `classify_roles`). A share-based fallback (promote a top-ranked name below
# the absolute count floor when it accounts for a large SHARE of turns) was
# tried and rejected: on a short call with few distinct analysts, a single
# one-off analyst question already claims a large share of the (small)
# non-operator-turn denominator, which incorrectly promoted one-shot
# analysts to MANAGEMENT (caught by
# test_single_question_analyst_not_classified_management). The absolute
# floor alone is the honest choice — every real multi-question call
# measured this session had every management name answer >=2 questions
# (MELI: 8/8/4, NU: 13/7/3/3/2), so the floor essentially never misses a
# real management speaker; the one accepted gap is a call with exactly ONE
# Q&A exchange total, where frequency alone cannot distinguish the two
# roles at all.
_MAX_MANAGEMENT_CANDIDATES = 4
_MIN_MANAGEMENT_TURN_COUNT = 2


def _now_naive_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _normalize_name(raw: str) -> str:
    """Collapse internal whitespace runs and trim (PDF/scrape artifact)."""
    return re.sub(r"\s+", " ", raw).strip()


# ---------------------------------------------------------------------------
# Turn model + parsing (see module docstring for the three source shapes)
# ---------------------------------------------------------------------------


class SpeakerRole(StrEnum):
    OPERATOR = "operator"
    MANAGEMENT = "management"
    ANALYST = "analyst"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Turn:
    seq: int
    speaker: str | None
    text: str


@dataclass(frozen=True)
class RosterEntry:
    name: str
    role: SpeakerRole
    turn_count: int
    total_chars: int
    exec_role: str | None = None
    # 'participants_block' (D2.4 — this transcript's own CORPORATE
    # PARTICIPANTS roster) | 'exec_comp_packages' (DEF 14A join) | None
    # (unresolved). Reported so coverage can be measured honestly rather
    # than inferred from exec_role alone.
    exec_role_source: str | None = None


@dataclass(frozen=True)
class QAExchange:
    index: int
    analyst_name: str | None
    question_text: str
    answer_speakers: tuple[str, ...]
    answer_text: str


@dataclass(frozen=True)
class CallSnapshot:
    """Everything derived deterministically from ONE transcript."""

    transcript_id: int
    ticker: str
    fiscal_period_type: str
    fiscal_year: int
    period_end: datetime
    turns: list[Turn]
    roster: dict[str, RosterEntry]
    exchanges: list[QAExchange]
    # D2.4 — name -> ParsedParticipant, parsed from THIS transcript's own
    # CORPORATE PARTICIPANTS block (or inline intro prose). Empty when no
    # such block/intro was found (honest degrade, not a guess) — see
    # `parse_participants_roster`.
    participants_roster: dict[str, ParsedParticipant]
    # 'segmented' (trusted transcript_segments rows) | 'reparsed_aggregator' |
    # 'reparsed_whisper' | 'reparsed_fallback' | 'unattributed' (single blob,
    # no speaker structure recoverable at all).
    parse_coverage: str


# --- aggregator (roic.ai-style) blob re-parsing -----------------------------
#
# execution/fetch_qa_transcript.py stores Q&A-only text where each speaker
# turn is a paragraph starting "<first-initial-of-first-name> <Full Name>"
# immediately followed by their speech with NO delimiter (no colon, no
# newline) — e.g. "A Andrew Ruben Hey. Andrew of Morgan Stanley here...".
# aggregator_sources._split_into_speaker_paragraphs already inserts the
# blank-line breaks between turns at fetch time, so paragraph splitting on
# blank lines recovers turn boundaries; recovering the NAME within a
# paragraph requires walking capitalized tokens until we hit prose, which
# is inherently a heuristic (documented limitation: this occasionally over-
# captures a leading capitalized word of the reply, e.g. an interjection
# right after the name — accepted as noise a low-frequency ANALYST bucket
# absorbs harmlessly; it never contaminates the high-frequency MANAGEMENT
# bucket that the tone/non-answer measures below actually depend on).

_AGG_BANNER_END_RE = re.compile(r"===\s*Q&A SEGMENT\s*===\s*\n")

_AGG_STOP_AFTER_NAME = frozenset(
    {
        "and",
        "so",
        "thanks",
        "thank",
        "hey",
        "yes",
        "yeah",
        "well",
        "i",
        "we",
        "our",
        "this",
        "that",
        "great",
        "good",
        "morning",
        "afternoon",
        "hi",
        "hello",
        "ok",
        "okay",
        "sure",
        "perhaps",
        "no",
        "not",
        "it",
        "there",
        "just",
        "first",
        "one",
        "two",
        "regarding",
        "please",
        "go",
        "ahead",
        "congrats",
        "congratulations",
        "nice",
        "quick",
        "maybe",
        "actually",
        "obviously",
        "again",
        "operator",
        "all",
        "if",
        "it's",
    }
)
_AGG_NAME_PARTICLES = frozenset(
    {"de", "do", "dos", "das", "los", "van", "der", "la", "du", "st", "von", "al", "da", "di"}
)
_AGG_LEAD_RE = re.compile(r"^([A-Z])\s+(.*)$", re.DOTALL)


def _split_aggregator_paragraphs(text: str) -> list[str]:
    m = _AGG_BANNER_END_RE.search(text)
    body = text[m.end() :] if m else text
    parts = re.split(r"\n\s*\n", body)
    return [p.strip() for p in parts if p.strip()]


def _parse_aggregator_paragraph(paragraph: str) -> tuple[str | None, str]:
    """Return ``(speaker_name_or_None, body_text)`` for one aggregator paragraph."""
    if paragraph.startswith("Operator "):
        return "Operator", paragraph[len("Operator ") :].strip()
    m = _AGG_LEAD_RE.match(paragraph)
    if not m:
        return None, paragraph
    letter, rest = m.group(1), m.group(2)
    tokens = rest.split()
    name_tokens: list[str] = []
    for i, tok in enumerate(tokens):
        bare = re.sub(r"[^\w'.\-]", "", tok)
        if not bare:
            break
        low = bare.lower().rstrip(".,!?;:\"'")
        if i == 0:
            if not bare[0].isupper() or bare[0].upper() != letter:
                break
            name_tokens.append(tok)
            continue
        if len(name_tokens) >= 5:
            break
        if low in _AGG_STOP_AFTER_NAME:
            break
        if bare[0].isupper() or low in _AGG_NAME_PARTICLES:
            name_tokens.append(tok)
            continue
        break
    if not name_tokens:
        return None, paragraph
    name_prefix = " ".join(name_tokens)
    remainder = rest[len(name_prefix) :].strip() if rest.startswith(name_prefix) else rest
    return name_prefix, remainder


def _parse_aggregator_blob(text: str) -> list[Turn]:
    turns: list[Turn] = []
    for i, paragraph in enumerate(_split_aggregator_paragraphs(text)):
        name, body = _parse_aggregator_paragraph(paragraph)
        turns.append(Turn(seq=i, speaker=_normalize_name(name) if name else None, text=body))
    return turns


# --- Whisper-timestamp blob re-parsing (rare: "Speaker N (HH:MM:SS): ...") --

_WHISPER_SPEAKER_RE = re.compile(r"Speaker\s+(\d+)\s*\(\d{2}:\d{2}:\d{2}\):\s*")


def _parse_whisper_blob(text: str) -> list[Turn]:
    matches = list(_WHISPER_SPEAKER_RE.finditer(text))
    if not matches:
        return []
    turns: list[Turn] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end() : end].strip()
        if body:
            turns.append(Turn(seq=i, speaker=f"Speaker {m.group(1)}", text=body))
    return turns


def load_call_turns(conn: sqlite3.Connection, transcript_id: int) -> tuple[list[Turn], str]:
    """Return ``(turns, parse_coverage)`` for one transcript.

    Branches on how many ``transcript_segments`` rows exist:
      * >=2  — trust the stored rows verbatim (real per-turn ingest).
      * 1    — the row is a single blob (the aggregator-fetch common case);
               re-split it at read time via the aggregator paragraph parser,
               falling back to the Whisper-timestamp parser, then to
               ``compute.transcript_ingest.segment_by_speaker`` (the SAME
               function the PDF ingest path already uses — reused, not
               reinvented) in case some other blob shape sneaks in.
      * 0    — no segments at all; returns ``([], "unattributed")``.
    """
    rows = conn.execute(
        "SELECT seq, speaker, text FROM transcript_segments WHERE transcript_id = ? ORDER BY seq",
        (transcript_id,),
    ).fetchall()
    if len(rows) >= 2:
        turns = [
            Turn(
                seq=int(r[0]),
                speaker=_normalize_name(str(r[1])) if r[1] else None,
                text=str(r[2]),
            )
            for r in rows
        ]
        return turns, "segmented"
    if not rows:
        return [], "unattributed"

    blob = str(rows[0][2])
    if "=== Q&A SEGMENT ===" in blob:
        turns = _parse_aggregator_blob(blob)
        if turns:
            return turns, "reparsed_aggregator"
    if _WHISPER_SPEAKER_RE.search(blob):
        whisper_turns = _parse_whisper_blob(blob)
        if whisper_turns:
            return whisper_turns, "reparsed_whisper"

    # Last resort: reuse the ingest-time heuristic rather than reinventing a
    # third "Name:"/newline-name parser here.
    from compute.transcript_ingest import segment_by_speaker

    raw_turns = segment_by_speaker(blob)
    if len(raw_turns) >= 2:
        return [
            Turn(
                seq=i,
                speaker=_normalize_name(t.speaker) if t.speaker else None,
                text=t.text,
            )
            for i, t in enumerate(raw_turns)
        ], "reparsed_fallback"

    return [Turn(seq=0, speaker=None, text=blob)], "unattributed"


# ---------------------------------------------------------------------------
# Boilerplate stripping (Hassan et al.: spoken-language filtering is
# "always essential" before any scoring). Mostly matters for the ~3% of
# transcripts with real prepared remarks — aggregator pulls never include
# the safe-harbor opening at all.
# ---------------------------------------------------------------------------

_BOILERPLATE_CUE_RE = re.compile(
    r"(forward[\s-]looking statements?|safe harbor|actual results (?:could|may) differ materially|"
    r"risks and uncertainties|non-gaap financial measures?|reconciliations? (?:of|to) "
    r"(?:non-)?gaap)",
    re.IGNORECASE,
)
# A turn is dropped as boilerplate only when it densely repeats these cues
# AND is short — a long substantive answer that happens to mention "risks
# and uncertainties" once in passing must never be stripped.
_BOILERPLATE_MIN_CUES = 2
_BOILERPLATE_MAX_CHARS = 2000


def strip_boilerplate(turns: list[Turn]) -> list[Turn]:
    kept: list[Turn] = []
    for t in turns:
        cues = {m.group(0).lower() for m in _BOILERPLATE_CUE_RE.finditer(t.text)}
        if len(cues) >= _BOILERPLATE_MIN_CUES and len(t.text) < _BOILERPLATE_MAX_CHARS:
            continue
        kept.append(t)
    return kept


# ---------------------------------------------------------------------------
# Document-artifact stripping — a DIFFERENT failure mode from boilerplate,
# found during verification against real NVDA/GOOG/TSM calls: the ~3% of
# transcripts ingested from FactSet CallStreet PDFs (compute.transcript_
# ingest.py's own speaker-turn splitter, trusted verbatim as "segmented" —
# see load_call_turns) sometimes captures PAGE FURNITURE — the page-footer
# copyright line, or the "CORPORATE PARTICIPANTS"/"CONFERENCE CALL
# PARTICIPANTS" roster block — as if it were a real speaker turn. Left in,
# these get paired into fake "questions"/"answers" that are pure PDF
# artifacts, and the LLM judge (correctly) flags them non_answer=True
# because there IS no question or answer — which silently inflates the
# measured non-answer rate with garbage, not a real signal. Two of NVDA's
# highest measured rates (50-60%) were roughly half document artifacts
# before this filter existed.
# ---------------------------------------------------------------------------

_DOC_ARTIFACT_RE = re.compile(
    r"copyright\s*©|factset callstreet|www\.callstreet\.com|1-877-factset", re.IGNORECASE
)
# A roster-listing turn ("Analyst, Morgan Stanley & Co. LLC Analyst, BofA
# Securities, Inc. ...") repeats the "Analyst," title prefix several times
# in a row — real analyst speech never does this.
_ROSTER_LISTING_RE = re.compile(r"Analyst,", re.IGNORECASE)
_ROSTER_LISTING_MIN_COUNT = 2


def strip_document_artifacts(turns: list[Turn]) -> list[Turn]:
    # D2.4: the CORPORATE PARTICIPANTS span (management + analyst roster) is
    # now parsed for name/title BEFORE this function runs (see
    # `build_call_snapshot` / `parse_participants_roster`) — it must still be
    # removed from the turn stream here so it never pollutes Q&A pairing.
    roster_span = _find_participants_roster_span(turns)
    kept: list[Turn] = []
    for i, t in enumerate(turns):
        if roster_span is not None and roster_span[0] <= i < roster_span[1]:
            continue
        if _DOC_ARTIFACT_RE.search(t.text):
            continue
        if len(_ROSTER_LISTING_RE.findall(t.text)) >= _ROSTER_LISTING_MIN_COUNT:
            continue
        kept.append(t)
    return kept


# ---------------------------------------------------------------------------
# Speaker-role classification
# ---------------------------------------------------------------------------

# Introduction cues that name the NEXT ANALYST about to speak. Measured on
# the real book (NU): this is said by whoever is running the queue — the
# "Operator" speaker on most calls, but on NU's it is the IR officer's OWN
# turn ("Guilherme Souto: ... Please open the line for Jorge Kuri from
# Morgan Stanley") — so this scans ALL turns, not just Operator-labeled
# ones. This is an AUTHORITATIVE override on top of the frequency heuristic
# below: without it, a real analyst who asks several follow-ups in one call
# (Jorge Kuri asked 3 in one NU quarter) ranks high enough by raw turn count
# to get misclassified MANAGEMENT — caught by cross-checking real
# ``executive_speaker_change`` output against known NU sell-side analysts
# during verification (see the CLI's build notes).
_ANALYST_INTRO_RE = re.compile(
    r"(?:next|first)\s+question\s+(?:is\s+from|comes\s+from|will\s+come\s+from)|"
    r"open(?:ed)?\s+the\s+line\s+for",
    re.IGNORECASE,
)
# Optional honorific ("Mr. Mario Pierry from Bank of America") before the
# name proper — without stripping it, the name-capture regex below stops
# dead at the bare "Mr" (the trailing period isn't a name-token character),
# so the override silently never fires for every honorific-prefixed intro.
_HONORIFIC_RE = re.compile(r"^(?:Mr|Ms|Mrs|Dr)\.?\s+", re.IGNORECASE)
_INTRO_NAME_RE = re.compile(r"[A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+){0,3}")


def _extract_introduced_analyst_names(turns: list[Turn]) -> set[str]:
    """Names explicitly introduced as the next questioner, across ALL turns."""
    names: set[str] = set()
    for t in turns:
        for cue in _ANALYST_INTRO_RE.finditer(t.text):
            tail = _HONORIFIC_RE.sub("", t.text[cue.end() :].lstrip())
            m = _INTRO_NAME_RE.match(tail)
            if m:
                names.add(_normalize_name(m.group(0)))
    return names


# A NAMED human operator ("Sarah", not the literal string "Operator" — seen
# on some CallStreet-derived NVDA calls) runs the question queue just like
# an "Operator"-labeled speaker, but speaks on almost every turn, so raw
# frequency alone ranks her #1 and misclassifies her MANAGEMENT — and her
# transition line ("next question comes from X, your line is open") then
# gets paired as if it were management's ANSWER to the PRIOR question,
# corrupting the non-answer measure with a transition line that was never
# an answer at all. Detected structurally: a speaker whose turns are
# DOMINATED by transition cues, regardless of name.
_OPERATOR_TRANSITION_RE = re.compile(
    r"your line is open|next question comes from|please go ahead|please proceed|"
    r"we'?ll take (?:our|the) next question|open(?:ed)?\s+the\s+line\s+for",
    re.IGNORECASE,
)
_OPERATOR_LIKE_MIN_SHARE = 0.5


def _is_operator_like(turns_for_name: list[Turn]) -> bool:
    if not turns_for_name:
        return False
    hits = sum(1 for t in turns_for_name if _OPERATOR_TRANSITION_RE.search(t.text))
    return hits / len(turns_for_name) >= _OPERATOR_LIKE_MIN_SHARE


def _is_management_alias(name: str, management_names: set[str]) -> bool:
    """True when ``name`` is a longer variant of an already-identified
    management name (``name`` starts with ``mgmt + " "``). See the
    docstring at the call site in ``classify_roles`` for the real
    mis-parse this guards against."""
    return any(name != mgmt and name.startswith(mgmt + " ") for mgmt in management_names)


def classify_roles(turns: list[Turn]) -> dict[str, RosterEntry]:
    """Classify each unique speaker name as OPERATOR / MANAGEMENT / ANALYST.

    No external roster is required (``exec_comp_packages`` is joined
    separately, see ``resolve_exec_role`` — it augments this, it is not a
    prerequisite for it). Management is identified structurally: the
    handful of names that answer questions REPEATEDLY across the call,
    versus the many analyst names that each appear once or twice — EXCEPT
    (a) a name explicitly introduced as the next questioner
    (``_extract_introduced_analyst_names``), which always wins as ANALYST
    regardless of how many follow-ups they asked, and (b) a speaker whose
    turns are dominated by queue-transition language
    (``_is_operator_like``), which always wins as OPERATOR regardless of
    how often they speak. This is the honest fallback the real book
    requires today (aggregator-sourced calls have no "on the call today are
    CEO X, CFO Y" roster line at all — that line lives in the prepared
    remarks, which those files never fetch)."""
    by_name: dict[str, list[Turn]] = defaultdict(list)
    for t in turns:
        if t.speaker:
            by_name[t.speaker].append(t)
    if not by_name:
        return {}

    introduced_analysts = _extract_introduced_analyst_names(turns)
    operator_like_names = {
        name
        for name, tlist in by_name.items()
        if name.lower() != "operator" and _is_operator_like(tlist)
    }
    non_operator = {
        name: tlist
        for name, tlist in by_name.items()
        if name.lower() != "operator" and name not in operator_like_names
    }
    ranked = sorted(non_operator.items(), key=lambda kv: -len(kv[1]))
    management_names: set[str] = {
        name
        for name, tlist in ranked[:_MAX_MANAGEMENT_CANDIDATES]
        if len(tlist) >= _MIN_MANAGEMENT_TURN_COUNT and name not in introduced_analysts
    }

    roster: dict[str, RosterEntry] = {}
    for name, tlist in by_name.items():
        if name.lower() == "operator" or name in operator_like_names:
            role = SpeakerRole.OPERATOR
        elif name in management_names or _is_management_alias(name, management_names):
            # A low-frequency variant of an already-identified management
            # name (real example: the aggregator name-capture parser
            # occasionally over-captures a trailing word from a management
            # continuation phrase — "Martin de los Santos Regarding..." —
            # producing a one-off "ghost" identity that would otherwise
            # default to ANALYST and corrupt Q&A pairing: a management
            # continuation gets misread as a fake new question, and the
            # real answer text that follows gets misattributed as its
            # "answer," inflating the non-answer measure with a parsing
            # artifact rather than a real evasive answer). Folding by name
            # PREFIX is deliberately conservative — it only fires when the
            # variant literally starts with a confirmed management name
            # followed by a word boundary, never the reverse.
            role = SpeakerRole.MANAGEMENT
        else:
            role = SpeakerRole.ANALYST
        roster[name] = RosterEntry(
            name=name,
            role=role,
            turn_count=len(tlist),
            total_chars=sum(len(t.text) for t in tlist),
        )
    return roster


def resolve_exec_role(conn: sqlite3.Connection, ticker: str, name: str) -> str | None:
    """Join a MANAGEMENT speaker name against ``exec_comp_packages`` (DEF 14A
    NEO names/roles). Returns 'CEO', the recorded ``role`` string, or None
    when unresolved (table empty, no match, or a query error) — None is the
    correct, expected answer today for the entire real book (0 rows as of
    this writing); see the module docstring."""
    try:
        rows = conn.execute(
            "SELECT executive_name, role, is_ceo FROM exec_comp_packages WHERE UPPER(ticker) = ?",
            (ticker.upper(),),
        ).fetchall()
    except sqlite3.Error:
        return None
    norm = _normalize_name(name).lower()
    norm_last = norm.split()[-1] if norm.split() else ""
    for exec_name_raw, role_raw, is_ceo_raw in rows:
        exec_name = _normalize_name(str(exec_name_raw)).lower()
        exec_last = exec_name.split()[-1] if exec_name.split() else ""
        if exec_name == norm or (norm_last and exec_last == norm_last and len(norm.split()) >= 2):
            if is_ceo_raw:
                return "CEO"
            return str(role_raw) if role_raw else "executive"
    return None


# ---------------------------------------------------------------------------
# CORPORATE PARTICIPANTS roster parsing (D2.4 — Larcker & Zakolyukina, JAR
# 2012: CFO language was tradeable, CEO language was not; pooling discards
# the finding). This module does NOT implement, and explicitly does not
# claim, their deception-marker language model — the -1..1 tone score this
# repo tracks (`transcript_judgment.judge_call`) is a different, sentiment-
# only construct. What this section adds is role RESOLUTION: turning a named
# speaker into a CEO/CFO label using the transcript's OWN participant
# roster, ahead of the (still book-wide-empty) `exec_comp_packages` DEF 14A
# join `resolve_exec_role` already provides.
#
# `strip_document_artifacts` used to delete this block outright as PDF page
# furniture (see that function's own docstring). It still must not leak into
# Q&A/tone turns — but the block is parsed for name/title FIRST, on the RAW
# turns, before any stripping runs (see `build_call_snapshot`).
#
# Measured against a copy of prod on 2026-07-25 (165 transcripts with >=2
# stored `transcript_segments` rows — the only ingestion path that could
# ever carry this heading): exactly 7 carry a CORPORATE PARTICIPANTS
# heading (NVDA x3, TSM x4), in TWO visibly different raw shapes, plus a
# THIRD shape with no heading at all:
#   1. "packed" (Refinitiv StreetEvents, e.g. TSM): one participant per line,
#      already inside a single stored row: "<Name> <Company> - <Title>".
#   2. "fragmented" (FactSet CallStreet, e.g. NVDA; also S&P Global Market
#      Intelligence, e.g. CRM under its "Call Participants EXECUTIVES"
#      heading): the ingest-time speaker/text splitter mis-segments the
#      block, gluing the heading onto the first person's name ("CORPORATE
#      PARTICIPANTS Toshiya Hari") and splitting name/title lines across
#      several pseudo-turns, sometimes with PDF dot-leader garbage ("....")
#      or a title continuation glued onto the NEXT person's name
#      interleaved. Degrades to a partial roster (some names/titles lost to
#      the glue, never a wrong CEO/CFO claim) rather than a full parse.
#   3. "inline" (no heading at all — the common case for a segmented,
#      non-aggregator source with real prepared remarks, e.g. a Bio-Techne-
#      style call): the IR host names management inline in prose ("On the
#      call with me this morning are Kim Kelderman, President and Chief
#      Executive Officer, and Jim Hippel, Chief Financial Officer").
# A call with none of the three degrades to an EMPTY roster (`{}`), never a
# guessed role — the 97%-aggregator majority of the book always takes this
# path, by design (see the module docstring on `parse_coverage`).
# ---------------------------------------------------------------------------

_ROSTER_HEADING_RE = re.compile(
    r"^(CORPORATE PARTICIPANTS|CALL PARTICIPANTS EXECUTIVES)\s*(.*)$", re.IGNORECASE
)
# The analyst-roster sub-heading — still INSIDE the overall span (it must be
# scanned past, not stopped at), just not part of the management name/title
# extraction (see `_roster_span_lines`). Matched with `.search`, not
# `.match`: the S&P Global source sometimes glues the "ANALYSTS" marker onto
# the end of the prior person's title line rather than starting its own
# turn (e.g. "Customer Success Officer ANALYSTS").
_ANALYST_ROSTER_HEADING_RE = re.compile(
    r"(OTHER PARTICIPANTS|CONFERENCE CALL PARTICIPANTS|\bANALYSTS\b)", re.IGNORECASE
)
# The real end of the roster block(s) — prepared remarks or Q&A begins here.
_ROSTER_HARD_STOP_RE = re.compile(
    r"^(PRESENTATION|QUESTIONS AND ANSWERS|Operator)\b", re.IGNORECASE
)
# PDF table-of-contents leader lines ("...................") left over from
# the page-furniture the FactSet "fragmented" shape sometimes interleaves.
_DOT_LEADER_RE = re.compile(r"^[.\s]{5,}$")
_TITLE_KEYWORDS_RE = re.compile(
    r"\b(chief\s+\w+\s+officer|president|vice\s+president|chairman|director|founder|"
    r"co-founder|investor\s+relations|treasurer|controller|managing\s+director|"
    r"head\s+of|general\s+counsel|secretary)\b",
    re.IGNORECASE,
)
# Shape 3 (inline prose): "<Name>, <title phrase>[, <title phrase>]*" —
# bounded to a handful of title-starting keywords so this never fires on
# ordinary prose that happens to contain a comma. Token separators use
# `[ \t]` (never bare `\s`, which also matches newline) so a name can never
# accidentally swallow the start of the NEXT line — a real mis-match found
# against the real book (CRM) when this used `\s+`.
_INLINE_PARTICIPANT_RE = re.compile(
    r"([A-Z][A-Za-z.'\-]+(?:[ \t]+[A-Z][A-Za-z.'\-]+){0,3}),[ \t]+"
    r"((?:President|Chief\s+\w+\s+Officer|Chairman|Founder|Co-Founder)[^,.;\n]*"
    r"(?:,[ \t]*(?:President|Chief\s+\w+\s+Officer|Chairman|Founder|Co-Founder)[^,.;\n]*)*)"
)
# Shape 3 is only attempted over a call's leading turns (prepared-remarks/
# IR-intro territory) — bounding it here keeps false-positive risk low on
# the rare Q&A exchange that happens to mention a title in passing.
_INLINE_INTRO_MAX_TURNS = 5
# Safety bound on how many turns past a CORPORATE PARTICIPANTS heading are
# scanned before giving up — guards against a heading that, for whatever
# reason, never hits a recognized stop marker.
_ROSTER_SPAN_MAX_TURNS = 20


@dataclass(frozen=True)
class ParsedParticipant:
    name: str
    title: str
    # 'CEO' | 'CFO' | 'other_exec' | 'unresolved' (a title matched but named
    # no recognizable role keyword at all — kept rather than dropped, since
    # the name IS a confirmed management participant even when the title
    # text itself doesn't parse to a role).
    exec_role: str


_CEO_ABBR_RE = re.compile(r"\bCEO\b")
_CFO_ABBR_RE = re.compile(r"\bCFO\b")


def classify_exec_title(title: str) -> str:
    """Map a free-text title to 'CEO' / 'CFO' / 'other_exec' / 'unresolved'.
    Checked in this order because a title can legitimately contain BOTH
    phrases ("Chairman and Chief Executive Officer") — CEO/CFO win over the
    generic executive-keyword bucket, never the reverse. Accepts both the
    spelled-out phrase and the bare abbreviation ("Chairman & CEO" — seen on
    the real book's S&P Global Market Intelligence source)."""
    low = title.lower()
    if "chief executive officer" in low or _CEO_ABBR_RE.search(title):
        return "CEO"
    if "chief financial officer" in low or _CFO_ABBR_RE.search(title):
        return "CFO"
    if _TITLE_KEYWORDS_RE.search(title):
        return "other_exec"
    return "unresolved"


def _find_participants_roster_span(turns: list[Turn]) -> tuple[int, int] | None:
    """(start, stop) turn-index span covering the CORPORATE PARTICIPANTS
    block AND any immediately-following analyst roster block, or None when
    no heading is found. `stop` is exclusive."""
    start = None
    for i, t in enumerate(turns):
        if _ROSTER_HEADING_RE.match((t.speaker or "").strip()):
            start = i
            break
    if start is None:
        return None
    stop = min(start + 1 + _ROSTER_SPAN_MAX_TURNS, len(turns))
    for j in range(start + 1, stop):
        spk = (turns[j].speaker or "").strip()
        if _ANALYST_ROSTER_HEADING_RE.search(spk):
            continue  # analyst-roster sub-heading — still inside the span
        if _ROSTER_HARD_STOP_RE.match(spk):
            return start, j
    return start, stop


def _roster_span_lines(turns: list[Turn], span: tuple[int, int]) -> list[str]:
    """Flatten the management sub-block of a roster span into raw text
    lines, stopping at the first analyst-roster sub-heading (OTHER
    PARTICIPANTS / CONFERENCE CALL PARTICIPANTS) — those names are analysts,
    not management, and are not needed for CEO/CFO resolution."""
    start, stop = span
    lines: list[str] = []
    heading_m = _ROSTER_HEADING_RE.match((turns[start].speaker or "").strip())
    lead_name = heading_m.group(2).strip() if heading_m else ""
    if lead_name:
        lines.append(lead_name)
    lines.extend(line.strip() for line in (turns[start].text or "").split("\n") if line.strip())
    for j in range(start + 1, stop):
        spk = (turns[j].speaker or "").strip()
        if _ANALYST_ROSTER_HEADING_RE.search(spk):
            break
        if spk and not _ROSTER_HEADING_RE.match(spk):
            lines.append(spk)
        lines.extend(line.strip() for line in (turns[j].text or "").split("\n") if line.strip())
    return [line for line in lines if not _DOT_LEADER_RE.match(line)]


def _parse_packed_participant_lines(lines: list[str]) -> dict[str, ParsedParticipant]:
    """Shape 1: one participant per line, "<Name> <Company...> - <Title>".
    Requires EVERY non-trivial line to match — a partial match means this
    isn't actually the packed shape, and falling back to the alternating
    parser is safer than returning a half-parsed roster."""
    roster: dict[str, ParsedParticipant] = {}
    for line in lines:
        if " - " not in line:
            return {}
        head, title = line.rsplit(" - ", 1)
        name_tokens = head.split()[:2]
        if not name_tokens or not title.strip():
            return {}
        name = " ".join(name_tokens)
        roster[name] = ParsedParticipant(
            name=name, title=title.strip(), exec_role=classify_exec_title(title)
        )
    return roster


def _looks_like_name_line(line: str) -> bool:
    if _TITLE_KEYWORDS_RE.search(line):
        return False
    tokens = line.split()
    if not (1 <= len(tokens) <= 4):
        return False
    return all(re.match(r"^[A-Z][\w.'\-]*,?$", t) for t in tokens)


def _parse_alternating_participant_lines(lines: list[str]) -> dict[str, ParsedParticipant]:
    """Shape 2: name line, then title line(s), repeating — tolerant of the
    FactSet ingest-time mis-segmentation gluing the heading onto the first
    name and splitting subsequent name/title pairs across pseudo-turns (see
    this section's module-level note). A title line is anything containing
    a recognizable title keyword; a name line is a short run of capitalized
    tokens with none. Stray lines that match neither are skipped rather than
    guessed onto either role."""
    roster: dict[str, ParsedParticipant] = {}
    pending_name: str | None = None
    for line in lines:
        if pending_name is not None and _TITLE_KEYWORDS_RE.search(line):
            roster[pending_name] = ParsedParticipant(
                name=pending_name, title=line, exec_role=classify_exec_title(line)
            )
            pending_name = None
        elif _looks_like_name_line(line):
            pending_name = line
    return roster


def _parse_inline_participant_intro(turns: list[Turn]) -> dict[str, ParsedParticipant]:
    """Shape 3: no CORPORATE PARTICIPANTS heading at all — management is
    named inline in prepared-remarks prose ("...are Kim Kelderman, President
    and Chief Executive Officer, and Jim Hippel, Chief Financial Officer of
    Bio-Techne."). Only scanned over the call's leading turns to bound
    false-positive risk on an unrelated Q&A mention of a title."""
    roster: dict[str, ParsedParticipant] = {}
    for t in turns[:_INLINE_INTRO_MAX_TURNS]:
        for m in _INLINE_PARTICIPANT_RE.finditer(t.text or ""):
            name, title = m.group(1).strip(), m.group(2).strip()
            role = classify_exec_title(title)
            if role != "unresolved":
                roster[name] = ParsedParticipant(name=name, title=title, exec_role=role)
    return roster


def parse_participants_roster(turns: list[Turn]) -> dict[str, ParsedParticipant]:
    """Parse the transcript's own management roster into name -> role.

    Tried in order: the heading-based "packed" shape, then the
    heading-based "alternating" shape, then (only when no heading was found
    at all) the headingless "inline intro" shape. Returns ``{}`` when none
    match — an honest degrade, never a guessed role. Must be called on the
    RAW turns (before `strip_document_artifacts`), since that function
    removes this exact block."""
    span = _find_participants_roster_span(turns)
    if span is None:
        return _parse_inline_participant_intro(turns)
    lines = _roster_span_lines(turns, span)
    packed = _parse_packed_participant_lines(lines)
    if packed:
        return packed
    return _parse_alternating_participant_lines(lines)


def resolve_role_from_participants_roster(
    name: str, roster: dict[str, ParsedParticipant]
) -> ParsedParticipant | None:
    """Match a Q&A speaker name against a parsed participants roster — exact
    match first, then last-name match (mirrors `resolve_exec_role`'s DEF 14A
    matching exactly, so e.g. "Colette Kress" in Q&A resolves against
    "Colette M. Kress" in the roster block)."""
    norm = _normalize_name(name).lower()
    norm_last = norm.split()[-1] if norm.split() else ""
    for candidate_name, participant in roster.items():
        cand = _normalize_name(candidate_name).lower()
        cand_last = cand.split()[-1] if cand.split() else ""
        if cand == norm or (norm_last and cand_last == norm_last and len(norm.split()) >= 2):
            return participant
    return None


# ---------------------------------------------------------------------------
# Q&A pairing (Matsumoto/Pronk/Roelofsen: Q&A is the informative half;
# Gow/Larcker/Zakolyukina operate at exactly this per-question-turn grain)
# ---------------------------------------------------------------------------


def pair_qa_exchanges(turns: list[Turn], roster: dict[str, RosterEntry]) -> list[QAExchange]:
    """Deterministically pair each ANALYST turn with the MANAGEMENT turn(s)
    that immediately follow it, up to the next ANALYST turn or an OPERATOR
    transition. One or more management speakers answering the same question
    in sequence (common — see the MELI/NU verification samples) are merged
    into a single exchange's answer. UNKNOWN-role and unattributed turns are
    dropped rather than guessed into either side."""
    exchanges: list[QAExchange] = []
    idx = 0
    analyst_name: str | None = None
    question_parts: list[str] = []
    answer_speakers: list[str] = []
    answer_parts: list[str] = []

    def role_of(speaker: str | None) -> SpeakerRole:
        if speaker is None:
            return SpeakerRole.UNKNOWN
        entry = roster.get(speaker)
        return entry.role if entry is not None else SpeakerRole.UNKNOWN

    def flush() -> None:
        nonlocal idx, analyst_name, question_parts, answer_speakers, answer_parts
        if analyst_name is not None and question_parts and answer_parts:
            exchanges.append(
                QAExchange(
                    index=idx,
                    analyst_name=analyst_name,
                    question_text=" ".join(question_parts).strip(),
                    answer_speakers=tuple(dict.fromkeys(answer_speakers)),
                    answer_text=" ".join(answer_parts).strip(),
                )
            )
            idx += 1
        analyst_name = None
        question_parts = []
        answer_speakers = []
        answer_parts = []

    for t in turns:
        role = role_of(t.speaker)
        if role is SpeakerRole.OPERATOR:
            if analyst_name is not None and answer_parts:
                flush()
            continue
        if role is SpeakerRole.ANALYST:
            if analyst_name is not None and answer_parts:
                flush()
            if analyst_name is None:
                analyst_name = t.speaker
            question_parts.append(t.text)
            continue
        if role is SpeakerRole.MANAGEMENT:
            if analyst_name is None:
                continue  # management speaking with no open question — ignore
            answer_speakers.append(t.speaker or "")
            answer_parts.append(t.text)
            continue
        # UNKNOWN — never guessed onto either side.
        continue
    flush()
    return exchanges


# ---------------------------------------------------------------------------
# KPI/topic presence (novel, unvalidated measure — see module docstring +
# the CLI's --help and the disclosure_events.verdict on every row this
# produces: the literature has NO published validation for topic
# disappearance specifically; nearest analog is guidance withdrawal).
# ---------------------------------------------------------------------------

_PAREN_RE = re.compile(r"\([^)]*\)")

# Single words too generic to mean anything as a standalone "topic" — every
# one of these appears in ordinary financial speech constantly regardless of
# whether the SPECIFIC metric the catalog row names got discussed. Measured
# on the real book (2026-07-25): without this list, `kpi_definitions` rows
# like "... Net" or "... Gross" reduce to the bare word "net"/"gross" as a
# "phrase", which is present in nearly every call by chance and manufactures
# hundreds of false candidates (143 for MELI alone in the first pass).
_KPI_GENERIC_STOPWORDS = frozenset(
    {
        "net",
        "gross",
        "current",
        "cash",
        "total",
        "other",
        "balance",
        "assets",
        "liabilities",
        "components",
        "schedule",
        "summary",
        "revenue",
        "income",
        "expense",
        "expenses",
        "margin",
        "growth",
        "value",
        "long-term",
        "short-term",
    }
)


def _split_kpi_phrases(raw_name: str) -> list[str]:
    """``kpi_definitions.name`` rows are a mixed bag — some are a single
    precise metric name, some are semicolon-joined lists of several
    concepts (e.g. ``"GMV; items sold; TPV; fintech MAU; credit NPL; take
    rate"``). Split on ``;``/``,``, strip parenthetical qualifiers, and keep
    only clauses of plausible topic-phrase length — long formula-like names
    (``"Revenue YoY Growth Deceleration (YoY of YoY growth rate)"``) almost
    never appear verbatim in spoken language and are filtered out here
    rather than fed to a presence check that would just always miss. A
    single-word clause that is one of ``_KPI_GENERIC_STOPWORDS`` is dropped
    outright — see that set's docstring comment for why."""
    cleaned = _PAREN_RE.sub("", raw_name).replace("_", " ")
    clauses = [c.strip() for c in re.split(r"[;,]", cleaned) if c.strip()]
    phrases: list[str] = []
    for c in clauses:
        c = re.sub(r"\s+", " ", c).strip(" -")
        if not (2 <= len(c) <= 40 and re.search(r"[A-Za-z]{3,}", c)):
            continue
        if " " not in c and c.lower() in _KPI_GENERIC_STOPWORDS:
            continue
        phrases.append(c)
    return phrases


def extract_kpi_terms(conn: sqlite3.Connection, ticker: str) -> list[tuple[str, list[str]]]:
    """Return ``[(raw_kpi_definitions_name, [candidate short phrases]), ...]``
    for a ticker's ANALYST-curated KPI catalog, or ``[]`` when the ticker has
    none / the table is unavailable.

    Filtered to ``definition_origin = 'analyst'``: ``kpi_definitions`` also
    holds ``definition_origin = 'capture'`` rows from the "capture every
    reported number" ``xbrl_capture_all`` firehose (~9,600 of ~14,000 rows
    book-wide) — auto-extracted XBRL footnote/schedule captions like
    ``"BALANCE SHEET COMPONENTS - Schedule of Accounts Receivable, Net —
    Accounts receivable, net"``. Those are accounting plumbing, not a
    business KPI a call would ever "stop discussing" in any meaningful
    sense, and including them was the single largest noise source measured
    against the real book (see the CLI's verification notes)."""
    try:
        rows = conn.execute(
            "SELECT name FROM kpi_definitions WHERE UPPER(ticker) = ? "
            "AND definition_origin = 'analyst' ORDER BY name",
            (ticker.upper(),),
        ).fetchall()
    except sqlite3.Error:
        return []
    out: list[tuple[str, list[str]]] = []
    for (raw_name,) in rows:
        phrases = _split_kpi_phrases(str(raw_name))
        if phrases:
            out.append((str(raw_name), phrases))
    return out


def find_kpi_mention(text: str, phrases: list[str]) -> str | None:
    """Return a verbatim ~160-char excerpt around the first phrase match in
    ``text``, or None if none of ``phrases`` appear. Word-boundary-safe
    substring match, case-insensitive."""
    for phrase in phrases:
        pattern = re.compile(r"(?<![A-Za-z])" + re.escape(phrase) + r"(?![A-Za-z])", re.IGNORECASE)
        m = pattern.search(text)
        if m:
            start = max(0, m.start() - 80)
            end = min(len(text), m.end() + 80)
            return text[start:end].strip()
    return None


def call_full_text(turns: list[Turn]) -> str:
    return "\n".join(t.text for t in turns if t.text)


# ---------------------------------------------------------------------------
# Earnings-surprise join (fundamentals control for D2.3's ABTONE residual
# fit and the legacy tone_delta_heuristic_flag proxy)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SurpriseInfo:
    release_date: str
    eps_surprise_pct: float | None
    # Secondary fundamentals control (D2.3 — Huang/Teoh/Zhang's ABTONE
    # regresses tone on more than just EPS surprise). Same table, same join,
    # so adding it costs no new query — see fit_tone_residual_model.
    revenue_surprise_pct: float | None = None


def nearest_earnings_surprise(
    conn: sqlite3.Connection, ticker: str, period_end: datetime
) -> SurpriseInfo | None:
    """Nearest ``earnings_surprises`` release on/after ``period_end`` within a
    100-day window (calls typically land 2-8 weeks after quarter-end).
    Returns None when no row is found or the table is unavailable —
    callers must treat that as "no control variable available" and skip
    residualization for that call, not assume a zero surprise."""
    try:
        rows = conn.execute(
            "SELECT release_date, eps_surprise_pct, revenue_surprise_pct FROM earnings_surprises "
            "WHERE UPPER(ticker) = ? AND release_date >= ? AND release_date <= ? "
            "ORDER BY release_date ASC LIMIT 1",
            (
                ticker.upper(),
                period_end.date().isoformat(),
                (period_end + timedelta(days=100)).date().isoformat(),
            ),
        ).fetchall()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    release_date, eps_pct, rev_pct = rows[0]
    return SurpriseInfo(
        release_date=str(release_date),
        eps_surprise_pct=float(eps_pct) if eps_pct is not None else None,
        revenue_surprise_pct=float(rev_pct) if rev_pct is not None else None,
    )


# ---------------------------------------------------------------------------
# ABTONE — the real residual fit (D2.3, Huang/Teoh/Zhang, TAR 2014).
#
# "Abnormal" tone is the RESIDUAL after controlling for fundamentals — raw
# tone mostly re-derives the earnings surprise, so the surprise-adjusted
# residual is the actual signal, not the raw score. Fitted deterministically
# (ordinary least squares via the normal equations, pure Python — no new
# dependency, no LLM call) over tone scores `transcript_judgment.judge_call`
# has ALREADY produced and cached: this step is Layer-3 arithmetic on
# existing numbers, zero new tokens.
#
# Pooled ACROSS the whole tracked book rather than fit separately per
# same-period cross-section: with ~15-90 (ticker, period, speaker)
# observations total measured against a copy of prod on 2026-07-25, a
# per-quarter cross-section (a handful of tickers reporting in any given
# quarter) is far too small to fit 2 coefficients. Pooling trades "true"
# cross-sectional detrending (Huang/Teoh/Zhang's own design) for a
# book-specific normalization step — exactly the caveat
# docs/design/disclosure_gap_scoping.md's Gap 2 section calls out: "fine as
# a within-book deviation flag... never a claim the coefficient is
# significant." Measured this session (eps_surprise_pct only, n=48):
# R^2 = 0.058 — consistent with "explains almost none of the variance,"
# which is itself informative (tone is NOT simply re-deriving the earnings
# surprise in this book, so a residual delta is not automatically near-zero
# noise). Revenue surprise was tried as a second predictor (n=32, R^2=0.031,
# lower N for no R^2 gain) and is not used by default — see
# `fit_tone_residual_model`'s `use_revenue_surprise` parameter for a caller
# that wants to try it once more history accumulates.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToneObservation:
    """One (ticker, fiscal_period, speaker) tone reading, ready to fit
    against fundamentals controls. One row per name-key in a judged call's
    ``CallJudgment.tone`` dict — the SAME literal name string
    ``transcript_judgment`` used, so callers matching call-over-call deltas
    by name (see ``execution/detect_transcript_disclosure_events.py``) stay
    consistent with how every other by-name measure in this module already
    matches (exact string; no cross-call identity resolution attempted)."""

    ticker: str
    fiscal_period_type: str
    fiscal_year: int
    period_end: datetime
    speaker: str
    tone_score: float
    eps_surprise_pct: float | None
    revenue_surprise_pct: float | None


@dataclass(frozen=True)
class ToneResidualModel:
    """A fitted ``tone_score ~ intercept + beta_eps * eps_surprise_pct
    [+ beta_revenue * revenue_surprise_pct]`` OLS model. ``r_squared`` is
    reported for transparency, not as a significance claim — see this
    section's module-level note on statistical power."""

    intercept: float
    beta_eps: float
    beta_revenue: float | None
    n_obs: int
    r_squared: float
    uses_revenue_surprise: bool


def _solve_linear_system(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting. Returns None on a
    singular system (e.g. a predictor with zero variance) rather than
    raising — callers treat that as "cannot fit," not a crash."""
    n = len(b)
    aug = [[*row, b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot_row = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot_row][col]) < 1e-12:
            return None
        aug[col], aug[pivot_row] = aug[pivot_row], aug[col]
        piv = aug[col][col]
        aug[col] = [v / piv for v in aug[col]]
        for r in range(n):
            if r != col:
                factor = aug[r][col]
                aug[r] = [aug[r][j] - factor * aug[col][j] for j in range(n + 1)]
    return [aug[i][n] for i in range(n)]


def fit_tone_residual_model(
    observations: list[ToneObservation],
    *,
    use_revenue_surprise: bool = False,
) -> ToneResidualModel | None:
    """Deterministic pooled OLS fit of tone score on fundamentals controls.
    Returns None when there are fewer than ``ABTONE_MIN_OBSERVATIONS`` usable
    rows or the normal equations are singular (e.g. every observation has an
    identical eps_surprise_pct) — callers must treat that as "cannot
    residualize this run," never silently fall back to raw tone."""
    predictors: list[str] = ["eps_surprise_pct"]
    if use_revenue_surprise:
        predictors.append("revenue_surprise_pct")
    usable = [
        o
        for o in observations
        if o.eps_surprise_pct is not None
        and (not use_revenue_surprise or o.revenue_surprise_pct is not None)
    ]
    n = len(usable)
    if n < ABTONE_MIN_OBSERVATIONS:
        return None

    k = len(predictors) + 1  # + intercept
    x_rows: list[list[float]] = []
    y_vals: list[float] = []
    for o in usable:
        row = [1.0, float(o.eps_surprise_pct)]  # type: ignore[arg-type]
        if use_revenue_surprise:
            row.append(float(o.revenue_surprise_pct))  # type: ignore[arg-type]
        x_rows.append(row)
        y_vals.append(o.tone_score)

    xtx = [[sum(x_rows[i][a] * x_rows[i][b] for i in range(n)) for b in range(k)] for a in range(k)]
    xty = [sum(x_rows[i][a] * y_vals[i] for i in range(n)) for a in range(k)]
    beta = _solve_linear_system(xtx, xty)
    if beta is None:
        return None

    y_mean = sum(y_vals) / n
    ss_tot = sum((y - y_mean) ** 2 for y in y_vals)
    preds = [sum(beta[a] * x_rows[i][a] for a in range(k)) for i in range(n)]
    ss_res = sum((y_vals[i] - preds[i]) ** 2 for i in range(n))
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    return ToneResidualModel(
        intercept=beta[0],
        beta_eps=beta[1],
        beta_revenue=beta[2] if use_revenue_surprise else None,
        n_obs=n,
        r_squared=r_squared,
        uses_revenue_surprise=use_revenue_surprise,
    )


def tone_residual(model: ToneResidualModel, obs: ToneObservation) -> float | None:
    """``obs.tone_score`` minus the model's fitted prediction. Returns None
    when ``obs`` lacks a control the model needs (honest degrade — never
    silently substitutes a zero surprise, which would fabricate a residual
    equal to the raw tone score)."""
    if obs.eps_surprise_pct is None:
        return None
    predicted = model.intercept + model.beta_eps * obs.eps_surprise_pct
    if model.uses_revenue_surprise:
        if obs.revenue_surprise_pct is None:
            return None
        predicted += (model.beta_revenue or 0.0) * obs.revenue_surprise_pct
    return obs.tone_score - predicted


def tone_delta_heuristic_flag(tone_delta: float, eps_surprise_pct: float | None) -> bool:
    """A TRANSPARENT TWO-RULE HEURISTIC — NOT Huang/Teoh/Zhang's ABTONE
    residual, NOT a fitted OLS regression. Renamed from
    ``tone_shift_is_abnormal`` (D2.3): that name implied a resolved
    abnormal-tone judgment, which this function never was — its own
    docstring said so, but the name didn't. The REAL fitted residual is
    ``fit_tone_residual_model`` / ``tone_residual`` below, which is what
    ``execution/detect_transcript_disclosure_events.py`` now uses to emit
    ``abnormal_tone_shift`` events. This heuristic is kept, unused in
    production, purely for side-by-side comparison (per
    ``docs/design/disclosure_gap_scoping.md``'s explicit "alongside, not
    replaced by" guidance) — do not resurrect it as the primary signal.

    Flags a tone delta as abnormal when either (a) it moved OPPOSITE the
    direction the same-quarter earnings surprise would suggest, or (b) it
    moved by a material amount despite an in-line/small surprise (large
    move, small fundamentals change). Both conditions are transparent and
    re-checkable by eye from the two numbers in the event row; neither
    claims the statistical guarantees of a fitted residual."""
    if abs(tone_delta) < TONE_MATERIALITY:
        return False
    if eps_surprise_pct is None:
        # No control variable — still worth surfacing as an unexplained
        # (not "abnormal-vs-surprise") tone move; callers label this in
        # interpretation_md rather than silently dropping it.
        return True
    if abs(eps_surprise_pct) <= TONE_SMALL_SURPRISE_PCT:
        return True  # big tone move, in-line results
    same_sign = (tone_delta > 0) == (eps_surprise_pct > 0)
    return not same_sign  # opposite-signed move


# ---------------------------------------------------------------------------
# Roster deltas (deterministic, no LLM; UNVALIDATED per §1.7 — "analyst-
# roster change" and "executive-speaker change" are both explicitly listed
# as unvalidated-but-buildable)
# ---------------------------------------------------------------------------


def roster_names_by_role(roster: dict[str, RosterEntry], role: SpeakerRole) -> set[str]:
    return {name for name, entry in roster.items() if entry.role is role}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


# ---------------------------------------------------------------------------
# Snapshot builder
# ---------------------------------------------------------------------------


def build_call_snapshot(conn: sqlite3.Connection, transcript_id: int) -> CallSnapshot | None:
    row = conn.execute(
        "SELECT ticker, fiscal_period_type, period_end FROM transcripts WHERE id = ?",
        (transcript_id,),
    ).fetchone()
    if row is None:
        return None
    ticker_raw, fpt_raw, period_end_raw = row
    if period_end_raw is None:
        return None
    period_end = (
        period_end_raw
        if isinstance(period_end_raw, datetime)
        else datetime.fromisoformat(str(period_end_raw))
    )
    raw_turns, coverage = load_call_turns(conn, transcript_id)
    # D2.4: parsed on the RAW turns — strip_document_artifacts removes this
    # exact block, so it must be captured before either strip call runs.
    participants_roster = parse_participants_roster(raw_turns)
    turns = strip_document_artifacts(strip_boilerplate(raw_turns))
    roster = classify_roles(turns)
    ticker = str(ticker_raw).upper()
    for name, entry in list(roster.items()):
        if entry.role is not SpeakerRole.MANAGEMENT:
            continue
        # Transcript-scoped roster first (authoritative for THIS call), DEF
        # 14A join second (see module docstring — exec_comp_packages is
        # empty book-wide as of this writing, so this fallback almost never
        # fires today, but stays for when it's backfilled).
        participant = resolve_role_from_participants_roster(name, participants_roster)
        if participant is not None:
            roster[name] = replace(
                entry, exec_role=participant.exec_role, exec_role_source="participants_block"
            )
            continue
        exec_role = resolve_exec_role(conn, ticker, name)
        if exec_role:
            roster[name] = replace(
                entry, exec_role=exec_role, exec_role_source="exec_comp_packages"
            )
    exchanges = pair_qa_exchanges(turns, roster)
    return CallSnapshot(
        transcript_id=transcript_id,
        ticker=ticker,
        fiscal_period_type=str(fpt_raw),
        fiscal_year=period_end.year,
        period_end=period_end,
        turns=turns,
        roster=roster,
        exchanges=exchanges,
        participants_roster=participants_roster,
        parse_coverage=coverage,
    )


def prior_transcript_ids(
    conn: sqlite3.Connection, ticker: str, before_period_end: datetime, limit: int = 3
) -> list[int]:
    """Up to ``limit`` quarterly transcript ids strictly before
    ``before_period_end``, most-recent-first."""
    rows = conn.execute(
        "SELECT id FROM transcripts WHERE UPPER(ticker) = ? "
        "AND fiscal_period_type IN ('Q1','Q2','Q3','Q4') AND period_end < ? "
        "ORDER BY period_end DESC LIMIT ?",
        (ticker.upper(), before_period_end.isoformat(), limit),
    ).fetchall()
    return [int(r[0]) for r in rows]


# ---------------------------------------------------------------------------
# disclosure_events write contract (migration 0203, ex-0200 — reused, not
# extended; see the migration's vocabulary comment update for the new
# event_type values this module emits).
# ---------------------------------------------------------------------------

# canonical_id is part of disclosure_events' row identity (migration 0203
# made it NOT NULL + a unique-key member, after ~32% of P0's item events
# were found silently overwriting each other without it — same section
# heading legitimately recurring across two different filing sections).
# Transcripts have no equivalent "section" concept the way filings do: every
# P4 measure below runs over the Q&A portion (aggregator-sourced calls are
# Q&A-only to begin with — see the module docstring), so ALL transcript
# events share one stable value rather than a fabricated per-event one.
TRANSCRIPT_QA_CANONICAL_ID = "transcript_qa"


class TranscriptEvent(BaseModel):
    ticker: str
    event_type: str
    fiscal_year: int | None
    fiscal_period: str | None
    prior_fiscal_year: int | None = None
    prior_fiscal_period: str | None = None
    source_ref: str | None = None
    source_doc_id: int | None = None
    canonical_id: str = TRANSCRIPT_QA_CANONICAL_ID
    subject: str
    subject_label: str | None = None
    prior_excerpt: str | None = None
    current_excerpt: str | None = None
    evidence_quote: str = Field(min_length=1)
    materiality: float | None = None
    verdict: str = "unclassified"
    interpretation_md: str | None = None
    confidence: float | None = None
    detector_version: str = DETECTOR_VERSION


def write_transcript_events(conn: sqlite3.Connection, events: list[TranscriptEvent]) -> int:
    """Idempotent upsert into ``disclosure_events`` on its unique key
    (ticker, event_type, fiscal_year, fiscal_period, canonical_id, subject,
    detector_version). NULL-safe explicit lookup rather than ``ON
    CONFLICT`` — SQLite does not treat two NULL fiscal_year/fiscal_period
    values as conflicting in a UNIQUE index (mirrors
    ``filings.metric_lifecycle.write_lifecycle_events`` / ``filings.store``).
    ``canonical_id`` itself is NOT NULL at the schema level (migration 0203),
    so no ``IS ?``/``IS NULL`` handling is needed for that column."""
    if not events:
        return 0
    if not _table_exists(conn, EVENTS_TABLE):
        raise HardStopError(
            f"{EVENTS_TABLE} missing — run `alembic upgrade head` (migration 0203) first"
        )
    now = _now_naive_utc()
    written = 0
    for e in events:
        existing = conn.execute(
            f"""
            SELECT id FROM {EVENTS_TABLE}
            WHERE ticker = ? AND event_type = ? AND fiscal_year IS ? AND fiscal_period IS ?
              AND canonical_id = ? AND subject = ? AND detector_version = ?
            LIMIT 1
            """,
            (
                e.ticker,
                e.event_type,
                e.fiscal_year,
                e.fiscal_period,
                e.canonical_id,
                e.subject,
                e.detector_version,
            ),
        ).fetchone()
        common = (
            e.subject_label,
            e.prior_excerpt,
            e.current_excerpt,
            e.evidence_quote,
            e.materiality,
            e.verdict,
            e.interpretation_md,
            e.confidence,
        )
        if existing is None:
            conn.execute(
                f"""
                INSERT INTO {EVENTS_TABLE}
                    (ticker, event_type, fiscal_year, fiscal_period, prior_fiscal_year,
                     prior_fiscal_period, source_ref, source_doc_id, canonical_id, subject,
                     subject_label, prior_excerpt, current_excerpt, evidence_quote, materiality,
                     verdict, interpretation_md, confidence, detector_version, status, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    e.ticker,
                    e.event_type,
                    e.fiscal_year,
                    e.fiscal_period,
                    e.prior_fiscal_year,
                    e.prior_fiscal_period,
                    e.source_ref,
                    e.source_doc_id,
                    e.canonical_id,
                    e.subject,
                    *common,
                    e.detector_version,
                    "new",
                    now,
                ),
            )
            written += 1
        else:
            conn.execute(
                f"""
                UPDATE {EVENTS_TABLE}
                SET subject_label = ?, prior_excerpt = ?, current_excerpt = ?,
                    evidence_quote = ?, materiality = ?, verdict = ?, interpretation_md = ?,
                    confidence = ?
                WHERE id = ?
                """,
                (*common, existing[0]),
            )
            written += 1
    return written
