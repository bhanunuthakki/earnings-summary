"""
src/index_manager.py
--------------------
Manages two indexes:
  - transcript_index.json   (legacy, transcript-only — preserved for compatibility)
  - document_index.json     (multi-doc-type: transcript, press_release, presentation)

Key: {TICKER}_{YEAR}_{QUARTER}_{doc_type}
     e.g. GOOG_2025_Q3_press_release
"""

import os
import json
import datetime
from alias_manager import resolve_ticker

PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
CACHE_DIR = os.path.join(PROJECT_ROOT, ".tmp")

# Legacy transcript-only index (kept for backward compat with fetch_audio_transcripts)
TRANSCRIPT_INDEX_PATH = os.path.join(CACHE_DIR, "transcript_index.json")

# New multi-doc-type index
DOCUMENT_INDEX_PATH = os.path.join(CACHE_DIR, "document_index.json")

# Transcript files land in transcripts/raw/ during fetch and are later promoted
# to transcripts/processed/. Both directories are searched when canonicalizing
# a bare filename so the index always points at a file that actually exists on
# disk (rather than a bare basename that only resolves with the right CWD).
TRANSCRIPTS_RAW_DIR = os.path.join(PROJECT_ROOT, "transcripts", "raw")
TRANSCRIPTS_PROCESSED_DIR = os.path.join(PROJECT_ROOT, "transcripts", "processed")

VALID_DOC_TYPES = {
    "transcript",
    "press_release",
    "presentation",
    "investor_update",
    "supplement",
    "event",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_dir() -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)


def _load(path: str) -> dict:
    _ensure_dir()
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump({}, f, indent=4)
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(path: str, data: dict) -> None:
    _ensure_dir()
    with open(path, "w") as f:
        json.dump(data, f, indent=4)


def _transcript_key(ticker: str, year, quarter: str) -> str:
    ticker = resolve_ticker(ticker)
    return f"{ticker.upper()}_{year}_{quarter.upper()}"


def _doc_key(ticker: str, year, quarter: str, doc_type: str) -> str:
    ticker = resolve_ticker(ticker)
    if doc_type not in VALID_DOC_TYPES:
        raise ValueError(f"Invalid doc_type '{doc_type}'. Must be one of {VALID_DOC_TYPES}")
    return f"{ticker.upper()}_{year}_{quarter.upper()}_{doc_type}"


def _canonicalize_transcript_filepath(filepath: str | None) -> str | None:
    """Resolve a bare transcript filename to its project-root-relative location.

    Callers historically passed `output_path.name` (a bare basename like
    `AMZN_Q1_2026.txt`); `process_ir_documents.py` then ran
    `Path(local_path).exists()` from project root and silently skipped every
    transcript because the basename doesn't resolve from there. We pin the
    stored path to `transcripts/{raw,processed}/<name>` so the index entry
    always resolves regardless of caller CWD.

    Pass-through cases:
      - None stays None (callers register a stub before the file lands).
      - A path that already contains a directory separator is trusted as-is.
    """
    if filepath is None:
        return None
    if os.sep in filepath or "/" in filepath:
        return filepath
    raw_candidate = os.path.join(TRANSCRIPTS_RAW_DIR, filepath)
    processed_candidate = os.path.join(TRANSCRIPTS_PROCESSED_DIR, filepath)
    if os.path.exists(raw_candidate):
        chosen = raw_candidate
    elif os.path.exists(processed_candidate):
        chosen = processed_candidate
    else:
        # Default to processed/ — the steady-state location after the raw→processed
        # promotion. Registering before the file exists is rare (fetchers register
        # right after writing) but if it happens, the value is at least correct
        # relative to project root.
        chosen = processed_candidate
    return os.path.relpath(chosen, PROJECT_ROOT).replace(os.sep, "/")


# ---------------------------------------------------------------------------
# Legacy transcript API (backward compatible)
# ---------------------------------------------------------------------------

def has_transcript(ticker: str, year, quarter: str) -> dict | None:
    """Returns the metadata dict if a transcript exists, else None."""
    index = _load(TRANSCRIPT_INDEX_PATH)
    return index.get(_transcript_key(ticker, year, quarter))


def register_transcript(
    ticker: str,
    year,
    quarter: str,
    source: str,
    filepath: str | None = None,
    has_qa: bool | None = None,
    qa_status: str | None = None,
    qa_details: dict | None = None,
) -> bool:
    index = _load(TRANSCRIPT_INDEX_PATH)
    key = _transcript_key(ticker, year, quarter)
    existing = index.get(key)

    # Don't overwrite FMP_API with MANUAL
    if existing and existing.get("source") == "FMP_API" and source == "MANUAL":
        source = "FMP_API"

    # Preserve existing has_qa if not provided
    updated_has_qa = has_qa
    if updated_has_qa is None and existing:
        updated_has_qa = existing.get("has_qa")

    # Preserve existing QA fields if caller didn't pass them — re-registering
    # for an unrelated reason (e.g. metadata fix) shouldn't blow away QA state.
    updated_qa_status = qa_status if qa_status is not None else (existing.get("qa_status") if existing else None)
    updated_qa_details = qa_details if qa_details is not None else (existing.get("qa_details") if existing else None)

    canonical_filepath = _canonicalize_transcript_filepath(filepath)

    index[key] = {
        "ticker": resolve_ticker(ticker).upper(),
        "year": str(year),
        "quarter": quarter.upper(),
        "source": source,
        "filepath": canonical_filepath,
        "indexed_at": existing["indexed_at"] if existing else datetime.datetime.now().isoformat(),
        "has_qa": updated_has_qa,
        "qa_status": updated_qa_status,
        "qa_details": updated_qa_details,
        "qa_checked_at": (
            datetime.datetime.now().isoformat()
            if qa_status is not None
            else (existing.get("qa_checked_at") if existing else None)
        ),
    }
    _save(TRANSCRIPT_INDEX_PATH, index)

    # Mirror into document_index as a transcript entry
    _register_document(
        ticker=ticker,
        year=year,
        quarter=quarter,
        doc_type="transcript",
        source=source,
        local_path=canonical_filepath,
        processed=True,  # Legacy flow already processed
    )
    return True


def update_transcript_qa(
    ticker: str,
    year,
    quarter: str,
    qa_status: str,
    qa_details: dict | None = None,
) -> bool:
    """Update only the QA fields of an existing transcript entry. Returns False
    if the entry does not exist (caller should register_transcript first)."""
    index = _load(TRANSCRIPT_INDEX_PATH)
    key = _transcript_key(ticker, year, quarter)
    if key not in index:
        return False
    index[key]["qa_status"] = qa_status
    index[key]["qa_details"] = qa_details
    index[key]["qa_checked_at"] = datetime.datetime.now().isoformat()
    _save(TRANSCRIPT_INDEX_PATH, index)
    return True


# ---------------------------------------------------------------------------
# New multi-doc-type API
# ---------------------------------------------------------------------------

def has_document(ticker: str, year, quarter: str, doc_type: str) -> dict | None:
    """Returns the metadata dict if a document exists in the index, else None."""
    index = _load(DOCUMENT_INDEX_PATH)
    return index.get(_doc_key(ticker, year, quarter, doc_type))


def _register_document(
    ticker: str,
    year,
    quarter: str,
    doc_type: str,
    source: str,
    local_path: str | None = None,
    ir_url: str | None = None,
    processed: bool = False,
    fiscal_label: str | None = None,
    note: str | None = None,
) -> bool:
    """
    Internal registration; also callable directly for IR documents.
    """
    index = _load(DOCUMENT_INDEX_PATH)
    key = _doc_key(ticker, year, quarter, doc_type)
    existing = index.get(key)

    index[key] = {
        "ticker": resolve_ticker(ticker).upper(),
        "year": str(year),
        "quarter": quarter.upper(),
        "doc_type": doc_type,
        "source": source,
        "ir_url": ir_url,
        "local_path": local_path,
        "fiscal_label": fiscal_label,
        "note": note,
        "processed": processed,
        "indexed_at": existing["indexed_at"] if existing else datetime.datetime.now().isoformat(),
        "updated_at": datetime.datetime.now().isoformat(),
    }
    _save(DOCUMENT_INDEX_PATH, index)
    return True


def register_ir_document(
    ticker: str,
    year,
    quarter: str,
    doc_type: str,
    ir_url: str,
    local_path: str | None = None,
    fiscal_label: str | None = None,
    note: str | None = None,
    processed: bool = False,
) -> bool:
    """
    Register a document sourced from an IR website.
    Idempotent — safe to call repeatedly; only updates if local_path or processed changes.
    """
    return _register_document(
        ticker=ticker,
        year=year,
        quarter=quarter,
        doc_type=doc_type,
        source="IR_WEBSITE",
        local_path=local_path,
        ir_url=ir_url,
        processed=processed,
        fiscal_label=fiscal_label,
        note=note,
    )


def register_manual_document(
    ticker: str,
    year,
    quarter: str,
    doc_type: str,
    local_path: str,
    fiscal_label: str | None = None,
    note: str | None = None,
    processed: bool = False,
) -> bool:
    """
    Register a document the user manually dropped into micro_thesis/sources/<TICKER>/.
    Distinct from register_ir_document because the source is MANUAL_DROP, not an IR URL.
    Idempotent.
    """
    return _register_document(
        ticker=ticker,
        year=year,
        quarter=quarter,
        doc_type=doc_type,
        source="MANUAL_DROP",
        local_path=local_path,
        ir_url=None,
        processed=processed,
        fiscal_label=fiscal_label,
        note=note,
    )


def _event_key(ticker: str, event_date: str, sha256: str) -> str:
    """Index key for non-quarterly events.

    Quarterly docs key on `(ticker, year, quarter, doc_type)` since each tuple has at most
    one artifact per kind. Events can have multiple distinct PDFs on the same date (deck
    + transcript + supplement etc.), so the sha256 prefix differentiates them.
    """
    return f"{resolve_ticker(ticker).upper()}_event_{event_date}_{sha256[:8]}"


def has_event(ticker: str, event_date: str, sha256: str) -> dict | None:
    index = _load(DOCUMENT_INDEX_PATH)
    return index.get(_event_key(ticker, event_date, sha256))


def register_event_document(
    ticker: str,
    event_date: str,
    local_path: str,
    sha256: str,
    confidence: float,
    note: str | None = None,
) -> bool:
    """Register a non-quarterly IR event artifact (investor day, AGM, capital markets day).

    Lives in document_index.json under a separate key namespace from quarterly docs;
    `doc_type` is fixed to "event". `event_date` is the ISO date the event occurred.
    """
    index = _load(DOCUMENT_INDEX_PATH)
    key = _event_key(ticker, event_date, sha256)
    existing = index.get(key)

    intake_note = f"intake sha={sha256[:8]} confidence={confidence:.2f}"
    full_note = f"{intake_note}; {note}" if note else intake_note

    index[key] = {
        "ticker": resolve_ticker(ticker).upper(),
        "event_date": event_date,
        "doc_type": "event",
        "source": "USER_INTAKE",
        "ir_url": None,
        "local_path": local_path,
        "fiscal_label": None,
        "note": full_note,
        "processed": False,
        "indexed_at": existing["indexed_at"] if existing else datetime.datetime.now().isoformat(),
        "updated_at": datetime.datetime.now().isoformat(),
    }
    _save(DOCUMENT_INDEX_PATH, index)
    return True


def mark_event_processed(ticker: str, event_date: str, sha8: str) -> bool:
    """Mark an event artifact as processed. `sha8` is the 8-character sha256 prefix
    that appears in the event's index key and on-disk filename."""
    index = _load(DOCUMENT_INDEX_PATH)
    key = f"{resolve_ticker(ticker).upper()}_event_{event_date}_{sha8}"
    if key not in index:
        return False
    index[key]["processed"] = True
    index[key]["updated_at"] = datetime.datetime.now().isoformat()
    _save(DOCUMENT_INDEX_PATH, index)
    return True


def get_unprocessed_events(ticker: str | None = None) -> list[dict]:
    """Return all registered but unprocessed event artifacts, optionally filtered by ticker."""
    index = _load(DOCUMENT_INDEX_PATH)
    result = [
        v for v in index.values()
        if v.get("doc_type") == "event"
        and not v.get("processed")
        and v.get("local_path")
    ]
    if ticker:
        ticker = resolve_ticker(ticker).upper()
        result = [v for v in result if v.get("ticker") == ticker]
    result.sort(key=lambda d: (d.get("ticker", ""), d.get("event_date", "")))
    return result


def register_user_intake_document(
    ticker: str,
    year,
    quarter: str,
    doc_type: str,
    local_path: str,
    sha256: str,
    confidence: float,
    note: str | None = None,
) -> bool:
    """
    Register a document filed by the user-intake handler (`src/intake.py`).

    Source is fixed to `USER_INTAKE` so downstream consumers can distinguish
    intake-handler ingests from manual-drop registrations (`MANUAL_DROP`),
    IR-website downloads (`IR_WEBSITE`), and FMP/SEC ingests.
    """
    intake_note = f"intake sha={sha256[:8]} confidence={confidence:.2f}"
    full_note = f"{intake_note}; {note}" if note else intake_note
    return _register_document(
        ticker=ticker,
        year=year,
        quarter=quarter,
        doc_type=doc_type,
        source="USER_INTAKE",
        local_path=local_path,
        ir_url=None,
        processed=False,
        fiscal_label=None,
        note=full_note,
    )


def mark_document_processed(ticker: str, year, quarter: str, doc_type: str) -> bool:
    """Mark a registered document as LLM-processed."""
    index = _load(DOCUMENT_INDEX_PATH)
    key = _doc_key(ticker, year, quarter, doc_type)
    if key not in index:
        return False
    index[key]["processed"] = True
    index[key]["updated_at"] = datetime.datetime.now().isoformat()
    _save(DOCUMENT_INDEX_PATH, index)
    return True


def get_documents_for_ticker(ticker: str) -> list[dict]:
    """Return all registered documents for a ticker, sorted by year/quarter/doc_type."""
    ticker = resolve_ticker(ticker).upper()
    index = _load(DOCUMENT_INDEX_PATH)
    docs = [v for v in index.values() if v.get("ticker") == ticker]
    docs.sort(key=lambda d: (d["year"], d["quarter"], d["doc_type"]))
    return docs


def get_unprocessed_documents(ticker: str | None = None) -> list[dict]:
    """Return all registered but unprocessed quarterly documents, optionally filtered by ticker.

    Events live in a separate keyspace (no year/quarter fields) — fetch via
    `get_unprocessed_events`.
    """
    index = _load(DOCUMENT_INDEX_PATH)
    result = [
        v for v in index.values()
        if v.get("doc_type") != "event"
        and not v.get("processed")
        and v.get("local_path")
    ]
    if ticker:
        ticker = resolve_ticker(ticker).upper()
        result = [v for v in result if v.get("ticker") == ticker]
    result.sort(key=lambda d: (d.get("ticker", ""), d["year"], d["quarter"], d["doc_type"]))
    return result
