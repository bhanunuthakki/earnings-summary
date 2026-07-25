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
* **~11% of analyst questions get an explicit non-answer**, stable across
  time and industry — deviation from that baseline is the signal, not the
  level (Gow, Larcker & Zakolyukina, *JAR* 59(4), 2021).
* **>90% of topic-share variation is firm-level**, ~70% of that
  within-firm-over-time — track deltas, never raw levels (Hassan,
  Hollander, van Lent & Tahoun, *QJE* 134(4), 2019).
* **Tone must be residualized against fundamentals** before calling it a
  shift (Huang, Teoh & Zhang, *TAR* 89(3), 2014 — ABTONE).
* **CEO and CFO language are not interchangeable** — only CFO language was
  tradeable (Larcker & Zakolyukina, *JAR* 50(2), 2012). Speakers are always
  tracked BY NAME below, never pooled into one "management" bucket.
* **Vocal affect is an audio signal** (Mayew & Venkatachalam, *JF* 67(1),
  2012) — explicitly out of scope for a text-only pipeline. This module
  never proxies it from "um"/"uh" disfluency counts.

Real-book note (measured 2026-07-25 against a copy of ``data/portfolio.db``,
546 ``transcripts`` rows / 863 ``transcript_segments`` rows): only ~17
transcripts (~3%) were ingested with real per-turn speaker attribution
(``compute.transcript_ingest``'s PDF path, i.e. >=2 stored segment rows).
The other ~97% are ``execution/fetch_qa_transcript.py`` aggregator pulls —
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

# Gow/Larcker/Zakolyukina's headline number. Deviation from this is the
# signal (§1.7) — never the raw level.
NONANSWER_BASELINE_PCT = 11.0
# Below this many questions, a call's non-answer rate is too noisy a
# statistic to trust (a single non-answer on 3 questions is 33%, which
# would swamp the baseline for no real reason).
NONANSWER_MIN_QUESTIONS = 5
# A rate more than this many percentage points from EITHER the ~11%
# baseline or the ticker's own prior call is treated as material. This is a
# round, transparent threshold (not fit to this book) — revisit once P5's
# event-study panel exists.
NONANSWER_MATERIALITY_PP = 15.0

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
# disproportion check in `tone_shift_is_abnormal` — see that function's
# docstring for what this proxy is and, more importantly, is NOT.
TONE_SMALL_SURPRISE_PCT = 2.0

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


def classify_roles(turns: list[Turn]) -> dict[str, RosterEntry]:
    """Classify each unique speaker name as OPERATOR / MANAGEMENT / ANALYST.

    No external roster is required (``exec_comp_packages`` is joined
    separately, see ``resolve_exec_role`` — it augments this, it is not a
    prerequisite for it). Management is identified structurally: the
    handful of names that answer questions REPEATEDLY across the call,
    versus the many analyst names that each appear once or twice — EXCEPT
    a name explicitly introduced as the next questioner
    (``_extract_introduced_analyst_names``), which always wins as ANALYST
    regardless of how many follow-ups they asked. This is the honest
    fallback the real book requires today (aggregator-sourced calls have no
    "on the call today are CEO X, CFO Y" roster line at all — that line
    lives in the prepared remarks, which those files never fetch)."""
    by_name: dict[str, list[Turn]] = defaultdict(list)
    for t in turns:
        if t.speaker:
            by_name[t.speaker].append(t)
    if not by_name:
        return {}

    introduced_analysts = _extract_introduced_analyst_names(turns)
    non_operator = {name: tlist for name, tlist in by_name.items() if name.lower() != "operator"}
    ranked = sorted(non_operator.items(), key=lambda kv: -len(kv[1]))
    management_names: set[str] = {
        name
        for name, tlist in ranked[:_MAX_MANAGEMENT_CANDIDATES]
        if len(tlist) >= _MIN_MANAGEMENT_TURN_COUNT and name not in introduced_analysts
    }

    roster: dict[str, RosterEntry] = {}
    for name, tlist in by_name.items():
        if name.lower() == "operator":
            role = SpeakerRole.OPERATOR
        elif name in management_names:
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
# Earnings-surprise join (control variable for the ABTONE-style proxy — see
# transcript_judgment / the CLI's tone_shift_is_abnormal usage)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SurpriseInfo:
    release_date: str
    eps_surprise_pct: float | None


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
            "SELECT release_date, eps_surprise_pct FROM earnings_surprises "
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
    release_date, eps_pct = rows[0]
    return SurpriseInfo(
        release_date=str(release_date),
        eps_surprise_pct=float(eps_pct) if eps_pct is not None else None,
    )


def tone_shift_is_abnormal(tone_delta: float, eps_surprise_pct: float | None) -> bool:
    """A TRANSPARENT PROXY for Huang/Teoh/Zhang's ABTONE residual — NOT a
    fitted OLS regression. A real fitted residual needs a historical panel
    per ticker (dozens of quarters) this book does not yet have; P5 ("own
    event study") is where that belongs once one exists. Until then: flag a
    tone delta as abnormal when either (a) it moved OPPOSITE the direction
    the same-quarter earnings surprise would suggest, or (b) it moved by a
    material amount despite an in-line/small surprise (large move, small
    fundamentals change). Both conditions are transparent and re-checkable
    by eye from the two numbers in the event row; neither claims the
    statistical guarantees of a fitted residual."""
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
    turns, coverage = load_call_turns(conn, transcript_id)
    turns = strip_boilerplate(turns)
    roster = classify_roles(turns)
    ticker = str(ticker_raw).upper()
    for name, entry in list(roster.items()):
        if entry.role is SpeakerRole.MANAGEMENT:
            exec_role = resolve_exec_role(conn, ticker, name)
            if exec_role:
                roster[name] = replace(entry, exec_role=exec_role)
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
