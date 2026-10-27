from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from llm_artifact_store import (
    DocumentProductArtifact,
    DocumentProductGeneration,
    DocumentProductKey,
)
from report.models import SectionStatus
from report.sections import earnings


class _VerifierStub:
    def __init__(self, db_path: Path, state_root: Path) -> None:
        self.db_path = db_path
        self.state_root = state_root


def _verifier_stub(db_path: Path, state_root: Path) -> _VerifierStub:
    return _VerifierStub(db_path, state_root)


def _no_surprise_card(*_args: object, **_kwargs: object) -> None:
    pass


def _no_document_summary(
    _conn: sqlite3.Connection,
    _document_version_id: str,
    *,
    db_path: Path,
    state_root: Path,
    verifier: object,
) -> None:
    _ = db_path, state_root, verifier


@pytest.fixture
def report_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "report.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE v_evidence_document_current "
        "(document_version_id TEXT, ticker TEXT, document_type TEXT)"
    )
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def _artifact(document_version_id: str = "doc-v1") -> DocumentProductArtifact:
    key = DocumentProductKey(
        issuer_id="issuer-1",
        document_version_id=document_version_id,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 3, 31),
        period_type="Q1",
        fiscal_year=2025,
        fiscal_quarter=1,
        product="transcript_summary",
    )
    manifest = {
        "document": {
            "document_version_id": document_version_id,
            "source_ref": "https://issuer.example/earnings-call-q1-2025",
        }
    }
    generation = DocumentProductGeneration(
        input_manifest_json=json.dumps(manifest),
        input_sha256="a" * 64,
        generation_id="generation-1",
        generated_at="2026-01-01T00:00:00+00:00",
        model="qualified-test-model",
        backend="test",
        prompt_version="v1",
        prompt_sha256="b" * 64,
    )
    return DocumentProductArtifact(
        artifact_id=9,
        key=key,
        generation=generation,
        body_bytes=b"## 1. Executive Summary\n\nVerified summary.",
        body_sha256="c" * 64,
        input_sha256="a" * 64,
        output_sha256="d" * 64,
    )


def test_report_ignores_legacy_quarter_summary_files(
    tmp_path: Path, report_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / ".tmp"
    legacy.mkdir()
    (legacy / "NU_Q1_2025_summary.txt").write_text("Old generated summary", encoding="utf-8")
    (legacy / "NU_Q2_2025_investor_update_summary.txt").write_text(
        "Old investor update", encoding="utf-8"
    )
    monkeypatch.setattr(earnings, "_build_surprise_card", _no_surprise_card)
    monkeypatch.setattr(earnings, "TranscriptSummaryVerifier", _verifier_stub)
    read_calls: list[str] = []

    def no_summary(
        _conn: sqlite3.Connection,
        document_version_id: str,
        **_kwargs: object,
    ) -> None:
        read_calls.append(document_version_id)
        pass

    monkeypatch.setattr(earnings, "read_transcript_summary", no_summary)
    section = earnings.build("NU", tmp_path, conn=report_db)

    assert read_calls == []
    assert section.status == SectionStatus.MISSING_DATA
    assert section.full_quarters == []
    assert section.digest_quarters == []
    assert section.missing is not None
    assert "No current registered transcript summary artifacts" in (section.missing.detail or "")


def test_report_reads_exact_verified_document_and_keeps_source_ref(
    tmp_path: Path, report_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_db.execute(
        "INSERT INTO v_evidence_document_current VALUES (?, ?, ?)",
        ("doc-v1", "NU", "earnings_call_transcript"),
    )
    report_db.commit()
    monkeypatch.setattr(earnings, "_build_surprise_card", _no_surprise_card)
    monkeypatch.setattr(earnings, "TranscriptSummaryVerifier", _verifier_stub)
    calls: list[tuple[str, Path, Path]] = []

    def current_summary(
        conn: sqlite3.Connection,
        document_version_id: str,
        *,
        db_path: Path,
        state_root: Path,
        verifier: object,
    ) -> DocumentProductArtifact:
        assert conn is report_db
        assert isinstance(verifier, _VerifierStub)
        assert verifier.db_path == db_path
        calls.append((document_version_id, db_path, state_root))
        return _artifact(document_version_id)

    monkeypatch.setattr(earnings, "read_transcript_summary", current_summary)
    section = earnings.build("NU", tmp_path, conn=report_db)

    assert calls == [
        ("doc-v1", Path(report_db.execute("PRAGMA database_list").fetchone()[2]), tmp_path)
    ]
    assert section.status == SectionStatus.OK
    assert len(section.full_quarters) == 1
    card = section.full_quarters[0]
    assert card.summary_md == "## 1. Executive Summary\n\nVerified summary."
    assert card.transcript_path == "https://issuer.example/earnings-call-q1-2025"


def test_unavailable_verified_artifact_does_not_fall_back_to_legacy_file(
    tmp_path: Path, report_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_db.execute(
        "INSERT INTO v_evidence_document_current VALUES (?, ?, ?)",
        ("doc-v1", "NU", "transcript"),
    )
    report_db.commit()
    legacy = tmp_path / ".tmp"
    legacy.mkdir()
    (legacy / "NU_Q1_2025_summary.txt").write_text("Unverified old text", encoding="utf-8")
    monkeypatch.setattr(earnings, "_build_surprise_card", _no_surprise_card)
    monkeypatch.setattr(earnings, "TranscriptSummaryVerifier", _verifier_stub)
    monkeypatch.setattr(earnings, "read_transcript_summary", _no_document_summary)

    section = earnings.build("NU", tmp_path, conn=report_db)

    assert section.status == SectionStatus.MISSING_DATA
    assert section.full_quarters == []
    assert section.missing is not None
    assert "no current verified summary" in (section.missing.detail or "")
