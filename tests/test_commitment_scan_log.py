"""Tests for simplified transcript intake, commitment_scan_log.segments_sha256, and unified KPI admission."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from compute.say_do_extractor import (
    compute_transcript_segments_sha256,
    current_transcript_scan_eligibility,
    record_scan,
    transcripts_pending_extraction,
)
from llm.prompt_versions import prompt_version_for
from models.documents import SourceType
from models.facts import Currency, FactLocator, FiscalPeriodType, SpreadsheetCellRef, Unit
from pipeline.kpi_atomic_admission import ensure_deterministic_kpi_semantic_context
from pipeline.kpi_persistence import KpiExtractionManifest, KpiValue, persist_manifest
from pipeline.kpi_semantics import KpiSemanticStatus, current_kpi_semantic_context
from tests.evidence_support import seed_evidence_document, seed_issuer
from transcripts.acquisition_semantics import (
    is_canonical_transcript_file_path,
    parse_canonical_transcript_filename,
)


def test_migration_0078_adds_segments_sha256_and_drops_rigid_receipt_triggers(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = migrated_db(tmp_path / "wave1.db")
    with sqlite3.connect(db_path) as conn:
        cols = {
            str(r[1]) for r in conn.execute("PRAGMA table_info(commitment_scan_log)").fetchall()
        }
        assert "segments_sha256" in cols

        triggers = {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='transcript_acquisition_receipts'"
            ).fetchall()
        }
        assert "trg_transcript_acquisition_receipts_validate" not in triggers
        assert "trg_transcript_acquisition_receipts_stored_target_binding" not in triggers
        assert "trg_transcript_acquisition_receipts_document_binding" not in triggers
        assert {
            "trg_transcript_acquisition_receipts_no_update",
            "trg_transcript_acquisition_receipts_no_delete",
        } <= triggers

        # Arbitrary new provider names are accepted without requiring a table-rebuild migration.
        sha = "a" * 64
        conn.execute(
            "INSERT INTO transcript_acquisition_receipts "
            "(receipt_id, idempotency_key, document_id, canonical_ticker, fiscal_year, "
            "fiscal_quarter, canonical_document_path, artifact_sha256, artifact_size_bytes, "
            "source_url, provider, source_type, document_type, source_regime, "
            "source_regime_contract_sha256, authorization_json, artifact_json, recorded_at) "
            "VALUES (?, ?, NULL, 'TSM', 2025, 4, 'transcripts/raw/TSM_Q4_2025.pdf', ?, 128, "
            "NULL, 'manual_pdf', 'ir_doc', 'earnings_call_transcript', 'combined', ?, '{}', '{}', "
            "'2026-11-28T00:00:00Z')",
            (sha, f"transcript:{sha}", sha, sha),
        )
        # Append-only guards remain enforced.
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE transcript_acquisition_receipts SET provider='other' WHERE receipt_id=?",
                (sha,),
            )


def test_canonical_transcript_filename_supports_txt_pdf_html_and_digest_suffix() -> None:
    for ext in ("txt", "pdf", "html"):
        assert parse_canonical_transcript_filename(f"TSM_Q4_2025.{ext}") == ("TSM", 2025, 4)
        assert parse_canonical_transcript_filename(f"TSM_Q4_2025__deadbeef.{ext}") == (
            "TSM",
            2025,
            4,
        )
        assert is_canonical_transcript_file_path(
            f"transcripts/raw/TSM_Q4_2025.{ext}",
            canonical_ticker="TSM",
            fiscal_year=2025,
            fiscal_quarter=4,
        )
    assert parse_canonical_transcript_filename("TSM_Q4_2025.docx") is None


def test_commitment_scan_log_tracks_segments_sha256_without_acquisition_receipts(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = migrated_db(tmp_path / "scan_log.db")
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        sha = hashlib.sha256(b"TSM Q4 2025 PDF").hexdigest()
        doc_id = int(
            conn.execute(
                "INSERT INTO documents "
                "(ticker, source_type, doc_type, period_end, file_path, sha256, fetched_at, "
                "fetch_status, raw_bytes_size) VALUES "
                "('TSM', 'ir_doc', 'earnings_call_transcript', '2025-12-31 00:00:00', "
                "'transcripts/raw/TSM_Q4_2025.pdf', ?, '2026-02-01 00:00:00', 'ok', 15)",
                (sha,),
            ).lastrowid
            or 0
        )
        tid = int(
            conn.execute(
                "INSERT INTO transcripts "
                "(document_id, ticker, fiscal_period_type, period_end, source, is_current, is_active) "
                "VALUES (?, 'TSM', 'Q4', '2025-12-31 00:00:00', 'manual_pdf', 1, 1)",
                (doc_id,),
            ).lastrowid
            or 0
        )
        conn.execute(
            "INSERT INTO transcript_segments (transcript_id, seq, speaker, text) "
            "VALUES (?, 0, 'CFO', 'Gross margin expected between 57% and 59%.')",
            (tid,),
        )
        conn.commit()

        # No transcript_acquisition_receipts row exists, yet scan eligibility passes directly.
        elig = current_transcript_scan_eligibility(conn, tid)
        assert elig is not None
        assert elig[0] == doc_id
        assert elig[2] == compute_transcript_segments_sha256(conn, tid)
        assert [row[0] for row in transcripts_pending_extraction(conn, ticker="TSM")] == [tid]

        version = prompt_version_for("saydo_commitment_extract")
        record_scan(conn, tid, n_extracted=0, prompt_version=version)
        assert transcripts_pending_extraction(conn, ticker="TSM") == []

        # Changing prompt_version re-queues automatically.
        conn.execute(
            "UPDATE commitment_scan_log SET prompt_version='old_v0' WHERE transcript_id=?",
            (tid,),
        )
        assert [row[0] for row in transcripts_pending_extraction(conn, ticker="TSM")] == [tid]


def test_deterministic_kpi_manifest_and_helper_auto_admit_on_write_and_replay(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = migrated_db(tmp_path / "kpi_admit.db")
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        seed_issuer(conn, "issuer-nu")
        seed_evidence_document(
            conn,
            doc_id=101,
            ticker="NU",
            issuer_id="issuer-nu",
            period_end="2026-09-30",
            file_path="ir_documents/NU/nu_q3.xlsx",
            sha_char="b",
        )
        doc_id = 101
        manifest = KpiExtractionManifest(
            ticker="NU",
            period_end=datetime(2026, 9, 30),
            fiscal_period_type=FiscalPeriodType.Q3,
            source_doc_id=doc_id,
            primary_source=SourceType.IR_DOC,
            extracted_by="ir_spreadsheet",
            values=[
                KpiValue(
                    name="Monthly ARPAC",
                    value=Decimal("12.4"),
                    unit=Unit.ACTUAL,
                    currency=Currency.USD,
                    locator=FactLocator(
                        spreadsheet_cell=SpreadsheetCellRef(sheet_name="P&L", cell_address="G14")
                    ),
                )
            ],
        )
        res = persist_manifest(conn, run_id="r-sheet", manifest=manifest)
        assert res.inserted == 1
        fact_id = int(
            conn.execute("SELECT id FROM kpi_facts ORDER BY id DESC LIMIT 1").fetchone()[0]
        )
        ctx = current_kpi_semantic_context(conn, kpi_fact_id=fact_id)
        assert ctx is not None and ctx.context.status is KpiSemanticStatus.ADMITTED

        # Idempotent replay helper does not duplicate an admitted context and upgrades missing ones.
        ctx_id = ensure_deterministic_kpi_semantic_context(
            conn,
            kpi_fact_id=fact_id,
            metric_name="Monthly ARPAC",
            period_end=datetime(2026, 9, 30).date(),
            value=Decimal("12.4"),
            unit=Unit.ACTUAL,
            extracted_by="ir_spreadsheet",
            knowledge_at=datetime.now(UTC),
        )
        assert ctx_id == ctx.id
