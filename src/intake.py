"""src/intake.py

User-drop inbox. The single intake point for any IR document, transcript text, or
earnings-call audio the user wants the pipeline to consume.

Stages (per `directives/intake_documents.md`):
  1. Scan `_inbox/` for files (PDFs, .txt transcripts, audio).
  2. Classify each file into (ticker, period_end, doc_type) using filename hints
     plus an LLM call against the document's first pages.
  3. Compute sha256, file the artifact into the canonical layout:
       - PDF / TXT  →  ir_documents/<TICKER>/<period_end_iso>/ir_<doctype>__<sha8>.<ext>
       - Audio      →  transcripts/raw/<TICKER>_Q<n>_<YYYY>.<ext>  (legacy whisper path)
  4. Register in `document_index.json` with source = USER_INTAKE.
  5. Low-confidence or unparseable files stay in `_inbox/` next to a `.error.json`
     marker so the user can rename and retry.

This module is pure logic. The Layer 3 CLI is `execution/intake_documents.py`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sys
from datetime import date
from pathlib import Path
from typing import Final

from pydantic import BaseModel, Field, ValidationError, field_validator

import index_manager
from alias_manager import resolve_ticker
from llm_client import classify_intake_document
from models.documents import DocType
from parser import extract_text_from_pdf, read_text_file

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
INBOX_DIR: Final[Path] = PROJECT_ROOT / "_inbox"
IR_DOCS_DIR: Final[Path] = PROJECT_ROOT / "ir_documents"
EVENTS_DIR: Final[Path] = IR_DOCS_DIR / "_events"
TRANSCRIPTS_RAW_DIR: Final[Path] = PROJECT_ROOT / "transcripts" / "raw"

PDF_EXTS: Final[frozenset[str]] = frozenset({".pdf"})
TEXT_EXTS: Final[frozenset[str]] = frozenset({".txt"})
AUDIO_EXTS: Final[frozenset[str]] = frozenset({".mp3", ".m4a", ".wav"})

CONFIDENCE_THRESHOLD: Final[float] = 0.6
LLM_TEXT_BUDGET: Final[int] = 8000

# Mapping from the canonical DocType enum to on-disk filename stems. The leading
# `ir_` prefix matches the convention already in `ir_documents/<TICKER>/...`.
DOC_TYPE_FILE_STEM: Final[dict[DocType, str]] = {
    DocType.IR_PRESS_RELEASE: "ir_press_release",
    DocType.IR_PRESENTATION: "ir_presentation",
    DocType.IR_SUPPLEMENT: "ir_supplement",
    DocType.IR_INVESTOR_UPDATE: "ir_investor_update",
    DocType.EARNINGS_CALL_TRANSCRIPT: "ir_transcript",
    DocType.IR_EVENT: "ir_event",
}

# Mapping from DocType to the short keys index_manager.VALID_DOC_TYPES uses.
DOC_TYPE_INDEX_KEY: Final[dict[DocType, str]] = {
    DocType.IR_PRESS_RELEASE: "press_release",
    DocType.IR_PRESENTATION: "presentation",
    DocType.IR_SUPPLEMENT: "supplement",
    DocType.IR_INVESTOR_UPDATE: "investor_update",
    DocType.EARNINGS_CALL_TRANSCRIPT: "transcript",
    DocType.IR_EVENT: "event",
}

# DocTypes that are non-quarterly: stored under ir_documents/_events/<TICKER>/<event_date>/
# rather than ir_documents/<TICKER>/<period_end>/. Period_end on the classification
# represents the event date, not a fiscal-quarter end.
EVENT_DOC_TYPES: Final[frozenset[DocType]] = frozenset({DocType.IR_EVENT})

# Hints captured from filenames before any LLM call. Two orderings — `Q3 2025`
# and `2025 Q3` — both appear in user-supplied filenames.
QUARTER_YEAR_RE: Final[re.Pattern[str]] = re.compile(
    r"q[ -]?([1-4])[ _-]*?(20\d{2})|(20\d{2})[ _-]*?q[ -]?([1-4])",
    re.IGNORECASE,
)
TICKER_PREFIX_RE: Final[re.Pattern[str]] = re.compile(r"^([A-Za-z]{1,6})[ _\-]")

LOG_FORMAT = json.dumps({"level": "%(levelname)s", "ts": "%(asctime)s", "msg": "%(message)s"})
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stderr)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class FilenameHint(BaseModel):
    """Deterministic data extracted from filename — passed to the LLM as a hint."""

    ticker_hint: str | None = None
    quarter_hint: int | None = Field(default=None, ge=1, le=4)
    year_hint: int | None = Field(default=None, ge=2000, le=2099)


class IntakeClassification(BaseModel):
    """Schema for the LLM's classification response."""

    ticker: str = Field(min_length=1, max_length=10)
    period_end: date
    doc_type: DocType
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1, max_length=500)

    @field_validator("ticker")
    @classmethod
    def _resolve_ticker(cls, v: str) -> str:
        return resolve_ticker(v)


class IntakeResult(BaseModel):
    source: Path
    classification: IntakeClassification | None
    dest: Path | None
    sha256: str | None
    skipped: bool
    skip_reason: str | None


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def filename_hint(filename: str) -> FilenameHint:
    """Extract (ticker, quarter, year) hints from a filename. Data, not classification."""
    base = Path(filename).stem

    quarter_hint: int | None = None
    year_hint: int | None = None
    qy_match = QUARTER_YEAR_RE.search(base)
    if qy_match:
        if qy_match.group(1):
            quarter_hint = int(qy_match.group(1))
            year_hint = int(qy_match.group(2))
        else:
            quarter_hint = int(qy_match.group(4))
            year_hint = int(qy_match.group(3))

    ticker_match = TICKER_PREFIX_RE.match(base)
    ticker_hint = resolve_ticker(ticker_match.group(1)) if ticker_match else None

    return FilenameHint(
        ticker_hint=ticker_hint,
        quarter_hint=quarter_hint,
        year_hint=year_hint,
    )


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def quarter_str_for(period_end: date) -> str:
    """Calendar quarter label (Q1..Q4) for a period-end date."""
    return f"Q{((period_end.month - 1) // 3) + 1}"


def write_error_marker(source: Path, reason: str, hint: FilenameHint, text_sample: str) -> None:
    """Drop a `.error.json` next to the source so the user can diagnose and retry."""
    payload = {
        "reason": reason,
        "ticker_hint": hint.ticker_hint,
        "quarter_hint": hint.quarter_hint,
        "year_hint": hint.year_hint,
        "text_sample": text_sample[:200],
    }
    error_path = source.with_suffix(source.suffix + ".error.json")
    with open(error_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _clear_error_marker(source: Path) -> None:
    err = source.with_suffix(source.suffix + ".error.json")
    if err.exists():
        err.unlink()


# ---------------------------------------------------------------------------
# Classification + filing
# ---------------------------------------------------------------------------


def _classify(source: Path, full_text: str) -> tuple[IntakeClassification | None, str]:
    """Return (classification, skip_reason). Empty `skip_reason` means success."""
    hint = filename_hint(source.name)
    if not full_text.strip() and hint.ticker_hint is None:
        return None, "empty_no_filename_hint"

    raw = classify_intake_document(
        filename=source.name,
        text=full_text[:LLM_TEXT_BUDGET],
        hint=hint.model_dump(),
    )
    if raw is None:
        return None, "classification_failed"

    try:
        classification = IntakeClassification.model_validate(raw)
    except ValidationError as e:
        log.warning({"event": "classification_validation_failed", "file": source.name, "error": str(e)})
        return None, "validation_failed"

    if classification.confidence < CONFIDENCE_THRESHOLD:
        return classification, "low_confidence"

    return classification, ""


def _dest_path_for(classification: IntakeClassification, sha: str, file_ext: str) -> Path:
    """Compute the canonical filesystem destination for a classified artifact."""
    stem = DOC_TYPE_FILE_STEM[classification.doc_type]
    if classification.doc_type in EVENT_DOC_TYPES:
        folder = EVENTS_DIR / classification.ticker.upper() / classification.period_end.isoformat()
    else:
        folder = IR_DOCS_DIR / classification.ticker.upper() / classification.period_end.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{stem}__{sha[:8]}{file_ext}"


def _register_classified(
    classification: IntakeClassification,
    dest: Path,
    sha: str,
) -> None:
    """Write the index entry. Quarterly docs go via the standard register path;
    events use the event-keyed register since they have no fiscal quarter."""
    doc_type_key = DOC_TYPE_INDEX_KEY[classification.doc_type]
    if classification.doc_type in EVENT_DOC_TYPES:
        index_manager.register_event_document(
            ticker=classification.ticker,
            event_date=classification.period_end.isoformat(),
            local_path=str(dest),
            sha256=sha,
            confidence=classification.confidence,
            note=classification.reasoning,
        )
        return
    index_manager.register_user_intake_document(
        ticker=classification.ticker,
        year=str(classification.period_end.year),
        quarter=quarter_str_for(classification.period_end),
        doc_type=doc_type_key,
        local_path=str(dest),
        sha256=sha,
        confidence=classification.confidence,
        note=classification.reasoning,
    )


def _file_to_ir_layout(
    source: Path,
    classification: IntakeClassification,
    file_ext: str,
    dry_run: bool,
) -> IntakeResult:
    """Move `source` to the canonical IR location and register it in the index."""
    sha = sha256_of(source)
    dest = _dest_path_for(classification, sha, file_ext)

    if dest.exists():
        log.info({"event": "duplicate_skip", "source": source.name, "dest": str(dest)})
        if not dry_run:
            source.unlink()
            _clear_error_marker(source)
        return IntakeResult(
            source=source, classification=classification, dest=dest,
            sha256=sha, skipped=True, skip_reason="duplicate",
        )

    if dry_run:
        log.info({"event": "dry_run_plan", "source": source.name, "dest": str(dest)})
        return IntakeResult(
            source=source, classification=classification, dest=dest,
            sha256=sha, skipped=False, skip_reason=None,
        )

    shutil.move(str(source), str(dest))
    _register_classified(classification, dest, sha)
    _clear_error_marker(source)
    log.info({
        "event": "intake_filed",
        "source": source.name,
        "dest": str(dest),
        "doc_type": classification.doc_type.value,
        "ticker": classification.ticker,
    })
    return IntakeResult(
        source=source, classification=classification, dest=dest,
        sha256=sha, skipped=False, skip_reason=None,
    )


# ---------------------------------------------------------------------------
# Per-extension intake routes
# ---------------------------------------------------------------------------


def intake_pdf(source: Path, dry_run: bool = False) -> IntakeResult:
    full_text = ""
    try:
        full_text = extract_text_from_pdf(str(source))
    except Exception as e:
        log.error({"event": "pdf_extract_failed", "file": source.name, "error": str(e)})

    classification, skip = _classify(source, full_text)
    if skip:
        write_error_marker(source, skip, filename_hint(source.name), full_text[:200])
        return IntakeResult(
            source=source, classification=classification, dest=None,
            sha256=None, skipped=True, skip_reason=skip,
        )
    assert classification is not None  # narrowed by `not skip`
    return _file_to_ir_layout(source, classification, ".pdf", dry_run)


def intake_text_transcript(source: Path, dry_run: bool = False) -> IntakeResult:
    full_text = read_text_file(str(source))
    classification, skip = _classify(source, full_text)
    if skip:
        write_error_marker(source, skip, filename_hint(source.name), full_text[:200])
        return IntakeResult(
            source=source, classification=classification, dest=None,
            sha256=None, skipped=True, skip_reason=skip,
        )
    assert classification is not None
    return _file_to_ir_layout(source, classification, source.suffix.lower(), dry_run)


def intake_audio(source: Path, dry_run: bool = False) -> IntakeResult:
    """Audio routes to `transcripts/raw/` so the existing whisper pipeline can pick it up.

    We rely on filename hints only — running whisper at intake time would block the
    whole batch on a CPU-bound transcription. Audio classification by content is the
    legacy pipeline's job.
    """
    hint = filename_hint(source.name)
    if hint.ticker_hint is None or hint.quarter_hint is None or hint.year_hint is None:
        write_error_marker(source, "audio_filename_unclassifiable", hint, "")
        return IntakeResult(
            source=source, classification=None, dest=None,
            sha256=None, skipped=True, skip_reason="audio_filename_unclassifiable",
        )

    TRANSCRIPTS_RAW_DIR.mkdir(parents=True, exist_ok=True)
    dest = TRANSCRIPTS_RAW_DIR / f"{hint.ticker_hint}_Q{hint.quarter_hint}_{hint.year_hint}{source.suffix.lower()}"

    if dest.exists():
        log.info({"event": "duplicate_skip", "source": source.name, "dest": str(dest)})
        if not dry_run:
            source.unlink()
            _clear_error_marker(source)
        return IntakeResult(
            source=source, classification=None, dest=dest,
            sha256=None, skipped=True, skip_reason="duplicate",
        )

    if dry_run:
        log.info({"event": "dry_run_plan", "source": source.name, "dest": str(dest)})
        return IntakeResult(
            source=source, classification=None, dest=dest,
            sha256=None, skipped=False, skip_reason=None,
        )

    shutil.move(str(source), str(dest))
    _clear_error_marker(source)
    log.info({"event": "audio_filed", "source": source.name, "dest": str(dest)})
    return IntakeResult(
        source=source, classification=None, dest=dest,
        sha256=None, skipped=False, skip_reason=None,
    )


# ---------------------------------------------------------------------------
# Top-level scan
# ---------------------------------------------------------------------------


def scan_inbox(inbox: Path = INBOX_DIR, dry_run: bool = False) -> list[IntakeResult]:
    """Walk `inbox` and route each supported file. Returns one IntakeResult per file."""
    if not inbox.exists():
        log.warning({"event": "inbox_not_found", "path": str(inbox)})
        return []

    results: list[IntakeResult] = []
    for child in sorted(inbox.iterdir()):
        if child.is_dir() or child.name.startswith("."):
            continue
        # Skip companion error markers and stray JSON
        if child.suffixes[-2:] == [".pdf", ".error.json"] or child.suffix == ".json":
            continue

        ext = child.suffix.lower()
        if ext in PDF_EXTS:
            results.append(intake_pdf(child, dry_run=dry_run))
        elif ext in AUDIO_EXTS:
            results.append(intake_audio(child, dry_run=dry_run))
        elif ext in TEXT_EXTS:
            results.append(intake_text_transcript(child, dry_run=dry_run))
        else:
            log.info({"event": "skip_unsupported", "file": child.name, "ext": ext})

    return results
