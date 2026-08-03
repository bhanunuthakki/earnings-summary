"""The Ledger ingest pipeline — raw musing → landed analyst_notes row.

Integration over a fully-migrated tmp DB; transcription is mocked (no model).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from capture import ingest, sessions, transcribe
from capture.matcher import build_roster_index
from user_state import notes

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"
ROSTER = build_roster_index(
    symbols=["NU", "MELI"], phrases={"nubank": "NU", "mercadolibre": "MELI"}
)


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "ledger.db", stamp=PRIOR_HEAD)


def _audit_count(db_path: Path, **where: object) -> int:
    clause = " AND ".join(f"{k} = ?" for k in where)
    sql = "SELECT COUNT(*) FROM capture_audit_log" + (f" WHERE {clause}" if where else "")
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute(sql, tuple(where.values())).fetchone()[0])
    finally:
        conn.close()


def test_text_ingest_lands_musing_and_purges_raw(db_path: Path) -> None:
    res = ingest.ingest_capture(
        channel="telegram",
        media_kind="text",
        text="Nubank NPL formation worries me",
        external_ref="tg:1",
        roster=ROSTER,
        db_path=db_path,
    )
    assert res.status == "landed"
    assert res.ticker == "NU"
    note = notes.get_note(res.note_id or 0, db_path=db_path)
    assert note is not None
    assert note.kind == "musing"
    assert note.source == "capture"
    assert note.ticker == "NU"
    assert "NPL formation" in note.body
    session = sessions.get_session(res.session_id or 0, db_path=db_path)
    assert session is not None
    assert session.status == "landed"
    assert session.transcript == ""  # raw purged
    assert session.note_id == res.note_id
    assert _audit_count(db_path, note_id=res.note_id, action="landed") == 1


def test_dedup_external_ref_no_ops(db_path: Path) -> None:
    first = ingest.ingest_capture(
        channel="telegram",
        text="MELI looks cheap",
        external_ref="tg:9",
        roster=ROSTER,
        db_path=db_path,
    )
    second = ingest.ingest_capture(
        channel="telegram",
        text="MELI cheap again",
        external_ref="tg:9",
        roster=ROSTER,
        db_path=db_path,
    )
    assert first.status == "landed"
    assert second.status == "duplicate"
    assert len(notes.list_notes(kind="musing", db_path=db_path)) == 1


def test_multi_ticker_is_needs_ticker(db_path: Path) -> None:
    res = ingest.ingest_capture(
        channel="tray", text="NU and MELI both look compelling", roster=ROSTER, db_path=db_path
    )
    assert res.status == "landed"
    assert res.ticker is None
    assert res.needs_ticker is True
    note = notes.get_note(res.note_id or 0, db_path=db_path)
    assert note is not None and note.ticker is None
    assert note.context is not None
    assert note.context.get("needs_ticker") is True
    assert sorted(note.context.get("ticker_candidates", [])) == ["MELI", "NU"]


def test_no_match_lands_unattributed(db_path: Path) -> None:
    res = ingest.ingest_capture(
        channel="tray", text="the market feels toppy", roster=ROSTER, db_path=db_path
    )
    assert res.status == "landed"
    assert res.ticker is None
    assert res.needs_ticker is False


def test_voice_transcription_failure_retains_audio_no_note(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(path: object) -> str:
        raise transcribe.TranscriptionError("decode failed")

    monkeypatch.setattr(transcribe, "transcribe", _boom)
    audio = tmp_path / "v.oga"
    audio.write_bytes(b"\x00")
    res = ingest.ingest_capture(
        channel="telegram",
        media_kind="voice",
        audio_path=audio,
        external_ref="tg:v1",
        roster=ROSTER,
        db_path=db_path,
    )
    assert res.status == "transcription_failed"
    assert res.note_id is None
    session = sessions.get_session(res.session_id or 0, db_path=db_path)
    assert session is not None and session.status == "failed"  # retained for retry
    assert notes.list_notes(kind="musing", db_path=db_path) == []
    assert _audit_count(db_path, action="failed") == 1


def test_voice_success_lands(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transcribe, "transcribe", lambda path: "thinking about Nubank credit cycle")
    audio = tmp_path / "v.oga"
    audio.write_bytes(b"\x00")
    res = ingest.ingest_capture(
        channel="telegram",
        media_kind="voice",
        audio_path=audio,
        external_ref="tg:v2",
        roster=ROSTER,
        db_path=db_path,
    )
    assert res.status == "landed"
    assert res.ticker == "NU"
    session = sessions.get_session(res.session_id or 0, db_path=db_path)
    assert session is not None and session.status == "landed" and session.transcript == ""


def test_scrub_applied_before_landing(db_path: Path) -> None:
    res = ingest.ingest_capture(
        channel="tray",
        text="email me at jane@example.com about MELI",
        roster=ROSTER,
        db_path=db_path,
    )
    note = notes.get_note(res.note_id or 0, db_path=db_path)
    assert note is not None
    assert "[email]" in note.body
    assert "jane@example.com" not in note.body
    assert note.context is not None and note.context.get("scrubbed")


# ---------------------------------------------------------------------------
# ingest_reading — document and URL land as On My Mind readings (observations)
# ---------------------------------------------------------------------------


def test_reading_document_lands(db_path: Path, tmp_path: Path) -> None:
    pdf = tmp_path / "nubank_investor_deck.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    res = ingest.ingest_reading(
        channel="telegram",
        local_path=pdf,
        file_name="nubank_investor_deck.pdf",
        mime_type="application/pdf",
        caption="NU Q1 2026 investor presentation",
        external_ref="tg:doc:1",
        db_path=db_path,
    )
    assert res.status == "landed"
    note = notes.get_note(res.note_id or 0, db_path=db_path)
    assert note is not None
    assert note.kind == "observation"  # a reading, not a classified 'influence'
    assert note.source == "capture"
    assert note.ticker is None  # portfolio-level
    assert "nubank_investor_deck.pdf" in note.body
    assert "NU Q1 2026" in note.body
    assert note.context is not None
    assert note.context.get("media_kind") == "document"
    assert note.context.get("item_type") == "doc"
    assert note.context.get("ledger") == "onmymind"
    assert note.context.get("file_name") == "nubank_investor_deck.pdf"
    assert note.context.get("mime_type") == "application/pdf"
    assert note.context.get("caption") == "NU Q1 2026 investor presentation"
    assert _audit_count(db_path, note_id=res.note_id, action="landed") == 1


def test_reading_url_lands(db_path: Path) -> None:
    res = ingest.ingest_reading(
        channel="telegram",
        url="https://example.com/latam-fintech-report.pdf",
        external_ref="tg:url:1",
        db_path=db_path,
    )
    assert res.status == "landed"
    note = notes.get_note(res.note_id or 0, db_path=db_path)
    assert note is not None
    assert note.kind == "observation"
    assert note.ticker is None
    assert "https://example.com" in note.body
    assert note.context is not None
    assert note.context.get("media_kind") == "url"
    assert note.context.get("item_type") == "link"
    assert note.context.get("ledger") == "onmymind"
    assert note.context.get("url") == "https://example.com/latam-fintech-report.pdf"


def test_reading_scrubs_caption(db_path: Path, tmp_path: Path) -> None:
    pdf = tmp_path / "deck.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    res = ingest.ingest_reading(
        channel="telegram",
        local_path=pdf,
        file_name="deck.pdf",
        caption="from jane@example.com — great LATAM read",
        external_ref="tg:doc:scrub",
        db_path=db_path,
    )
    note = notes.get_note(res.note_id or 0, db_path=db_path)
    assert note is not None
    assert "jane@example.com" not in note.body  # caption PII scrubbed
    assert "[email]" in note.body
    assert note.context is not None and note.context.get("scrubbed")


def test_reading_dedup(db_path: Path) -> None:
    args = {"channel": "telegram", "url": "https://example.com/deck.pdf", "external_ref": "tg:u2"}
    first = ingest.ingest_reading(**args, db_path=db_path)
    second = ingest.ingest_reading(**args, db_path=db_path)
    assert first.status == "landed"
    assert second.status == "duplicate"


def test_reading_empty_returns_empty(db_path: Path) -> None:
    res = ingest.ingest_reading(channel="telegram", db_path=db_path)
    assert res.status == "empty"


def test_extract_url() -> None:
    assert ingest._extract_url("https://example.com/deck.pdf") == "https://example.com/deck.pdf"
    assert ingest._extract_url("  https://example.com  ") == "https://example.com"
    assert ingest._extract_url("check out https://example.com for details") is None
    assert ingest._extract_url("NU looks cheap to me") is None
    assert ingest._extract_url("http://example.com/path") == "http://example.com/path"
