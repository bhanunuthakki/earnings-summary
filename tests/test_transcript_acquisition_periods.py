"""Canonical period resolution tests for pipeline.transcript_acquisition."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from models.documents import DocType, SourceType
from pipeline import transcript_acquisition
from pipeline.source_policy import ReportedPeriodStatus, ReportedPeriodWindow
from pipeline.transcript_acquisition import (
    COMBINED_SOURCE_REGIME_IDENTITY,
    TranscriptAcquisitionDeniedError,
    authorize_transcript_request,
    persist_authorized_transcript_artifact,
    stage_pending_issuer_transcripts,
)
from transcripts.acquisition_semantics import (
    TRANSCRIPT_ACQUISITION_POLICY_VERSION,
    ExistingArtifactBehavior,
    TranscriptAcquisitionEntrypoint,
    TranscriptAcquisitionRequest,
    TranscriptAuthorizationStatus,
    TranscriptProvider,
)
from transcripts.immutable_staging import StagedTranscriptArtifact


def _install_staging_double(monkeypatch: pytest.MonkeyPatch) -> None:
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

    monkeypatch.setattr(transcript_acquisition, "stage_transcript_artifact", fake_stage)


def _install_qualified_window(monkeypatch: pytest.MonkeyPatch) -> None:
    def qualified_window(
        _conn: sqlite3.Connection, *, ticker: str, as_of: date, max_quarters: int = 8
    ) -> ReportedPeriodWindow:
        del as_of, max_quarters
        if ticker.upper() == "VEEV":
            return ReportedPeriodWindow(
                status=ReportedPeriodStatus.COMPLETE,
                period_ends=(date(2026, 7, 31),),
                snapshot_ids=("synthetic-sec", "synthetic-ir"),
                fiscal_year_end="01-31",
                proof_sha256="a" * 64,
                reason="test_fixture",
            )
        return ReportedPeriodWindow(
            status=ReportedPeriodStatus.COMPLETE,
            period_ends=(date(2026, 9, 30),),
            snapshot_ids=("synthetic-sec", "synthetic-ir"),
            fiscal_year_end="12-31",
            proof_sha256="a" * 64,
            reason="test_fixture",
        )

    monkeypatch.setattr(
        transcript_acquisition, "reported_period_window_from_inventory", qualified_window
    )


def _setup_db(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tracked_companies ("
        "ticker TEXT, list_type TEXT, archived_at TEXT, fiscal_year_end TEXT, instrument_type TEXT)"
    )
    conn.execute(
        "CREATE TABLE documents (id INTEGER PRIMARY KEY, ticker TEXT, source_type TEXT, "
        "doc_type TEXT, period_end TEXT, file_path TEXT, sha256 TEXT, "
        "raw_bytes_size INTEGER, source_url TEXT)"
    )
    conn.execute("CREATE TABLE transcripts (document_id INTEGER)")


def test_stages_canonical_content_addressed_transcript_by_period_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_staging_double(monkeypatch)
    _install_qualified_window(monkeypatch)
    repo_root = tmp_path / "repo"
    staging_root = repo_root / ".tmp" / "transcript-acquisition"
    staging_root.mkdir(parents=True)

    nu_rel = "ir_documents/NU/2026-09-30/ir_transcript__a1b2c3d4.pdf"
    nu_file = repo_root / nu_rel
    nu_file.parent.mkdir(parents=True)
    nu_bytes = b"%PDF-1.4\nNU Q3 transcript"
    nu_file.write_bytes(nu_bytes)

    veev_rel = "ir_documents/VEEV/2026-07-31/ir_transcript__3b838a1c.pdf"
    veev_file = repo_root / veev_rel
    veev_file.parent.mkdir(parents=True)
    veev_bytes = b"%PDF-1.4\nVEEV FY27 Q2 transcript"
    veev_file.write_bytes(veev_bytes)

    with sqlite3.connect(repo_root / "test.db") as conn:
        _setup_db(conn)
        conn.execute(
            "INSERT INTO tracked_companies VALUES ('NU', 'portfolio', NULL, '12-31', 'equity')"
        )
        conn.execute(
            "INSERT INTO tracked_companies VALUES ('VEEV', 'portfolio', NULL, '01-31', 'equity')"
        )
        conn.execute(
            "INSERT INTO documents VALUES (1, 'NU', ?, ?, '2026-09-30T00:00:00', ?, ?, ?, ?)",
            (
                SourceType.IR_DOC.value,
                DocType.IR_TRANSCRIPT.value,
                nu_rel,
                hashlib.sha256(nu_bytes).hexdigest(),
                len(nu_bytes),
                "https://issuer.example.invalid/nu-q3-transcript.pdf",
            ),
        )
        conn.execute(
            "INSERT INTO documents VALUES (2, 'VEEV', ?, ?, '2026-07-31T00:00:00', ?, ?, ?, ?)",
            (
                SourceType.IR_DOC.value,
                DocType.IR_TRANSCRIPT.value,
                veev_rel,
                hashlib.sha256(veev_bytes).hexdigest(),
                len(veev_bytes),
                "https://issuer.example.invalid/veev-q2-transcript.pdf",
            ),
        )
        artifacts = stage_pending_issuer_transcripts(
            conn,
            tickers=["NU", "VEEV"],
            project_root=repo_root,
            private_root=staging_root,
            entrypoint=TranscriptAcquisitionEntrypoint.QUARTERLY_REFRESH,
            as_of=date(2026, 11, 22),
        )

    assert artifacts[1].authorization.status is TranscriptAuthorizationStatus.AUTHORIZED
    assert artifacts[1].authorization.request.fiscal_year == 2026
    assert artifacts[1].authorization.request.fiscal_quarter == 3

    assert artifacts[2].authorization.status is TranscriptAuthorizationStatus.AUTHORIZED
    assert artifacts[2].authorization.request.fiscal_year == 2027
    assert artifacts[2].authorization.request.fiscal_quarter == 2


def test_denies_transcript_without_legacy_name_or_period_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_staging_double(monkeypatch)
    repo_root = tmp_path / "repo"
    staging_root = repo_root / ".tmp" / "transcript-acquisition"
    staging_root.mkdir(parents=True)

    bad_rel = "ir_documents/NU/unrecognized_name.pdf"
    bad_file = repo_root / bad_rel
    bad_file.parent.mkdir(parents=True)
    bad_bytes = b"%PDF-1.4\nunknown period"
    bad_file.write_bytes(bad_bytes)

    with sqlite3.connect(repo_root / "test.db") as conn:
        _setup_db(conn)
        conn.execute(
            "INSERT INTO tracked_companies VALUES ('NU', 'portfolio', NULL, '12-31', 'equity')"
        )
        conn.execute(
            "INSERT INTO documents VALUES (1, 'NU', ?, ?, NULL, ?, ?, ?, ?)",
            (
                SourceType.IR_DOC.value,
                DocType.IR_TRANSCRIPT.value,
                bad_rel,
                hashlib.sha256(bad_bytes).hexdigest(),
                len(bad_bytes),
                "https://issuer.example.invalid/unknown.pdf",
            ),
        )
        with pytest.raises(TranscriptAcquisitionDeniedError, match="canonical period identity"):
            stage_pending_issuer_transcripts(
                conn,
                tickers=["NU"],
                project_root=repo_root,
                private_root=staging_root,
                entrypoint=TranscriptAcquisitionEntrypoint.QUARTERLY_REFRESH,
                as_of=date(2026, 11, 22),
            )


def test_rejects_persisted_transcript_under_mismatched_ticker_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_staging_double(monkeypatch)
    _install_qualified_window(monkeypatch)
    repo_root = tmp_path / "repo"
    staging_root = repo_root / ".tmp" / "transcript-acquisition"
    staging_root.mkdir(parents=True)

    wrong_rel = "ir_documents/OTHER/2026-09-30/ir_transcript__a1b2c3d4.pdf"
    wrong_file = repo_root / wrong_rel
    wrong_file.parent.mkdir(parents=True)
    wrong_bytes = b"%PDF-1.4\nwrong ticker folder"
    wrong_file.write_bytes(wrong_bytes)

    with sqlite3.connect(repo_root / "test.db") as conn:
        _setup_db(conn)
        conn.execute(
            "INSERT INTO tracked_companies VALUES ('NU', 'portfolio', NULL, '12-31', 'equity')"
        )
        conn.execute(
            "INSERT INTO documents VALUES (1, 'NU', ?, ?, '2026-09-30T00:00:00', ?, ?, ?, ?)",
            (
                SourceType.IR_DOC.value,
                DocType.IR_TRANSCRIPT.value,
                wrong_rel,
                hashlib.sha256(wrong_bytes).hexdigest(),
                len(wrong_bytes),
                "https://issuer.example.invalid/nu-q3-transcript.pdf",
            ),
        )
        with pytest.raises(
            ValidationError, match="canonical document path does not match authorized target"
        ):
            stage_pending_issuer_transcripts(
                conn,
                tickers=["NU"],
                project_root=repo_root,
                private_root=staging_root,
                entrypoint=TranscriptAcquisitionEntrypoint.QUARTERLY_REFRESH,
                as_of=date(2026, 11, 22),
            )


def test_calendar_fallback_authorizes_portfolio_and_denies_index_member_without_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_staging_double(monkeypatch)
    repo_root = tmp_path / "repo"
    staging_root = repo_root / ".tmp" / "transcript-acquisition"
    staging_root.mkdir(parents=True)

    rel = "ir_documents/BKNG/2026-09-30/ir_transcript__q3.pdf"
    pdf_path = repo_root / rel
    pdf_path.parent.mkdir(parents=True)
    payload = b"%PDF-1.4\nBKNG Q3 2026"
    pdf_path.write_bytes(payload)

    with sqlite3.connect(repo_root / "test.db") as conn:
        _setup_db(conn)
        conn.execute(
            "INSERT INTO tracked_companies VALUES ('BKNG', 'portfolio', NULL, '12-31', 'equity')"
        )
        conn.execute(
            "INSERT INTO tracked_companies VALUES ('IDX', 'index_member', NULL, '12-31', 'equity')"
        )
        conn.execute(
            "INSERT INTO documents VALUES (1, 'BKNG', ?, ?, '2026-09-30', ?, ?, ?, ?)",
            (
                SourceType.IR_DOC.value,
                DocType.IR_TRANSCRIPT.value,
                rel,
                hashlib.sha256(payload).hexdigest(),
                len(payload),
                "https://s25.q4cdn.com/383369491/files/content_files/Q3.pdf",
            ),
        )
        req_ok = TranscriptAcquisitionRequest(
            schema_version="transcript-acquisition-request@2",
            entrypoint=TranscriptAcquisitionEntrypoint.QUARTERLY_REFRESH,
            canonical_ticker="BKNG",
            fiscal_year=2026,
            fiscal_quarter=3,
            as_of=date(2026, 11, 24),
            source_type=SourceType.IR_DOC,
            document_type=DocType.EARNINGS_CALL_TRANSCRIPT,
            provider=TranscriptProvider.ISSUER_IR,
            owner_requested=False,
            existing_artifact=False,
            existing_artifact_behavior=ExistingArtifactBehavior.REFRESH,
            source_policy_version=TRANSCRIPT_ACQUISITION_POLICY_VERSION,
            source_regime_identity=COMBINED_SOURCE_REGIME_IDENTITY,
        )
        auth_ok = authorize_transcript_request(conn, req_ok)
        assert auth_ok.status is TranscriptAuthorizationStatus.AUTHORIZED

        req_idx = req_ok.model_copy(update={"canonical_ticker": "IDX"})
        auth_idx = authorize_transcript_request(conn, req_idx)
        assert auth_idx.status is TranscriptAuthorizationStatus.DENIED

        artifacts = stage_pending_issuer_transcripts(
            conn,
            tickers=["BKNG"],
            project_root=repo_root,
            private_root=staging_root,
            entrypoint=TranscriptAcquisitionEntrypoint.INGEST_TRANSCRIPTS,
            as_of=date(2026, 11, 24),
        )
        assert 1 in artifacts

        outside_pdf = tmp_path / "outside.pdf"
        outside_pdf.write_bytes(payload)
        conn.execute(
            "INSERT INTO documents VALUES (2, 'BKNG', ?, ?, '2026-09-30', ?, ?, ?, ?)",
            (
                SourceType.IR_DOC.value,
                DocType.IR_TRANSCRIPT.value,
                "../outside.pdf",
                hashlib.sha256(payload).hexdigest(),
                len(payload),
                "https://s25.q4cdn.com/383369491/files/content_files/Q3.pdf",
            ),
        )
        with pytest.raises(TranscriptAcquisitionDeniedError, match="escapes project root"):
            stage_pending_issuer_transcripts(
                conn,
                tickers=["BKNG"],
                project_root=repo_root,
                private_root=staging_root,
                entrypoint=TranscriptAcquisitionEntrypoint.INGEST_TRANSCRIPTS,
                as_of=date(2026, 11, 24),
            )


def test_persisted_ir_document_receipt_allows_cdn_url(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_staging_double(monkeypatch)
    if sys.platform == "darwin":

        def _read_staged(artifact: StagedTranscriptArtifact, **_kwargs: object) -> bytes:
            return artifact.staged_path.read_bytes()

        monkeypatch.setattr(
            transcript_acquisition,
            "read_staged_transcript",
            _read_staged,
        )
    repo_root = tmp_path / "repo"
    staging_root = repo_root / ".tmp" / "transcript-acquisition"
    staging_root.mkdir(parents=True)
    rel = "ir_documents/BKNG/2026-09-30/ir_transcript__q3.pdf"
    pdf_path = repo_root / rel
    pdf_path.parent.mkdir(parents=True)
    payload = b"%PDF-1.4\nBKNG Q3 CDN"
    pdf_path.write_bytes(payload)

    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type, fiscal_year_end, instrument_type) "
            "VALUES ('BKNG', 'Booking Holdings', 'portfolio', '12-31', 'equity')"
        )
        conn.execute(
            "INSERT INTO documents (id, ticker, source_type, doc_type, period_end, file_path, "
            "sha256, fetched_at, fetch_status, raw_bytes_size, source_url) VALUES "
            "(1, 'BKNG', 'ir_doc', 'ir_transcript', '2026-09-30', ?, ?, "
            "'2026-11-04T00:00:00', 'ok', ?, ?)",
            (
                rel,
                hashlib.sha256(payload).hexdigest(),
                len(payload),
                "https://s25.q4cdn.com/383369491/files/content_files/Q3.pdf",
            ),
        )
        artifacts = stage_pending_issuer_transcripts(
            conn,
            tickers=["BKNG"],
            project_root=repo_root,
            private_root=staging_root,
            entrypoint=TranscriptAcquisitionEntrypoint.INGEST_TRANSCRIPTS,
            as_of=date(2026, 11, 24),
        )
        receipt_id = persist_authorized_transcript_artifact(
            conn,
            artifacts[1],
            project_root=repo_root,
            trusted_staging_root=staging_root,
        )
        assert len(receipt_id) == 64


def test_evidence_root_resolves_db_directory_with_ir_documents(tmp_path: Path) -> None:
    from execution import ingest_transcripts, quarterly_refresh

    scratch = tmp_path / "scratch" / "earnings-summary"
    (scratch / "ir_documents").mkdir(parents=True)
    (scratch / "data").mkdir(parents=True)
    db_file = scratch / "data" / "portfolio.db"
    db_file.write_bytes(b"")

    assert ingest_transcripts.evidence_root_for_database(db_file) == scratch.resolve()
    assert quarterly_refresh.evidence_root_for_database(db_file) == scratch.resolve()

    other_db = tmp_path / "isolated" / "data" / "test.db"
    other_db.parent.mkdir(parents=True)
    other_db.write_bytes(b"")
    assert (
        ingest_transcripts.evidence_root_for_database(other_db) == ingest_transcripts.PROJECT_ROOT
    )
    assert quarterly_refresh.evidence_root_for_database(other_db) == quarterly_refresh.PROJECT_ROOT


def test_persisted_ir_document_receipt_skips_legacy_reindex_source_url(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_staging_double(monkeypatch)
    if sys.platform == "darwin":

        def _read_staged(artifact: StagedTranscriptArtifact, **_kwargs: object) -> bytes:
            return artifact.staged_path.read_bytes()

        monkeypatch.setattr(
            transcript_acquisition,
            "read_staged_transcript",
            _read_staged,
        )
    repo_root = tmp_path / "repo"
    staging_root = repo_root / ".tmp" / "transcript-acquisition"
    staging_root.mkdir(parents=True)
    legacy_rel = "ir_documents/MELI/2024-06-30/ir_transcript__legacy.pdf"
    q3_rel = "ir_documents/MELI/2026-09-30/ir_transcript__q3.pdf"
    for rel, body in (
        (legacy_rel, b"%PDF-1.4\nMELI legacy"),
        (q3_rel, b"%PDF-1.4\nMELI Q3"),
    ):
        p = repo_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)

    legacy_payload = b"%PDF-1.4\nMELI legacy"
    q3_payload = b"%PDF-1.4\nMELI Q3"
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type, fiscal_year_end, instrument_type) "
            "VALUES ('MELI', 'MercadoLibre', 'portfolio', '12-31', 'equity')"
        )
        conn.execute(
            "INSERT INTO documents (id, ticker, source_type, doc_type, period_end, file_path, "
            "sha256, fetched_at, fetch_status, raw_bytes_size, source_url) VALUES "
            "(1, 'MELI', 'ir_doc', 'ir_transcript', '2024-06-30', ?, ?, "
            "'2026-11-04T00:00:00', 'ok', ?, 'reindex_subdir:ir_transcript__legacy.pdf'), "
            "(2, 'MELI', 'ir_doc', 'ir_transcript', '2026-09-30', ?, ?, "
            "'2026-11-04T00:00:00', 'ok', ?, 'https://http2.mlstatic.com/q3.pdf')",
            (
                legacy_rel,
                hashlib.sha256(legacy_payload).hexdigest(),
                len(legacy_payload),
                q3_rel,
                hashlib.sha256(q3_payload).hexdigest(),
                len(q3_payload),
            ),
        )
        artifacts = stage_pending_issuer_transcripts(
            conn,
            tickers=["MELI"],
            project_root=repo_root,
            private_root=staging_root,
            entrypoint=TranscriptAcquisitionEntrypoint.INGEST_TRANSCRIPTS,
            as_of=date(2026, 11, 24),
        )
        assert list(artifacts.keys()) == [2]
        receipt_id = persist_authorized_transcript_artifact(
            conn,
            artifacts[2],
            project_root=repo_root,
            trusted_staging_root=staging_root,
        )
        assert len(receipt_id) == 64


def test_ingest_transcripts_ir_only_skips_unreceipted_raw_files(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime
    from types import SimpleNamespace

    from execution import ingest_transcripts

    _install_staging_double(monkeypatch)
    if sys.platform == "darwin":

        def _read_staged(artifact: StagedTranscriptArtifact, **_kwargs: object) -> bytes:
            return artifact.staged_path.read_bytes()

        monkeypatch.setattr(
            transcript_acquisition,
            "read_staged_transcript",
            _read_staged,
        )
    repo_root = tmp_path / "repo"
    (repo_root / "transcripts" / "raw").mkdir(parents=True)
    (repo_root / "transcripts" / "raw" / "BKNG_Q1_2025.txt").write_text("legacy unreceipted")
    rel = "ir_documents/BKNG/2026-09-30/ir_transcript__q3.pdf"
    pdf_path = repo_root / rel
    pdf_path.parent.mkdir(parents=True)
    payload = b"%PDF-1.4\nBKNG Q3"
    pdf_path.write_bytes(payload)

    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type, fiscal_year_end, instrument_type) "
            "VALUES ('BKNG', 'Booking Holdings', 'portfolio', '12-31', 'equity')"
        )
        conn.execute(
            "INSERT INTO documents (id, ticker, source_type, doc_type, period_end, file_path, "
            "sha256, fetched_at, fetch_status, raw_bytes_size, source_url) VALUES "
            "(1, 'BKNG', 'ir_doc', 'ir_transcript', '2026-09-30', ?, ?, "
            "'2026-11-04T00:00:00', 'ok', ?, 'https://s25.q4cdn.com/q3.pdf')",
            (rel, hashlib.sha256(payload).hexdigest(), len(payload)),
        )
        conn.commit()

    ingested_ids: list[int] = []

    def fake_ingest_evidence_file(_conn: sqlite3.Connection, **kwargs: object) -> object:
        doc_id = int(str(kwargs["document_id"]))
        ingested_ids.append(doc_id)
        return SimpleNamespace(
            skipped_existing=False,
            ticker="BKNG",
            period_end=datetime(2026, 9, 30),
            document_id=doc_id,
            transcript_id=99,
            segment_count=1,
            qa_status=SimpleNamespace(value="ok"),
            qa_signals=(),
        )

    monkeypatch.setattr(ingest_transcripts, "ingest_evidence_file", fake_ingest_evidence_file)
    monkeypatch.setattr(
        sys,
        "argv",
        ["ingest_transcripts.py", "--db", str(db_path), "--ticker", "BKNG", "--ir-only"],
    )
    rc = ingest_transcripts.main()
    assert rc == 0
    assert ingested_ids == [1]


def test_ingest_sec_fpi_filings_accepts_explicit_db_path(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from execution import ingest_sec_fpi_filings

    db_path = migrated_db(tmp_path / "external" / "portfolio.db")
    monkeypatch.setattr(
        sys,
        "argv",
        ["ingest_sec_fpi_filings.py", "--all-fpi", "--db", str(db_path)],
    )
    assert ingest_sec_fpi_filings.main() == 0


def test_52_53_week_fye_requires_reported_db_witness_and_authorizes_qcom(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipeline.transcript_acquisition import stage_authorized_payload
    from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
    from transcripts.acquisition_semantics import TranscriptAuthorizationReason

    _install_staging_double(monkeypatch)
    if sys.platform == "darwin":

        def _read_staged(artifact: StagedTranscriptArtifact, **_kwargs: object) -> bytes:
            return artifact.staged_path.read_bytes()

        monkeypatch.setattr(transcript_acquisition, "read_staged_transcript", _read_staged)

    repo_root = tmp_path / "repo"
    staging_root = repo_root / ".tmp" / "transcript-acquisition"
    staging_root.mkdir(parents=True)
    db_path = migrated_db(repo_root / "data" / "portfolio.db")

    qcom_q4_req = TranscriptAcquisitionRequest(
        schema_version="transcript-acquisition-request@2",
        entrypoint=TranscriptAcquisitionEntrypoint.FETCH_QA_TRANSCRIPT,
        canonical_ticker="QCOM",
        fiscal_year=2026,
        fiscal_quarter=4,
        as_of=date(2026, 11, 25),
        source_type=SourceType.IR_DOC,
        document_type=DocType.EARNINGS_CALL_TRANSCRIPT,
        provider=TranscriptProvider.GOOGLE_FINANCE_BETA,
        owner_requested=True,
        existing_artifact=False,
        existing_artifact_behavior=ExistingArtifactBehavior.REFRESH,
        source_policy_version=TRANSCRIPT_ACQUISITION_POLICY_VERSION,
        source_regime_identity=COMBINED_SOURCE_REGIME_IDENTITY,
    )

    with connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=False) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type, fiscal_year_end, instrument_type) "
            "VALUES ('QCOM', 'Qualcomm', 'portfolio', '09-28', 'equity'),"
            "('ODD', 'OddCalendar', 'portfolio', '09-15', 'equity')"
        )
        conn.commit()

        # Negative 1: 52/53-week FYE with NO reported DB witness stays fail-closed pending.
        pending = authorize_transcript_request(conn, qcom_q4_req)
        assert pending.status is TranscriptAuthorizationStatus.DENIED
        assert pending.reason is TranscriptAuthorizationReason.REPORTED_QUARTER_WINDOW_PENDING

        # Insert reported IR release witness at 2026-09-30 for both QCOM and ODD.
        conn.execute(
            "INSERT INTO documents (ticker, source_type, doc_type, period_end, file_path, "
            "sha256, fetched_at, fetch_status, raw_bytes_size, source_url) VALUES "
            "('QCOM', 'ir_doc', 'ir_press_release', '2026-09-30', 'ir_documents/QCOM/pr.pdf', "
            "?, '2026-11-05T00:00:00', 'ok', 100, 'https://investor.qualcomm.com/q4.pdf'),"
            "('ODD', 'ir_doc', 'ir_press_release', '2026-09-30', 'ir_documents/ODD/pr.pdf', "
            "?, '2026-11-05T00:00:00', 'ok', 100, 'https://odd.example.invalid/q4.pdf')",
            ("1" * 64, "2" * 64),
        )
        conn.commit()

        # Negative 2: non-52/53-week day ('09-15') remains fail-closed even with a witness.
        odd_auth = authorize_transcript_request(
            conn, qcom_q4_req.model_copy(update={"canonical_ticker": "ODD"})
        )
        assert odd_auth.status is TranscriptAuthorizationStatus.DENIED
        assert odd_auth.reason is TranscriptAuthorizationReason.REPORTED_QUARTER_WINDOW_PENDING

        # Positive 1: QCOM FY2026 Q4 is authorized and passes full staging + SQLite receipt trigger.
        auth = authorize_transcript_request(conn, qcom_q4_req)
        assert auth.status is TranscriptAuthorizationStatus.AUTHORIZED
        assert auth.stored_target.fiscal_year_end == "09-30"
        assert auth.stored_target.reported_window is not None
        assert auth.stored_target.reported_window.reason == "fiscal_calendar_52_53_week_witness"

        artifact = stage_authorized_payload(
            auth,
            payload=b"[Operator Instructions]\nQualcomm Q4 2026.\n",
            private_root=staging_root,
            source_url="https://www.google.com/finance/beta/quote/QCOM:NASDAQ",
            canonical_document_path=Path("transcripts/raw/QCOM_Q4_2026.txt"),
        )
        receipt_id = persist_authorized_transcript_artifact(
            conn,
            artifact,
            project_root=repo_root,
            trusted_staging_root=staging_root,
        )
        assert len(receipt_id) == 64

        # Negative 3: Unwitnessed future quarter (FY2027 Q1 -> 2026-12-31) is denied out-of-window.
        future_auth = authorize_transcript_request(
            conn, qcom_q4_req.model_copy(update={"fiscal_year": 2027, "fiscal_quarter": 1})
        )
        assert future_auth.status is TranscriptAuthorizationStatus.DENIED
        assert future_auth.reason is TranscriptAuthorizationReason.REPORTED_QUARTER_WINDOW_DENIED

    # Positive 2: Pre-cutover DB with canonical financial_facts witness at 2026-09-27 (±7d of 09-30)
    with sqlite3.connect(":memory:") as fact_conn:
        _setup_db(fact_conn)
        fact_conn.execute(
            "CREATE TABLE financial_facts (ticker TEXT, period_end TEXT, fiscal_period_type TEXT)"
        )
        fact_conn.execute(
            "INSERT INTO tracked_companies VALUES ('QCOM', 'portfolio', NULL, '09-28', 'equity')"
        )
        fact_conn.execute("INSERT INTO financial_facts VALUES ('QCOM', '2026-09-27', 'Q4')")
        fact_auth = authorize_transcript_request(fact_conn, qcom_q4_req)
        assert fact_auth.status is TranscriptAuthorizationStatus.AUTHORIZED
        unwitnessed_q3 = authorize_transcript_request(
            fact_conn, qcom_q4_req.model_copy(update={"fiscal_quarter": 3})
        )
        assert unwitnessed_q3.status is TranscriptAuthorizationStatus.DENIED
        assert unwitnessed_q3.reason is TranscriptAuthorizationReason.REPORTED_QUARTER_WINDOW_DENIED
