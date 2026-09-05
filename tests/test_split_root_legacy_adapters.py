# pyright: reportPrivateUsage=false
"""Split-root regressions for legacy transcript and NVO adapters."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import extract_nvo_patent_timeline_state as nvo_adapter  # noqa: E402
import ingest_transcripts as legacy_ingest  # noqa: E402
import ingest_transcripts_state as transcript_adapter  # noqa: E402

import db  # noqa: E402


class _FakeIndexManager:
    PROJECT_ROOT = ""
    CACHE_DIR = ""
    TRANSCRIPT_INDEX_PATH = ""
    DOCUMENT_INDEX_PATH = ""
    TRANSCRIPTS_RAW_DIR = ""
    TRANSCRIPTS_PROCESSED_DIR = ""


class _FakeIngester:
    PROJECT_ROOT = Path(".")
    _TRANSCRIPT_DIRS = (Path("."), Path("."))
    index_manager = _FakeIndexManager()

    def __init__(self) -> None:
        self.seen_argv: list[str] = []

    def main(self) -> int:
        self.seen_argv = list(sys.argv)
        return 0


class _FakeNvoExtractor:
    PROJECT_ROOT = Path(".")
    OUT_DIR = Path(".")
    IR_DOCS_DIR = Path(".")
    SOURCES_DIR = Path(".")

    def __init__(self) -> None:
        self.loaded_env_root: Path | None = None
        self.seen_argv: list[str] = []

    def load_project_env(self, project_root: Path) -> None:
        self.loaded_env_root = project_root

    def main(self) -> None:
        self.seen_argv = list(sys.argv)


def test_transcript_adapter_binds_files_and_db_to_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingester = _FakeIngester()
    monkeypatch.setattr(transcript_adapter, "_load_ingester", lambda: ingester)

    assert (
        transcript_adapter.main(["--repo-root", str(tmp_path), "--ticker", "nu", "--no-promote"])
        == 0
    )

    assert tmp_path == ingester.PROJECT_ROOT
    assert (
        tmp_path / "transcripts/processed",
        tmp_path / "transcripts/raw",
    ) == ingester._TRANSCRIPT_DIRS
    assert Path(ingester.index_manager.TRANSCRIPTS_RAW_DIR) == tmp_path / "transcripts/raw"
    assert ingester.seen_argv == [
        "ingest_transcripts.py",
        "--db",
        str(tmp_path / "data/portfolio.db"),
        "--ticker",
        "NU",
        "--automatic",
        "--no-promote",
    ]


def test_transcript_adapter_forwards_exact_receipt_selectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingester = _FakeIngester()
    monkeypatch.setattr(transcript_adapter, "_load_ingester", lambda: ingester)
    receipts = ("a" * 64, "b" * 64)

    assert (
        transcript_adapter.main(
            [
                "--repo-root",
                str(tmp_path),
                "--ticker",
                "nu",
                "--receipt-id",
                receipts[0],
                "--receipt-id",
                receipts[1],
            ]
        )
        == 0
    )

    assert ingester.seen_argv[-4:] == [
        "--receipt-id",
        receipts[0],
        "--receipt-id",
        receipts[1],
    ]


def test_transcript_adapter_preserves_explicit_owner_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingester = _FakeIngester()
    monkeypatch.setattr(transcript_adapter, "_load_ingester", lambda: ingester)

    assert (
        transcript_adapter.main(
            ["--repo-root", str(tmp_path), "--ticker", "nu", "--owner-requested"]
        )
        == 0
    )

    assert ingester.seen_argv == [
        "ingest_transcripts.py",
        "--db",
        str(tmp_path / "data/portfolio.db"),
        "--ticker",
        "NU",
    ]


def test_transcript_adapter_retargets_real_legacy_candidate_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    stale_code_root = tmp_path / "code"
    state_raw = state_root / "transcripts" / "raw"
    stale_raw = stale_code_root / "transcripts" / "raw"
    state_raw.mkdir(parents=True)
    stale_raw.mkdir(parents=True)
    state_file = state_raw / "NU_Q4_2024.txt"
    stale_file = stale_raw / "BAD_Q4_2024.txt"
    state_file.write_text("state evidence", encoding="utf-8")
    stale_file.write_text("stale code evidence", encoding="utf-8")
    monkeypatch.setattr(legacy_ingest, "_TRANSCRIPT_DIRS", (stale_raw,))

    transcript_adapter._bind_state(
        cast("transcript_adapter._IngestTranscripts", legacy_ingest),
        state_root,
    )

    candidates = legacy_ingest._candidate_files(None)
    assert [path for path, _parsed in candidates] == [state_file]


def test_nvo_adapter_binds_sources_outputs_env_and_legacy_args_to_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extractor = _FakeNvoExtractor()
    monkeypatch.setattr(nvo_adapter, "_load_extractor", lambda: extractor)
    monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)
    original_db_path = db.DB_PATH
    pdf = tmp_path / "annual.pdf"

    try:
        assert nvo_adapter.main(["--repo-root", str(tmp_path), "--pdf", str(pdf), "--force"]) == 0

        assert tmp_path == extractor.PROJECT_ROOT
        assert tmp_path / ".tmp/nvo_patents" == extractor.OUT_DIR
        assert tmp_path / "ir_documents/NVO" == extractor.IR_DOCS_DIR
        assert tmp_path / "micro_thesis/sources/NVO" == extractor.SOURCES_DIR
        assert extractor.loaded_env_root == tmp_path
        assert Path(db.DB_PATH) == tmp_path / "data/portfolio.db"
        assert extractor.seen_argv == [
            "extract_nvo_patent_timeline.py",
            "--pdf",
            str(pdf),
            "--force",
        ]
    finally:
        db.set_db_path(original_db_path)
