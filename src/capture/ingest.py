"""The capture ingest pipeline — raw musing → landed analyst_notes row.

LLM-FREE by design (Phase 0): a musing lands deterministically so capture is
frictionless and can NEVER be blocked by an LLM budget, a model outage, or a
parse failure. The invariant is *words are durably stored before any fallible
step*: a text session persists its words at stage 1; a voice session retains its
audio (caller-owned) until transcription succeeds. Structuring/enrichment
(``note_structure``) is a Phase-1 consumer that runs AFTER landing, never on the
write path.

Stages:
  1. new_session  — durable staging row (raw words; dedup on external_ref)
  2. transcribe   — voice only (local faster-whisper); failure RETAINS audio
  3. scrub        — light deterministic PII redaction before anything else sees it
  4. match        — deterministic roster ticker (no LLM; ambiguity → needs_ticker)
  5. land         — create the kind='musing', source='capture' note
  6. purge        — blank the session transcript (raw is transient)
  7. audit        — one summary-level audit row (summary, not raw words)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from capture import audit, scrub, sessions, transcribe
from capture.matcher import RosterIndex, load_roster, match_ticker
from user_state.notes import create_note


@dataclass(frozen=True, slots=True)
class IngestResult:
    status: str  # landed | duplicate | transcription_failed | empty | no_audio
    note_id: int | None = None
    session_id: int | None = None
    ticker: str | None = None
    needs_ticker: bool = False


def ingest_capture(
    *,
    channel: str,
    media_kind: str = "text",
    text: str | None = None,
    audio_path: Path | str | None = None,
    external_ref: str | None = None,
    roster: RosterIndex | None = None,
    db_path: Path | str | None = None,
) -> IngestResult:
    """Land one captured musing. Returns an ``IngestResult`` describing the
    outcome; never raises on a transcription/parse failure (capture degrades, it
    does not crash the poller)."""
    raw_text = (text or "").strip()

    # 1. Durable staging row first (text words are durable here; voice audio is
    #    caller-owned until transcribed). None == this message already ingested.
    session_id = sessions.new_session(
        channel=channel,
        media_kind=media_kind,
        transcript=raw_text,
        external_ref=external_ref,
        db_path=db_path,
    )
    if session_id is None:
        return IngestResult(status="duplicate")

    # 2. Voice → text (local, $0). A failure RETAINS the audio for retry.
    if media_kind == "voice":
        if not audio_path:
            sessions.mark_failed(session_id, db_path=db_path)
            return IngestResult(status="no_audio", session_id=session_id)
        try:
            raw_text = transcribe.transcribe(audio_path).strip()
            sessions.set_transcript(session_id, raw_text, db_path=db_path)
        except transcribe.TranscriptionError:
            sessions.mark_failed(session_id, db_path=db_path)
            audit.write_audit(
                channel=channel,
                action="failed",
                utterance_summary="voice transcription failed; audio retained for retry",
                session_id=session_id,
                db_path=db_path,
            )
            return IngestResult(status="transcription_failed", session_id=session_id)

    if not raw_text:
        sessions.mark_failed(session_id, db_path=db_path)
        return IngestResult(status="empty", session_id=session_id)

    # 3. Scrub PII before the words reach the note (and any future LLM hop).
    scrubbed = scrub.scrub(raw_text)
    body = scrubbed.text

    # 4. Deterministic ticker match (no LLM). Ambiguity → needs_ticker (NULL).
    index = roster if roster is not None else load_roster(db_path)
    result = match_ticker(body, index)

    # 5. Land the musing.
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

    # 6. Land the session + purge raw words.
    sessions.mark_landed(session_id, note_id=note.id, db_path=db_path)

    # 7. Audit (summary, never the raw words).
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
