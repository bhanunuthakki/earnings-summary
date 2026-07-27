"""
src/transcript_qa.py
--------------------
Structural validation for transcript files in `transcripts/raw/`.

Two flavors:
  - validate_audio_transcript:    Whisper-produced files (timestamped segments)
  - validate_synthesized_transcript: aggregator Q&A files from fetch_qa_transcript.py (banner sections)

Both return a `QaResult` so the index_manager can persist a single shape and
downstream consumers (skip-existing logic, audio-cache cleanup gate) can
decide on a single field.

These are STRUCTURAL/STATISTICAL checks only — line-count, byte-size, segment
duration coverage, words-per-second band, adjacent-repeat ratio (Whisper
hallucination signal), and presence of the writer's own banner. No
keyword/intent matching on transcript content.
"""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Thresholds — tuned for ~30-90 min earnings calls. Cross-referenced against
# the 24 known-good transcripts produced during the May 2026 backfill.
# ---------------------------------------------------------------------------

AUDIO_MIN_BYTES = 10 * 1024
AUDIO_MIN_LINES = 100
AUDIO_MIN_DURATION_SEC = 10 * 60
AUDIO_MIN_TIMESTAMPED_FRACTION = 0.95
AUDIO_MIN_WORDS_PER_SECOND = 0.5
AUDIO_MAX_WORDS_PER_SECOND = 5.0
AUDIO_MAX_REPEAT_RATIO = 0.30

SYNTH_MIN_BYTES = 10 * 1024
SYNTH_MIN_SECTIONS = 1  # at least one === SECTION === banner besides the header
SYNTH_HEADER_TOKEN = "=== SYNTHESIZED QUARTERLY UPDATE"
SYNTH_SECTION_RE = re.compile(r"^=== .+ ===\s*$")

TIMESTAMP_LINE_RE = re.compile(r"^\[(?P<start>\d+\.\d+)s -> (?P<end>\d+\.\d+)s\]\s?(?P<text>.*)$")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class QaStatus(str, Enum):  # noqa: UP042 - preserve serialized enum compatibility
    OK = "ok"
    FAILED = "failed"


class QaResult(BaseModel):
    status: QaStatus
    file_size_bytes: int
    line_count: int
    issues: list[str] = Field(default_factory=list)

    # Audio-flavor metrics (zero / None for synthesized)
    timestamped_line_count: int | None = None
    duration_covered_sec: float | None = None
    total_words: int | None = None
    words_per_second: float | None = None
    repeat_ratio: float | None = None

    # Synth-flavor metrics
    section_count: int | None = None
    header_present: bool | None = None


# ---------------------------------------------------------------------------
# Source-router
# ---------------------------------------------------------------------------


SYNTHESIZED_SOURCES: frozenset[str] = frozenset({"synthesized_text"})
# Aggregator-fetched Q&A files (source=aggregator_<name>) carry the same
# banner-header shape as synthesized files but no Whisper timestamps, so
# they route to the synthesized validator.
AGGREGATOR_SOURCE_PREFIX = "aggregator_"


def validate_transcript(path: Path, source: str) -> QaResult:
    """Dispatch to the right validator based on the registered source label."""
    if source in SYNTHESIZED_SOURCES or source.startswith(AGGREGATOR_SOURCE_PREFIX):
        return validate_synthesized_transcript(path)
    return validate_audio_transcript(path)


# ---------------------------------------------------------------------------
# Audio (Whisper) validator
# ---------------------------------------------------------------------------


def validate_audio_transcript(path: Path) -> QaResult:
    if not path.exists():
        return QaResult(
            status=QaStatus.FAILED,
            file_size_bytes=0,
            line_count=0,
            issues=[f"file does not exist: {path}"],
        )

    size = path.stat().st_size
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    line_count = len(lines)
    issues: list[str] = []

    if size < AUDIO_MIN_BYTES:
        issues.append(f"size {size} < {AUDIO_MIN_BYTES} bytes")
    if line_count < AUDIO_MIN_LINES:
        issues.append(f"line_count {line_count} < {AUDIO_MIN_LINES}")

    starts: list[float] = []
    ends: list[float] = []
    texts: list[str] = []
    for line in lines:
        m = TIMESTAMP_LINE_RE.match(line)
        if not m:
            continue
        starts.append(float(m.group("start")))
        ends.append(float(m.group("end")))
        texts.append(m.group("text").strip())

    timestamped = len(texts)
    if line_count > 0 and timestamped < line_count * AUDIO_MIN_TIMESTAMPED_FRACTION:
        issues.append(
            f"timestamped {timestamped}/{line_count} < {AUDIO_MIN_TIMESTAMPED_FRACTION:.0%}"
        )

    duration = 0.0
    words = 0
    wps = 0.0
    repeat_ratio = 0.0
    if timestamped > 0:
        duration = max(ends) - min(starts)
        if duration < AUDIO_MIN_DURATION_SEC:
            issues.append(f"duration_covered {duration:.0f}s < {AUDIO_MIN_DURATION_SEC}s")
        words = sum(len(t.split()) for t in texts)
        wps = (words / duration) if duration > 0 else 0.0
        if wps < AUDIO_MIN_WORDS_PER_SECOND:
            issues.append(f"words/sec {wps:.2f} < {AUDIO_MIN_WORDS_PER_SECOND}")
        if wps > AUDIO_MAX_WORDS_PER_SECOND:
            issues.append(f"words/sec {wps:.2f} > {AUDIO_MAX_WORDS_PER_SECOND}")
        if timestamped > 1:
            repeats = sum(1 for i in range(1, timestamped) if texts[i] == texts[i - 1])
            repeat_ratio = repeats / (timestamped - 1)
            if repeat_ratio > AUDIO_MAX_REPEAT_RATIO:
                issues.append(
                    f"repeat_ratio {repeat_ratio:.2f} > {AUDIO_MAX_REPEAT_RATIO} "
                    f"(possible Whisper hallucination)"
                )
    else:
        issues.append("no timestamped segments parsed")

    return QaResult(
        status=QaStatus.OK if not issues else QaStatus.FAILED,
        file_size_bytes=size,
        line_count=line_count,
        timestamped_line_count=timestamped,
        duration_covered_sec=duration,
        total_words=words,
        words_per_second=wps,
        repeat_ratio=repeat_ratio,
        issues=issues,
    )


# ---------------------------------------------------------------------------
# Synthesized validator
# ---------------------------------------------------------------------------


def validate_synthesized_transcript(path: Path) -> QaResult:
    if not path.exists():
        return QaResult(
            status=QaStatus.FAILED,
            file_size_bytes=0,
            line_count=0,
            issues=[f"file does not exist: {path}"],
        )

    size = path.stat().st_size
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    line_count = len(lines)
    issues: list[str] = []

    if size < SYNTH_MIN_BYTES:
        issues.append(f"size {size} < {SYNTH_MIN_BYTES} bytes")

    header_present = lines and lines[0].startswith(SYNTH_HEADER_TOKEN)
    if not header_present:
        issues.append(f"missing synthesizer header banner ({SYNTH_HEADER_TOKEN!r})")

    section_count = sum(1 for line in lines if SYNTH_SECTION_RE.match(line))
    # Subtract the header banner from the count so SECTIONS means "real content".
    real_sections = max(0, section_count - 1)
    if real_sections < SYNTH_MIN_SECTIONS:
        issues.append(f"section_count {real_sections} < {SYNTH_MIN_SECTIONS}")

    return QaResult(
        status=QaStatus.OK if not issues else QaStatus.FAILED,
        file_size_bytes=size,
        line_count=line_count,
        section_count=real_sections,
        header_present=bool(header_present),
        issues=issues,
    )
