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

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".tmp")

# Legacy transcript-only index (kept for backward compat with run_pipeline / fetch_audio_transcripts)
TRANSCRIPT_INDEX_PATH = os.path.join(CACHE_DIR, "transcript_index.json")

# New multi-doc-type index
DOCUMENT_INDEX_PATH = os.path.join(CACHE_DIR, "document_index.json")

VALID_DOC_TYPES = {"transcript", "press_release", "presentation"}


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

    index[key] = {
        "ticker": resolve_ticker(ticker).upper(),
        "year": str(year),
        "quarter": quarter.upper(),
        "source": source,
        "filepath": filepath,
        "indexed_at": existing["indexed_at"] if existing else datetime.datetime.now().isoformat(),
        "has_qa": updated_has_qa,
    }
    _save(TRANSCRIPT_INDEX_PATH, index)

    # Mirror into document_index as a transcript entry
    _register_document(
        ticker=ticker,
        year=year,
        quarter=quarter,
        doc_type="transcript",
        source=source,
        local_path=filepath,
        processed=True,  # Legacy flow already processed
    )
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
    """Return all registered but unprocessed documents, optionally filtered by ticker."""
    index = _load(DOCUMENT_INDEX_PATH)
    result = [v for v in index.values() if not v.get("processed") and v.get("local_path")]
    if ticker:
        ticker = resolve_ticker(ticker).upper()
        result = [v for v in result if v.get("ticker") == ticker]
    result.sort(key=lambda d: (d.get("ticker", ""), d["year"], d["quarter"], d["doc_type"]))
    return result
