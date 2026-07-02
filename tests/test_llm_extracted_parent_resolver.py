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
) -> int:
    cur = conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, period_end, "
        "file_path, sha256, fetched_at, fetch_status, raw_bytes_size) "
        "VALUES (?, ?, ?, ?, 'x', 'x', ?, 'ok', 1)",
        (ticker, source_type, doc_type, period_end, fetched_at),
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


def test_unique_ir_transcript_candidate(conn: sqlite3.Connection) -> None:
    doc_id = _insert_doc(
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
    assert result is not None
    assert result.parent_document_id == doc_id
    assert result.confidence == "unique"


def test_multi_candidate_picks_earliest_fetched(conn: sqlite3.Connection) -> None:
    """Two eligible parents for the same period — the summary was written once
    (kpi_extract_summaries + process_ir_documents both cache-hit on an existing
    .tmp file), so the one ingested FIRST is the one actually read."""
    earlier_id = _insert_doc(
        conn,
        ticker="UBER",
        source_type="transcript_audio",
        doc_type="earnings_call_transcript",
        period_end="2024-12-31 00:00:00",
        fetched_at="2026-05-19 01:47:11.053932",
    )
    _insert_doc(
        conn,
        ticker="UBER",
        source_type="ir_doc",
        doc_type="ir_transcript",
        period_end="2024-12-31T00:00:00",
        fetched_at="2026-06-04T23:56:09.601781",
    )
    result = resolve_parent(
        conn,
        ticker="UBER",
        doc_type="llm_summary",
        file_path=".tmp/UBER_Q4_2024_summary.txt",
        period_end=datetime(2024, 12, 31),
    )
    assert result is not None
    assert result.parent_document_id == earlier_id
    assert result.confidence == "earliest_of_2"
    assert result.candidate_count == 2


def test_earliest_tiebreak_is_time_aware_not_lexicographic(conn: sqlite3.Connection) -> None:
    """Regression: a naive string sort of fetched_at picks the WRONG winner
    whenever two rows share a calendar date but differ in separator style
    (' ' < 'T' in ASCII, independent of actual time of day). Candidate A is a
    space-separated stamp at 09:00 (lexicographically SMALLER string); candidate
    B is a "T"-separated stamp at 01:00 (the true earliest). B must win.
    """
    space_later_id = _insert_doc(
        conn,
        ticker="CRM",
        source_type="transcript_audio",
        doc_type="earnings_call_transcript",
        period_end="2025-12-31 00:00:00",
        fetched_at="2025-12-31 09:00:00.000000",
    )
    t_earlier_id = _insert_doc(
        conn,
        ticker="CRM",
        source_type="ir_doc",
        doc_type="ir_transcript",
        period_end="2025-12-31T00:00:00",
        fetched_at="2025-12-31T01:00:00.000000",
    )
    # Sanity: prove the trap is real — plain string comparison picks the wrong row.
    assert "2025-12-31 09:00:00.000000" < "2025-12-31T01:00:00.000000"

    result = resolve_parent(
        conn,
        ticker="CRM",
        doc_type="llm_summary",
        file_path=".tmp/CRM_Q4_2025_summary.txt",
        period_end=datetime(2025, 12, 31),
    )
    assert result is not None
    assert result.parent_document_id == t_earlier_id
    assert result.parent_document_id != space_later_id


def test_aware_and_naive_fetched_at_do_not_crash_the_sort(conn: sqlite3.Connection) -> None:
    """Some rows carry a '+00:00' offset, others don't (naive-UTC convention) —
    mixing them in the same candidate set must not raise."""
    naive_id = _insert_doc(
        conn,
        ticker="ABNB",
        source_type="transcript_audio",
        doc_type="earnings_call_transcript",
        period_end="2025-03-31 00:00:00",
        fetched_at="2026-05-19 02:01:49.601393",
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
    assert result is not None
    assert result.parent_document_id == naive_id


def test_investor_update_filename_routes_to_investor_update_doc_type(
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
