"""Regression tests for the unified document, transcript, and KPI admission pipeline."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from execution import audit_transcript_evidence
from models.facts import (
    FactLocator,
    FiscalPeriodType,
    LocatorKind,
    SpreadsheetCellRef,
    TableCellRef,
    Unit,
)
from pipeline import transcript_acquisition
from pipeline.commitment_scan_receipts import current_transcript_scan_binding
from pipeline.kpi_atomic_admission import build_atomic_semantic_context
from pipeline.kpi_semantics import KpiSemanticStatus, current_kpi_semantic_context
from pipeline.transcript_acquisition import admit_retained_transcripts
from transcripts.acquisition_semantics import (
    is_canonical_issuer_transcript_path,
    is_canonical_transcript_file_path,
)
from transcripts.immutable_staging import StagedTranscriptArtifact


def _install_darwin_staging_doubles(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform != "darwin":
        return

    def fake_stage(
        source_path: Path,
        private_root: Path,
        *,
        expected_sha256: str,
        expected_size_bytes: int,
        max_bytes: int,
    ) -> StagedTranscriptArtifact:
        del max_bytes
        source = source_path.resolve()
        root = private_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        payload = source.read_bytes()
        assert len(payload) == expected_size_bytes
        assert hashlib.sha256(payload).hexdigest() == expected_sha256
        staged = root / f"{expected_sha256}.transcript"
        staged.write_bytes(payload)
        s_stat = source.stat()
        r_stat = root.stat()
        return StagedTranscriptArtifact(
            source_path=source,
            source_device=int(s_stat.st_dev),
            source_inode=int(s_stat.st_ino),
            staging_root=root,
            staging_root_device=int(r_stat.st_dev),
            staging_root_inode=int(r_stat.st_ino),
            staged_path=staged,
            sha256=expected_sha256,
            size_bytes=expected_size_bytes,
        )

    def fake_read(artifact: StagedTranscriptArtifact, **_kwargs: object) -> bytes:
        return artifact.staged_path.read_bytes()

    monkeypatch.setattr(transcript_acquisition, "stage_transcript_artifact", fake_stage)
    monkeypatch.setattr(transcript_acquisition, "read_staged_transcript", fake_read)


def test_canonical_transcript_path_predicates() -> None:
    sha = "a" * 64
    assert is_canonical_issuer_transcript_path(
        "ir_documents/BKNG/2026-09-30/ir_transcript__1.pdf", "BKNG"
    )
    assert not is_canonical_issuer_transcript_path(
        "ir_documents/OTHER/2026-09-30/ir_transcript__1.pdf", "BKNG"
    )
    assert not is_canonical_issuer_transcript_path("../ir_documents/BKNG/x.pdf", "BKNG")
    assert is_canonical_transcript_file_path(
        "transcripts/raw/MELI_Q1_2025.txt",
        canonical_ticker="MELI",
        fiscal_year=2025,
        fiscal_quarter=1,
    )
    assert is_canonical_transcript_file_path(
        "transcripts/processed/MELI_Q1_2025.txt",
        canonical_ticker="MELI",
        fiscal_year=2025,
        fiscal_quarter=1,
    )
    assert is_canonical_transcript_file_path(
        f"transcripts/raw/.evidence/{sha}/MELI_Q1_2025.txt",
        canonical_ticker="MELI",
        fiscal_year=2025,
        fiscal_quarter=1,
        sha256=sha,
    )
    assert not is_canonical_transcript_file_path(
        f"transcripts/raw/.evidence/{'b' * 64}/MELI_Q1_2025.txt",
        canonical_ticker="MELI",
        fiscal_year=2025,
        fiscal_quarter=1,
        sha256=sha,
    )


def test_admit_retained_transcripts_end_to_end_and_fail_closed(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_darwin_staging_doubles(monkeypatch)
    repo_root = tmp_path / "scratch"
    staging_root = repo_root / ".tmp" / "transcript-acquisition"
    db_path = migrated_db(repo_root / "data" / "portfolio.db")

    # 1. Retained issuer IR transcript with HTTPS URL (8 quarters back)
    ir_https_rel = "ir_documents/BKNG/2024-12-31/ir_transcript__https.pdf"
    ir_https_bytes = b"%PDF-1.4\nBKNG Q4 2024 HTTPS"
    (repo_root / ir_https_rel).parent.mkdir(parents=True, exist_ok=True)
    (repo_root / ir_https_rel).write_bytes(ir_https_bytes)

    # 2. Retained issuer IR transcript with reindex_subdir: URL
    ir_reindex_rel = "ir_documents/BKNG/2025-03-31/ir_transcript__reindex.pdf"
    ir_reindex_bytes = b"%PDF-1.4\nBKNG Q1 2025 REINDEX"
    (repo_root / ir_reindex_rel).parent.mkdir(parents=True, exist_ok=True)
    (repo_root / ir_reindex_rel).write_bytes(ir_reindex_bytes)

    # 3. Retained legacy aggregator transcript under transcripts/raw/
    agg_rel = "transcripts/raw/MELI_Q4_2024.txt"
    agg_bytes = b"MELI Q4 2024 earnings call transcript"
    (repo_root / agg_rel).parent.mkdir(parents=True, exist_ok=True)
    (repo_root / agg_rel).write_bytes(agg_bytes)

    # 4. Tampered artifact (hash mismatch must fail closed)
    tampered_rel = "transcripts/processed/MELI_Q1_2025.txt"
    (repo_root / tampered_rel).parent.mkdir(parents=True, exist_ok=True)
    (repo_root / tampered_rel).write_bytes(b"tampered bytes")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type, fiscal_year_end, instrument_type) "
            "VALUES ('BKNG', 'Booking Holdings', 'portfolio', '12-31', 'equity'), "
            "('MELI', 'MercadoLibre', 'portfolio', '12-31', 'equity')"
        )
        docs = [
            (
                1,
                "BKNG",
                "ir_doc",
                "ir_transcript",
                "2024-12-31",
                ir_https_rel,
                hashlib.sha256(ir_https_bytes).hexdigest(),
                len(ir_https_bytes),
                "https://s25.q4cdn.com/383369491/q4-2024.pdf",
            ),
            (
                2,
                "BKNG",
                "ir_doc",
                "ir_transcript",
                "2025-03-31",
                ir_reindex_rel,
                hashlib.sha256(ir_reindex_bytes).hexdigest(),
                len(ir_reindex_bytes),
                "reindex_subdir:ir_transcript__reindex.pdf",
            ),
            (
                3,
                "MELI",
                "transcript_audio",
                "earnings_call_transcript",
                "2024-12-31",
                agg_rel,
                hashlib.sha256(agg_bytes).hexdigest(),
                len(agg_bytes),
                None,
            ),
            (
                4,
                "MELI",
                "transcript_audio",
                "earnings_call_transcript",
                "2025-03-31",
                tampered_rel,
                hashlib.sha256(b"original bytes").hexdigest(),
                len(b"tampered bytes"),
                None,
            ),
        ]
        conn.executemany(
            "INSERT INTO documents (id, ticker, source_type, doc_type, period_end, file_path, "
            "sha256, fetched_at, fetch_status, raw_bytes_size, source_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, '2026-01-01T00:00:00', 'ok', ?, ?)",
            docs,
        )
        transcripts = [
            (1, 1, "BKNG", "2025-02-20", "Q4", "2024-12-31", "ir_transcript_doc"),
            (2, 2, "BKNG", "2025-05-01", "Q1", "2025-03-31", "ir_transcript_doc"),
            (3, 3, "MELI", "2025-02-20", "Q4", "2024-12-31", "aggregator_roic"),
            (4, 4, "MELI", "2025-05-01", "Q1", "2025-03-31", "aggregator_roic"),
        ]
        conn.executemany(
            "INSERT INTO transcripts (id, document_id, ticker, call_date, fiscal_period_type, "
            "period_end, source, is_active, is_current, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, '2026-01-01T00:00:00')",
            transcripts,
        )
        conn.executemany(
            "INSERT INTO transcript_segments (transcript_id, seq, speaker, text) VALUES (?, 0, 'CEO', ?)",
            [(1, "seg1"), (2, "seg2"), (3, "seg3"), (4, "seg4")],
        )
        conn.commit()

        # Dry run touches nothing
        dry = admit_retained_transcripts(
            conn,
            project_root=repo_root,
            private_root=staging_root,
            as_of=date(2026, 11, 26),
            apply=False,
        )
        assert dry.scanned == 4
        assert dry.eligible == 3
        assert dry.admitted == 0
        assert dry.skipped_counts == {"artifact_hash_mismatch": 1}
        assert (
            conn.execute("SELECT COUNT(*) FROM transcript_acquisition_receipts").fetchone()[0] == 0
        )

        # Apply persists 3 genuine receipts passing all SQLite triggers & binds all 3
        applied = admit_retained_transcripts(
            conn,
            project_root=repo_root,
            private_root=staging_root,
            as_of=date(2026, 11, 26),
            apply=True,
        )
        assert applied.admitted == 3
        assert applied.skipped_counts == {"artifact_hash_mismatch": 1}
        assert current_transcript_scan_binding(conn, 1) is not None
        assert current_transcript_scan_binding(conn, 2) is not None
        assert current_transcript_scan_binding(conn, 3) is not None
        assert current_transcript_scan_binding(conn, 4) is None

        # Second run is completely idempotent
        rerun = admit_retained_transcripts(
            conn,
            project_root=repo_root,
            private_root=staging_root,
            as_of=date(2026, 11, 26),
            apply=True,
        )
        assert rerun.already_bound == 3
        assert rerun.admitted == 0

    # audit_transcript_evidence recognizes ir_documents/ paths as valid (not unsafe_path)
    audit = audit_transcript_evidence.audit_transcript_evidence(repo_root, db_path, ticker="BKNG")
    assert audit.documents_scanned == 2
    assert audit.failures == 0


def test_admit_retained_legacy_raw_path_falls_back_to_processed_and_fails_closed(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_darwin_staging_doubles(monkeypatch)
    repo_root = tmp_path / "scratch"
    staging_root = repo_root / ".tmp" / "transcript-acquisition"
    db_path = migrated_db(repo_root / "data" / "portfolio.db")

    q1_bytes = b"AMZN Q1 2026 retained call transcript"
    q1_processed = repo_root / "transcripts" / "processed" / "AMZN_Q1_2026.txt"
    q1_processed.parent.mkdir(parents=True, exist_ok=True)
    q1_processed.write_bytes(q1_bytes)

    q2_bytes = b"AMZN Q2 2026 retained call transcript"
    q2_processed = repo_root / "transcripts" / "processed" / "AMZN_Q2_2026.txt"
    q2_processed.write_bytes(q2_bytes)
    q2_raw = repo_root / "transcripts" / "raw" / "AMZN_Q2_2026.txt"
    q2_raw.parent.mkdir(parents=True, exist_ok=True)
    q2_raw.write_bytes(b"newer mismatched bytes in raw")

    q3_processed = repo_root / "transcripts" / "processed" / "AMZN_Q3_2026.txt"
    q3_processed.write_bytes(b"corrupted processed bytes")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type, fiscal_year_end, instrument_type) "
            "VALUES ('AMZN', 'Amazon.com, Inc.', 'portfolio', '12-31', 'equity')"
        )
        conn.executemany(
            "INSERT INTO documents (id, ticker, source_type, doc_type, period_end, file_path, "
            "sha256, fetched_at, fetch_status, raw_bytes_size, source_url) "
            "VALUES (?, 'AMZN', 'transcript_audio', 'earnings_call_transcript', ?, ?, ?, "
            "'2026-01-01T00:00:00', 'ok', ?, NULL)",
            [
                (
                    1,
                    "2026-03-31",
                    "transcripts/raw/AMZN_Q1_2026.txt",
                    hashlib.sha256(q1_bytes).hexdigest(),
                    len(q1_bytes),
                ),
                (
                    2,
                    "2026-06-30",
                    "transcripts/raw/AMZN_Q2_2026.txt",
                    hashlib.sha256(q2_bytes).hexdigest(),
                    len(q2_bytes),
                ),
                (
                    3,
                    "2026-09-30",
                    "transcripts/raw/AMZN_Q3_2026.txt",
                    hashlib.sha256(b"expected q3 bytes").hexdigest(),
                    len(b"expected q3 bytes"),
                ),
            ],
        )
        conn.executemany(
            "INSERT INTO transcripts (id, document_id, ticker, call_date, fiscal_period_type, "
            "period_end, source, is_active, is_current, recorded_at) "
            "VALUES (?, ?, 'AMZN', ?, ?, ?, 'aggregator_roic', 1, 1, '2026-01-01T00:00:00')",
            [
                (1, 1, "2026-05-01", "Q1", "2026-03-31"),
                (2, 2, "2026-07-30", "Q2", "2026-06-30"),
                (3, 3, "2026-10-30", "Q3", "2026-09-30"),
            ],
        )
        conn.executemany(
            "INSERT INTO transcript_segments (transcript_id, seq, speaker, text) VALUES (?, 0, 'CEO', 'seg')",
            [(1,), (2,), (3,)],
        )
        conn.commit()

        report = admit_retained_transcripts(
            conn,
            project_root=repo_root,
            private_root=staging_root,
            as_of=date(2026, 11, 26),
            apply=True,
        )
        assert report.admitted == 2
        assert report.skipped_counts == {"artifact_missing_or_unsafe": 1}
        assert current_transcript_scan_binding(conn, 1) is not None
        assert current_transcript_scan_binding(conn, 2) is not None
        assert current_transcript_scan_binding(conn, 3) is None


def test_authorized_ir_doc_supersedes_unreceipted_legacy_and_stops_churn(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from compute import transcript_ingest
    from models.documents import DocType, SourceType
    from pipeline.transcript_acquisition import (
        COMBINED_SOURCE_REGIME_IDENTITY,
        TRANSCRIPT_ACQUISITION_POLICY_VERSION,
        ExistingArtifactBehavior,
        TranscriptAcquisitionEntrypoint,
        TranscriptAcquisitionRequest,
        TranscriptProvider,
        authorize_transcript_request,
        persist_authorized_transcript_artifact,
        stage_authorized_payload,
    )

    _install_darwin_staging_doubles(monkeypatch)
    repo_root = tmp_path / "scratch"
    staging_root = repo_root / ".tmp" / "transcript-acquisition"
    db_path = migrated_db(repo_root / "data" / "portfolio.db")

    legacy_bytes = b"legacy unreceipted aggregator transcript"
    payload_two = (
        b"=== SYNTHESIZED QUARTERLY UPDATE: AMZN Q2 2026 ===\n"
        b"Source: aggregator_roic\n\n"
        b"John Doe: First segment for Q2 2026.\n\n"
        b"Jane Smith: Second segment for Q2 2026.\n"
    )
    payload_three = (
        b"=== SYNTHESIZED QUARTERLY UPDATE: AMZN Q2 2026 ===\n"
        b"Source: aggregator_roic\n\n"
        b"John Doe: Updated wording for Q2 2026.\n\n"
        b"Jane Smith: Another wording for Q2 2026.\n"
    )
    turns_two = transcript_ingest.segment_by_speaker(payload_two.decode("utf-8"))
    turns_three = transcript_ingest.segment_by_speaker(payload_three.decode("utf-8"))
    assert len(turns_two) == len(turns_three)

    raw_rel = Path("transcripts/raw/AMZN_Q2_2026.txt")
    (repo_root / raw_rel).parent.mkdir(parents=True, exist_ok=True)
    (repo_root / raw_rel).write_bytes(legacy_bytes)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type, fiscal_year_end, instrument_type) "
            "VALUES ('AMZN', 'Amazon.com, Inc.', 'portfolio', '12-31', 'equity')"
        )
        conn.execute(
            "INSERT INTO documents (id, ticker, source_type, doc_type, period_end, file_path, "
            "sha256, fetched_at, fetch_status, raw_bytes_size, source_url) "
            "VALUES (1, 'AMZN', 'transcript_audio', 'earnings_call_transcript', '2026-06-30 00:00:00', ?, ?, "
            "'2026-01-01T00:00:00', 'ok', ?, NULL)",
            (raw_rel.as_posix(), hashlib.sha256(legacy_bytes).hexdigest(), len(legacy_bytes)),
        )
        conn.execute(
            "INSERT INTO transcripts (id, document_id, ticker, call_date, fiscal_period_type, "
            "period_end, source, is_active, is_current, recorded_at) "
            "VALUES (1, 1, 'AMZN', '2026-07-30', 'Q2', '2026-06-30 00:00:00', 'aggregator_roic', "
            "1, 1, '2026-01-01T00:00:00')"
        )
        conn.executemany(
            "INSERT INTO transcript_segments (transcript_id, seq, speaker, text) VALUES (1, ?, 'Speaker', 'seg')",
            [(idx,) for idx in range(len(turns_two))],
        )
        conn.commit()

        def _authorize_and_persist(payload: bytes, suffix: str) -> transcript_ingest.IngestResult:
            req = TranscriptAcquisitionRequest(
                schema_version="transcript-acquisition-request@2",
                entrypoint=TranscriptAcquisitionEntrypoint.FETCH_QA_TRANSCRIPT,
                canonical_ticker="AMZN",
                fiscal_year=2026,
                fiscal_quarter=2,
                as_of=date(2026, 11, 26),
                source_type=SourceType.IR_DOC,
                document_type=DocType.EARNINGS_CALL_TRANSCRIPT,
                provider=TranscriptProvider.ROIC,
                owner_requested=True,
                existing_artifact=False,
                existing_artifact_behavior=ExistingArtifactBehavior.REFRESH,
                source_policy_version=TRANSCRIPT_ACQUISITION_POLICY_VERSION,
                source_regime_identity=COMBINED_SOURCE_REGIME_IDENTITY,
            )
            auth = authorize_transcript_request(conn, req)
            staged = stage_authorized_payload(
                auth,
                payload=payload,
                private_root=staging_root,
                source_url=f"https://www.roic.ai/quote/AMZN/transcripts/2026-year/2-quarter{suffix}",
                canonical_document_path=raw_rel,
            )
            persist_authorized_transcript_artifact(
                conn,
                staged,
                project_root=repo_root,
                trusted_staging_root=staging_root,
            )
            evidence_path = (
                repo_root / "transcripts" / "raw" / ".evidence" / staged.sha256 / raw_rel.name
            )
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_bytes(payload)
            ingest_result = transcript_ingest.ingest_evidence_file(
                conn,
                file_path=evidence_path,
                allowed_root=evidence_path.parent,
                project_root=repo_root,
                tracked_tickers=frozenset({"AMZN"}),
                authorized_artifact=staged,
                commit=True,
            )
            assert ingest_result is not None
            return ingest_result

        # Authorized receipted ir_doc supersedes unreceipted legacy row even on equal segments.
        promoted = _authorize_and_persist(payload_two, "")
        assert not promoted.skipped_existing
        assert promoted.transcript_id is not None
        assert promoted.transcript_id != 1
        assert current_transcript_scan_binding(conn, promoted.transcript_id) is not None
        row_after_promote = conn.execute(
            "SELECT is_current, superseded_by_transcript_id FROM transcripts WHERE id = 1"
        ).fetchone()
        assert tuple(row_after_promote) == (0, promoted.transcript_id)

        # Re-ingesting another equal-segment authorized ir_doc does NOT supersede/churn.
        reingest = _authorize_and_persist(payload_three, "?retry=1")
        assert reingest.skipped_existing
        assert reingest.transcript_id == promoted.transcript_id
        assert (
            conn.execute(
                "SELECT id FROM transcripts WHERE ticker = 'AMZN' AND is_current = 1"
            ).fetchone()[0]
            == promoted.transcript_id
        )


def test_structured_deterministic_locator_admits_without_prose_excerpt() -> None:
    xbrl_ctx = build_atomic_semantic_context(
        metric_name="Operating lease right-of-use asset",
        period_end=date(2025, 12, 31),
        value=Decimal("500000000"),
        unit=Unit.ACTUAL,
        extracted_by="capture_xbrl_v1",
        locator=FactLocator(
            kind=LocatorKind.FMP_JSON_TABLE,
            table_cell=TableCellRef(
                section="Leases (Details)",
                row_label="Operating lease right-of-use asset",
                column_header="2025-12-31",
            ),
        ),
        source_excerpt=None,
    )
    assert xbrl_ctx.status is KpiSemanticStatus.ADMITTED

    cell_ctx = build_atomic_semantic_context(
        metric_name="Active customers",
        period_end=date(2025, 12, 31),
        value=Decimal("1000"),
        unit=Unit.ACTUAL,
        extracted_by="ir_spreadsheet",
        locator=FactLocator(
            kind=LocatorKind.SPREADSHEET_CELL,
            spreadsheet_cell=SpreadsheetCellRef(sheet_name="Summary", cell_address="C12"),
        ),
        source_excerpt=None,
    )
    assert cell_ctx.status is KpiSemanticStatus.ADMITTED

    # Unstructured PDF readout without excerpt still fails closed
    with pytest.raises(ValueError, match="non-empty source_excerpt"):
        build_atomic_semantic_context(
            metric_name="Active customers",
            period_end=date(2025, 12, 31),
            value=Decimal("1000"),
            unit=Unit.ACTUAL,
            extracted_by="ir_press_release_readout",
            locator=FactLocator(pdf_page=4),
            source_excerpt=None,
        )


def test_metrics_engine_persist_auto_admits_semantic_context(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    from compute.metrics_engine.engine import ComputedValue
    from compute.metrics_engine.inputs import CanonicalConcept
    from compute.metrics_engine.io import persist_attempt, upsert_formula_definitions
    from compute.metrics_engine.registry import REGISTRY

    db_path = migrated_db(tmp_path / "metrics-auto-admit.db")
    sha = "a" * 64
    stamp = "2026-11-01T00:00:00Z"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO issuer_entities (issuer_id, idempotency_key, entity_kind, created_at) "
            "VALUES ('issuer-acme', 'issuer:acme', 'operating_company', ?)",
            (stamp,),
        )
        conn.execute(
            "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256, "
            "fetched_at, fetch_status, raw_bytes_size) "
            "VALUES (1, 'ACME', 'sec_xbrl', 'form_10q', 'sec/acme_q3.json', ?, ?, 'ok', 100)",
            (sha, stamp),
        )
        conn.execute(
            "INSERT INTO evidence_content_blobs (sha256, byte_size, media_type, storage_uri, recorded_at) "
            "VALUES (?, 100, 'application/json', 'sec/acme_q3.json', ?)",
            (sha, stamp),
        )
        conn.execute(
            "INSERT INTO evidence_source_observations "
            "(observation_id, idempotency_key, source_kind, source_url, blob_sha256, observed_at, "
            "retrieved_at, retrieval_config_sha256, collector_code_version) VALUES "
            "('obs-acme-1', 'obs:acme:1', 'sec_filing', 'https://sec.gov/acme', ?, ?, ?, ?, 'v1')",
            (sha, stamp, stamp, "b" * 64),
        )
        conn.execute(
            "INSERT INTO evidence_document_versions "
            "(document_version_id, document_key, version_sequence, observation_id, blob_sha256, "
            "issuer_id, ticker, document_type, form_type, period_end, language, legacy_document_id, "
            "recorded_at) VALUES "
            "('docver-acme-1', 'doc:acme:1', 1, 'obs-acme-1', ?, 'issuer-acme', 'ACME', 'form_10q', "
            "'10-Q', '2026-09-30', 'en', 1, ?)",
            (sha, stamp),
        )
        conn.execute(
            "INSERT INTO evidence_extraction_runs "
            "(extraction_run_id, idempotency_key, document_version_id, input_sha256, "
            "extractor_name, extractor_config_sha256, extractor_code_version, output_sha256, "
            "started_at, completed_at, outcome) VALUES "
            "('run-acme-1', 'run:acme:1', 'docver-acme-1', ?, 'test', ?, 'v1', ?, ?, ?, 'succeeded')",
            (sha, "c" * 64, "d" * 64, stamp, stamp),
        )
        conn.execute(
            "INSERT INTO evidence_nodes "
            "(node_id, evidence_key, revision, extraction_run_id, parent_node_id, supersedes_node_id, "
            "node_kind, text, locator_json, locator_sha256, recorded_at) VALUES "
            "('node-acme-1', 'node:acme:1', 1, 'run-acme-1', NULL, NULL, 'document', '10-Q.', NULL, NULL, ?)",
            (stamp,),
        )
        formula_ids = upsert_formula_definitions(conn)
        conn.commit()
        key = next(k for k in REGISTRY if k[0] == "gross_margin")
        formula = REGISTRY[key]
        formula_id = formula_ids[key]
        period = datetime(2026, 9, 30)
        persist_attempt(
            conn,
            ticker="ACME",
            period_end=period,
            fiscal_period_type=FiscalPeriodType.Q3.value,
            formula=formula,
            formula_id=formula_id,
            result=ComputedValue(value=Decimal("60.0")),
            lineage=[
                (CanonicalConcept.REVENUE, period, 1),
                (CanonicalConcept.GROSS_PROFIT, period, 1),
            ],
        )
        conn.commit()
        row = conn.execute(
            "SELECT kpi_fact_id FROM metric_computation_attempts WHERE ticker='ACME' AND formula_id=?",
            (formula_id,),
        ).fetchone()
        assert row is not None and row["kpi_fact_id"] is not None
        ctx = current_kpi_semantic_context(conn, kpi_fact_id=int(row["kpi_fact_id"]))
        assert ctx is not None
        assert ctx.context.status is KpiSemanticStatus.ADMITTED
        assert ctx.reviewed_by == "deterministic:metrics_engine"
