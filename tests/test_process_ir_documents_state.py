"""Tests for the split-root IR-document processing adapter."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import process_ir_documents_state as adapter  # noqa: E402

import db  # noqa: E402


class _FakeIndexManager:
    PROJECT_ROOT = ""
    CACHE_DIR = ""
    TRANSCRIPT_INDEX_PATH = ""
    DOCUMENT_INDEX_PATH = ""
    TRANSCRIPTS_RAW_DIR = ""
    TRANSCRIPTS_PROCESSED_DIR = ""


class _FakeProcessor:
    PROJECT_ROOT = Path(".")
    CACHE_DIR = Path(".")
    index_manager = _FakeIndexManager()

    def __init__(self) -> None:
        self.seen_argv: list[str] = []

    def main(self) -> None:
        self.seen_argv = list(sys.argv)


def test_bind_state_routes_every_mutable_ir_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processor = _FakeProcessor()
    original_db_path = db.DB_PATH
    monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)

    try:
        adapter._bind_state(processor, tmp_path)

        assert tmp_path == processor.PROJECT_ROOT
        assert tmp_path / ".tmp" == processor.CACHE_DIR
        assert (
            Path(processor.index_manager.DOCUMENT_INDEX_PATH)
            == tmp_path / ".tmp/document_index.json"
        )
        assert Path(processor.index_manager.TRANSCRIPTS_RAW_DIR) == tmp_path / "transcripts/raw"
        assert Path(db.DB_PATH) == tmp_path / "data/portfolio.db"
    finally:
        db.set_db_path(original_db_path)


def test_main_binds_state_and_forwards_only_legacy_ticker_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processor = _FakeProcessor()
    original_argv = list(sys.argv)
    original_db_path = db.DB_PATH
    monkeypatch.setattr(adapter, "_load_processor", lambda: processor)

    try:
        assert adapter.main(["--ticker", "NU", "--repo-root", str(tmp_path)]) == 0

        assert tmp_path == processor.PROJECT_ROOT
        assert processor.seen_argv == ["process_ir_documents.py", "--ticker", "NU"]
        assert sys.argv == original_argv
    finally:
        db.set_db_path(original_db_path)
