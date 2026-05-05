"""§10 Appendix — full earnings-call transcripts, embedded inline.

Reads each transcript file referenced by the EarningsSection cards, extracts
the text (PDFs via parser.extract_text_from_pdf), cleans it (strips
Whisper-style timestamps and reflows pypdf's word-per-line garbage into
paragraphs), and packs them into the ReportSpec.
"""

from __future__ import annotations

import re
from pathlib import Path

from parser import extract_text_from_pdf, read_text_file
from report.models import (
    AppendixSection,
    EarningsSection,
    QuarterlyEarningsCard,
    SectionStatus,
    TranscriptEntry,
)

_WHISPER_TIMESTAMP_RX = re.compile(r"\[\s*\d+(?:\.\d+)?s?\s*->\s*\d+(?:\.\d+)?s?\s*\]\s*")
_HYPHEN_BREAK_RX = re.compile(r"-\s*\n+\s*")
_ALL_WHITESPACE_RX = re.compile(r"\s+")
# Insert paragraph breaks before speaker labels: "Operator:", "Sundar Pichai:",
# "Mr. Smith:", up to three capitalized words followed by a colon.
_SPEAKER_RX = re.compile(
    r"(?<=[\.\?!])\s+(?=(?:Operator|Mr\.|Ms\.|Dr\.|[A-Z][a-z]+(?: [A-Z][a-z]+){0,2})\s*[:\-—])"
)


def build(earnings: EarningsSection) -> AppendixSection:
    cards = list(earnings.full_quarters) + list(earnings.digest_quarters)
    cards.sort(key=lambda c: (c.year, c.quarter), reverse=True)

    entries = [e for e in (_load_transcript(c) for c in cards) if e is not None]
    if not entries:
        return AppendixSection(status=SectionStatus.MISSING_DATA)
    return AppendixSection(status=SectionStatus.OK, transcripts=entries)


def _load_transcript(card: QuarterlyEarningsCard) -> TranscriptEntry | None:
    if not card.transcript_path:
        return None
    path = Path(card.transcript_path)
    if not path.exists():
        return None
    text = _extract(path)
    if text is None:
        return None
    return TranscriptEntry(
        quarter=card.quarter,
        year=card.year,
        source_path=str(path),
        text=_clean(text),
    )


def _extract(path: Path) -> str | None:
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            return extract_text_from_pdf(str(path))
        return read_text_file(str(path))
    except (OSError, ValueError):
        return None


def _clean(text: str) -> str:
    """Normalize transcript text into readable paragraphs.

    pypdf returns one PDF text element per line, with blank lines between —
    sometimes that means a paragraph break, often it means nothing (it's just
    visual word wrapping). We can't reliably tell which from the input, so we
    aggressively flatten ALL whitespace into single spaces and then re-segment
    into paragraphs at speaker-label boundaries (which are the only reliable
    paragraph anchors in earnings-call transcripts).
    """
    cleaned = _WHISPER_TIMESTAMP_RX.sub("", text)
    cleaned = _HYPHEN_BREAK_RX.sub("", cleaned)
    cleaned = _ALL_WHITESPACE_RX.sub(" ", cleaned)
    cleaned = _SPEAKER_RX.sub("\n\n", cleaned)
    return cleaned.strip()
