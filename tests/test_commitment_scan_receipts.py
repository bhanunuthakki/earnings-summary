from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime

from pipeline.commitment_scan_receipts import (
    append_commitment_scan_receipt,
    current_commitment_scan_receipt,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY,ticker TEXT,file_path TEXT,sha256 TEXT
        );
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY,document_id INTEGER,ticker TEXT,period_end TEXT,
            fiscal_period_type TEXT,is_active INTEGER,is_current INTEGER
        );
        CREATE TABLE transcript_segments (
            id INTEGER PRIMARY KEY,transcript_id INTEGER,seq INTEGER,text TEXT
        );
        CREATE TABLE transcript_acquisition_receipts (
            receipt_id TEXT PRIMARY KEY,document_id INTEGER,canonical_ticker TEXT,
            fiscal_year INTEGER,fiscal_quarter INTEGER,canonical_document_path TEXT,
            artifact_sha256 TEXT,provider TEXT,source_type TEXT,document_type TEXT,
            artifact_json TEXT,recorded_at TEXT
        );
        CREATE TABLE management_commitments (
            id INTEGER PRIMARY KEY,ticker TEXT,period_made TEXT,
            transcript_segment_id INTEGER,period_target TEXT,kpi_name TEXT,
            comparator TEXT,target_value TEXT,unit TEXT,narrative TEXT
        );
        CREATE TABLE commitment_scan_receipts (
            receipt_id TEXT PRIMARY KEY,transcript_id INTEGER,document_id INTEGER,
            transcript_acquisition_receipt_id TEXT,transcript_sha256 TEXT,
            prompt_version TEXT,n_extracted INTEGER,output_manifest_json TEXT,
            output_manifest_sha256 TEXT,recorded_at TEXT,
            UNIQUE(transcript_id,prompt_version,output_manifest_sha256)
        );
        """
    )
    artifact_json = '{"artifact":"exact"}'
    receipt_id = _sha(artifact_json)
    transcript_sha = "a" * 64
    conn.execute(
        "INSERT INTO documents VALUES (1,'ACME','transcripts/processed/ACME_Q2_2026.txt',?)",
        (transcript_sha,),
    )
    conn.execute("INSERT INTO transcripts VALUES (1,1,'ACME','2026-06-30','Q2',1,1)")
    conn.execute("INSERT INTO transcript_segments VALUES (10,1,0,'exact transcript')")
    conn.execute(
        "INSERT INTO transcript_acquisition_receipts VALUES "
        "(?,NULL,'ACME',2026,2,'transcripts/raw/ACME_Q2_2026.txt',?,"
        "'issuer_ir','ir_doc','earnings_call_transcript',?,'2026-09-05T00:00:00Z')",
        (receipt_id, transcript_sha, artifact_json),
    )
    conn.commit()
    return conn


def test_scan_receipt_requires_current_prompt_and_selected_transcript() -> None:
    conn = _connection()
    receipt = append_commitment_scan_receipt(
        conn,
        transcript_id=1,
        prompt_version="v1",
        recorded_at=datetime(2026, 9, 5, tzinfo=UTC),
    )
    conn.commit()

    assert current_commitment_scan_receipt(conn, transcript_id=1, prompt_version="v1") == receipt
    assert current_commitment_scan_receipt(conn, transcript_id=1, prompt_version="v2") is None
    conn.execute("UPDATE transcripts SET is_active=0,is_current=0 WHERE id=1")
    conn.execute("INSERT INTO transcripts VALUES (2,1,'ACME','2026-06-30','Q2',1,1)")
    assert current_commitment_scan_receipt(conn, transcript_id=1, prompt_version="v1") is None


def test_scan_receipt_rejects_mutated_source_bound_output() -> None:
    conn = _connection()
    conn.execute(
        "INSERT INTO management_commitments VALUES "
        "(7,'ACME','2026-06-30',10,'2026-09-30','Revenue','ge','20',"
        "'percent','Original commitment')"
    )
    receipt = append_commitment_scan_receipt(
        conn,
        transcript_id=1,
        prompt_version="v1",
        commitment_ids=(7,),
        recorded_at=datetime(2026, 9, 5, tzinfo=UTC),
    )
    conn.commit()
    assert current_commitment_scan_receipt(conn, transcript_id=1, prompt_version="v1") == receipt

    conn.execute("UPDATE management_commitments SET narrative='Mutated' WHERE id=7")
    assert current_commitment_scan_receipt(conn, transcript_id=1, prompt_version="v1") is None
