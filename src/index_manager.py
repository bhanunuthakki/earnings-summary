"""
src/index_manager.py
--------------------
Manages two indexes:
  - transcript_index.json   (legacy, transcript-only — preserved for compatibility)
  - document_index.json     (multi-doc-type: transcript, press_release, presentation)

Key: {TICKER}_{YEAR}_{QUARTER}_{doc_type}
     e.g. GOOG_2025_Q3_press_release
"""

import datetime
import json
import os
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast

from alias_manager import resolve_ticker
from pipeline.transcript_acquisition import AuthorizedTranscriptArtifact

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

IndexEntry: TypeAlias = dict[str, object]
DocumentIndex: TypeAlias = dict[str, IndexEntry]
_DOCUMENT_INDEX_CACHE_LOCK = threading.Lock()
_DOCUMENT_INDEX_READ_CACHE: tuple[str, int, int, DocumentIndex] | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ensure_dir(path: str | None = None) -> None:
    os.makedirs(str(Path(path).parent) if path is not None else CACHE_DIR, exist_ok=True)


def _load(path: str) -> dict[str, dict[str, Any]]:
    _ensure_dir(path)
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump({}, f, indent=4)
        return {}
    try:
        with open(path) as f:
            payload = json.load(f)
        return cast("dict[str, dict[str, Any]]", payload) if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _save(path: str, data: dict[str, dict[str, Any]]) -> None:
    global _DOCUMENT_INDEX_READ_CACHE
    _ensure_dir(path)
    with open(path, "w") as f:
        json.dump(data, f, indent=4)
    if os.path.abspath(path) == os.path.abspath(DOCUMENT_INDEX_PATH):
        with _DOCUMENT_INDEX_CACHE_LOCK:
            _DOCUMENT_INDEX_READ_CACHE = None


def _document_index_snapshot(document_index_path: Path | None = None) -> DocumentIndex:
    """Parse the legacy document index once per unchanged file signature.

    ``categorize_ir_uploads --ticker`` asks about every canonical document for
    that issuer.  Re-reading and JSON-decoding the multi-megabyte index for
    every lookup made the hourly onboarding path CPU-bound for minutes.  Size
    plus nanosecond mtime invalidates external writers; in-process saves also
    clear the snapshot explicitly.
    """
    global _DOCUMENT_INDEX_READ_CACHE
    path = str(document_index_path) if document_index_path is not None else DOCUMENT_INDEX_PATH
    try:
        stat = os.stat(path)
        signature = (stat.st_mtime_ns, stat.st_size)
    except FileNotFoundError:
        signature = (-1, -1)
    with _DOCUMENT_INDEX_CACHE_LOCK:
        cached = _DOCUMENT_INDEX_READ_CACHE
        if cached is not None and cached[:3] == (path, *signature):
            return cached[3]

    data = cast(DocumentIndex, _load(path))
    stat = os.stat(path)
    with _DOCUMENT_INDEX_CACHE_LOCK:
        _DOCUMENT_INDEX_READ_CACHE = (path, stat.st_mtime_ns, stat.st_size, data)
    return data


def _transcript_key(ticker: str, year: int | str, quarter: str) -> str:
    ticker = resolve_ticker(ticker)
    return f"{ticker.upper()}_{year}_{quarter.upper()}"


def _doc_key(ticker: str, year: int | str, quarter: str, doc_type: str) -> str:
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


def has_transcript(ticker: str, year: int | str, quarter: str) -> dict[str, Any] | None:
    """Returns the metadata dict if a transcript exists, else None."""
    index = _load(TRANSCRIPT_INDEX_PATH)
    return index.get(_transcript_key(ticker, year, quarter))


def peek_transcript(ticker: str, year: int, quarter: str) -> IndexEntry | None:
    """Read transcript metadata without creating or updating an index file."""

    if not os.path.exists(TRANSCRIPT_INDEX_PATH):
        return None
    try:
        with open(TRANSCRIPT_INDEX_PATH, encoding="utf-8") as handle:
            payload = cast(object, json.load(handle))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    entry = cast(DocumentIndex, payload).get(_transcript_key(ticker, year, quarter))
    return entry if isinstance(entry, dict) else None


def _register_transcript(
    ticker: str,
    year: int | str,
    quarter: str,
    source: str,
    filepath: str | None = None,
    has_qa: bool | None = None,
    qa_status: str | None = None,
    qa_details: dict[str, Any] | None = None,
    acquisition_receipt: IndexEntry | None = None,
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
    updated_qa_status = (
        qa_status if qa_status is not None else (existing.get("qa_status") if existing else None)
    )
    updated_qa_details = (
        qa_details if qa_details is not None else (existing.get("qa_details") if existing else None)
    )
    existing_acquisition_receipt = (
        cast(object, existing.get("acquisition_receipt")) if existing else None
    )
    updated_acquisition_receipt: IndexEntry | None = (
        acquisition_receipt
        if acquisition_receipt is not None
        else (
            cast(IndexEntry, existing_acquisition_receipt)
            if isinstance(existing_acquisition_receipt, dict)
            else None
        )
    )

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
        "acquisition_receipt": updated_acquisition_receipt,
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


def register_transcript(
    ticker: str,
    year: int,
    quarter: str,
    source: str,
    *,
    acquisition_receipt: AuthorizedTranscriptArtifact,
    filepath: str | None = None,
    has_qa: bool | None = None,
    qa_status: str | None = None,
    qa_details: dict[str, object] | None = None,
) -> bool:
    """Register only transcript metadata carrying a validated acquisition receipt."""

    artifact = AuthorizedTranscriptArtifact.model_validate(acquisition_receipt, strict=True)
    request = artifact.authorization.request
    expected_quarter = f"Q{request.fiscal_quarter}"
    if (
        resolve_ticker(ticker).upper() != request.canonical_ticker
        or int(year) != request.fiscal_year
        or quarter.upper() != expected_quarter
        or source != "issuer_ir"
    ):
        raise ValueError("transcript index metadata does not match acquisition receipt")
    return _register_transcript(
        ticker,
        year,
        quarter,
        source,
        filepath=filepath,
        has_qa=has_qa,
        qa_status=qa_status,
        qa_details=qa_details,
        acquisition_receipt=artifact.model_dump(mode="json"),
    )


def update_transcript_qa(
    ticker: str,
    year: int | str,
    quarter: str,
    qa_status: str,
    qa_details: dict[str, Any] | None = None,
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


def update_local_path(
    ticker: str,
    year: int | str,
    quarter: str,
    doc_type: str,
    new_path: str,
) -> bool:
    """Rewrite the on-disk location for an existing index entry.

    Returns True if any index was touched. The transcript flow writes to both
    `transcript_index.json` (legacy) and `document_index.json` (multi-doctype),
    so the raw→processed promoter calls this for `doc_type="transcript"` to
    keep both stores in sync. `new_path` is expected to be project-root
    relative with forward slashes.
    """
    touched = False
    if doc_type == "transcript":
        t_index = _load(TRANSCRIPT_INDEX_PATH)
        t_key = _transcript_key(ticker, year, quarter)
        if t_key in t_index:
            t_index[t_key]["filepath"] = new_path
            _save(TRANSCRIPT_INDEX_PATH, t_index)
            touched = True
    d_index = _load(DOCUMENT_INDEX_PATH)
    d_key = _doc_key(ticker, year, quarter, doc_type)
    if d_key in d_index:
        d_index[d_key]["local_path"] = new_path
        d_index[d_key]["updated_at"] = datetime.datetime.now().isoformat()
        _save(DOCUMENT_INDEX_PATH, d_index)
        touched = True
    return touched


# ---------------------------------------------------------------------------
# New multi-doc-type API
# ---------------------------------------------------------------------------


def has_document(
    ticker: str,
    year: int | str,
    quarter: str,
    doc_type: str,
    *,
    document_index_path: Path | None = None,
) -> dict[str, Any] | None:
    """Returns the metadata dict if a document exists in the index, else None."""
    entry = _document_index_snapshot(document_index_path).get(
        _doc_key(ticker, year, quarter, doc_type)
    )
    # Do not expose the cached object to callers that may mutate the returned
    # metadata. This preserves the old fresh-parse read semantics.
    return dict(entry) if isinstance(entry, dict) else None


def _register_document(
    ticker: str,
    year: int | str,
    quarter: str,
    doc_type: str,
    source: str,
    local_path: str | None = None,
    ir_url: str | None = None,
    processed: bool = False,
    fiscal_label: str | None = None,
    note: str | None = None,
    *,
    document_index_path: Path | None = None,
) -> bool:
    """
    Internal registration; also callable directly for IR documents.
    """
    path = str(document_index_path) if document_index_path is not None else DOCUMENT_INDEX_PATH
    index = _load(path)
    key = _doc_key(ticker, year, quarter, doc_type)
    existing = index.get(key)
    indexed_at = existing.get("indexed_at") if isinstance(existing, dict) else None

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
        "indexed_at": indexed_at or datetime.datetime.now().isoformat(),
        "updated_at": datetime.datetime.now().isoformat(),
    }
    _save(path, index)
    return True


@dataclass(frozen=True)
class IRDocumentProjection:
    """Verified document metadata for the legacy quarter/type alias."""

    ticker: str
    year: int
    quarter: str
    doc_type: str
    ir_url: str
    local_path: str
    fiscal_label: str | None


def _valid_ir_alias(entry: object, key: str) -> bool:
    if not isinstance(entry, dict):
        return False
    record = cast("dict[str, Any]", entry)
    if not all(
        isinstance(record.get(field), str) and record[field]
        for field in (
            "ticker",
            "year",
            "quarter",
            "doc_type",
            "source",
            "local_path",
            "indexed_at",
            "updated_at",
        )
    ) or not isinstance(record.get("processed"), bool):
        return False
    url = record.get("ir_url")
    if not (
        (isinstance(url, str) and bool(url))
        or (
            "ir_url" in record
            and url is None
            and record["source"] in {"MANUAL_DROP", "USER_INTAKE"}
        )
    ):
        return False
    if (
        _doc_key(record["ticker"], record["year"], record["quarter"], record["doc_type"]) != key
        or "fiscal_label" not in record
        or (record["fiscal_label"] is not None and not isinstance(record["fiscal_label"], str))
    ):
        return False
    try:
        datetime.datetime.fromisoformat(record["indexed_at"])
        datetime.datetime.fromisoformat(record["updated_at"])
    except ValueError:
        return False
    return True


IRProjectionDisposition: TypeAlias = Literal[
    "coherent", "repaired", "alias_held_by_other_document", "repair_required"
]


def project_ir_document(
    candidates: Sequence[IRDocumentProjection],
    *,
    document_index_path: Path,
    newly_published: IRDocumentProjection | None = None,
    allow_repair: bool = True,
) -> IRProjectionDisposition:
    """Preserve a coherent replay alias; repair only an unambiguous missing alias.

    The alias does not prove that every candidate has a completed narrative.
    A newly executed publication retains the existing registration behavior.
    """
    distinct = tuple(dict.fromkeys(candidates))
    if not distinct or (newly_published is not None and newly_published not in distinct):
        raise ValueError("compatibility_projection_candidates_invalid")
    keys = {_doc_key(item.ticker, item.year, item.quarter, item.doc_type) for item in distinct}
    if len(keys) != 1:
        raise ValueError("compatibility_projection_coordinates_mismatch")
    key = keys.pop()
    try:
        payload = json.loads(document_index_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        payload = {}
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("compatibility_projection_index_corrupt") from exc
    if not isinstance(payload, dict):
        raise ValueError("compatibility_projection_index_corrupt")
    index = cast("dict[str, dict[str, Any]]", payload)
    incumbent = index.get(key)
    valid_incumbent = _valid_ir_alias(incumbent, key)
    selected = newly_published
    for candidate in distinct:
        expected = {
            "ticker": resolve_ticker(candidate.ticker).upper(),
            "year": str(candidate.year),
            "quarter": candidate.quarter.upper(),
            "doc_type": candidate.doc_type,
            "source": "IR_WEBSITE",
            "ir_url": candidate.ir_url,
            "local_path": candidate.local_path,
            "fiscal_label": candidate.fiscal_label,
        }
        if (
            valid_incumbent
            and isinstance(incumbent, dict)
            and (newly_published is None or candidate == newly_published)
            and all(incumbent.get(name) == value for name, value in expected.items())
        ):
            return "coherent"
    if selected is None:
        if valid_incumbent:
            return "alias_held_by_other_document"
        if len(distinct) != 1:
            raise ValueError("compatibility_projection_ambiguous")
        selected = distinct[0]
    if not allow_repair:
        return "repair_required"
    if not register_ir_document(
        ticker=selected.ticker,
        year=selected.year,
        quarter=selected.quarter,
        doc_type=selected.doc_type,
        ir_url=selected.ir_url,
        local_path=selected.local_path,
        fiscal_label=selected.fiscal_label,
        note="managed_issuer_document_publication",
        processed=False,
        document_index_path=document_index_path,
    ):
        raise ValueError("compatibility_projection_write_failed")
    return "repaired"


def register_ir_document(
    ticker: str,
    year: int | str,
    quarter: str,
    doc_type: str,
    ir_url: str,
    local_path: str | None = None,
    fiscal_label: str | None = None,
    note: str | None = None,
    processed: bool = False,
    *,
    document_index_path: Path | None = None,
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
        document_index_path=document_index_path,
    )


def register_manual_document(
    ticker: str,
    year: int | str,
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


def has_event(ticker: str, event_date: str, sha256: str) -> dict[str, Any] | None:
    index = _load(DOCUMENT_INDEX_PATH)
    return index.get(_event_key(ticker, event_date, sha256))


def register_event_document(
    ticker: str,
    event_date: str,
    local_path: str,
    sha256: str,
    confidence: float,
    note: str | None = None,
    *,
    document_index_path: Path | None = None,
) -> bool:
    """Register a non-quarterly IR event artifact (investor day, AGM, capital markets day).

    Lives in document_index.json under a separate key namespace from quarterly docs;
    `doc_type` is fixed to "event". `event_date` is the ISO date the event occurred.
    """
    path = str(document_index_path) if document_index_path is not None else DOCUMENT_INDEX_PATH
    index = _load(path)
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
    _save(path, index)
    return True


def register_user_intake_document(
    ticker: str,
    year: int | str,
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


def mark_document_processed(
    ticker: str,
    year: int | str,
    quarter: str,
    doc_type: str,
    *,
    document_index_path: Path | None = None,
) -> bool:
    """Mark a registered document as LLM-processed."""
    path = str(document_index_path) if document_index_path is not None else DOCUMENT_INDEX_PATH
    index = _load(path)
    key = _doc_key(ticker, year, quarter, doc_type)
    if key not in index:
        return False
    index[key]["processed"] = True
    index[key]["updated_at"] = datetime.datetime.now().isoformat()
    _save(path, index)
    return True


def get_documents_for_ticker(
    ticker: str, *, document_index_path: Path | None = None, quarterly_only: bool = False
) -> list[dict[str, Any]]:
    """Return all registered documents for a ticker, sorted by year/quarter/doc_type."""
    ticker = resolve_ticker(ticker).upper()
    path = str(document_index_path) if document_index_path is not None else DOCUMENT_INDEX_PATH
    index = _load(path)
    docs = [
        v
        for v in index.values()
        if v.get("ticker") == ticker and (not quarterly_only or v.get("doc_type") != "event")
    ]
    docs.sort(key=lambda d: (d["year"], d["quarter"], d["doc_type"]))
    return docs


def get_unprocessed_documents(
    ticker: str | None = None, *, document_index_path: Path | None = None
) -> list[dict[str, Any]]:
    """Return all registered but unprocessed quarterly documents, optionally filtered by ticker.

    Raw events retain their separate registration keyspace and are excluded
    from quarterly summary synthesis.
    """
    path = str(document_index_path) if document_index_path is not None else DOCUMENT_INDEX_PATH
    index = _load(path)
    result = [
        v
        for v in index.values()
        if v.get("doc_type") != "event" and not v.get("processed") and v.get("local_path")
    ]
    if ticker:
        ticker = resolve_ticker(ticker).upper()
        result = [v for v in result if v.get("ticker") == ticker]
    result.sort(key=lambda d: (d.get("ticker", ""), d["year"], d["quarter"], d["doc_type"]))
    return result
