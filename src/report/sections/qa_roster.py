"""Q&A roster — parses the structured analyst-Q&A segment out of the latest
earnings-call transcript. No LLM; the transcripts emitted by
``execution/fetch_qa_transcript.py`` are already structured enough to parse
with regex.

Transcript shape (per the fetcher):

    === Q&A SEGMENT ===
    first question comes from <Analyst> with <Firm>.

    <X> <Full Name> [question text — may span multiple sentences]

    <Y> <Full Name> [answer text]

    <Z> <Full Name> [follow-up answer text]

    Operator Your next question comes from <Analyst> with <Firm>.
    ...

Each ``<X>`` is the speaker's first-initial used as a paragraph marker by the
aggregator. We split on the Operator boundaries, treat the first
``<X> <Full Name>`` paragraph in each segment as the analyst question, and
treat subsequent paragraphs as answers (unless they come from the original
analyst, in which case they're a follow-up).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import cast

from llm_client import generate_qa_topics, is_hard_stop
from report.models import (
    AppendixSection,
    QAEntry,
    QARosterQuarter,
    QARosterSection,
    SectionStatus,
    TranscriptEntry,
)
from report.sections._common import budget_gate, missing

# A turn boundary is the operator/host handing the floor to an analyst. The
# free aggregators (roic.ai et al.) emit many shapes, all of the form
# "<lead-in> <Analyst> <connector> <Firm>." where the connector is with/at/from/of:
#
#   US operator:  "...(first|next) question comes from [the line of] <A> with <Firm>."
#                 "...our first question will come from <A> from <Firm>."
#                 "...first question today is coming from <A> of <Firm>."
#                 "...first question is going to come from <A> at <Firm>."
#                 "...our first question or comment comes from <A> with <Firm>."
#                 "...our first question from <A> of <Firm>."   (no verb at all)
#   Webcast / IR host (NU and other foreign issuers):
#                 "...open the line for [Mr.] <A> from <Firm>."
#
# The lead-in verb between "question[s]" and "from" varies wildly across roic
# output ("comes" / "will come" / "is coming" / "coming" / "will be coming" /
# "is going to come" / "will be" / bare), optionally carrying a "today" /
# "for today" / "or comment" interjection. Rather than enumerate every phrase,
# we accept any run of those filler/motion words — a *whitelist*, so ordinary
# prose like "I have a question from last year" can't false-match (the run must
# be followed by lowercase "from" + a Capitalized analyst + connector + firm +
# "."). Keywords are case-insensitive (scoped via (?i:...)) so the name/firm
# captures stay case-sensitive — a global (?i) would make [A-Z] match lowercase
# and the analyst group would swallow connective words like "the line of". The
# appendix loader flattens transcripts to a single line for the legacy
# renderer, so we don't anchor to ^/$ — we search the flat text for the phrase.
_TURN_BOUNDARY_RX = re.compile(
    r"""(?x)
    (?:
        (?i:questions?)
        (?:\s+(?i:for|today|or|comment|we|have|will|be|is|going|to|come|comes|coming))*
        \s+from\s+
        (?i:the\s+line\s+of\s+)?
        |
        (?i:open\s+the\s+line\s+for)\s+(?:(?i:mr|mrs|ms|mx|dr)\.?\s+)?
        |
        # host queue style: "we'll go (first|next) [today|this morning] to <A> with <Firm>."
        (?i:go)\s+(?i:first|next)\s+
        (?:(?:(?i:today)|(?i:this)\s+(?i:morning|afternoon|evening))\s+)?
        (?i:to)\s+
    )
    (?P<analyst>[A-Z][\w'.\-]+(?:\s+[A-Z][\w'.\-]+){0,3})
    \s+(?i:with|at|from|of)\s+
    (?P<firm>[^?!\n]+?)
    # Terminate at the sentence-ending . / ? / !, but not at an abbreviation dot
    # like the ones in "J.P. Morgan" or "H.C. Wainwright" — a period preceded by
    # a single capital letter (itself after a space or another dot). The
    # fixed-width lookbehind treats "UBS." (multi-letter acronym) as a real stop
    # while skipping the dots inside "J." / "P.".
    (?<![\s.][A-Z])[.?!]
    """,
)

# A speaker block starts with a single capital letter (the first-initial
# marker), then a name whose first word begins with that same letter. The
# backreference ``(?P=initial)`` enforces the initial==first-letter-of-name
# constraint, which keeps the regex from matching mid-sentence patterns like
# ". I Think this is...". The leading anchor accepts either start-of-text or a
# sentence-terminator + whitespace.
#
# The regex deliberately matches ONLY through the first name and stops (via a
# zero-width lookahead requiring a following capitalized word — the surname).
# How many of the following capitalized words belong to the NAME vs. to the
# SPOKEN TEXT is decided in Python by ``_split_name_tail``: a pure-regex name
# capture can't tell a 3rd name word ("Ryan James Merkel") or a middle-initial
# surname ("Matthew J. Tobolski") apart from a capitalized sentence opener
# ("How are you") — a non-greedy quantifier leaks the surname into the body, a
# greedy one eats the first spoken word. The token walk resolves this with a
# speech-opener guard.
_SPEAKER_BLOCK_START_RX = re.compile(
    r"""(?x)
    (?:(?<=[.!?])\s+|\A\s*)
    (?P<initial>[A-Z])\s+
    (?P<first>(?P=initial)[a-z]+)
    (?=\s+[A-Z])
    """,
)

# Operator / IR-host hand-off stubs that leak into a turn body as a short
# trailing "speaker block" (e.g. "Operator, could you please [open the line
# for ...]" — the boundary regex eats the "open the line..." part, leaving the
# stub). They aren't substantive Q&A, so we drop them rather than render them
# as a one-line "answer".
_HANDOFF_RX = re.compile(r"(?i)^operator[,.]?\s+(?:could|can|please|we\b|i\b|the\s+next)")

# ---------------------------------------------------------------------------
# Full-name speaker format (stockanalysis.com aggregator)
# ---------------------------------------------------------------------------
# Some aggregators (stockanalysis.com, incl. every NU quarter) label each
# speaker turn with the FULL NAME + TITLE + COMPANY rather than an initial
# marker, e.g.:
#
#   "Eduardo Rosman Analyst, BTG Pactual Hi, everyone. ..."
#   "Guilherme Lago Chief Financial Officer, Nu Holdings Hi, Jorge. ..."
#   "David Vélez Founder, Chief Executive Officer, and Chairman, Nu Holdings Sure. ..."
#
# There is no single-initial marker, so _SPEAKER_BLOCK_START_RX finds nothing.
# We detect a block start as "<2-4 capitalized-word name> <role keyword>": the
# name is captured and the match ends right at the role keyword, after which a
# title prefix ("<role…>, <Company>") precedes the spoken text. _strip_title_prefix
# walks tokens off that prefix until the speech begins. The hard part is the
# company→speech boundary (both start capitalized, role phrases carry commas),
# so we lean on a speech-opener guard plus a lowercase-word stop — see below.

# Leading words of an earnings-call speaker title. Anchoring on these (rather
# than "any capitalized run") is what separates a real speaker header from an
# ordinary capitalized phrase mid-answer. Kept tight to the words that actually
# *lead* a title here (analysts + C-suite); "Research"/"Group"/"Global" are
# omitted because they recur mid-firm-name ("Morgan Stanley Research") and would
# false-trigger. Case-sensitive (titles are Title-Cased in this format).
_ROLE_ANCHOR = (
    r"Analyst|Co-Founder|Founder|Managing|Chief|Equity|Vice|Partner|Director"
    r"|President|Senior|Investor|Executive|Head|Chairman|Chairwoman|Treasurer"
)

# A full-name speaker header START. The name word uses [A-Z][^\s,]+ (not
# [A-Z][\w…]+) so accented / mojibake surnames survive — e.g. "Vélez" is stored
# as "V\xc3\xa9lez" where \xa9 is a *symbol*, not a \w letter, and [\w…]+ would
# truncate the name to "VÃ" and break the anchor. The trailing-words count is
# non-greedy so the name stops at the *shortest* prefix immediately followed by
# a role keyword (handles 2-, 3- and 4-word names). The lookahead is
# non-consuming: .end() lands at the role keyword so the body slice keeps the
# whole "<role…>, <Company>" title for _strip_title_prefix to remove.
# A single name word: a capitalized token that does NOT end in sentence
# punctuation (so the trailing "Thanks." of the previous turn isn't mistaken for
# the first word of the next speaker's name), OR a single-letter middle initial
# like "P." in "John P. Kim".
_NAME_WORD = r"(?:[A-Z]\.|[A-Z][^\s,]{0,29}[^\s,.?!])"
_FULLNAME_HEADER_RX = re.compile(
    r"(?:(?<=[.!?])\s+|\A\s*)"
    r"(?P<name>" + _NAME_WORD + r"(?:\s+" + _NAME_WORD + r"){1,3}?)"
    r"\s+(?=(?:" + _ROLE_ANCHOR + r")\b)"
)

# ---------------------------------------------------------------------------
# Colon-delimited speaker format (issuer's own IR-published transcript)
# ---------------------------------------------------------------------------
# Companies that publish their own transcript (PDF/HTML on the IR site, e.g. Nu
# Holdings) label each turn "<Name>: <speech>" or "<Name>, <Firm>: <speech>":
#
#   "David Velez: Thanks, Jorge, for that question. ..."
#   "Jorge Kuri, Morgan Stanley: Congrats on the results. ..."
#
# There is no single-initial marker (so _SPEAKER_BLOCK_START_RX finds nothing)
# and no role keyword after the name (so _FULLNAME_HEADER_RX finds nothing), so
# this is the third and last fallback in the splitter ladder.
#
# We require >=2 name words (the surname guarantees a real person) so a section
# or acronym label ("NPS:", "Pix:", "Q&A:", "Note:") inside a turn body can't be
# mistaken for a speaker, and the speech must open with a capital/digit after the
# colon — both keep false splits out of answer text. The optional ", <Firm>" is
# matched but not captured (the analyst's firm already comes from the operator
# hand-off boundary).
_COLON_SPEAKER_HDR_RX = re.compile(
    r"(?:(?<=[.!?”\"])\s+|\A\s*)"
    r"(?P<name>" + _NAME_WORD + r"(?:\s+" + _NAME_WORD + r"){1,3})"
    r"(?:\s*,\s*[A-Z][^:\n]{1,40})?"
    r"\s*:\s+(?=[A-Z0-9])"
)

# Lowercase words that belong to a title (consumed) rather than marking speech.
_TITLE_CONNECTORS = frozenset(
    {"and", "of", "the", "for", "to", "in", "at", "&", "do", "de", "da", "e"}
)

# Discourse / greeting words that open spoken text. When the title-stripping
# walk reaches one of these (capitalized) tokens it stops — that's where the
# company name ends and speech begins. Deliberately excludes words that can lead
# a *company* name ("First", "Capital", "Global", …) so we never truncate a
# firm; the cost is that a turn opening with a vocative ("Mario, thanks…") leaks
# one word into the company, which is harmless for the topic/answer text.
# "operator" is included because it leads an IR-host hand-off stub ("Operator,
# could you please open the line for …") — stopping there keeps the stub intact
# so _HANDOFF_RX drops it instead of leaving a bare "could you please" answer.
_SPEECH_OPENER_WORDS = (
    "hi hello hey thanks thank sure yes yeah yep no okay ok great perfect good so"
    " well maybe just could can let yet understood excellent cool super quickly"
    " very now look right absolutely correct exactly indeed morning afternoon"
    " evening we my our and but i operator"
)
_SPEECH_OPENERS = frozenset(_SPEECH_OPENER_WORDS.split())

# Keyword → short tag for the panel chip. Order matters (first match wins).
_TAG_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(capex|capital|backlog|TPU|infrastructure|compute)\b", re.IGNORECASE), "INFRA"),
    (re.compile(r"\b(cloud|GCP|hyperscale|AWS|Azure)\b", re.IGNORECASE), "CLOUD"),
    (
        re.compile(
            r"\b(search|query|queries|ads|advertising|AI mode|AI overviews)\b", re.IGNORECASE
        ),
        "SEARCH",
    ),
    (re.compile(r"\b(margin|profitability|ROI|ROIC)\b", re.IGNORECASE), "MARGIN"),
    (re.compile(r"\b(subscription|consumer|YouTube|Gemini app)\b", re.IGNORECASE), "CONSUMER"),
    (re.compile(r"\b(regulation|antitrust|legal|DOJ|EU)\b", re.IGNORECASE), "LEGAL"),
    (re.compile(r"\b(Waymo|Other Bets|alpha|moonshot)\b", re.IGNORECASE), "OTHER BETS"),
    (re.compile(r"\b(agent|agentic|commerce|UCP)\b", re.IGNORECASE), "AGENT"),
]


def build(
    appendix: AppendixSection,
    ticker: str = "",
    repo_root: Path | None = None,
    enable_llm: bool = False,
    max_quarters: int = 5,
    force_budget_bypass: bool = False,
) -> QARosterSection:
    """Parse the most recent ``max_quarters`` transcripts into per-quarter rosters.

    When ``enable_llm`` is True, also runs ``generate_qa_topics`` per quarter
    to upgrade the regex-derived topic strings to clean LLM-summarized labels.
    Cached under ``data/qa_topics/<TICKER>.json`` keyed by sha256 of the
    question payload so re-runs are free.
    """
    if not appendix.transcripts:
        return QARosterSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="TRANSCRIBE(latest_quarter)",
                fix_command="python execution/fetch_audio_transcripts.py --ticker <T>",
                detail="No transcripts available to parse.",
            ),
        )
    # Budget gate for the (optional) topic-labeling LLM. On skip the roster
    # still renders with regex-derived labels; only the LLM upgrade is forgone.
    topics_skip = (
        budget_gate("qa_topics", "Q&A topics", repo_root, bypass=force_budget_bypass)
        if enable_llm and ticker and repo_root is not None
        else None
    )
    rosters: list[QARosterQuarter] = []
    for transcript in appendix.transcripts[:max_quarters]:
        entries = _parse(transcript)
        if not entries:
            # Skip silently — older transcripts often have format drift; the
            # latest quarter is what the user is looking at first anyway.
            continue
        if enable_llm and ticker and repo_root is not None and topics_skip is None:
            entries = _apply_llm_topics(ticker, repo_root, transcript, entries)
        rosters.append(
            QARosterQuarter(quarter=transcript.quarter, year=transcript.year, entries=entries)
        )
    if not rosters:
        return QARosterSection(
            status=SectionStatus.PARTIAL,
            missing=missing(
                stage="TRANSCRIBE(format_drift)",
                fix_command="inspect transcripts/{processed,raw}/<T>_Q<n>_<yyyy>.txt",
                detail=(
                    "Transcripts present but the Q&A boundary parser found no "
                    "turns in any — the source format may have drifted from "
                    "the fetch_qa_transcript.py output shape."
                ),
            ),
        )
    return QARosterSection(status=SectionStatus.OK, quarters=rosters, budget_skip=topics_skip)


def _parse(transcript: TranscriptEntry) -> list[QAEntry]:
    text = transcript.text
    # Find each turn boundary; the body of a turn is from one boundary's end
    # to the next boundary's start (or end of string).
    boundaries: list[tuple[int, int, str, str]] = []
    for m in _TURN_BOUNDARY_RX.finditer(text):
        boundaries.append((m.start(), m.end(), m.group("analyst"), m.group("firm").strip()))
    if not boundaries:
        return []

    entries: list[QAEntry] = []
    for i, (_start, body_start, analyst_name, firm) in enumerate(boundaries):
        body_end = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(text)
        body = text[body_start:body_end].strip()
        entry = _parse_turn(analyst_name, firm, body)
        if entry is not None:
            entries.append(entry)
    return entries


def _parse_turn(analyst_name: str, firm: str, body: str) -> QAEntry | None:
    """Parse one turn into a QAEntry. Returns None if the turn has no question."""
    paras = _split_speaker_paragraphs(body)
    if not paras:
        return None

    # First paragraph by the analyst (or someone whose name matches the intro)
    # is the question. The fetcher sometimes labels the analyst with a slightly
    # different name ("Unknown Analyst" when introductions are missing), so we
    # accept any first paragraph as the question rather than requiring an
    # exact name match.
    q_speaker, question = paras[0]
    # Skip turns that don't actually contain a question paragraph (rare —
    # operator hand-off without a follow-up question).
    if not question:
        return None

    answers: list[tuple[str, str]] = []
    follow_up: str | None = None
    for speaker, para in paras[1:]:
        if _names_match(speaker, q_speaker):
            # Follow-up question from the same analyst — capture once; ignore
            # subsequent follow-ups to keep the row compact.
            if follow_up is None:
                follow_up = para
            continue
        answers.append((speaker, para))

    analysts = f"{analyst_name} ({firm})"
    topic = _topic_from_question(question)
    tag = _tag_from_text(question)
    return QAEntry(
        analysts=analysts,
        topic=topic,
        tag=tag,
        question=question,
        answers=answers,
        follow_up=follow_up,
        transcript_ref=None,
    )


def _split_speaker_paragraphs(body: str) -> list[tuple[str, str]]:
    """Scan a turn body for ``<X> <Name>`` speaker blocks; return [(name, text)].

    ``_SPEAKER_BLOCK_START_RX`` finds each block's header through the speaker's
    first name; ``_split_name_tail`` then peels the remaining name words
    (surname, middle initial, particle) off the front of the block body so they
    don't leak into the spoken text. The body of each block runs from the
    header's end to the next header's start (or end of the turn).
    """
    headers = list(_SPEAKER_BLOCK_START_RX.finditer(body))
    if not headers:
        # No "<Initial> <Name>" markers — fall back to the stockanalysis.com
        # full-name format ("<Name> <Role…>, <Company> <text>").
        return _split_fullname_paragraphs(body)
    out: list[tuple[str, str]] = []
    for i, m in enumerate(headers):
        body_start = m.end()
        body_end = headers[i + 1].start() if i + 1 < len(headers) else len(body)
        first = m.group("first")
        name_tail, text = _split_name_tail(_normalize_whitespace(body[body_start:body_end]))
        name = f"{first} {name_tail}".strip()
        # Trim trailing operator "stub" markers like " . " or " O " that the
        # aggregator inserts between paragraphs.
        text = text.strip(" .").strip()
        if not text:
            continue
        if _HANDOFF_RX.match(text):
            # IR/operator hand-off stub, not a Q&A turn — drop it.
            continue
        out.append((name, text))
    return out


def _split_fullname_paragraphs(body: str) -> list[tuple[str, str]]:
    """Scan a turn body for full-name speaker blocks; return [(name, text)].

    For the stockanalysis.com format where each turn is "<Name> <Role…>,
    <Company> <spoken text>". ``_FULLNAME_HEADER_RX`` locates each block's name
    and the start of its title; ``_strip_title_prefix`` removes the
    "<role…>, <Company>" prefix so only the spoken text remains.
    """
    headers = list(_FULLNAME_HEADER_RX.finditer(body))
    if not headers:
        # No role-anchored full-name headers — try the issuer's own
        # colon-delimited "<Name>: <speech>" format before giving up.
        return _split_colon_paragraphs(body)
    out: list[tuple[str, str]] = []
    for i, m in enumerate(headers):
        body_start = m.end()
        body_end = headers[i + 1].start() if i + 1 < len(headers) else len(body)
        name = m.group("name").strip()
        seg = _normalize_whitespace(body[body_start:body_end])
        text = _strip_title_prefix(seg).strip(" .").strip()
        if not text:
            continue
        if _HANDOFF_RX.match(text):
            # IR/operator hand-off stub ("Operator, could you please …") — drop.
            continue
        out.append((name, text))
    return out


def _split_colon_paragraphs(body: str) -> list[tuple[str, str]]:
    """Scan a turn body for colon-delimited speaker blocks; return [(name, text)].

    For the issuer-published format where each turn is "<Name>: <speech>" or
    "<Name>, <Firm>: <speech>" (no initial marker, no role keyword). The block
    body runs from one header's end to the next header's start (or end of turn).
    A ``_HANDOFF_RX`` stub ("Operator, could you please …") is dropped.
    """
    headers = list(_COLON_SPEAKER_HDR_RX.finditer(body))
    if not headers:
        return []
    out: list[tuple[str, str]] = []
    for i, m in enumerate(headers):
        body_start = m.end()
        body_end = headers[i + 1].start() if i + 1 < len(headers) else len(body)
        name = m.group("name").strip()
        text = _normalize_whitespace(body[body_start:body_end]).strip(" .").strip()
        if not text:
            continue
        if _HANDOFF_RX.match(text):
            continue
        out.append((name, text))
    return out


def _strip_title_prefix(seg: str) -> str:
    """Drop the leading "<role…>, <Company>" title from a full-name block body.

    ``seg`` begins at the role keyword (where ``_FULLNAME_HEADER_RX`` ended). We
    walk whitespace tokens off the front: title connectors ("and", "of", …) and
    Title-Cased role/company words are consumed; we stop at the first speech
    opener ("Hi", "Thanks", "Sure", …) or the first lowercase non-connector word
    — that's where the company name ends and the spoken text begins.
    """
    tokens = seg.split()
    i = 0
    while i < len(tokens):
        bare = tokens[i].strip(",.;:&'\"()-").lower()
        if not bare:
            i += 1  # pure-punctuation token
        elif bare in _TITLE_CONNECTORS:
            i += 1  # part of the title ("Bank of America")
        elif bare in _SPEECH_OPENERS:
            break  # greeting / discourse marker — speech starts here
        elif tokens[i][:1].isupper():
            i += 1  # a role or company word (Title-Cased / acronym)
        else:
            break  # lowercase non-connector word — speech starts here
    return " ".join(tokens[i:])


# ---------------------------------------------------------------------------
# Initial-marker name/speech split (used by _split_speaker_paragraphs)
# ---------------------------------------------------------------------------
# _SPEAKER_BLOCK_START_RX matches only through the first name; these helpers
# decide how many of the following capitalized words are still the NAME
# (surname, middle initials, particle) vs. where the SPOKEN TEXT begins.

# Lowercase Latin/Dutch surname particles ("do Lago", "van Beurden"). Matched
# case-sensitively (lowercase only) so a sentence-initial "Do/De" can't trigger
# a spurious surname join.
_NAME_PARTICLES = frozenset(
    {"do", "de", "da", "dos", "das", "del", "von", "van", "der", "di", "la", "le"}
)

# A bare middle initial ("J.", "F.") always pulls in the following surname.
_MIDDLE_INITIAL_RX = re.compile(r"[A-Z]\.")

# Curly quote characters the aggregators emit, built with chr() so the source
# stays ASCII (ruff RUF001 flags ambiguous-quote literals).
_SMART_SINGLE = chr(0x2018) + chr(0x2019)  # left/right single quotation marks
_SMART_QUOTES = _SMART_SINGLE + chr(0x201C) + chr(0x201D)  # + double quotes
_EDGE_PUNCT = ",.;:!?\"'()" + _SMART_QUOTES

# A plausible name token (letters plus an inner apostrophe/hyphen/period, e.g.
# "O'Brien", "Smith-Jones", "St."); the apostrophe class covers the curly
# variants so a smart-quoted surname still parses.
_NAME_TOKEN_RX = re.compile(rf"[A-Za-z][\w'{_SMART_SINGLE}.\-]*")

# A contraction ("It's", "I'll", "We've", "That's") — speech, never a name word.
_CONTRACTION_RX = re.compile(rf"['{_SMART_SINGLE}](?:s|ll|m|ve|re|d|t|em)$", re.IGNORECASE)

# Trailing punctuation that ends the name region: a comma/colon flags a direct
# address ("Adam,") and a sentence terminator flags an interjection the
# aggregator runs into the next speaker's text ("Trevor.", "Pardon?").
_NAME_STOP_PUNCT = frozenset(".,;:!?")

# Names have at most four words after the first ("Roger J. M. Dassen", "First
# Marques do Lago"); cap the walk so a body opening with capitalized words can't
# run away.
_MAX_NAME_TAIL = 4

# Words that OPEN spoken text — reuses the full-name title-stripper's openers
# (greetings/discourse) and adds the pronouns, question/aux verbs, and fillers
# the name-tail walk also needs. A capitalized token whose lowercase form is in
# this set marks the START OF SPEECH, not a surname. Surname-likely words ("may",
# "will", "young", "given") are deliberately absent so a real name isn't cut.
_EXTRA_OPENER_WORDS = (
    "got guess think mean wonder wondering curious you they it this that these"
    " those a an if as in on at with from about during since while before after"
    " how what when where why who whom whose which would should do does did is"
    " are was were am be been being have has had two three couple first second"
    " secondly also again all anyway anyways obviously clearly honestly frankly"
    " basically essentially overall regarding fair definitely welcome congrats"
    " congratulations appreciate appreciated apologies sorry awesome fantastic"
    " wonderful terrific alright nope yup listen his her their"
)
_NAME_SPLIT_OPENERS = _SPEECH_OPENERS | frozenset(_EXTRA_OPENER_WORDS.split())


def _split_name_tail(after: str) -> tuple[str, str]:
    """Split a block body into (trailing name words, spoken text).

    ``after`` is the whitespace-normalized text immediately following a
    speaker's first name — for the common two-word case it begins with the
    surname. Returns ``(name_tail, speech)`` where ``name_tail`` may be "".
    """
    tokens = after.split()
    k = _name_tail_len(tokens)
    return " ".join(tokens[:k]), " ".join(tokens[k:])


def _name_tail_len(tokens: list[str]) -> int:
    """Count the leading ``tokens`` that belong to the speaker's name.

    A leading run of middle initials ("J.", "J. M.") is followed by the
    surname, which is always taken. A further full word (a middle name + a
    second surname word, or a particle surname) is taken only on strong
    evidence: a clean token (no trailing punctuation) that is clearly the LAST
    name word — followed by the start of speech (a capitalized opener) or
    nothing. That separates a true three-word name ("Ryan James Merkel Hey")
    from a direct address ("...McDonnell Adam, I'll..."), a vocative
    ("...Chesky Eli do you..." — next token lowercase), and an interjection the
    aggregator runs together ("...Sinatra Gotcha. Thank...").
    """
    n = len(tokens)
    k = 0
    # A run of middle initials ("J.", "J. M.") directly after the first name.
    while k < n and k < _MAX_NAME_TAIL and _MIDDLE_INITIAL_RX.fullmatch(tokens[k]):
        k += 1
    if k > 0:
        # Initials are always followed by the surname — take it unconditionally
        # (it may carry a particle: "von Beurden").
        if k < n and tokens[k] in _NAME_PARTICLES and k + 1 < n and _is_name_word(tokens[k + 1]):
            k += 2
        elif k < n and _is_name_word(tokens[k]):
            k += 1
    elif tokens and tokens[0] in _NAME_PARTICLES and n > 1 and _is_name_word(tokens[1]):
        k = 2  # particle surname with no preceding word ("da Silva ...")
    elif tokens and _is_name_word(tokens[0]) and tokens[0][-1] not in _NAME_STOP_PUNCT:
        k = 1  # the surname
    else:
        return 0  # one-word name (e.g. "Operator") — the body starts here
    # Further name words (a full middle name + surname, or a particle surname),
    # accepted only on strong evidence: a clean token (no trailing punctuation)
    # that is clearly the LAST name word — followed by the start of speech.
    while k < n and k < _MAX_NAME_TAIL:
        tok = tokens[k]
        if tok in _NAME_PARTICLES and k + 1 < n and _is_name_word(tokens[k + 1]):
            k += 2  # particle surname ("Marques do Lago")
            continue
        if not _is_name_word(tok) or tok[-1] in _NAME_STOP_PUNCT:
            break  # opener / contraction / address ("Adam,", "Trevor.", "Pardon?")
        nxt = tokens[k + 1] if k + 1 < n else None
        if nxt is None or _is_capitalized_opener(nxt):
            k += 1
            continue
        break
    return k


def _is_name_word(tok: str) -> bool:
    """True if ``tok`` looks like a name word rather than the start of speech."""
    core = tok.strip(_EDGE_PUNCT)
    if not core or not core[0].isupper():
        return False
    if _CONTRACTION_RX.search(tok):
        return False
    if any(ch.isdigit() for ch in core):
        return False
    if core.isupper() and len(core) > 1:
        # All-caps token (EBITDA, GAAP, AI) — an acronym, not a surname.
        return False
    if core.lower() in _NAME_SPLIT_OPENERS:
        return False
    return _NAME_TOKEN_RX.fullmatch(core) is not None


def _is_capitalized_opener(tok: str) -> bool:
    """True if ``tok`` is a capitalized word (or contraction) that begins speech."""
    core = tok.strip(_EDGE_PUNCT)
    if not core or not core[0].isupper():
        return False
    if _CONTRACTION_RX.search(tok):
        return True
    return core.lower() in _NAME_SPLIT_OPENERS


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _names_match(a: str, b: str) -> bool:
    """Loose match: same first + last word (case-insensitive)."""
    aa = a.lower().split()
    bb = b.lower().split()
    if not aa or not bb:
        return False
    return aa[0] == bb[0] and aa[-1] == bb[-1]


def _topic_from_question(question: str) -> str:
    """Pick a short, informative topic phrase from the analyst question.

    Strategy: split into sentences, drop boilerplate openers ("I have two",
    "Yes", "Thanks", numeric "2," etc.), and return the first sentence that
    looks like substantive content (length > 25 chars, or contains a
    question keyword). Falls back to the first 70 chars of the question.
    """
    sentences = re.split(r"(?<=[.?!])\s+", question)
    skip_re = re.compile(
        r"^(?:i\s+have|i'?ll|i'll|thanks|thank you|one|two|yes|yeah|just|maybe|sure|"
        r"first|congrats|first off|two for me|one for me|just one|two questions|"
        r"\d+[,.]?)\b",
        re.IGNORECASE,
    )
    for s in sentences:
        s_clean = s.strip().lstrip(",")
        if len(s_clean) < 20:
            continue
        if skip_re.match(s_clean):
            continue
        head = s_clean.rstrip(".?!").strip()
        if len(head) > 70:
            head = head[:67].rstrip() + "..."
        return head
    # Nothing substantive — return the first 70 chars verbatim.
    fallback = question.strip().rstrip(".?!")
    return fallback[:70] + ("..." if len(fallback) > 70 else "")


def _tag_from_text(text: str) -> str:
    for pattern, tag in _TAG_RULES:
        if pattern.search(text):
            return tag
    return "Q&A"


# ---------------------------------------------------------------------------
# LLM-summarized Q&A topics — batched per quarter, cached on disk
# ---------------------------------------------------------------------------


def _apply_llm_topics(
    ticker: str,
    repo_root: Path,
    transcript: TranscriptEntry,
    entries: list[QAEntry],
) -> list[QAEntry]:
    """Replace regex-derived ``entry.topic`` and ``entry.tag`` with LLM picks.

    Falls back to the original regex values when the cache misses AND the LLM
    call fails — the analyst panel always renders, just with weaker labels.
    """
    if not entries:
        return entries
    payload = [
        {"id": str(i), "analyst": e.analysts, "question": e.question[:1200]}
        for i, e in enumerate(entries)
    ]
    cache_key = _topics_cache_key(transcript, payload)
    cached = _load_topics_cache(repo_root, ticker, cache_key)
    if cached is None:
        try:
            quarter_label = f"{transcript.quarter} {transcript.year}"
            raw = generate_qa_topics(ticker, quarter_label, payload)
            cached = _parse_topics_response(raw)
            _save_topics_cache(repo_root, ticker, cache_key, cached)
        except Exception as exc:
            # Hard stops (monthly budget cap, CLI not installed) must propagate
            # — they affect the whole build, not just these Q&A labels.
            if is_hard_stop(exc):
                raise
            # Soft-fail: keep regex-derived labels. The exception was logged in
            # generate_qa_topics; no second log here keeps the noise down.
            return entries
    by_id = {item["id"]: item for item in cached}
    out: list[QAEntry] = []
    for i, e in enumerate(entries):
        item = by_id.get(str(i))
        if item is None:
            out.append(e)
            continue
        out.append(
            e.model_copy(
                update={
                    "topic": item.get("topic") or e.topic,
                    "tag": (item.get("tag") or e.tag).upper(),
                }
            )
        )
    return out


def _topics_cache_key(transcript: TranscriptEntry, payload: list[dict[str, str]]) -> str:
    h = hashlib.sha256()
    h.update(transcript.quarter.encode("utf-8"))
    h.update(b"\x00")
    h.update(str(transcript.year).encode("utf-8"))
    h.update(b"\x00")
    h.update(json.dumps(payload, sort_keys=True).encode("utf-8"))
    return h.hexdigest()


def _topics_cache_path(repo_root: Path, ticker: str) -> Path:
    return repo_root / "data" / "qa_topics" / f"{ticker.upper()}.json"


def _load_topics_cache(repo_root: Path, ticker: str, cache_key: str) -> list[dict[str, str]] | None:
    path = _topics_cache_path(repo_root, ticker)
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
    # Validate the shape: list of {"id","topic","tag"} dicts.
    out: list[dict[str, str]] = []
    for item in entry:
        if not isinstance(item, dict):
            return None
        item_dict = cast("dict[str, object]", item)
        out.append({str(k): str(v) for k, v in item_dict.items()})
    return out


def _save_topics_cache(
    repo_root: Path, ticker: str, cache_key: str, items: list[dict[str, str]]
) -> None:
    path = _topics_cache_path(repo_root, ticker)
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
    by_key[cache_key] = items
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def _parse_topics_response(raw: str) -> list[dict[str, str]]:
    parsed_raw = json.loads(raw)
    if not isinstance(parsed_raw, list):
        raise ValueError("qa_topics response was not a JSON array")
    parsed = cast("list[object]", parsed_raw)
    out: list[dict[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        item_dict = cast("dict[str, object]", item)
        out.append(
            {
                "id": str(item_dict.get("id", "")),
                "topic": str(item_dict.get("topic", "")).strip(),
                "tag": str(item_dict.get("tag", "")).strip(),
            }
        )
    return out
