# pyright: reportPrivateUsage=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false
from __future__ import annotations

import ast
import hashlib
import json
import os
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from models.documents import DocType, SourceType
from transcript_qa import QaStatus
from transcripts.acquisition_semantics import (
    TranscriptAcquisitionEntrypoint,
    TranscriptAuthorizationStatus,
    TranscriptProvider,
)
from transcripts.immutable_staging import StagedTranscriptArtifact, TranscriptStagingError


@pytest.fixture
def darwin_staging_double(monkeypatch: pytest.MonkeyPatch) -> Callable[[Any], None]:
    """Replace unavailable Darwin staging with a typed, read-only test seam."""

    from execution import fetch_qa_transcript as fetch
    from pipeline import transcript_acquisition as acquisition

    def install_for(fetch_module: Any) -> None:
        monkeypatch.setattr(fetch_module, "install_transcript_output", install)

    def stage(
        source_path: Path,
        private_root: Path,
        *,
        expected_sha256: str,
        expected_size_bytes: int,
        max_bytes: int,
    ) -> StagedTranscriptArtifact:
        del max_bytes
        payload = source_path.read_bytes()
        assert len(payload) == expected_size_bytes
        assert hashlib.sha256(payload).hexdigest() == expected_sha256
        source = source_path.resolve()
        root = private_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        source_metadata = source.stat()
        root_metadata = root.stat()
        staged_path = root / f"{expected_sha256}.transcript"
        if staged_path.exists():
            existing_payload = staged_path.read_bytes()
            if (
                len(existing_payload) != expected_size_bytes
                or hashlib.sha256(existing_payload).hexdigest() != expected_sha256
            ):
                raise TranscriptStagingError("existing staged transcript does not match")
        else:
            staged_path.write_bytes(payload)
            staged_path.chmod(0o400)
        return StagedTranscriptArtifact(
            source_path=source,
            source_device=int(source_metadata.st_dev),
            source_inode=int(source_metadata.st_ino),
            staging_root=root,
            staging_root_device=int(root_metadata.st_dev),
            staging_root_inode=int(root_metadata.st_ino),
            staged_path=staged_path,
            sha256=expected_sha256,
            size_bytes=expected_size_bytes,
        )

    def read(
        artifact: StagedTranscriptArtifact,
        *,
        trusted_staging_root: Path,
        trusted_staging_root_device: int,
        trusted_staging_root_inode: int,
        expected_source_path: Path,
        expected_source_device: int,
        expected_source_inode: int,
        expected_sha256: str,
        expected_size_bytes: int,
    ) -> bytes:
        root = trusted_staging_root.resolve()
        expected_staged_path = root / f"{expected_sha256}.transcript"
        if (
            artifact.source_path != expected_source_path.resolve()
            or artifact.source_device != expected_source_device
            or artifact.source_inode != expected_source_inode
            or artifact.staging_root != root
            or artifact.staging_root_device != trusted_staging_root_device
            or artifact.staging_root_inode != trusted_staging_root_inode
            or artifact.staged_path != expected_staged_path
            or artifact.sha256 != expected_sha256
            or artifact.size_bytes != expected_size_bytes
        ):
            raise TranscriptStagingError("staged transcript provenance does not match")
        try:
            root_metadata = root.stat()
            payload = expected_staged_path.read_bytes()
        except OSError as exc:
            raise TranscriptStagingError("staged transcript is unavailable") from exc
        if (
            int(root_metadata.st_dev) != trusted_staging_root_device
            or int(root_metadata.st_ino) != trusted_staging_root_inode
            or len(payload) != expected_size_bytes
            or hashlib.sha256(payload).hexdigest() != expected_sha256
        ):
            raise TranscriptStagingError(
                "staged transcript identity, digest, or size does not match"
            )
        return payload

    def install(
        payload: bytes,
        output_root: Path,
        target_name: str,
        *,
        expected_sha256: str,
        expected_size_bytes: int,
    ) -> Path:
        assert len(payload) == expected_size_bytes
        assert hashlib.sha256(payload).hexdigest() == expected_sha256
        assert Path(target_name).name == target_name
        output_root.mkdir(parents=True, exist_ok=True)
        target = output_root / target_name
        if target.exists():
            assert target.read_bytes() == payload
        else:
            target.write_bytes(payload)
        return target

    if sys.platform == "darwin":
        monkeypatch.setattr(acquisition, "stage_transcript_artifact", stage)
        monkeypatch.setattr(acquisition, "read_staged_transcript", read)
    install_for(fetch)
    return install_for


def _stored_company(path: Path, *, role: str = "portfolio") -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE tracked_companies ("
            "ticker TEXT, list_type TEXT, archived_at TEXT, fiscal_year_end TEXT)"
        )
        conn.execute(
            "INSERT INTO tracked_companies VALUES ('ACME', ?, NULL, '12-31')",
            (role,),
        )
        conn.execute(
            "CREATE TABLE transcript_acquisition_receipts ("
            "receipt_id TEXT PRIMARY KEY,idempotency_key TEXT NOT NULL,"
            "document_id INTEGER,canonical_ticker TEXT NOT NULL,fiscal_year INTEGER NOT NULL,"
            "fiscal_quarter INTEGER NOT NULL,canonical_document_path TEXT NOT NULL,"
            "artifact_sha256 TEXT NOT NULL,"
            "artifact_size_bytes INTEGER NOT NULL,source_url TEXT,provider TEXT NOT NULL,"
            "source_type TEXT NOT NULL,document_type TEXT NOT NULL,source_regime TEXT NOT NULL,"
            "source_regime_contract_sha256 TEXT NOT NULL,authorization_json TEXT NOT NULL,"
            "artifact_json TEXT NOT NULL,recorded_at TEXT NOT NULL)"
        )


def _issuer_config(repo_root: Path, ticker: str = "ACME") -> None:
    path = repo_root / "micro_thesis" / "ir_config" / f"{ticker}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "ticker": ticker,
                "platform": "mz",
                "results_center_url": "https://issuer.example.invalid/results",
                "spreadsheet_kpis": [],
            }
        ),
        encoding="utf-8",
    )


def _acquire_acme_q2(
    *,
    fetch: Any,
    repo_root: Path,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    fetch.RAW_DIR = repo_root / "transcripts" / "raw"
    fetch.STAGING_DIR = repo_root / ".tmp" / "transcript-acquisition"
    fetch.STAGING_DIR.mkdir(parents=True)
    monkeypatch.setattr(
        fetch,
        "SOURCES",
        (
            replace(
                fetch.SOURCES[0],
                fetch_qa=lambda *_args: fetch.AggregatorHit(
                    source_name="issuer_ir",
                    page_url="https://issuer.example.invalid/transcript",
                    qa_text=(
                        "Operator\nWelcome.\n\nChief Executive Officer\nRevenue grew.\n\n"
                        "Analyst\nQuestion?\n\nQUESTION AND ANSWER SECTION\n"
                    ),
                    full_text_chars=120,
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        fetch,
        "validate_synthesized_transcript",
        lambda _path: SimpleNamespace(
            status=QaStatus.OK,
            issues=(),
            model_dump=lambda **_kwargs: {"status": "ok"},
        ),
    )
    monkeypatch.setattr(fetch.index_manager, "register_transcript", lambda *_args, **_kwargs: True)
    acquired = fetch.fetch_qa(
        fetch.FetchQaSpec(ticker="ACME", year=2026, quarter=2),
        db_path=db_path,
        owner_requested=False,
        as_of=date.today(),
    )
    assert acquired.status is fetch.FetchQaStatus.ACQUIRED
    assert acquired.result is not None
    return acquired


def test_fetch_qa_calls_only_authorized_issuer_and_replays_without_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    darwin_staging_double: None,
) -> None:
    from execution import fetch_qa_transcript as fetch

    db_path = tmp_path / "portfolio.db"
    _stored_company(db_path)
    _issuer_config(tmp_path)
    fetch.RAW_DIR = tmp_path / "transcripts" / "raw"
    fetch.STAGING_DIR = tmp_path / ".tmp" / "transcript-acquisition"
    fetch.STAGING_DIR.mkdir(parents=True)
    calls: list[str] = []
    registrations: list[dict[str, Any]] = []

    def issuer_hit(_ticker: str, _year: int, _quarter: int) -> Any:
        calls.append("issuer_ir")
        return fetch.AggregatorHit(
            source_name="issuer_ir",
            page_url="https://issuer.example.invalid/transcript",
            qa_text="Operator\nQuestion and answer section.",
            full_text_chars=100,
        )

    def denied_aggregator(*_args: object) -> None:
        pytest.fail("denied aggregator crossed the network boundary")

    monkeypatch.setattr(
        fetch,
        "SOURCES",
        (
            replace(fetch.SOURCES[0], fetch_qa=issuer_hit),
            replace(fetch.SOURCES[1], fetch_qa=denied_aggregator),
        ),
    )
    monkeypatch.setattr(
        fetch,
        "validate_synthesized_transcript",
        lambda _path: SimpleNamespace(
            status=QaStatus.OK,
            issues=(),
            model_dump=lambda **_kwargs: {"status": "ok"},
        ),
    )

    def register(*_args: object, **kwargs: Any) -> None:
        registrations.append(kwargs)

    monkeypatch.setattr(fetch.index_manager, "register_transcript", register)

    spec = fetch.FetchQaSpec(ticker="ACME", year=2026, quarter=2)
    first = fetch.fetch_qa(
        spec,
        db_path=db_path,
        owner_requested=False,
        as_of=date(2026, 8, 12),
    )
    second = fetch.fetch_qa(
        spec,
        db_path=db_path,
        owner_requested=False,
        as_of=date(2026, 8, 12),
    )

    assert first.status is fetch.FetchQaStatus.ACQUIRED
    assert first.result is not None
    assert first.result.authorization.request.provider is TranscriptProvider.ISSUER_IR
    assert calls == ["issuer_ir"]
    assert len(registrations) == 2
    assert second.status is fetch.FetchQaStatus.IDEMPOTENT_REPLAY


def test_backfill_split_root_acquisition_stages_and_receipts_under_state_root(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    darwin_staging_double: Callable[[Any], None],
) -> None:
    from execution import backfill_transcripts as backfill

    fetch = backfill.fetch_qa_transcript_module
    darwin_staging_double(fetch)
    state_root = tmp_path / "state-repo"
    _issuer_config(state_root)
    db_path = migrated_db(state_root / "data" / "portfolio.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker,name,list_type,fiscal_year_end) "
            "VALUES ('ACME','Acme','portfolio','12-31')"
        )
    registrations: list[dict[str, Any]] = []
    monkeypatch.setattr(
        fetch,
        "SOURCES",
        (
            replace(
                fetch.SOURCES[0],
                fetch_qa=lambda *_args: fetch.AggregatorHit(
                    source_name="issuer_ir",
                    page_url="https://issuer.example.invalid/transcript",
                    qa_text="Operator\nQuestion and answer section.",
                    full_text_chars=100,
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        fetch,
        "validate_synthesized_transcript",
        lambda _path: SimpleNamespace(
            status=QaStatus.OK,
            issues=(),
            model_dump=lambda **_kwargs: {"status": "ok"},
        ),
    )
    monkeypatch.setattr(
        fetch.index_manager,
        "register_transcript",
        lambda *_args, **kwargs: registrations.append(kwargs),
    )

    backfill._retarget_paths(state_root.resolve())
    outcome = fetch.fetch_qa(
        fetch.FetchQaSpec(ticker="ACME", year=2026, quarter=2),
        db_path=db_path,
        owner_requested=False,
        as_of=date(2026, 8, 12),
    )

    assert outcome.status is fetch.FetchQaStatus.ACQUIRED
    assert state_root.resolve() / "transcripts" / "raw" == fetch.RAW_DIR
    assert state_root.resolve() / ".tmp" / "transcript-acquisition" == fetch.STAGING_DIR
    assert (fetch.RAW_DIR / "ACME_Q2_2026.txt").is_file()
    with sqlite3.connect(db_path) as conn:
        receipt = conn.execute(
            "SELECT canonical_document_path,artifact_sha256 FROM transcript_acquisition_receipts"
        ).fetchone()
    assert receipt is not None
    assert receipt[0] == "transcripts/raw/ACME_Q2_2026.txt"
    assert (fetch.STAGING_DIR / f"{receipt[1]}.transcript").is_file()
    assert len(registrations) == 1


def test_fetch_qa_denial_has_zero_network_and_zero_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from execution import fetch_qa_transcript as fetch

    db_path = tmp_path / "portfolio.db"
    _stored_company(db_path, role="watchlist")
    fetch.RAW_DIR = tmp_path / "transcripts" / "raw"
    fetch.STAGING_DIR = tmp_path / ".tmp" / "transcript-acquisition"

    monkeypatch.setattr(
        fetch,
        "SOURCES",
        tuple(
            replace(
                source,
                fetch_qa=lambda *_args: pytest.fail("network boundary was crossed"),
            )
            for source in fetch.SOURCES
        ),
    )
    monkeypatch.setattr(
        fetch.index_manager,
        "register_transcript",
        lambda *_args, **_kwargs: pytest.fail("index persistence was crossed"),
    )

    outcome = fetch.fetch_qa(
        fetch.FetchQaSpec(ticker="ACME", year=2026, quarter=2),
        db_path=db_path,
        owner_requested=False,
        as_of=date(2026, 8, 12),
    )

    assert outcome.status is fetch.FetchQaStatus.DENIED
    assert not fetch.RAW_DIR.exists()
    assert not fetch.STAGING_DIR.exists()


def test_fetch_qa_denial_leaves_database_and_sidecars_byte_identical(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from execution import fetch_qa_transcript as fetch

    db_path = migrated_db(tmp_path / "data" / "portfolio.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker,name,list_type,fiscal_year_end) "
            "VALUES ('ACME','Acme','watchlist','12-31')"
        )
    before = db_path.read_bytes()
    before_sidecars = {
        suffix: (db_path.parent / f"{db_path.name}{suffix}").read_bytes()
        for suffix in ("-wal", "-shm")
        if (db_path.parent / f"{db_path.name}{suffix}").exists()
    }
    monkeypatch.setattr(
        fetch,
        "SOURCES",
        tuple(
            replace(source, fetch_qa=lambda *_args: pytest.fail("network boundary crossed"))
            for source in fetch.SOURCES
        ),
    )

    outcome = fetch.fetch_qa(
        fetch.FetchQaSpec(ticker="ACME", year=2026, quarter=2),
        db_path=db_path,
        owner_requested=False,
        as_of=date(2026, 8, 12),
    )

    assert outcome.status is fetch.FetchQaStatus.DENIED
    assert db_path.read_bytes() == before
    assert {
        suffix: (db_path.parent / f"{db_path.name}{suffix}").read_bytes()
        for suffix in ("-wal", "-shm")
        if (db_path.parent / f"{db_path.name}{suffix}").exists()
    } == before_sidecars


def test_refetch_denial_precedes_work_manifest_and_accounting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from execution import refetch_aggregator_transcripts as refetch

    db_path = tmp_path / "portfolio.db"
    _stored_company(db_path)
    manifest_dir = tmp_path / "manifests"
    monkeypatch.setattr(refetch, "_MANIFEST_DIR", manifest_dir)
    monkeypatch.setattr(refetch, "_scope_tickers", lambda *_args: frozenset({"ACME"}))
    monkeypatch.setattr(refetch, "_roic_quarters_in_scope", lambda *_args: [("ACME", 2026, 2)])
    monkeypatch.setattr(
        refetch,
        "_process_one",
        lambda *_args, **_kwargs: pytest.fail("denied refetch crossed provider/persistence work"),
    )
    monkeypatch.setattr(sys, "argv", ["refetch_aggregator_transcripts.py", "--sleep-s", "0"])
    monkeypatch.setattr(
        refetch,
        "connect_sqlite",
        lambda *_args, **_kwargs: sqlite3.connect(db_path),
    )

    assert refetch.main() == 2
    assert not manifest_dir.exists()


def test_audio_denial_precedes_files_network_model_and_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from execution import fetch_audio_transcripts as audio

    db_path = tmp_path / "portfolio.db"
    _stored_company(db_path)
    audio.RAW_DIR = tmp_path / "raw"
    audio.TMP_DIR = tmp_path / "tmp"
    monkeypatch.setattr(
        audio,
        "smart_search_url",
        lambda *_args, **_kwargs: pytest.fail("audio search crossed the network boundary"),
    )
    monkeypatch.setattr(
        audio,
        "_transcribe",
        lambda *_args, **_kwargs: pytest.fail("Whisper boundary was crossed"),
    )
    monkeypatch.setattr(
        audio.index_manager,
        "register_transcript",
        lambda *_args, **_kwargs: pytest.fail("index persistence was crossed"),
    )

    with pytest.raises(audio.AudioCollectionPolicyError):
        audio.fetch_and_transcribe(
            audio.FetchSpec(ticker="ACME", year=2026, quarter=2),
            ffmpeg_location=None,
            db_path=db_path,
            owner_requested=True,
            as_of=date(2026, 8, 12),
        )
    assert not audio.RAW_DIR.exists()
    assert not audio.TMP_DIR.exists()


def test_quarterly_authorizes_and_stages_before_run_accounting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from execution import quarterly_refresh
    from pipeline.transcript_acquisition import TranscriptAcquisitionDeniedError

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(quarterly_refresh, "open_db", lambda _path: conn)
    monkeypatch.setattr(quarterly_refresh, "_resolve_tickers", lambda *_args: ["ACME"])
    monkeypatch.setattr(
        quarterly_refresh,
        "stage_pending_issuer_transcripts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TranscriptAcquisitionDeniedError("denied")),
    )
    monkeypatch.setattr(
        quarterly_refresh,
        "start_run",
        lambda *_args, **_kwargs: pytest.fail("run accounting was persisted before denial"),
    )

    with pytest.raises(TranscriptAcquisitionDeniedError):
        quarterly_refresh.main(["--db", "unused.db"])


def test_staged_existing_issuer_bytes_are_exact_and_replay_is_content_addressed(
    tmp_path: Path,
    darwin_staging_double: None,
) -> None:
    from pipeline.transcript_acquisition import (
        read_authorized_transcript,
        stage_pending_issuer_transcripts,
    )

    repo_root = tmp_path / "repo"
    evidence = repo_root / "ir_documents" / "ACME_Q2_2026.txt"
    evidence.parent.mkdir(parents=True)
    payload = b"Operator\nWelcome.\nAnalyst\nQuestion?"
    evidence.write_bytes(payload)
    staging_root = repo_root / ".tmp" / "transcript-acquisition"
    staging_root.mkdir(parents=True)
    db_path = repo_root / "portfolio.db"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE tracked_companies ("
            "ticker TEXT, list_type TEXT, archived_at TEXT, fiscal_year_end TEXT)"
        )
        conn.execute("INSERT INTO tracked_companies VALUES ('ACME','portfolio',NULL,'12-31')")
        conn.execute(
            "CREATE TABLE documents (id INTEGER PRIMARY KEY,ticker TEXT,source_type TEXT,"
            "doc_type TEXT,file_path TEXT,sha256 TEXT,raw_bytes_size INTEGER,source_url TEXT)"
        )
        conn.execute("CREATE TABLE transcripts (document_id INTEGER)")
        conn.execute(
            "INSERT INTO documents VALUES (1,'ACME',?,?,?,?,?,?)",
            (
                SourceType.IR_DOC.value,
                DocType.IR_TRANSCRIPT.value,
                "ir_documents/ACME_Q2_2026.txt",
                hashlib.sha256(payload).hexdigest(),
                len(payload),
                "https://issuer.example.invalid/transcript",
            ),
        )
        first = stage_pending_issuer_transcripts(
            conn,
            tickers=["ACME"],
            project_root=repo_root,
            private_root=staging_root,
            entrypoint=TranscriptAcquisitionEntrypoint.QUARTERLY_REFRESH,
            as_of=date(2026, 8, 12),
        )
        second = stage_pending_issuer_transcripts(
            conn,
            tickers=["ACME"],
            project_root=repo_root,
            private_root=staging_root,
            entrypoint=TranscriptAcquisitionEntrypoint.QUARTERLY_REFRESH,
            as_of=date(2026, 8, 12),
        )
        artifact = first[1]
        assert artifact.authorization.status is TranscriptAuthorizationStatus.AUTHORIZED
        assert first == second
        assert (
            read_authorized_transcript(
                conn,
                artifact,
                project_root=repo_root,
                trusted_staging_root=staging_root,
            )
            == payload
        )


def test_same_hash_is_unique_and_artifact_cannot_cross_document_identity(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    darwin_staging_double: None,
) -> None:
    from pipeline.transcript_acquisition import (
        TranscriptAcquisitionDeniedError,
        persist_authorized_transcript_artifact,
        stage_pending_issuer_transcripts,
    )

    repo_root = tmp_path / "repo"
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    payloads = {
        "ACME": b"Operator\nAcme bytes\nAnalyst\nQuestion?",
        "BETA": b"Operator\nBeta bytes\nAnalyst\nQuestion?",
    }
    paths = {
        "ACME": repo_root / "ir_documents" / "ACME_Q2_2026.txt",
        "BETA": repo_root / "ir_documents" / "BETA_Q2_2026.txt",
    }
    for ticker, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payloads[ticker])
    staging_root = repo_root / ".tmp" / "transcript-acquisition"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.executemany(
            "INSERT INTO tracked_companies (ticker,name,list_type,fiscal_year_end) "
            "VALUES (?,?,'portfolio','12-31')",
            (("ACME", "Acme"), ("BETA", "Beta")),
        )
        for ticker in ("ACME", "BETA"):
            conn.execute(
                "INSERT INTO documents "
                "(ticker,source_type,doc_type,file_path,sha256,raw_bytes_size,source_url,"
                "fetch_status,fetched_at) VALUES (?,?,?,?,?,?,?,'ok','2026-08-12T00:00:00Z')",
                (
                    ticker,
                    SourceType.IR_DOC.value,
                    DocType.IR_TRANSCRIPT.value,
                    f"ir_documents/{ticker}_Q2_2026.txt",
                    hashlib.sha256(payloads[ticker]).hexdigest(),
                    len(payloads[ticker]),
                    f"https://{ticker.lower()}.example.invalid/transcript",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match=r"documents\.sha256"):
            conn.execute(
                "INSERT INTO documents "
                "(ticker,source_type,doc_type,file_path,sha256,raw_bytes_size,source_url,"
                "fetch_status,fetched_at) VALUES (?,?,?,?,?,?,?,'ok','2026-08-12T00:00:00Z')",
                (
                    "BETA",
                    SourceType.IR_DOC.value,
                    DocType.IR_TRANSCRIPT.value,
                    "ir_documents/BETA_Q2_2026.txt",
                    hashlib.sha256(payloads["ACME"]).hexdigest(),
                    len(payloads["ACME"]),
                    "https://beta.example.invalid/transcript",
                ),
            )
        artifacts = stage_pending_issuer_transcripts(
            conn,
            tickers=["ACME"],
            project_root=repo_root,
            private_root=staging_root,
            entrypoint=TranscriptAcquisitionEntrypoint.QUARTERLY_REFRESH,
            as_of=date(2026, 8, 12),
        )
        forged = artifacts[1].model_copy(update={"document_id": 2})
        with pytest.raises(
            TranscriptAcquisitionDeniedError,
            match="canonical document row",
        ):
            persist_authorized_transcript_artifact(
                conn,
                forged,
                project_root=repo_root,
                trusted_staging_root=staging_root,
            )


def test_legacy_ingested_q1_does_not_block_new_authorized_q2_ingest(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    darwin_staging_double: Callable[[Any], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from execution import fetch_qa_transcript as fetch
    from execution import ingest_transcripts as ingest

    darwin_staging_double(ingest)

    repo_root = tmp_path / "repo"
    _issuer_config(repo_root)
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker,name,list_type,fiscal_year_end) "
            "VALUES ('ACME','Acme','portfolio','12-31')"
        )
        legacy_path = repo_root / "transcripts" / "processed" / "ACME_Q1_2026.txt"
        legacy_path.parent.mkdir(parents=True)
        legacy_payload = b"Legacy Q1 transcript that predates acquisition receipts."
        legacy_path.write_bytes(legacy_payload)
        legacy_sha = hashlib.sha256(legacy_payload).hexdigest()
        cursor = conn.execute(
            "INSERT INTO documents "
            "(ticker,source_type,doc_type,period_end,file_path,sha256,fetched_at,fetch_status,"
            "raw_bytes_size) VALUES (?,?,?,?,?,?,?,'ok',?)",
            (
                "ACME",
                SourceType.TRANSCRIPT_AUDIO.value,
                DocType.EARNINGS_CALL_TRANSCRIPT.value,
                "2026-03-31 00:00:00",
                "transcripts/processed/ACME_Q1_2026.txt",
                legacy_sha,
                "2026-04-01 00:00:00",
                len(legacy_payload),
            ),
        )
        assert cursor.lastrowid is not None
        legacy_document_id = cursor.lastrowid
        transcript_cursor = conn.execute(
            "INSERT INTO transcripts "
            "(document_id,ticker,fiscal_period_type,period_end,source,is_current) "
            "VALUES (?,?,?,?,?,1)",
            (
                legacy_document_id,
                "ACME",
                "Q1",
                "2026-03-31 00:00:00",
                "unknown_legacy",
            ),
        )
        assert transcript_cursor.lastrowid is not None
        conn.execute(
            "INSERT INTO transcript_segments (transcript_id,seq,text) VALUES (?,0,?)",
            (transcript_cursor.lastrowid, "Legacy Q1 transcript segment."),
        )
        # Reproduce BN's pre-lifecycle-migration state: the canonical active
        # relation selects this row even though its historical current bit is stale.
        conn.execute("DROP TRIGGER trg_transcripts_lifecycle_update")
        conn.execute(
            "UPDATE transcripts SET is_current=0 WHERE id=?", (transcript_cursor.lastrowid,)
        )
    acquired = _acquire_acme_q2(
        fetch=fetch,
        repo_root=repo_root,
        db_path=db_path,
        monkeypatch=monkeypatch,
    )
    unrelated_legacy_raw = repo_root / "transcripts" / "raw" / "ACME_Q1_2025.txt"
    unrelated_legacy_raw.write_text("unreceipted historical raw", encoding="utf-8")
    capsys.readouterr()

    monkeypatch.setattr(ingest, "PROJECT_ROOT", repo_root)
    monkeypatch.setattr(
        ingest,
        "_TRANSCRIPT_DIRS",
        (repo_root / "transcripts" / "processed", repo_root / "transcripts" / "raw"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ingest_transcripts.py",
            "--db",
            str(db_path),
            "--ticker",
            "ACME",
            "--automatic",
            "--receipt-id",
            acquired.result.receipt_id,
        ],
    )
    assert ingest.main() == 0
    first_run = json.loads(capsys.readouterr().out)
    assert first_run["ingested"] == 1
    assert first_run["skipped_existing"] == 0
    assert first_run["candidates_total"] == 1
    assert unrelated_legacy_raw.read_text(encoding="utf-8") == "unreceipted historical raw"
    processed_q2 = repo_root / "transcripts" / "processed" / "ACME_Q2_2026.txt"
    assert processed_q2.is_file()
    assert hashlib.sha256(processed_q2.read_bytes()).hexdigest() == (
        acquired.result.acquired_artifact.sha256
    )
    assert ingest.main() == 0
    with sqlite3.connect(db_path) as conn:
        receipt = conn.execute(
            "SELECT canonical_document_path,artifact_sha256 FROM transcript_acquisition_receipts"
        ).fetchone()
        assert receipt == (
            "transcripts/raw/ACME_Q2_2026.txt",
            acquired.result.acquired_artifact.sha256,
        )
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 2
        q2 = conn.execute(
            "SELECT d.file_path,d.sha256,t.id FROM documents d "
            "JOIN transcripts t ON t.document_id=d.id "
            "WHERE d.ticker='ACME' AND t.fiscal_period_type='Q2'"
        ).fetchone()
        assert q2 is not None
        assert q2[0] == "transcripts/processed/ACME_Q2_2026.txt"
        assert q2[1] == acquired.result.acquired_artifact.sha256
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM transcript_segments WHERE transcript_id=?", (q2[2],)
            ).fetchone()[0]
            > 0
        )


def test_fresh_split_root_ingest_creates_processed_root_and_canonical_evidence(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    darwin_staging_double: Callable[[Any], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from execution import fetch_qa_transcript as fetch
    from execution import ingest_transcripts as ingest

    darwin_staging_double(ingest)
    repo_root = tmp_path / "repo"
    _issuer_config(repo_root)
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker,name,list_type,fiscal_year_end) "
            "VALUES ('ACME','Acme','portfolio','12-31')"
        )

    processed_root = repo_root / "transcripts" / "processed"
    acquired = _acquire_acme_q2(
        fetch=fetch,
        repo_root=repo_root,
        db_path=db_path,
        monkeypatch=monkeypatch,
    )
    unrelated_legacy_raw = repo_root / "transcripts" / "raw" / "ACME_Q1_2025.txt"
    unrelated_legacy_raw.write_text("unreceipted historical raw", encoding="utf-8")
    capsys.readouterr()
    assert not processed_root.exists()
    parsed = ingest.parse_transcript_filename(acquired.result.output_path)
    assert parsed is not None
    first_inputs = ingest._invocation_inputs(
        [(acquired.result.output_path, parsed)],
        [],
        include_ir_transcripts=False,
        no_promote=False,
        receipt_artifacts={
            acquired.result.output_path: acquired.result.acquired_artifact,
        },
    )
    alternate_receipt = acquired.result.acquired_artifact.model_copy(
        update={"source_url": "https://issuer.example.invalid/alternate-transcript"}
    )
    second_inputs = ingest._invocation_inputs(
        [(acquired.result.output_path, parsed)],
        [],
        include_ir_transcripts=False,
        no_promote=False,
        receipt_artifacts={acquired.result.output_path: alternate_receipt},
    )
    assert first_inputs["candidate_files"] == second_inputs["candidate_files"]
    assert first_inputs["transcript_receipts"] != second_inputs["transcript_receipts"]

    monkeypatch.setattr(ingest, "PROJECT_ROOT", repo_root)
    monkeypatch.setattr(
        ingest,
        "_TRANSCRIPT_DIRS",
        (processed_root, repo_root / "transcripts" / "raw"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ingest_transcripts.py",
            "--db",
            str(db_path),
            "--ticker",
            "ACME",
            "--automatic",
            "--receipt-id",
            acquired.result.receipt_id,
        ],
    )

    assert ingest.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ingested"] == 1
    assert result["candidates_total"] == 1
    assert unrelated_legacy_raw.read_text(encoding="utf-8") == "unreceipted historical raw"
    processed_path = processed_root / "ACME_Q2_2026.txt"
    assert processed_path.is_file()
    assert hashlib.sha256(processed_path.read_bytes()).hexdigest() == (
        acquired.result.acquired_artifact.sha256
    )
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT d.file_path,d.sha256,t.id FROM documents d "
            "JOIN transcripts t ON t.document_id=d.id WHERE d.ticker='ACME'"
        ).fetchone()
        assert row is not None
        assert row[0] == "transcripts/processed/ACME_Q2_2026.txt"
        assert row[1] == acquired.result.acquired_artifact.sha256
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM transcript_segments WHERE transcript_id=?", (row[2],)
            ).fetchone()[0]
            > 0
        )


def test_conflicting_db_path_ownership_fails_before_processed_install(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    darwin_staging_double: Callable[[Any], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from execution import fetch_qa_transcript as fetch
    from execution import ingest_transcripts as ingest

    darwin_staging_double(ingest)
    repo_root = tmp_path / "repo"
    _issuer_config(repo_root)
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker,name,list_type,fiscal_year_end) "
            "VALUES ('ACME','Acme','portfolio','12-31')"
        )
    acquired = _acquire_acme_q2(
        fetch=fetch,
        repo_root=repo_root,
        db_path=db_path,
        monkeypatch=monkeypatch,
    )
    capsys.readouterr()
    processed_root = repo_root / "transcripts" / "processed"
    processed_path = processed_root / "ACME_Q2_2026.txt"
    assert not processed_path.exists()
    assert acquired.result.acquired_artifact.sha256 != "f" * 64
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO documents "
            "(ticker,source_type,doc_type,period_end,file_path,sha256,fetched_at,fetch_status,"
            "raw_bytes_size) VALUES (?,?,?,?,?,?,?,'ok',?)",
            (
                "ACME",
                SourceType.TRANSCRIPT_AUDIO.value,
                DocType.EARNINGS_CALL_TRANSCRIPT.value,
                "2026-06-30 00:00:00",
                "transcripts/processed/ACME_Q2_2026.txt",
                "f" * 64,
                "2026-07-01 00:00:00",
                1,
            ),
        )
        counts_before = (
            conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0],
        )

    monkeypatch.setattr(ingest, "PROJECT_ROOT", repo_root)
    monkeypatch.setattr(
        ingest,
        "_TRANSCRIPT_DIRS",
        (processed_root, repo_root / "transcripts" / "raw"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ingest_transcripts.py",
            "--db",
            str(db_path),
            "--ticker",
            "ACME",
            "--automatic",
            "--receipt-id",
            acquired.result.receipt_id,
        ],
    )

    assert ingest.main() == 1
    capsys.readouterr()
    assert not processed_path.exists()
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0],
        ) == counts_before


def test_receipt_scope_rejects_unknown_wrong_owner_ticker_and_raw_identity(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    darwin_staging_double: Callable[[Any], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from execution import fetch_qa_transcript as fetch
    from execution import ingest_transcripts as ingest

    darwin_staging_double(ingest)
    repo_root = tmp_path / "repo"
    _issuer_config(repo_root)
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO tracked_companies (ticker,name,list_type,fiscal_year_end) "
            "VALUES (?,?,'portfolio','12-31')",
            (("ACME", "Acme"), ("BETA", "Beta")),
        )
    acquired = _acquire_acme_q2(
        fetch=fetch,
        repo_root=repo_root,
        db_path=db_path,
        monkeypatch=monkeypatch,
    )
    capsys.readouterr()
    monkeypatch.setattr(ingest, "PROJECT_ROOT", repo_root)
    monkeypatch.setattr(
        ingest,
        "_TRANSCRIPT_DIRS",
        (repo_root / "transcripts" / "processed", repo_root / "transcripts" / "raw"),
    )

    def assert_denied(*scope: str, expected_rc: int = 2) -> None:
        monkeypatch.setattr(
            sys,
            "argv",
            ["ingest_transcripts.py", "--db", str(db_path), *scope],
        )
        assert ingest.main() == expected_rc
        capsys.readouterr()
        with sqlite3.connect(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0

    assert_denied("--ticker", "ACME", "--automatic", "--receipt-id", "f" * 64)
    assert_denied(
        "--ticker",
        "BETA",
        "--automatic",
        "--receipt-id",
        acquired.result.receipt_id,
    )
    assert_denied("--ticker", "ACME", "--receipt-id", acquired.result.receipt_id)

    acquired.result.output_path.write_text("mutated raw bytes", encoding="utf-8")
    assert_denied(
        "--ticker",
        "ACME",
        "--automatic",
        "--receipt-id",
        acquired.result.receipt_id,
        expected_rc=1,
    )
    acquired.result.output_path.unlink()
    assert_denied(
        "--ticker",
        "ACME",
        "--automatic",
        "--receipt-id",
        acquired.result.receipt_id,
        expected_rc=1,
    )


def test_mutated_first_receipt_does_not_block_later_valid_receipt(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    darwin_staging_double: Callable[[Any], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from execution import fetch_qa_transcript as fetch
    from execution import ingest_transcripts as ingest

    darwin_staging_double(ingest)
    repo_root = tmp_path / "repo"
    _issuer_config(repo_root)
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker,name,list_type,fiscal_year_end) "
            "VALUES ('ACME','Acme','portfolio','12-31')"
        )
    q2 = _acquire_acme_q2(
        fetch=fetch,
        repo_root=repo_root,
        db_path=db_path,
        monkeypatch=monkeypatch,
    )
    q1 = fetch.fetch_qa(
        fetch.FetchQaSpec(ticker="ACME", year=2026, quarter=1),
        db_path=db_path,
        owner_requested=False,
        as_of=date.today(),
    )
    assert q1.result is not None
    q1.result.output_path.write_text("mutated after receipt selection", encoding="utf-8")
    capsys.readouterr()

    monkeypatch.setattr(ingest, "PROJECT_ROOT", repo_root)
    monkeypatch.setattr(
        ingest,
        "_TRANSCRIPT_DIRS",
        (repo_root / "transcripts" / "processed", repo_root / "transcripts" / "raw"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ingest_transcripts.py",
            "--db",
            str(db_path),
            "--ticker",
            "ACME",
            "--automatic",
            "--receipt-id",
            q1.result.receipt_id,
            "--receipt-id",
            q2.result.receipt_id,
        ],
    )

    assert ingest.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["candidates_total"] == 2
    assert result["ingested"] == 1
    assert result["failed"] == 1
    assert not (repo_root / "transcripts" / "processed" / "ACME_Q1_2026.txt").exists()
    assert (repo_root / "transcripts" / "processed" / "ACME_Q2_2026.txt").is_file()
    with sqlite3.connect(db_path) as conn:
        periods = conn.execute(
            "SELECT fiscal_period_type FROM transcripts ORDER BY fiscal_period_type"
        ).fetchall()
        assert periods == [("Q2",)]


def test_failed_ingest_retains_exact_authorized_processed_bytes_for_retry(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    darwin_staging_double: Callable[[Any], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from execution import fetch_qa_transcript as fetch
    from execution import ingest_transcripts as ingest

    darwin_staging_double(ingest)
    repo_root = tmp_path / "repo"
    _issuer_config(repo_root)
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker,name,list_type,fiscal_year_end) "
            "VALUES ('ACME','Acme','portfolio','12-31')"
        )
    acquired = _acquire_acme_q2(
        fetch=fetch,
        repo_root=repo_root,
        db_path=db_path,
        monkeypatch=monkeypatch,
    )
    capsys.readouterr()
    processed_root = repo_root / "transcripts" / "processed"
    processed_path = processed_root / "ACME_Q2_2026.txt"

    def fail_ingest(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected ingest failure")

    monkeypatch.setattr(ingest, "PROJECT_ROOT", repo_root)
    monkeypatch.setattr(
        ingest,
        "_TRANSCRIPT_DIRS",
        (processed_root, repo_root / "transcripts" / "raw"),
    )
    monkeypatch.setattr(ingest, "ingest_evidence_file", fail_ingest)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ingest_transcripts.py",
            "--db",
            str(db_path),
            "--ticker",
            "ACME",
            "--automatic",
            "--receipt-id",
            acquired.result.receipt_id,
        ],
    )

    assert ingest.main() == 1
    capsys.readouterr()
    assert processed_path.is_file()
    assert hashlib.sha256(processed_path.read_bytes()).hexdigest() == (
        acquired.result.acquired_artifact.sha256
    )
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0


def test_authorized_fetch_repairs_missing_raw_and_index_without_network_or_duplicate_receipt(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    darwin_staging_double: None,
) -> None:
    from execution import fetch_qa_transcript as fetch

    repo_root = tmp_path / "repo"
    _issuer_config(repo_root)
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker,name,list_type,fiscal_year_end) "
            "VALUES ('ACME','Acme','portfolio','12-31')"
        )
    fetch.RAW_DIR = repo_root / "transcripts" / "raw"
    fetch.STAGING_DIR = repo_root / ".tmp" / "transcript-acquisition"
    fetch.STAGING_DIR.mkdir(parents=True)
    calls = 0

    def issuer_hit(*_args: object) -> Any:
        nonlocal calls
        calls += 1
        if calls > 1:
            pytest.fail("durable replay crossed the network boundary")
        return fetch.AggregatorHit(
            source_name="issuer_ir",
            page_url="https://issuer.example.invalid/transcript",
            qa_text="Operator\nWelcome.\n\nAnalyst\nQuestion?\n",
            full_text_chars=50,
        )

    monkeypatch.setattr(fetch, "SOURCES", (replace(fetch.SOURCES[0], fetch_qa=issuer_hit),))
    monkeypatch.setattr(
        fetch,
        "validate_synthesized_transcript",
        lambda _path: SimpleNamespace(
            status=QaStatus.OK,
            issues=(),
            model_dump=lambda **_kwargs: {"status": "ok"},
        ),
    )
    registrations: list[dict[str, Any]] = []
    monkeypatch.setattr(
        fetch.index_manager,
        "register_transcript",
        lambda *_args, **kwargs: registrations.append(kwargs),
    )
    spec = fetch.FetchQaSpec(ticker="ACME", year=2026, quarter=2)
    first = fetch.fetch_qa(spec, db_path=db_path, owner_requested=False, as_of=date(2026, 8, 12))
    assert first.result is not None
    first.result.output_path.chmod(0o600)
    first.result.output_path.unlink()
    registrations.clear()

    replay = fetch.fetch_qa(spec, db_path=db_path, owner_requested=False, as_of=date(2026, 8, 13))

    assert replay.status is fetch.FetchQaStatus.IDEMPOTENT_REPLAY
    assert replay.result is not None
    assert replay.result.receipt_id == first.result.receipt_id
    assert replay.attempts[-1].status is fetch.FetchQaAttemptStatus.IDEMPOTENT_REPLAY
    assert first.result.output_path.read_bytes()
    assert calls == 1
    assert len(registrations) == 1
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM transcript_acquisition_receipts").fetchone()[0] == 1
        )


def test_authorized_fetch_replays_after_post_receipt_output_failure(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    darwin_staging_double: None,
) -> None:
    from execution import fetch_qa_transcript as fetch

    repo_root = tmp_path / "repo"
    _issuer_config(repo_root)
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker,name,list_type,fiscal_year_end) "
            "VALUES ('ACME','Acme','portfolio','12-31')"
        )
    fetch.RAW_DIR = repo_root / "transcripts" / "raw"
    fetch.STAGING_DIR = repo_root / ".tmp" / "transcript-acquisition"
    fetch.STAGING_DIR.mkdir(parents=True)
    calls = 0

    def issuer_hit(*_args: object) -> Any:
        nonlocal calls
        calls += 1
        if calls > 1:
            pytest.fail("restart replay crossed the network boundary")
        return fetch.AggregatorHit(
            source_name="issuer_ir",
            page_url="https://issuer.example.invalid/transcript",
            qa_text="Operator\nWelcome.\n\nAnalyst\nQuestion?\n",
            full_text_chars=50,
        )

    monkeypatch.setattr(fetch, "SOURCES", (replace(fetch.SOURCES[0], fetch_qa=issuer_hit),))
    monkeypatch.setattr(
        fetch,
        "validate_synthesized_transcript",
        lambda _path: SimpleNamespace(
            status=QaStatus.OK,
            issues=(),
            model_dump=lambda **_kwargs: {"status": "ok"},
        ),
    )
    monkeypatch.setattr(fetch.index_manager, "register_transcript", lambda *_args, **_kwargs: True)
    original_restore = fetch._restore_replay
    monkeypatch.setattr(
        fetch,
        "_restore_replay",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("simulated output failure")),
    )
    spec = fetch.FetchQaSpec(ticker="ACME", year=2026, quarter=2)

    with pytest.raises(OSError, match="simulated output failure"):
        fetch.fetch_qa(spec, db_path=db_path, owner_requested=False, as_of=date(2026, 8, 12))
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM transcript_acquisition_receipts").fetchone()[0] == 1
        )
    assert not (fetch.RAW_DIR / "ACME_Q2_2026.txt").exists()

    monkeypatch.setattr(fetch, "_restore_replay", original_restore)
    replay = fetch.fetch_qa(spec, db_path=db_path, owner_requested=False, as_of=date(2026, 8, 12))

    assert replay.status is fetch.FetchQaStatus.IDEMPOTENT_REPLAY
    assert (fetch.RAW_DIR / "ACME_Q2_2026.txt").read_bytes()
    assert calls == 1
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM transcript_acquisition_receipts").fetchone()[0] == 1
        )


@pytest.mark.parametrize("damage", ["delete", "tamper"])
def test_invalid_durable_replay_never_falls_through_to_network(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    darwin_staging_double: None,
    damage: str,
) -> None:
    from execution import fetch_qa_transcript as fetch

    repo_root = tmp_path / "repo"
    _issuer_config(repo_root)
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker,name,list_type,fiscal_year_end) "
            "VALUES ('ACME','Acme','portfolio','12-31')"
        )
    fetch.RAW_DIR = repo_root / "transcripts" / "raw"
    fetch.STAGING_DIR = repo_root / ".tmp" / "transcript-acquisition"
    fetch.STAGING_DIR.mkdir(parents=True)
    calls = 0

    def issuer_hit(*_args: object) -> Any:
        nonlocal calls
        calls += 1
        if calls > 1:
            pytest.fail("invalid durable replay fell through to network")
        return fetch.AggregatorHit(
            source_name="issuer_ir",
            page_url="https://issuer.example.invalid/transcript",
            qa_text="Operator\nWelcome.\n\nAnalyst\nQuestion?\n",
            full_text_chars=50,
        )

    monkeypatch.setattr(fetch, "SOURCES", (replace(fetch.SOURCES[0], fetch_qa=issuer_hit),))
    monkeypatch.setattr(
        fetch,
        "validate_synthesized_transcript",
        lambda _path: SimpleNamespace(
            status=QaStatus.OK,
            issues=(),
            model_dump=lambda **_kwargs: {"status": "ok"},
        ),
    )
    monkeypatch.setattr(fetch.index_manager, "register_transcript", lambda *_args, **_kwargs: True)
    spec = fetch.FetchQaSpec(ticker="ACME", year=2026, quarter=2)
    first = fetch.fetch_qa(spec, db_path=db_path, owner_requested=False, as_of=date(2026, 8, 12))
    assert first.result is not None
    staged_path = first.result.acquired_artifact.staged.staged_path
    staged_path.chmod(0o600)
    if damage == "delete":
        staged_path.unlink()
    else:
        staged_path.write_bytes(b"tampered")

    with pytest.raises(TranscriptStagingError):
        fetch.fetch_qa(spec, db_path=db_path, owner_requested=False, as_of=date(2026, 8, 12))
    assert calls == 1
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM transcript_acquisition_receipts").fetchone()[0] == 1
        )


def test_output_install_rejects_hardlink_without_mutating_victim(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from execution import fetch_qa_transcript as fetch
    from transcripts.immutable_staging import TranscriptStagingError

    repo_root = tmp_path / "repo"
    _issuer_config(repo_root)
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker,name,list_type,fiscal_year_end) "
            "VALUES ('ACME','Acme','portfolio','12-31')"
        )
    fetch.RAW_DIR = repo_root / "transcripts" / "raw"
    fetch.STAGING_DIR = repo_root / ".tmp" / "transcript-acquisition"
    fetch.STAGING_DIR.mkdir(parents=True)
    fetch.RAW_DIR.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"benign victim")
    os.link(victim, fetch.RAW_DIR / "ACME_Q2_2026.txt")
    monkeypatch.setattr(
        fetch,
        "SOURCES",
        (
            replace(
                fetch.SOURCES[0],
                fetch_qa=lambda *_args: fetch.AggregatorHit(
                    source_name="issuer_ir",
                    page_url="https://issuer.example.invalid/transcript",
                    qa_text="Operator\nWelcome.\n\nAnalyst\nQuestion?\n",
                    full_text_chars=50,
                ),
            ),
        ),
    )

    with pytest.raises(TranscriptStagingError):
        fetch.fetch_qa(
            fetch.FetchQaSpec(ticker="ACME", year=2026, quarter=2),
            db_path=db_path,
            owner_requested=False,
            as_of=date(2026, 8, 12),
        )

    assert victim.read_bytes() == b"benign victim"


def test_new_receipt_rejects_latent_or_changed_stored_target(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    darwin_staging_double: None,
) -> None:
    from pipeline.transcript_acquisition import (
        require_authorized_transcript_request,
        stage_authorized_payload,
    )
    from transcripts.acquisition_semantics import (
        TRANSCRIPT_ACQUISITION_POLICY_VERSION,
        ExistingArtifactBehavior,
        TranscriptAcquisitionRequest,
    )

    repo_root = tmp_path / "repo"
    _issuer_config(repo_root)
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    staging_root = repo_root / ".tmp" / "transcript-acquisition"
    staging_root.mkdir(parents=True)
    with sqlite3.connect(db_path) as raw:
        raw.execute(
            "INSERT INTO tracked_companies (ticker,name,list_type,fiscal_year_end) "
            "VALUES ('ACME','Acme','portfolio','12-31')"
        )
    from pipeline.transcript_acquisition import COMBINED_SOURCE_REGIME_IDENTITY
    from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

    with connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=False) as conn:
        request = TranscriptAcquisitionRequest(
            entrypoint=TranscriptAcquisitionEntrypoint.FETCH_QA_TRANSCRIPT,
            canonical_ticker="ACME",
            fiscal_year=2026,
            fiscal_quarter=2,
            as_of=date(2026, 8, 12),
            source_type=SourceType.IR_DOC,
            document_type=DocType.EARNINGS_CALL_TRANSCRIPT,
            provider=TranscriptProvider.ISSUER_IR,
            owner_requested=False,
            existing_artifact=False,
            existing_artifact_behavior=ExistingArtifactBehavior.REFRESH,
            source_policy_version=TRANSCRIPT_ACQUISITION_POLICY_VERSION,
            source_regime_identity=COMBINED_SOURCE_REGIME_IDENTITY,
        )
        authorization = require_authorized_transcript_request(conn, request)
        artifact = stage_authorized_payload(
            authorization,
            payload=b"issuer bytes",
            private_root=staging_root,
            source_url="https://issuer.example.invalid/transcript",
            canonical_document_path=Path("transcripts/raw/ACME_Q2_2026.txt"),
        )
        authorization_json = json.dumps(
            authorization.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        artifact_payload = artifact.model_dump(mode="json")
        artifact_payload["canonical_document_path"] = artifact.canonical_document_path.as_posix()
        artifact_json = json.dumps(artifact_payload, sort_keys=True, separators=(",", ":"))
        receipt_id = hashlib.sha256(artifact_json.encode("utf-8")).hexdigest()
        conn.execute("DELETE FROM tracked_companies WHERE ticker='ACME'")
        with pytest.raises(sqlite3.IntegrityError, match="stored target"):
            conn.execute(
                "INSERT INTO transcript_acquisition_receipts "
                "(receipt_id,idempotency_key,document_id,canonical_ticker,fiscal_year,"
                "fiscal_quarter,canonical_document_path,artifact_sha256,artifact_size_bytes,"
                "source_url,provider,source_type,document_type,source_regime,"
                "source_regime_contract_sha256,authorization_json,artifact_json,recorded_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    receipt_id,
                    authorization.idempotency_key,
                    None,
                    "ACME",
                    2026,
                    2,
                    "transcripts/raw/ACME_Q2_2026.txt",
                    artifact.sha256,
                    artifact.size_bytes,
                    artifact.source_url,
                    authorization.request.provider.value,
                    authorization.request.source_type.value,
                    authorization.request.document_type.value,
                    authorization.request.source_regime_identity.regime.value,
                    authorization.request.source_regime_identity.contract_sha256,
                    authorization_json,
                    artifact_json,
                    datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                ),
            )


def test_returned_issuer_url_must_match_configured_authority(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from execution import fetch_qa_transcript as fetch
    from pipeline.transcript_acquisition import TranscriptAcquisitionDeniedError

    repo_root = tmp_path / "repo"
    _issuer_config(repo_root)
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker,name,list_type,fiscal_year_end) "
            "VALUES ('ACME','Acme','portfolio','12-31')"
        )
    fetch.RAW_DIR = repo_root / "transcripts" / "raw"
    fetch.STAGING_DIR = repo_root / ".tmp" / "transcript-acquisition"
    monkeypatch.setattr(
        fetch,
        "SOURCES",
        (
            replace(
                fetch.SOURCES[0],
                fetch_qa=lambda *_args: fetch.AggregatorHit(
                    source_name="issuer_ir",
                    page_url="https://unrelated.example.invalid/transcript",
                    qa_text="Operator\nWelcome.\n\nAnalyst\nQuestion?\n",
                    full_text_chars=50,
                ),
            ),
        ),
    )

    with pytest.raises(TranscriptAcquisitionDeniedError, match="issuer source URL"):
        fetch.fetch_qa(
            fetch.FetchQaSpec(ticker="ACME", year=2026, quarter=2),
            db_path=db_path,
            owner_requested=False,
            as_of=date(2026, 8, 12),
        )
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM transcript_acquisition_receipts").fetchone()[0] == 0
        )


def test_transitive_production_entrypoint_scan_closes_unreceipted_writers() -> None:
    root = Path(__file__).resolve().parents[1]
    named = (
        root / "execution" / "quarterly_refresh.py",
        root / "execution" / "fetch_qa_transcript.py",
        root / "execution" / "refetch_aggregator_transcripts.py",
        root / "execution" / "fetch_audio_transcripts.py",
    )
    for path in named:
        assert "transcript_acquisition" in path.read_text(encoding="utf-8")

    production = tuple((root / "execution").glob("*.py")) + tuple((root / "src").rglob("*.py"))
    ingest_calls: list[tuple[Path, int]] = []
    index_calls: list[tuple[Path, int]] = []
    private_writer_calls: list[tuple[Path, int, str]] = []
    private_writer_names = {
        "_ingest_one",
        "_ingest_existing_ir_transcript",
        "_insert_document",
        "_insert_transcript",
        "_insert_segments",
        "_register_transcript",
    }
    for path in production:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            if name == "ingest_evidence_file":
                ingest_calls.append((path, node.lineno))
                assert any(keyword.arg == "authorized_artifact" for keyword in node.keywords), (
                    path,
                    node.lineno,
                )
            if name == "register_transcript":
                index_calls.append((path, node.lineno))
                assert any(keyword.arg == "acquisition_receipt" for keyword in node.keywords), (
                    path,
                    node.lineno,
                )
            if name in private_writer_names:
                private_writer_calls.append((path, node.lineno, name))
    assert {path.name for path, _line in ingest_calls} == {
        "ingest_transcripts.py",
        "quarterly_refresh.py",
    }
    assert {path.name for path, _line in index_calls} == {"fetch_qa_transcript.py"}
    assert {path.name for path, _line, _name in private_writer_calls} <= {
        "transcript_ingest.py",
        "index_manager.py",
    }
    compute_tree = ast.parse(
        (root / "src" / "compute" / "transcript_ingest.py").read_text(encoding="utf-8")
    )
    forbidden_public_writers = {
        "ingest_one",
        "ingest_existing_ir_transcript",
        "insert_document",
        "insert_transcript",
        "insert_segments",
    }
    assert not {
        node.name
        for node in compute_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in forbidden_public_writers
    }
