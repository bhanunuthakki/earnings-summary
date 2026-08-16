"""Tests for src/provenance/llm_extracted_parent.py — the llm_extracted ->
primary-document matcher shared by the write-path guard and the historical
backfill (execution/backfill_llm_extracted_parents.py)."""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from provenance.llm_extracted_parent import resolve_parent

_SCHEMA = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT, source_type TEXT, doc_type TEXT, period_end TIMESTAMP,
    file_path TEXT, sha256 TEXT, fetched_at TIMESTAMP, fetch_status TEXT,
    raw_bytes_size INTEGER, parent_document_id INTEGER
);
"""


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    c.commit()
    return c


def _insert_doc(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    source_type: str,
    doc_type: str,
    period_end: str,
    fetched_at: str,
    file_path: str = "x",
) -> int:
    cur = conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, period_end, "
        "file_path, sha256, fetched_at, fetch_status, raw_bytes_size) "
        "VALUES (?, ?, ?, ?, ?, 'x', ?, 'ok', 1)",
        (ticker, source_type, doc_type, period_end, file_path, fetched_at),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def test_no_candidates_returns_none(conn: sqlite3.Connection) -> None:
    result = resolve_parent(
        conn,
        ticker="NU",
        doc_type="llm_summary",
        file_path=".tmp/NU_Q1_2023_summary.txt",
        period_end=datetime(2023, 3, 31),
    )
    assert result is None


def test_unique_transcript_audio_candidate(conn: sqlite3.Connection) -> None:
    doc_id = _insert_doc(
        conn,
        ticker="NU",
        source_type="transcript_audio",
        doc_type="earnings_call_transcript",
        period_end="2023-03-31 00:00:00",
        fetched_at="2026-05-19 01:45:16.308875",
        file_path="transcripts/processed/NU_Q1_2023.txt",
    )
    result = resolve_parent(
        conn,
        ticker="NU",
        doc_type="llm_summary",
        file_path=".tmp/NU_Q1_2023_summary.txt",
        period_end=datetime(2023, 3, 31),
    )
    assert result is not None
    assert result.parent_document_id == doc_id
    assert result.confidence == "unique"


def test_llm_summary_does_not_match_ir_transcript(conn: sqlite3.Connection) -> None:
    _insert_doc(
        conn,
        ticker="NU",
        source_type="ir_doc",
        doc_type="ir_transcript",
        period_end="2023-03-31T00:00:00",
        fetched_at="2026-05-03T20:52:55.594311",
    )
    result = resolve_parent(
        conn,
        ticker="NU",
        doc_type="llm_summary",
        file_path=".tmp/NU_Q1_2023_summary.txt",
        period_end="2023-03-31 00:00:00",  # backfill path passes the raw DB string
    )
    assert result is None


def test_llm_summary_ambiguous_exact_basename_returns_none(conn: sqlite3.Connection) -> None:
    """The repair must not guess between duplicate processed-transcript rows."""
    _insert_doc(
        conn,
        ticker="UBER",
        source_type="transcript_audio",
        doc_type="earnings_call_transcript",
        period_end="2024-12-31 00:00:00",
        fetched_at="2026-05-19 01:47:11.053932",
        file_path="transcripts/processed/UBER_Q4_2024.txt",
    )
    _insert_doc(
        conn,
        ticker="UBER",
        source_type="transcript_audio",
        doc_type="earnings_call_transcript",
        period_end="2024-12-31T00:00:00",
        fetched_at="2026-06-04T23:56:09.601781",
        file_path="archive/UBER_Q4_2024.txt",
    )
    result = resolve_parent(
        conn,
        ticker="UBER",
        doc_type="llm_summary",
        file_path=".tmp/UBER_Q4_2024_summary.txt",
        period_end=datetime(2024, 12, 31),
    )
    assert result is None


def test_llm_summary_uses_exact_basename_not_period_end(conn: sqlite3.Connection) -> None:
    """The processed transcript's stored period is canonical for the child."""
    parent_id = _insert_doc(
        conn,
        ticker="RBRK",
        source_type="transcript_audio",
        doc_type="earnings_call_transcript",
        period_end="2025-04-30 00:00:00",
        fetched_at="2025-05-01 09:00:00.000000",
        file_path="transcripts/processed/RBRK_Q1_2026.txt",
    )

    result = resolve_parent(
        conn,
        ticker="RBRK",
        doc_type="llm_summary",
        file_path=".tmp/RBRK_Q1_2026_summary.txt",
        period_end=datetime(2026, 4, 30),
    )
    assert result is not None
    assert result.parent_document_id == parent_id
    assert result.parent_period_end == datetime(2025, 4, 30)


def test_llm_summary_ignores_nonmatching_transcript_basename(conn: sqlite3.Connection) -> None:
    """Matching the ticker and period alone is insufficient for lineage repair."""
    _insert_doc(
        conn,
        ticker="ABNB",
        source_type="transcript_audio",
        doc_type="earnings_call_transcript",
        period_end="2025-03-31 00:00:00",
        fetched_at="2026-05-19 02:01:49.601393",
        file_path="transcripts/processed/ABNB_Q2_2025.txt",
    )
    _insert_doc(
        conn,
        ticker="ABNB",
        source_type="ir_doc",
        doc_type="ir_transcript",
        period_end="2025-03-31T00:00:00",
        fetched_at="2026-06-17 06:14:50.440578+00:00",
    )
    result = resolve_parent(
        conn,
        ticker="ABNB",
        doc_type="llm_summary",
        file_path=".tmp/ABNB_Q1_2025_summary.txt",
        period_end=datetime(2025, 3, 31),
    )
    assert result is None


def test_investor_update_summary_resolves_canonical_ir_parent(
    conn: sqlite3.Connection,
) -> None:
    # A plain earnings-call transcript exists for the same period, but the
    # investor-update variant must NOT match against it.
    _insert_doc(
        conn,
        ticker="MELI",
        source_type="transcript_audio",
        doc_type="earnings_call_transcript",
        period_end="2024-06-30 00:00:00",
        fetched_at="2026-05-19 01:45:00",
    )
    investor_update_id = _insert_doc(
        conn,
        ticker="MELI",
        source_type="ir_doc",
        doc_type="ir_investor_update",
        period_end="2024-06-30T00:00:00",
        fetched_at="2026-05-03T20:52:55",
        file_path="ir_documents/MELI/2024-06-30/ir_investor_update__deadbeef.pdf",
    )
    result = resolve_parent(
        conn,
        ticker="MELI",
        doc_type="llm_summary",
        file_path=".tmp/MELI_Q2_2024_investor_update_summary.txt",
        period_end=datetime(2024, 6, 30),
    )
    assert result is not None
    assert result.parent_document_id == investor_update_id


def test_investor_update_summary_ambiguous_canonical_parent_returns_none(
    conn: sqlite3.Connection,
) -> None:
    for file_path in (
        "ir_documents/MELI/2024-06-30/ir_investor_update__deadbeef.pdf",
        "ir_documents/MELI/2024-06-30/ir_investor_update__cafebabe.pdf",
    ):
        _insert_doc(
            conn,
            ticker="MELI",
            source_type="ir_doc",
            doc_type="ir_investor_update",
            period_end="2024-06-30",
            fetched_at="2026-05-03T20:52:55",
            file_path=file_path,
        )

    result = resolve_parent(
        conn,
        ticker="MELI",
        doc_type="llm_summary",
        file_path=".tmp/MELI_Q2_2024_investor_update_summary.txt",
        period_end=datetime(2024, 6, 30),
    )
    assert result is None


def test_investor_update_summary_requires_matching_canonical_period(
    conn: sqlite3.Connection,
) -> None:
    _insert_doc(
        conn,
        ticker="MELI",
        source_type="ir_doc",
        doc_type="ir_investor_update",
        period_end="2024-06-30",
        fetched_at="2026-05-03T20:52:55",
        file_path="ir_documents/MELI/2024-06-30/ir_investor_update__deadbeef.pdf",
    )

    result = resolve_parent(
        conn,
        ticker="MELI",
        doc_type="llm_summary",
        file_path=".tmp/MELI_Q2_2024_investor_update_summary.txt",
        period_end=datetime(2024, 3, 31),
    )
    assert result is None


def test_malformed_parent_period_returns_none(conn: sqlite3.Connection) -> None:
    _insert_doc(
        conn,
        ticker="RBRK",
        source_type="transcript_audio",
        doc_type="earnings_call_transcript",
        period_end="not-a-date",
        fetched_at="2025-05-01",
        file_path="transcripts/processed/RBRK_Q1_2026.txt",
    )

    result = resolve_parent(
        conn,
        ticker="RBRK",
        doc_type="llm_summary",
        file_path=".tmp/RBRK_Q1_2026_summary.txt",
        period_end=datetime(2026, 4, 30),
    )
    assert result is None


@pytest.mark.parametrize(
    ("orphan_doc_type", "parent_source_type", "parent_doc_type"),
    [
        ("ir_press_release_synthesized", "ir_doc", "ir_press_release"),
        ("ir_presentation_synthesized", "ir_doc", "ir_presentation"),
    ],
)
def test_ir_synthesized_doc_types_route_correctly(
    conn: sqlite3.Connection,
    orphan_doc_type: str,
    parent_source_type: str,
    parent_doc_type: str,
) -> None:
    doc_id = _insert_doc(
        conn,
        ticker="NU",
        source_type=parent_source_type,
        doc_type=parent_doc_type,
        period_end="2023-03-31T00:00:00",
        fetched_at="2026-05-03T20:52:55",
    )
    result = resolve_parent(
        conn,
        ticker="NU",
        doc_type=orphan_doc_type,
        file_path=f".tmp/NU_Q1_2023_{orphan_doc_type}.txt",
        period_end=datetime(2023, 3, 31),
    )
    assert result is not None
    assert result.parent_document_id == doc_id


def test_unknown_doc_type_returns_none(conn: sqlite3.Connection) -> None:
    assert (
        resolve_parent(
            conn,
            ticker="NU",
            doc_type="something_else",
            file_path="x",
            period_end=datetime(2023, 3, 31),
        )
        is None
    )


def test_none_period_end_returns_none(conn: sqlite3.Connection) -> None:
    assert (
        resolve_parent(conn, ticker="NU", doc_type="llm_summary", file_path="x", period_end=None)
        is None
    )
