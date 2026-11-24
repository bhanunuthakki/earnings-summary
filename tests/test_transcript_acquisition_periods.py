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
        monkeypatch.setattr(
            transcript_acquisition,
            "read_staged_transcript",
            lambda artifact, **_kwargs: artifact.staged_path.read_bytes(),
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

    assert ingest_transcripts._evidence_root(db_file) == scratch.resolve()
    assert quarterly_refresh._evidence_root(db_file) == scratch.resolve()

    other_db = tmp_path / "isolated" / "data" / "test.db"
    other_db.parent.mkdir(parents=True)
    other_db.write_bytes(b"")
    assert ingest_transcripts._evidence_root(other_db) == ingest_transcripts.PROJECT_ROOT
    assert quarterly_refresh._evidence_root(other_db) == quarterly_refresh.PROJECT_ROOT
