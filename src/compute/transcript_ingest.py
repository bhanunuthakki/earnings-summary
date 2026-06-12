"""Register on-disk earnings transcripts as `documents` + `transcripts` + `transcript_segments`.

Walks `transcripts/processed/` and `transcripts/raw/` for files matching
`<TICKER>_Q<N>_<YYYY>.{pdf,txt}`, computes sha256 (idempotency key), and emits:
  - one `documents` row (source_type='transcript_audio', doc_type='earnings_call_transcript'),
  - one `transcripts` row,
  - one `transcript_segments` row per detected speaker turn (preserves speaker names).

Period mapping is calendar-year by default; per-ticker overrides (VEEV/RBRK Jan FYE,
TOL Oct FYE) are coded in `_FYE_OFFSETS`. Speaker detection is regex-based with
explicit named patterns — no substring matching, no LLM.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pypdf import PdfReader

from models.documents import DocType, FetchStatus, SourceType
from models.facts import FiscalPeriodType

_FILENAME_RE = re.compile(r"^(?P<ticker>[A-Z][A-Z0-9.]*)_Q(?P<q>[1-4])_(?P<year>20\d{2})$")

# (month, day, year_offset_from_fiscal_label)
_CALENDAR_QUARTER_END: dict[int, tuple[int, int, int]] = {
    1: (3, 31, 0),
    2: (6, 30, 0),
    3: (9, 30, 0),
    4: (12, 31, 0),
}

# Tickers with non-calendar fiscal year-ends. The fiscal label "Q1 2026" for
# Jan-FYE means quarter ending Apr 30 2025. Calendar tickers use the default.
_JAN_FYE_OFFSETS: dict[int, tuple[int, int, int]] = {
    1: (4, 30, -1),
    2: (7, 31, -1),
    3: (10, 31, -1),
    4: (1, 31, 0),
}
_OCT_FYE_OFFSETS: dict[int, tuple[int, int, int]] = {
    1: (1, 31, 0),
    2: (4, 30, 0),
    3: (7, 31, 0),
    4: (10, 31, 0),
}

_FYE_OFFSETS: dict[str, dict[int, tuple[int, int, int]]] = {
    "VEEV": _JAN_FYE_OFFSETS,
    "RBRK": _JAN_FYE_OFFSETS,
    "TOL": _OCT_FYE_OFFSETS,
    "AMAT": _OCT_FYE_OFFSETS,
}


@dataclass(frozen=True)
class ParsedFilename:
    ticker: str
    quarter_idx: int
    fiscal_year_label: int


@dataclass(frozen=True)
class PeriodMapping:
    period_end: datetime
    fiscal_period_type: FiscalPeriodType


def parse_transcript_filename(path: Path) -> ParsedFilename | None:
    """Match `<TICKER>_Q<N>_<YYYY>.{pdf,txt}`. Returns None on miss (e.g., master PDFs)."""
    m = _FILENAME_RE.match(path.stem)
    if m is None:
        return None
    return ParsedFilename(
        ticker=m.group("ticker"),
        quarter_idx=int(m.group("q")),
        fiscal_year_label=int(m.group("year")),
    )


def map_to_period(parsed: ParsedFilename) -> PeriodMapping:
    """Resolve (ticker, Qn, year_label) to a calendar period_end and fiscal_period_type."""
    offsets = _FYE_OFFSETS.get(parsed.ticker, _CALENDAR_QUARTER_END)
    month, day, year_offset = offsets[parsed.quarter_idx]
    period_end = datetime(parsed.fiscal_year_label + year_offset, month, day)
    fiscal_period_type = FiscalPeriodType(f"Q{parsed.quarter_idx}")
    return PeriodMapping(period_end=period_end, fiscal_period_type=fiscal_period_type)


def sha256_of(path: Path) -> str:
    """sha256 of file bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_transcript_text(path: Path) -> str:
    """Extract text from a transcript file (PDF or TXT). Raises on empty output."""
    if path.suffix.lower() == ".pdf":
        reader = PdfReader(str(path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    elif path.suffix.lower() == ".txt":
        with open(path, encoding="utf-8") as f:
            text = f.read()
    else:
        raise ValueError(f"Unsupported transcript suffix: {path.suffix}")
    if not text.strip():
        raise ValueError(f"Empty text extracted from {path}")
    return text


# Speaker patterns. Two conventions across the corpus:
#   (a) "...end of prior sentence.\n David Velez : Thank you, ..."  (NU IR PDFs)
#   (b) "...end.\n\nSundar Pichai\nThanks Jim, ..."                  (other PDFs)
# Required: name appears at line start (with optional leading whitespace) and is
# either (i) followed by a colon + whitespace (inline body, case a), or
# (ii) followed by a newline (body next paragraph, case b).
# Real speaker names are multi-word ("Sundar Pichai", "David Velez"), so we
# require >=2 capitalized tokens to exclude single-word sentence starters
# ("This:", "Studio", "Total"). "Operator" is handled by a separate pattern.
_SPEAKER_NAME_PATTERN = (
    r"[A-Z][A-Za-z]+"  # first capitalized word
    r"(?:\s+[A-Z][A-Za-z'\-]+){1,3}"  # 1-3 more capitalized tokens
)
_OPERATOR_RE = re.compile(r"(?:^|\n)[ \t]*(?P<name>Operator|OPERATOR)[ \t]*(?::[ \t]+|\n)")
_SPEAKER_RE = re.compile(rf"(?:^|\n)[ \t]*(?P<name>{_SPEAKER_NAME_PATTERN})[ \t]*(?::[ \t]+|\n)")


def _normalize_speaker(name: str) -> str:
    """Collapse internal whitespace runs (PDF extraction artifact) and trim."""
    return re.sub(r"\s+", " ", name).strip()


# Per-ticker whitelist of valid speakers. Tickers whose transcripts mention
# product names ("Wix Payments", "AI Mode", "YouTube Music") in capital-word
# form get false-positive speaker matches; restricting to the actual on-call
# executives cuts the noise. "Operator" is always allowed (distinct regex).
# Tickers absent from this map: no filter; all detected names accepted.
_KNOWN_SPEAKERS: dict[str, frozenset[str]] = {
    "WIX": frozenset(
        {
            "Avishai Abrahami",
            "Lior Shemesh",
            "Nir Zohar",
            "Maggie O'Donnell",
            "Maggie ODonnell",
        }
    ),
    "GOOG": frozenset(
        {
            "Sundar Pichai",
            "Anant Ashkenazi",
            "Philipp Schindler",
            "Ruth Porat",
            "Jim Friedland",
        }
    ),
}


def known_speakers_for(ticker: str) -> frozenset[str] | None:
    """Return the whitelist of valid speakers for ticker, or None if unrestricted."""
    return _KNOWN_SPEAKERS.get(ticker.upper())


@dataclass(frozen=True)
class SpeakerTurn:
    speaker: str | None
    text: str


@dataclass(frozen=True)
class _SpeakerMatch:
    """A regex match for one speaker turn header."""

    name_start: int
    name_end: int
    body_start: int
    name: str


def segment_by_speaker(
    text: str, *, known_speakers: frozenset[str] | None = None
) -> list[SpeakerTurn]:
    """Split text into speaker turns; falls back to one anonymous turn if no speakers detected.

    If `known_speakers` is provided, regex matches whose normalized name is not
    in the set are dropped (their text merges into the prior turn). "Operator"
    is always retained regardless of the whitelist.
    """
    matches: list[_SpeakerMatch] = []
    for m in _OPERATOR_RE.finditer(text):
        matches.append(
            _SpeakerMatch(
                name_start=m.start("name"),
                name_end=m.end("name"),
                body_start=m.end(),
                name=m.group("name"),
            )
        )
    for m in _SPEAKER_RE.finditer(text):
        ns = m.start("name")
        if any(sm.name_start == ns for sm in matches):
            continue
        candidate = _normalize_speaker(m.group("name"))
        if known_speakers is not None and candidate not in known_speakers:
            continue
        matches.append(
            _SpeakerMatch(
                name_start=ns,
                name_end=m.end("name"),
                body_start=m.end(),
                name=m.group("name"),
            )
        )
    matches.sort(key=lambda sm: sm.name_start)

    if not matches:
        stripped = text.strip()
        return [SpeakerTurn(speaker=None, text=stripped)] if stripped else []

    turns: list[SpeakerTurn] = []
    if matches[0].name_start > 0:
        prefix = text[: matches[0].name_start].strip()
        if prefix:
            turns.append(SpeakerTurn(speaker=None, text=prefix))

    for i, sm in enumerate(matches):
        end = matches[i + 1].name_start if i + 1 < len(matches) else len(text)
        body = text[sm.body_start : end].strip()
        if body:
            turns.append(SpeakerTurn(speaker=_normalize_speaker(sm.name), text=body))
    return turns


# --------------------------------------------------------------------------
# Q&A section detection
# --------------------------------------------------------------------------
#
# Earnings transcripts come in two flavors:
#   (a) full call: prepared remarks + analyst Q&A
#   (b) prepared-remarks only: IR-published PDF that ends at the hand-off
#       ("Operator -- we are now ready for questions.") with no Q&A content.
#
# Downstream consumers (Say-Do extraction, analyst-question KPI mining,
# commitments tracker) silently produce worse output on (b). We detect which
# kind we have via three structural signals against established transcript
# conventions, then persist the verdict on `transcripts.has_qa_section`.
#
# All signals are regex against well-defined boundary markers. No fuzzy
# substring scoring, no LLM.

# CallStreet/FactSet PDFs print this exact uppercase header at the Q&A boundary.
_QA_HEADER_RE = re.compile(r"(?:^|\n)\s*QUESTION\s+AND\s+ANSWER\s+SECTION\b")

# CallStreet analyst-question tag, e.g. "<Q - Eric Cha - Goldman Sachs (Asia) LLC>".
# Real PDFs use em-dash (U+2014), en-dash (U+2013), or ASCII hyphen as the
# separator after `Q`. The Unicode dashes inside the character class below
# are deliberate; they are part of what we match against.
_QA_ANALYST_TAG_RE = re.compile("<Q[\\s–—\\-]")  # noqa: RUF001

# Operator analyst-introduction patterns. Multiple occurrences = multi-question
# Q&A section. Single occurrence may be the boilerplate "after speaker
# presentations there will be a question-and-answer session" preamble, so the
# threshold is >=2 distinct introductions of analyst questions.
_OPERATOR_ANALYST_INTRO_RE = re.compile(
    r"(?:next|first)\s+question\s+(?:is\s+from|comes\s+from|will\s+come\s+from)",
    re.IGNORECASE,
)
_MIN_OPERATOR_INTROS_FOR_QA = 2


class QASectionStatus(StrEnum):
    """Tri-state verdict on whether a transcript contains analyst Q&A.

    PRESENT  : structural signals confirm analyst Q&A content is in the doc.
    ABSENT   : no Q&A signals found; treat as prepared-remarks only.
    UNKNOWN  : text too short to make a determination (rare; e.g. stub files).
    """

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


# Below this length we cannot tell whether the file is a stub or genuinely
# prepared-only — both look the same. Real prepared-remarks transcripts are
# typically 15-25k chars, full calls 50-80k chars. 2000 chars is well below
# any genuine call but enough to flag obviously broken/empty inputs.
_MIN_TEXT_LEN_FOR_DETECTION = 2000


@dataclass(frozen=True)
class QADetectionResult:
    """Detailed verdict from `detect_qa_section`. `signals` enumerates which
    structural patterns fired — useful for diagnostics and for surfacing
    confidence to the orchestrator.
    """

    status: QASectionStatus
    signals: tuple[str, ...]


def detect_qa_section(text: str) -> QADetectionResult:
    """Inspect transcript text for analyst Q&A content.

    Looks for any of three structural signals:
      1. The CallStreet `QUESTION AND ANSWER SECTION` header.
      2. CallStreet `<Q – ...>` analyst speaker tags.
      3. >=2 operator analyst-introduction phrases ("next question is from X").

    Any single signal at the required threshold is sufficient for PRESENT.
    """
    if len(text) < _MIN_TEXT_LEN_FOR_DETECTION:
        return QADetectionResult(status=QASectionStatus.UNKNOWN, signals=())

    signals: list[str] = []
    if _QA_HEADER_RE.search(text):
        signals.append("qa_header")
    if _QA_ANALYST_TAG_RE.search(text):
        signals.append("analyst_tag")
    operator_intros = len(_OPERATOR_ANALYST_INTRO_RE.findall(text))
    if operator_intros >= _MIN_OPERATOR_INTROS_FOR_QA:
        signals.append(f"operator_intros={operator_intros}")

    status = QASectionStatus.PRESENT if signals else QASectionStatus.ABSENT
    return QADetectionResult(status=status, signals=tuple(signals))


def find_existing_document_id(conn: sqlite3.Connection, sha256: str) -> int | None:
    """Idempotency: return documents.id if a row already exists with this sha256."""
    cur = conn.execute("SELECT id FROM documents WHERE sha256 = ? LIMIT 1", (sha256,))
    row = cur.fetchone()
    return int(row["id"]) if row is not None else None


def find_existing_transcript_id(conn: sqlite3.Connection, document_id: int) -> int | None:
    """Return transcripts.id linked to this document, or None if not yet parsed."""
    cur = conn.execute("SELECT id FROM transcripts WHERE document_id = ? LIMIT 1", (document_id,))
    row = cur.fetchone()
    return int(row["id"]) if row is not None else None


def find_document_for_path(conn: sqlite3.Connection, doc_id: int) -> tuple[str, datetime | None]:
    """Return (ticker, period_end) for a document_id."""
    cur = conn.execute("SELECT ticker, period_end FROM documents WHERE id = ?", (doc_id,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"No document with id={doc_id}")
    pe = row["period_end"]
    if isinstance(pe, str):
        pe = datetime.fromisoformat(pe)
    return (row["ticker"], pe)


def insert_document(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    file_path: Path,
    sha256: str,
    period_end: datetime,
    project_root: Path,
) -> int:
    """Insert one documents row; returns documents.id."""
    rel_path = str(file_path.relative_to(project_root)).replace("\\", "/")
    raw_size = file_path.stat().st_size
    cur = conn.execute(
        "INSERT INTO documents "
        "(ticker, source_type, doc_type, period_start, period_end, file_path, "
        " sha256, fetched_at, fetch_status, http_code, raw_bytes_size, source_url, "
        " parent_document_id) "
        "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?, NULL, NULL)",
        (
            ticker,
            SourceType.TRANSCRIPT_AUDIO.value,
            DocType.EARNINGS_CALL_TRANSCRIPT.value,
            period_end,
            rel_path,
            sha256,
            datetime.now(),
            FetchStatus.OK.value,
            raw_size,
        ),
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def insert_transcript(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    ticker: str,
    fiscal_period_type: FiscalPeriodType,
    period_end: datetime,
    has_qa_section: bool | None = None,
) -> int:
    """Insert one transcripts row; returns transcripts.id.

    `has_qa_section` is the tri-state Q&A verdict from `detect_qa_section`
    flattened to bool|None: PRESENT->True, ABSENT->False, UNKNOWN->None.
    """
    cur = conn.execute(
        "INSERT INTO transcripts "
        "(document_id, ticker, call_date, fiscal_period_type, period_end, "
        " source_url, has_qa_section) "
        "VALUES (?, ?, NULL, ?, ?, NULL, ?)",
        (document_id, ticker, fiscal_period_type.value, period_end, has_qa_section),
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def qa_status_to_db_value(status: QASectionStatus) -> bool | None:
    """Flatten the tri-state enum to the bool|None storage representation."""
    if status is QASectionStatus.PRESENT:
        return True
    if status is QASectionStatus.ABSENT:
        return False
    return None


def insert_segments(
    conn: sqlite3.Connection,
    *,
    transcript_id: int,
    turns: list[SpeakerTurn],
) -> int:
    """Bulk-insert transcript_segments rows."""
    insert_sql = (
        "INSERT INTO transcript_segments "
        "(transcript_id, seq, speaker, speaker_role, time_code_start, time_code_end, text) "
        "VALUES (?, ?, ?, NULL, NULL, NULL, ?)"
    )
    for seq, turn in enumerate(turns):
        conn.execute(insert_sql, (transcript_id, seq, turn.speaker, turn.text))
    return len(turns)


@dataclass(frozen=True)
class IngestResult:
    file_path: Path
    ticker: str
    period_end: datetime
    document_id: int
    transcript_id: int | None
    segment_count: int
    skipped_existing: bool
    qa_status: QASectionStatus = QASectionStatus.UNKNOWN
    qa_signals: tuple[str, ...] = ()


def ingest_one(
    conn: sqlite3.Connection,
    *,
    file_path: Path,
    project_root: Path,
    tracked_tickers: frozenset[str],
) -> IngestResult | None:
    """Process one transcript file. Returns None for filename mismatches / out-of-scope tickers."""
    parsed = parse_transcript_filename(file_path)
    if parsed is None:
        return None
    if parsed.ticker not in tracked_tickers:
        return None

    sha = sha256_of(file_path)
    existing = find_existing_document_id(conn, sha)
    period = map_to_period(parsed)

    if existing is not None:
        existing_transcript_id = find_existing_transcript_id(conn, existing)
        if existing_transcript_id is not None:
            return IngestResult(
                file_path=file_path,
                ticker=parsed.ticker,
                period_end=period.period_end,
                document_id=existing,
                transcript_id=existing_transcript_id,
                segment_count=0,
                skipped_existing=True,
            )
        text = read_transcript_text(file_path)
        turns = segment_by_speaker(text, known_speakers=known_speakers_for(parsed.ticker))
        qa = detect_qa_section(text)
        transcript_id = insert_transcript(
            conn,
            document_id=existing,
            ticker=parsed.ticker,
            fiscal_period_type=period.fiscal_period_type,
            period_end=period.period_end,
            has_qa_section=qa_status_to_db_value(qa.status),
        )
        segment_count = insert_segments(conn, transcript_id=transcript_id, turns=turns)
        conn.commit()
        return IngestResult(
            file_path=file_path,
            ticker=parsed.ticker,
            period_end=period.period_end,
            document_id=existing,
            transcript_id=transcript_id,
            segment_count=segment_count,
            skipped_existing=False,
            qa_status=qa.status,
            qa_signals=qa.signals,
        )

    text = read_transcript_text(file_path)
    turns = segment_by_speaker(text, known_speakers=known_speakers_for(parsed.ticker))
    qa = detect_qa_section(text)

    document_id = insert_document(
        conn,
        ticker=parsed.ticker,
        file_path=file_path,
        sha256=sha,
        period_end=period.period_end,
        project_root=project_root,
    )
    transcript_id = insert_transcript(
        conn,
        document_id=document_id,
        ticker=parsed.ticker,
        fiscal_period_type=period.fiscal_period_type,
        period_end=period.period_end,
        has_qa_section=qa_status_to_db_value(qa.status),
    )
    segment_count = insert_segments(conn, transcript_id=transcript_id, turns=turns)
    conn.commit()
    return IngestResult(
        file_path=file_path,
        ticker=parsed.ticker,
        period_end=period.period_end,
        document_id=document_id,
        transcript_id=transcript_id,
        segment_count=segment_count,
        skipped_existing=False,
        qa_status=qa.status,
        qa_signals=qa.signals,
    )


def ingest_existing_ir_transcript(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    file_path: Path,
) -> IngestResult:
    """Backfill `transcripts` + `transcript_segments` for an already-ingested ir_transcript document.

    Used to wire up existing `documents WHERE doc_type='ir_transcript'` rows that were
    fetched but never parsed. Reads the file, segments by speaker, and links to the
    existing document_id. Skips if a transcripts row already exists for this document_id.
    """
    ticker, period_end = find_document_for_path(conn, document_id)
    existing_transcript_id = find_existing_transcript_id(conn, document_id)
    if existing_transcript_id is not None:
        return IngestResult(
            file_path=file_path,
            ticker=ticker,
            period_end=period_end if period_end is not None else datetime.min,
            document_id=document_id,
            transcript_id=existing_transcript_id,
            segment_count=0,
            skipped_existing=True,
        )
    if period_end is None:
        raise ValueError(
            f"Document {document_id} ({file_path.name}) has NULL period_end; cannot link transcript"
        )
    text = read_transcript_text(file_path)
    turns = segment_by_speaker(text, known_speakers=known_speakers_for(ticker))
    qa = detect_qa_section(text)
    fiscal_period_type = _infer_fiscal_period_type(period_end)
    transcript_id = insert_transcript(
        conn,
        document_id=document_id,
        ticker=ticker,
        fiscal_period_type=fiscal_period_type,
        period_end=period_end,
        has_qa_section=qa_status_to_db_value(qa.status),
    )
    segment_count = insert_segments(conn, transcript_id=transcript_id, turns=turns)
    conn.commit()
    return IngestResult(
        file_path=file_path,
        ticker=ticker,
        period_end=period_end,
        document_id=document_id,
        transcript_id=transcript_id,
        segment_count=segment_count,
        skipped_existing=False,
        qa_status=qa.status,
        qa_signals=qa.signals,
    )


def _infer_fiscal_period_type(period_end: datetime) -> FiscalPeriodType:
    """Map a period_end month to its calendar/fiscal quarter.

    Covers calendar-year filers (Mar/Jun/Sep/Dec), Jan-FYE filers (Apr/Jul/Oct/Jan),
    and Oct-FYE filers (Jan/Apr/Jul/Oct) — i.e. all FYE patterns currently in scope.
    """
    month_to_quarter = {
        3: FiscalPeriodType.Q1,
        6: FiscalPeriodType.Q2,
        9: FiscalPeriodType.Q3,
        12: FiscalPeriodType.Q4,
        4: FiscalPeriodType.Q1,
        7: FiscalPeriodType.Q2,
        10: FiscalPeriodType.Q3,
        1: FiscalPeriodType.Q4,
    }
    if period_end.month not in month_to_quarter:
        raise ValueError(
            f"Cannot infer fiscal_period_type from period_end month={period_end.month}; "
            f"add an explicit mapping for non-standard quarter-end."
        )
    return month_to_quarter[period_end.month]
