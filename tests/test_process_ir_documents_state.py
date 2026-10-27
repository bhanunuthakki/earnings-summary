"""Registered transcript selection and explicit authority routing."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

import alias_manager
from execution import process_ir_documents as processor
from pipeline import transcript_summary_verification
from provenance.evidence_backfill import ensure_legacy_document_evidence
from provenance.issuer_registry_bootstrap import (
    SecSubmissionsIssuerRequest,
    bootstrap_sec_submissions_issuer,
)

_CIK = "0001691493"
_NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)


def _no_environment(_root: Path) -> None:
    return None


class _VerifierStub:
    def __init__(self, *, db_path: Path, state_root: Path) -> None:
        self.db_path = db_path
        self.state_root = state_root


def _seed_transcripts(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    database = migrated_db(tmp_path / "registered-transcripts.sqlite")
    conn = sqlite3.connect(database)
    conn.execute("PRAGMA foreign_keys=ON")
    sec_body = json.dumps(
        {
            "cik": int(_CIK),
            "name": "Nu Holdings Ltd.",
            "entityType": "operating",
            "tickers": ["NU"],
            "exchanges": ["NYSE"],
            "fiscalYearEnd": "0131",
        },
        separators=(",", ":"),
    ).encode()
    bootstrap_sec_submissions_issuer(
        conn,
        request=SecSubmissionsIssuerRequest(
            ticker="NU",
            normalized_cik=_CIK,
            source_url=f"https://data.sec.gov/submissions/CIK{_CIK}.json",
            raw_body=sec_body,
            blob_root=tmp_path / "sec-blobs",
            observed_at=_NOW,
            retrieved_at=_NOW,
            recorded_at=_NOW,
            apply=True,
        ),
    )
    conn.row_factory = sqlite3.Row
    documents = (
        (1, "earnings_call_transcript", "2026-04-30", "q1-transcript.txt"),
        (2, "earnings_call_transcript", "2026-07-31", "q2-transcript.txt"),
        (3, "event", None, "event.txt"),
    )
    for document_id, document_type, period_end, filename in documents:
        relative = f"transcripts/raw/NU/{filename}"
        source_path = tmp_path / relative
        source_path.parent.mkdir(parents=True, exist_ok=True)
        body = f"Synthetic {filename}".encode()
        source_path.write_bytes(body)
        source_url = f"https://issuer.example.test/NU/{filename}"
        conn.execute(
            "INSERT INTO documents (id,ticker,source_type,doc_type,period_end,file_path,sha256,"
            "fetched_at,fetch_status,raw_bytes_size,source_url) "
            "VALUES (?,?,'ir_doc',?,?,?,?,?,'ok',?,?)",
            (
                document_id,
                "NU",
                document_type,
                period_end,
                relative,
                hashlib.sha256(body).hexdigest(),
                _NOW.isoformat(),
                len(body),
                source_url,
            ),
        )
        ensure_legacy_document_evidence(conn, repo_root=tmp_path, document_id=document_id)
    conn.commit()
    conn.close()
    return database


def test_registered_selection_uses_fiscal_coordinates_and_excludes_events(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _seed_transcripts(tmp_path, migrated_db)
    monkeypatch.setattr(alias_manager, "_aliases_cache", {"GOOGL": "GOOG"})
    alias_resolutions: list[tuple[str, str]] = []

    def resolve_and_record(ticker: str) -> str:
        canonical = alias_manager.resolve_ticker(ticker)
        alias_resolutions.append((ticker, canonical))
        return canonical

    monkeypatch.setattr(processor, "resolve_ticker", resolve_and_record)

    q2 = processor.select_registered_transcripts(
        database, ticker="NU", all_tickers=False, fiscal_year=2027, fiscal_quarter=2
    )
    all_transcripts = processor.select_registered_transcripts(
        database, ticker=None, all_tickers=True, fiscal_year=None, fiscal_quarter=None
    )
    alias = processor.select_registered_transcripts(
        database, ticker="GOOGL", all_tickers=False, fiscal_year=None, fiscal_quarter=None
    )

    assert q2 == ["legacy-doc-2"]
    assert all_transcripts == ["legacy-doc-1", "legacy-doc-2"]
    assert alias == []
    assert alias_resolutions == [("NU", "NU"), ("GOOGL", "GOOG")]
    assert processor.select_registered_transcripts(
        database, ticker="NU", all_tickers=False, fiscal_year=2027, fiscal_quarter=1
    ) == ["legacy-doc-1"]


def test_cli_routes_exact_document_to_explicit_database_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "authority.sqlite")
    code_root = tmp_path / "code"
    code_root.mkdir()
    state_root = tmp_path / "retained-state"
    state_root.mkdir()
    verifier_instances: list[_VerifierStub] = []
    processed: list[tuple[str, Path, Path, object]] = []

    def verifier_factory(*, db_path: Path, state_root: Path) -> _VerifierStub:
        instance = _VerifierStub(db_path=db_path, state_root=state_root)
        verifier_instances.append(instance)
        return instance

    def process(
        document_version_id: str,
        *,
        db_path: Path,
        state_root: Path,
        verifier: object,
    ) -> dict[str, object]:
        processed.append((document_version_id, db_path, state_root, verifier))
        return {"status": "reused", "document_version_id": document_version_id}

    monkeypatch.setattr(
        transcript_summary_verification, "TranscriptSummaryVerifier", verifier_factory
    )
    monkeypatch.setattr(processor, "process_document", process)
    monkeypatch.setattr(processor, "load_project_env", _no_environment)

    assert (
        processor.main(
            [
                "--document-version-id",
                "legacy-doc-2",
                "--db",
                str(database),
                "--state-root",
                str(state_root),
                "--repo-root",
                str(code_root),
            ]
        )
        == 0
    )
    assert len(verifier_instances) == 1
    assert verifier_instances[0].db_path == database.resolve()
    assert verifier_instances[0].state_root == state_root.resolve()
    assert processed == [
        ("legacy-doc-2", database.resolve(), state_root.resolve(), verifier_instances[0])
    ]


def test_unregistered_index_or_local_file_cannot_trigger_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = migrated_db(tmp_path / "empty.sqlite")
    state_root = tmp_path / "state"
    state_root.mkdir()
    legacy_path = state_root / "transcripts/raw/GOOG_Q1_2026.txt"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text("A local transcript is not registration.", encoding="utf-8")
    index = state_root / ".tmp/document_index.json"
    index.parent.mkdir()
    index.write_text(
        json.dumps(
            {
                "GOOG_2026_Q1_transcript": {
                    "ticker": "GOOG",
                    "doc_type": "transcript",
                    "local_path": str(legacy_path),
                    "processed": False,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        transcript_summary_verification,
        "TranscriptSummaryVerifier",
        _VerifierStub,
    )
    monkeypatch.setattr(processor, "load_project_env", _no_environment)

    def forbidden_generation(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("only a registered current document can be processed")

    monkeypatch.setattr(processor, "process_document", forbidden_generation)
    assert (
        processor.main(
            [
                "--ticker",
                "GOOG",
                "--db",
                str(database),
                "--state-root",
                str(state_root),
                "--repo-root",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {"results": [], "status": "complete"}


def test_document_id_rejects_fiscal_filters_without_processing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "authority.sqlite")
    state_root = tmp_path / "state"
    state_root.mkdir()
    monkeypatch.setattr(transcript_summary_verification, "TranscriptSummaryVerifier", _VerifierStub)
    monkeypatch.setattr(processor, "load_project_env", _no_environment)

    def forbidden_generation(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("invalid selector combination must fail before processing")

    monkeypatch.setattr(processor, "process_document", forbidden_generation)
    assert (
        processor.main(
            [
                "--document-version-id",
                "legacy-doc-2",
                "--year",
                "2027",
                "--quarter",
                "2",
                "--db",
                str(database),
                "--state-root",
                str(state_root),
            ]
        )
        == 1
    )
