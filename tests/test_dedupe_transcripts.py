"""Tests for execution/dedupe_transcripts.py Q&A-first winner ranking."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_dedupe_module() -> Any:
    source = PROJECT_ROOT / "execution" / "dedupe_transcripts.py"
    spec = importlib.util.spec_from_file_location("dedupe_transcripts_test_mod", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["dedupe_transcripts_test_mod"] = module
    spec.loader.exec_module(module)
    return module


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            file_path TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        );
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            fiscal_period_type TEXT,
            period_end TEXT,
            source TEXT,
            has_qa_section INTEGER
        );
        CREATE TABLE transcript_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transcript_id INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            text TEXT NOT NULL
        );
        """
    )
    return conn


def test_dedupe_sweep_prioritizes_qa_over_source_reliability_tier(tmp_path: Path) -> None:
    mod = _load_dedupe_module()
    conn = _conn()
    conn.execute(
        "INSERT INTO documents (id, ticker, doc_type, file_path, fetched_at) "
        "VALUES (1, 'UBER', 'ir_transcript', 'ir_documents/UBER/q3.pdf', '2026-11-05 00:00:00')"
    )
    conn.execute(
        "INSERT INTO documents (id, ticker, doc_type, file_path, fetched_at) "
        "VALUES (2, 'UBER', 'earnings_call_transcript', 'transcripts/raw/UBER_Q3_2026.txt', '2026-11-04 00:00:00')"
    )
    conn.execute(
        "INSERT INTO transcripts (id, document_id, ticker, fiscal_period_type, period_end, source, has_qa_section) "
        "VALUES (10, 1, 'UBER', 'Q3', '2026-09-30 00:00:00', 'ir_transcript_doc', 0)"
    )
    conn.execute(
        "INSERT INTO transcripts (id, document_id, ticker, fiscal_period_type, period_end, source, has_qa_section) "
        "VALUES (20, 2, 'UBER', 'Q3', '2026-09-30 00:00:00', 'aggregator_google_finance_beta', 1)"
    )
    for seq in range(40):
        conn.execute(
            "INSERT INTO transcript_segments (transcript_id, seq, text) VALUES (10, ?, 'ir turn')",
            (seq,),
        )
    for seq in range(10):
        conn.execute(
            "INSERT INTO transcript_segments (transcript_id, seq, text) VALUES (20, ?, 'gfb turn')",
            (seq,),
        )

    reports, backfill, solo_count = mod._sweep(conn, tmp_path, "UBER")

    assert backfill == {}
    assert solo_count == 0
    assert len(reports) == 1
    report = reports[0]
    assert report.kept_transcript_id == 20
    assert report.kept_source == "aggregator_google_finance_beta"
    assert report.kept_has_qa_section is True
    assert len(report.superseded) == 1
    assert report.superseded[0].transcript_id == 10
    assert report.superseded[0].has_qa_section is False
