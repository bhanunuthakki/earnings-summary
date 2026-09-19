from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from pipeline.commitment_scan_receipts import current_transcript_scan_binding


@pytest.mark.parametrize(("matching_digest", "expected"), [(True, True), (False, False)])
def test_content_addressed_conflict_evidence_binding(
    matching_digest: bool,
    expected: bool,
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    path = migrated_db(tmp_path / f"content-addressed-{matching_digest}.db")
    transcript_text = "exact issuer transcript"
    digest = hashlib.sha256(transcript_text.encode()).hexdigest()
    path_digest = digest if matching_digest else "0" * 64
    artifact = {
        "authorization": {
            "idempotency_key": "transcript:" + "1" * 64,
            "request": {
                "canonical_ticker": "ACME",
                "document_type": "earnings_call_transcript",
                "fiscal_quarter": 2,
                "fiscal_year": 2026,
                "provider": "issuer_ir",
                "source_regime_identity": {
                    "contract_sha256": "2" * 64,
                    "regime": "combined",
                },
                "source_type": "ir_doc",
            },
            "schema_version": "transcript-acquisition-authorization@1",
            "status": "authorized",
            "stored_target": {"coverage_role": "holdings", "fiscal_year_end_month": 12},
        },
        "canonical_document_path": "transcripts/raw/ACME_Q2_2026.txt",
        "document_id": 1,
        "schema_version": "authorized-transcript-artifact@1",
        "source_url": None,
        "staged": {"sha256": digest, "size_bytes": len(transcript_text)},
    }
    artifact_json = json.dumps(artifact, sort_keys=True, separators=(",", ":"))
    receipt_id = hashlib.sha256(artifact_json.encode()).hexdigest()
    authorization_json = json.dumps(
        artifact["authorization"], sort_keys=True, separators=(",", ":")
    )
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO documents "
            "(id,ticker,source_type,doc_type,file_path,sha256,fetched_at,fetch_status,raw_bytes_size) "
            "VALUES (1,'ACME','ir_doc','ir_transcript',?,?,?,'ok',?)",
            (
                f"transcripts/raw/.evidence/{path_digest}/ACME_Q2_2026.txt",
                digest,
                "2026-07-01",
                len(transcript_text),
            ),
        )
        conn.execute(
            "INSERT INTO transcripts "
            "(id,document_id,ticker,call_date,fiscal_period_type,period_end,source,is_active,"
            "is_current,recorded_at) VALUES "
            "(1,1,'ACME','2026-07-01','Q2','2026-06-30','issuer_ir',1,1,'2026-07-01')"
        )
        conn.execute(
            "INSERT INTO transcript_segments "
            "(transcript_id,seq,speaker,time_code_start,time_code_end,text) "
            "VALUES (1,0,'CEO','00:00:00',NULL,?)",
            (transcript_text,),
        )
        for trigger in (
            "trg_transcript_acquisition_receipts_validate",
            "trg_transcript_acquisition_receipts_stored_target_binding",
            "trg_transcript_acquisition_receipts_document_binding",
        ):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute(
            "INSERT INTO transcript_acquisition_receipts "
            "(receipt_id,idempotency_key,document_id,canonical_ticker,fiscal_year,fiscal_quarter,"
            "canonical_document_path,artifact_sha256,artifact_size_bytes,source_url,provider,"
            "source_type,document_type,source_regime,source_regime_contract_sha256,"
            "authorization_json,artifact_json,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                receipt_id,
                "transcript:" + "1" * 64,
                1,
                "ACME",
                2026,
                2,
                "transcripts/raw/ACME_Q2_2026.txt",
                digest,
                len(transcript_text),
                None,
                "issuer_ir",
                "ir_doc",
                "earnings_call_transcript",
                "combined",
                "2" * 64,
                authorization_json,
                artifact_json,
                "2026-09-05T00:00:00Z",
            ),
        )
        conn.commit()
        assert (current_transcript_scan_binding(conn, 1) is not None) is expected
