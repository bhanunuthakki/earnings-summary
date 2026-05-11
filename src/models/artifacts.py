"""Filename parsers for pipeline artifacts in transcripts/processed/ and .tmp/.

Replaces the position-based split-by-underscore parsing that lived in
db.scan_and_sync_artifacts. The old approach silently misclassified any
filename with an extra underscore; this module is regex+Pydantic and returns
None for non-matches instead of guessing.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel


class Quarter(StrEnum):
    Q1 = "Q1"
    Q2 = "Q2"
    Q3 = "Q3"
    Q4 = "Q4"


class ArtifactKind(StrEnum):
    TRANSCRIPT_PROCESSED = "transcript_processed"
    AUDIO = "audio"
    SAYDO = "saydo"
    LLM_SUMMARY = "llm_summary"


class ArtifactName(BaseModel):
    """Parsed artifact filename — ticker, period, kind."""

    ticker: str
    quarter: Quarter
    year: int
    kind: ArtifactKind


class ArtifactFlags(BaseModel):
    """Per-quarter artifact existence + step-completion flags for a ticker."""

    has_release_file: bool = False
    has_slides_file: bool = False
    has_transcript_file: bool = False
    has_audio_file: bool = False
    step_audio_transcribed: bool = False
    step_llm_summarized: bool = False
    step_saydo_analyzed: bool = False
    step_thesis_updated: bool = False


_TRANSCRIPT_PROCESSED_EXTS = frozenset({".pdf", ".txt"})
_AUDIO_EXTS = frozenset({".mp3", ".wav", ".m4a"})

_TICKER_QUARTER_YEAR_RX = re.compile(
    r"^(?P<ticker>[A-Za-z]+)_(?P<quarter>Q[1-4])_(?P<year>\d{4})$",
    re.IGNORECASE,
)
_SAYDO_RX = re.compile(
    r"^SayDo_(?P<ticker>[A-Za-z]+)_Q[1-4]_\d{4}_(?P<curr_q>Q[1-4])_(?P<curr_y>\d{4})$",
    re.IGNORECASE,
)
_SUMMARY_RX = re.compile(
    r"^(?P<ticker>[A-Za-z]+)_(?P<quarter>Q[1-4])_(?P<year>\d{4})_summary$",
    re.IGNORECASE,
)


def _build_name(
    ticker_match: str, quarter_match: str, year_match: str, kind: ArtifactKind
) -> ArtifactName:
    """Construct an ArtifactName, normalizing ticker and quarter to uppercase."""
    return ArtifactName(
        ticker=ticker_match.upper(),
        quarter=Quarter(quarter_match.upper()),
        year=int(year_match),
        kind=kind,
    )


def parse_transcript_processed(filename: str) -> ArtifactName | None:
    """Match `{TICKER}_{Q}_{YYYY}.{pdf,txt}` for files in transcripts/processed/."""
    p = Path(filename)
    if p.suffix.lower() not in _TRANSCRIPT_PROCESSED_EXTS:
        return None
    m = _TICKER_QUARTER_YEAR_RX.match(p.stem)
    if m is None:
        return None
    return _build_name(m["ticker"], m["quarter"], m["year"], ArtifactKind.TRANSCRIPT_PROCESSED)


def parse_tmp_artifact(filename: str) -> ArtifactName | None:
    """Dispatch a `.tmp/` filename to its ArtifactKind, or None if not recognized."""
    p = Path(filename)
    suffix = p.suffix.lower()

    if suffix in _AUDIO_EXTS:
        m = _TICKER_QUARTER_YEAR_RX.match(p.stem)
        if m is None:
            return None
        return _build_name(m["ticker"], m["quarter"], m["year"], ArtifactKind.AUDIO)

    if suffix != ".txt":
        return None

    m = _SAYDO_RX.match(p.stem)
    if m is not None:
        return _build_name(m["ticker"], m["curr_q"], m["curr_y"], ArtifactKind.SAYDO)

    m = _SUMMARY_RX.match(p.stem)
    if m is not None:
        return _build_name(m["ticker"], m["quarter"], m["year"], ArtifactKind.LLM_SUMMARY)

    return None
