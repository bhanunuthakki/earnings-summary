"""The capture ingest pipeline — raw musing → landed analyst_notes row.

LLM-FREE by design (Phase 0): a musing lands deterministically so capture is
frictionless and can NEVER be blocked by an LLM budget, a model outage, or a
parse failure. The invariant is *words are durably stored before any fallible
step*: a text session persists its words at stage 1. Structuring/enrichment
(``note_structure``) is a Phase-1 consumer that runs AFTER landing, never on the
write path.

Stages:
  1. new_session  — durable staging row (raw words; dedup on external_ref)
  2. scrub        — light deterministic PII redaction before anything else sees it
  3. match        — deterministic roster ticker (no LLM; ambiguity → needs_ticker)
  4. land         — create the kind='musing', source='capture' note
  5. purge        — blank the session transcript (raw is transient)
  6. audit        — one summary-level audit row (summary, not raw words)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from capture import audit, scrub, sessions
from capture.matcher import RosterIndex, load_roster, match_ticker
from user_state.notes import create_note


@dataclass(frozen=True, slots=True)
class IngestResult:
    status: str  # landed | duplicate | empty
    note_id: int | None = None
    session_id: int | None = None
    ticker: str | None = None
    needs_ticker: bool = False


def ingest_capture(
    *,
    channel: str,
    media_kind: str = "text",
    text: str | None = None,
    external_ref: str | None = None,
    roster: RosterIndex | None = None,
    db_path: Path | str | None = None,
) -> IngestResult:
    """Land one text musing without model calls or media transcription."""
    if media_kind != "text":
        raise ValueError("musing capture supports text only")
    raw_text = (text or "").strip()

    # 1. Durable staging row first. None means this message was already ingested.
    session_id = sessions.new_session(
        channel=channel,
        media_kind=media_kind,
        transcript=raw_text,
        external_ref=external_ref,
        db_path=db_path,
    )
    if session_id is None:
        return IngestResult(status="duplicate")

    if not raw_text:
        sessions.mark_failed(session_id, db_path=db_path)
        return IngestResult(status="empty", session_id=session_id)

    # 2. Scrub PII before the words reach the note (and any future LLM hop).
    scrubbed = scrub.scrub(raw_text)
    body = scrubbed.text

    # 3. Deterministic ticker match (no LLM). Ambiguity → needs_ticker (NULL).
    index = roster if roster is not None else load_roster(db_path)
    result = match_ticker(body, index)

    # 4. Land the musing.
    context: dict[str, object] = {
        "channel": channel,
        "media_kind": media_kind,
        "ledger": "musing",
    }
    if result.needs_ticker:
        context["needs_ticker"] = True
        context["ticker_candidates"] = list(result.candidates)
    if scrubbed.redactions:
        context["scrubbed"] = scrubbed.redactions
    note = create_note(
        ticker=result.ticker,
        kind="musing",
        body=body,
        source="capture",
        context=context,
        db_path=db_path,
    )

    # 5. Land the session + purge raw words.
    sessions.mark_landed(session_id, note_id=note.id, db_path=db_path)

    # 6. Audit (summary, never the raw words).
    where = result.ticker or ("needs_ticker" if result.needs_ticker else "unattributed")
    audit.write_audit(
        channel=channel,
        action="landed",
        utterance_summary=audit.summarize_utterance(body),
        session_id=session_id,
        note_id=note.id,
        detail=f"ticker={where}",
        db_path=db_path,
    )
    return IngestResult(
        status="landed",
        note_id=note.id,
        session_id=session_id,
        ticker=result.ticker,
        needs_ticker=result.needs_ticker,
    )


def extract_url(text: str) -> str | None:
    """Return the URL if this message is a bare URL (no whitespace), else None."""
    stripped = text.strip()
    if stripped.startswith(("http://", "https://")) and " " not in stripped:
        return stripped
    return None


def ingest_reading(
    *,
    channel: str,
    local_path: Path | str | None = None,
    file_name: str | None = None,
    mime_type: str | None = None,
    caption: str | None = None,
    url: str | None = None,
    external_ref: str | None = None,
    db_path: Path | str | None = None,
) -> IngestResult:
    """Record a document or URL reference as a reading (kind='observation').

    A reading is something the analyst *found* (a deck, a link) vs a musing
    (something they *said*). It is NOT classified into a belief at ingest time —
    a targeted research request uses the existing backend research workflow.
    Portfolio-level (ticker=NULL); LLM-free write path: the file is indexed by
    reference (name + path) without blocking on any extraction step.
    """
    redactions: dict[str, int] | None = None
    name: str = ""
    caption_clean: str | None = None
    if local_path:
        name = file_name or Path(local_path).name
        item_type, media_kind = "doc", "document"
        if caption and caption.strip():
            # Scrub the free-text caption (PII) but leave the filename intact.
            scrubbed = scrub.scrub(caption.strip())
            caption_clean = scrubbed.text
            redactions = scrubbed.redactions or None
        body = name if not caption_clean else f"{name}: {caption_clean}"
    elif url:
        item_type, media_kind = "link", "url"
        body = url  # a bare URL is not free-text PII; keep it verbatim
    else:
        return IngestResult(status="empty")

    # 1. Durable staging row (dedup on external_ref).
    session_id = sessions.new_session(
        channel=channel,
        media_kind=media_kind,
        transcript=body,
        external_ref=external_ref,
        db_path=db_path,
    )
    if session_id is None:
        return IngestResult(status="duplicate")

    # 2. Keep the explicit source reference with the reading note.
    context: dict[str, object] = {
        "channel": channel,
        "media_kind": media_kind,
        "item_type": item_type,
        "ledger": "reading",
    }
    if local_path:
        context["file_name"] = name
        context["local_path"] = str(local_path)
        if mime_type:
            context["mime_type"] = mime_type
        if caption_clean:
            context["caption"] = caption_clean
    if url:
        context["url"] = url
    if redactions:
        context["scrubbed"] = redactions

    # 3. Land as a portfolio-level reading (ticker=None, kind='observation').
    note = create_note(
        ticker=None,
        kind="observation",
        body=body,
        source="capture",
        context=context,
        db_path=db_path,
    )

    # 4. Mark session landed + purge raw staging text.
    sessions.mark_landed(session_id, note_id=note.id, db_path=db_path)

    # 5. Audit summary (never the raw body).
    audit.write_audit(
        channel=channel,
        action="landed",
        utterance_summary=f"reading ({item_type}): {body[:100]}",
        session_id=session_id,
        note_id=note.id,
        detail=f"item_type={item_type}",
        db_path=db_path,
    )
    return IngestResult(status="landed", note_id=note.id, session_id=session_id)
