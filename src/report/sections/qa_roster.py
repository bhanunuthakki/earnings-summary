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
from report.sections._common import missing

# "Operator ..." or "first question comes from ..." — both mark the start of
# a Q&A turn. The fetcher's output uses both shapes (the very first turn
# omits the Operator prefix). The appendix loader flattens transcripts to a
# single line for the legacy renderer's needs, so we don't anchor to ^/$ —
# we search the flat text for the phrase and grab the analyst metadata after
# "from ... with ...".
_TURN_BOUNDARY_RX = re.compile(
    r"""(?ix)
    (?:
        Operator\s+(?:Your\s+next\s+question\s+comes|Our\s+(?:first|next|last)\s+question\s+comes)
        |
        \bfirst\s+question\s+comes
    )
    \s+from\s+
    (?P<analyst>[A-Z][\w'.\-]+(?:\s+[A-Z][\w'.\-]+){0,3})
    \s+with\s+
    (?P<firm>[^.\n]+?)
    \.
    """,
)

# A speaker block: single capital letter (the first-initial marker), then a
# name whose first word begins with that same letter, then 1-3 more
# capitalized words. The backreference ``(?P=initial)`` enforces the
# initial==first-letter-of-name constraint, which keeps the regex from
# matching mid-sentence patterns like ". I Think this is...". The leading
# anchor accepts either start-of-text or a sentence-terminator + whitespace.
_SPEAKER_BLOCK_START_RX = re.compile(
    r"""(?x)
    (?:(?<=[.!?])\s+|\A\s*)
    (?P<initial>[A-Z])\s+
    (?P<name>(?P=initial)[a-z]+(?:\s+[A-Z][\w'.\-]+){1,2}?)
    \s+
    (?=[A-Z])
    """,
)

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
    rosters: list[QARosterQuarter] = []
    for transcript in appendix.transcripts[:max_quarters]:
        entries = _parse(transcript)
        if not entries:
            # Skip silently — older transcripts often have format drift; the
            # latest quarter is what the user is looking at first anyway.
            continue
        if enable_llm and ticker and repo_root is not None:
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
    return QARosterSection(status=SectionStatus.OK, quarters=rosters)


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

    Uses ``_SPEAKER_BLOCK_START_RX`` to find each block's header; the body of
    each block runs from the header's end to the next header's start (or end
    of the turn).
    """
    headers = list(_SPEAKER_BLOCK_START_RX.finditer(body))
    if not headers:
        return []
    out: list[tuple[str, str]] = []
    for i, m in enumerate(headers):
        body_start = m.end()
        body_end = headers[i + 1].start() if i + 1 < len(headers) else len(body)
        name = m.group("name").strip()
        text = _normalize_whitespace(body[body_start:body_end])
        # Trim trailing operator "stub" markers like " . " or " O " that the
        # aggregator inserts between paragraphs.
        text = text.strip(" .").strip()
        if not text:
            continue
        out.append((name, text))
    return out


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
