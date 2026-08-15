from __future__ import annotations

import ast
import hashlib
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import date
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


def test_fetch_qa_calls_only_authorized_issuer_and_replays_without_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from execution import fetch_qa_transcript as fetch

    db_path = tmp_path / "portfolio.db"
    _stored_company(db_path)
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


def test_authorized_fetch_persists_once_and_direct_ingest_replays_after_restart(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from execution import fetch_qa_transcript as fetch
    from execution import ingest_transcripts as ingest

    repo_root = tmp_path / "repo"
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker,name,list_type,fiscal_year_end) "
            "VALUES ('ACME','Acme','portfolio','12-31')"
        )
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

    monkeypatch.setattr(ingest, "PROJECT_ROOT", repo_root)
    monkeypatch.setattr(
        ingest,
        "_TRANSCRIPT_DIRS",
        (repo_root / "transcripts" / "processed", repo_root / "transcripts" / "raw"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["ingest_transcripts.py", "--db", str(db_path), "--no-promote"],
    )
    assert ingest.main() == 0
    assert ingest.main() == 0
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM transcript_acquisition_receipts").fetchone()[0] == 1
        )
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 1


def test_authorized_fetch_repairs_missing_raw_and_index_without_network_or_duplicate_receipt(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from execution import fetch_qa_transcript as fetch

    repo_root = tmp_path / "repo"
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
    first.result.output_path.unlink()
    registrations.clear()

    replay = fetch.fetch_qa(spec, db_path=db_path, owner_requested=False, as_of=date(2026, 8, 12))

    assert replay.status is fetch.FetchQaStatus.IDEMPOTENT_REPLAY
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
) -> None:
    from execution import fetch_qa_transcript as fetch

    repo_root = tmp_path / "repo"
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
