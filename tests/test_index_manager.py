"""Tests for src/index_manager.py.

Regression coverage for the bug where `register_transcript` stored a bare
filename (e.g. `AMZN_Q1_2026.txt`) and `process_ir_documents.py:process_document`
then ran `Path(local_path).exists()` from project root and silently skipped
every transcript. The fix canonicalizes to `transcripts/{raw,processed}/<name>`
so the stored `local_path` resolves regardless of caller CWD.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

import pytest

import index_manager


@pytest.fixture
def isolated_indexes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect index_manager paths to a tmp project root.

    Returns the fake project root so tests can drop fixture files under it.
    """
    fake_root = tmp_path / "project"
    fake_root.mkdir()
    (fake_root / ".tmp").mkdir()
    (fake_root / "transcripts" / "raw").mkdir(parents=True)
    (fake_root / "transcripts" / "processed").mkdir(parents=True)

    monkeypatch.setattr(index_manager, "PROJECT_ROOT", str(fake_root))
    monkeypatch.setattr(index_manager, "CACHE_DIR", str(fake_root / ".tmp"))
    monkeypatch.setattr(
        index_manager, "TRANSCRIPT_INDEX_PATH", str(fake_root / ".tmp" / "transcript_index.json")
    )
    monkeypatch.setattr(
        index_manager, "DOCUMENT_INDEX_PATH", str(fake_root / ".tmp" / "document_index.json")
    )
    monkeypatch.setattr(
        index_manager, "TRANSCRIPTS_RAW_DIR", str(fake_root / "transcripts" / "raw")
    )
    monkeypatch.setattr(
        index_manager,
        "TRANSCRIPTS_PROCESSED_DIR",
        str(fake_root / "transcripts" / "processed"),
    )
    return fake_root


def test_register_transcript_canonicalizes_bare_filename_to_processed_dir(
    isolated_indexes: Path,
) -> None:
    """Bug repro: registering with a bare filename should resolve from project root.

    Before the fix, `local_path` in document_index was a bare basename like
    `AMZN_Q1_2026.txt`. `Path("AMZN_Q1_2026.txt").exists()` was False when run
    from project root, so `process_ir_documents.py` silently skipped every
    transcript with `local_path_missing`.
    """
    transcript_file = isolated_indexes / "transcripts" / "processed" / "AMZN_Q1_2026.txt"
    transcript_file.write_text("synthetic transcript body", encoding="utf-8")

    index_manager.register_transcript(
        ticker="AMZN",
        year=2026,
        quarter="Q1",
        source="MANUAL",
        filepath="AMZN_Q1_2026.txt",
    )

    doc_entry = index_manager.has_document("AMZN", 2026, "Q1", "transcript")
    assert doc_entry is not None, "document_index entry was not created"
    stored = doc_entry["local_path"]
    assert stored == "transcripts/processed/AMZN_Q1_2026.txt", (
        f"local_path not canonicalized: got {stored!r}"
    )

    # The file must exist when resolved from project root — this is the
    # invariant `process_ir_documents.py:process_document` relies on.
    resolved = isolated_indexes / stored
    assert resolved.exists(), f"stored local_path {stored!r} does not resolve from project root"


def test_register_transcript_prefers_raw_over_processed_when_file_is_in_raw(
    isolated_indexes: Path,
) -> None:
    """During fetch, the file initially lives in transcripts/raw/ before promotion."""
    raw_file = isolated_indexes / "transcripts" / "raw" / "GOOG_Q3_2025.txt"
    raw_file.write_text("freshly-fetched transcript", encoding="utf-8")

    index_manager.register_transcript(
        ticker="GOOG",
        year=2025,
        quarter="Q3",
        source="yt_dlp_whisper_search",
        filepath="GOOG_Q3_2025.txt",
    )

    doc_entry = index_manager.has_document("GOOG", 2025, "Q3", "transcript")
    assert doc_entry is not None
    assert doc_entry["local_path"] == "transcripts/raw/GOOG_Q3_2025.txt"


def test_register_transcript_defaults_to_processed_when_file_not_on_disk(
    isolated_indexes: Path,
) -> None:
    """If neither raw/ nor processed/ has the file (rare; pre-write registration),
    default to processed/ so the stored path is still project-root-relative."""
    index_manager.register_transcript(
        ticker="META",
        year=2026,
        quarter="Q2",
        source="MANUAL",
        filepath="META_Q2_2026.txt",
    )
    doc_entry = index_manager.has_document("META", 2026, "Q2", "transcript")
    assert doc_entry is not None
    assert doc_entry["local_path"] == "transcripts/processed/META_Q2_2026.txt"


def test_register_transcript_passes_through_path_with_directory_separator(
    isolated_indexes: Path,
) -> None:
    """If the caller already supplies a directory-bearing path, trust it."""
    index_manager.register_transcript(
        ticker="NVDA",
        year=2026,
        quarter="Q4",
        source="MANUAL",
        filepath="some/custom/dir/NVDA_Q4_2026.txt",
    )
    doc_entry = index_manager.has_document("NVDA", 2026, "Q4", "transcript")
    assert doc_entry is not None
    assert doc_entry["local_path"] == "some/custom/dir/NVDA_Q4_2026.txt"


def test_register_transcript_preserves_none_filepath(isolated_indexes: Path) -> None:
    """A None filepath stays None — used when registering a stub before the file lands."""
    index_manager.register_transcript(
        ticker="AAPL",
        year=2026,
        quarter="Q1",
        source="MANUAL",
        filepath=None,
    )
    doc_entry = index_manager.has_document("AAPL", 2026, "Q1", "transcript")
    assert doc_entry is not None
    assert doc_entry["local_path"] is None


def test_legacy_transcript_index_filepath_also_canonicalized(
    isolated_indexes: Path,
) -> None:
    """Both indexes need canonical paths — `transcript_index.json` is consumed by
    `_ensure_qa_recorded` and friends, which read `filepath` for diagnostics."""
    transcript_file = isolated_indexes / "transcripts" / "processed" / "MELI_Q1_2026.txt"
    transcript_file.write_text("body", encoding="utf-8")

    index_manager.register_transcript(
        ticker="MELI",
        year=2026,
        quarter="Q1",
        source="aggregator_roic",
        filepath="MELI_Q1_2026.txt",
    )

    legacy = json.loads(Path(index_manager.TRANSCRIPT_INDEX_PATH).read_text())
    entry = legacy["MELI_2026_Q1"]
    assert entry["filepath"] == "transcripts/processed/MELI_Q1_2026.txt"


def test_get_documents_for_ticker_includes_processed_docs(
    isolated_indexes: Path,
) -> None:
    """get_documents_for_ticker returns docs regardless of the `processed` flag.

    Transcripts are mirrored into document_index with processed=True at ingest
    ('legacy flow already processed'). The --regenerate-missing summary path
    relies on THIS accessor to re-find them — get_unprocessed_documents excludes
    them, which is exactly why their `_summary.txt` (and §6 Say-Do) never gets
    generated for freshly-onboarded names.
    """
    f = isolated_indexes / "transcripts" / "processed" / "UBER_Q1_2026.txt"
    f.write_text("body", encoding="utf-8")
    index_manager.register_transcript(
        ticker="UBER",
        year=2026,
        quarter="Q1",
        source="MANUAL",
        filepath="UBER_Q1_2026.txt",
    )

    doc = cast(
        "dict[str, object] | None",
        index_manager.has_document("UBER", 2026, "Q1", "transcript"),
    )
    assert doc is not None and doc["processed"] is True

    # The default summary-pipeline accessor excludes the processed doc ...
    assert index_manager.get_unprocessed_documents("UBER") == []
    # ... but get_documents_for_ticker (what the fix reuses) returns it.
    all_docs = cast("list[dict[str, object]]", index_manager.get_documents_for_ticker("UBER"))
    assert [d["doc_type"] for d in all_docs] == ["transcript"]
    assert all_docs[0]["processed"] is True


def test_canonicalize_helper_uses_forward_slashes_cross_platform(
    isolated_indexes: Path,
) -> None:
    """Stored paths must use forward slashes regardless of host OS so that
    `Path(stored).exists()` and on-disk diff against the index don't trip on
    Windows backslashes."""
    transcript_file = isolated_indexes / "transcripts" / "processed" / "BN_Q4_2025.txt"
    transcript_file.write_text("body", encoding="utf-8")

    result = index_manager._canonicalize_transcript_filepath("BN_Q4_2025.txt")
    assert result is not None
    assert os.sep == "\\" and "\\" not in result or os.sep == "/", (
        f"backslash leaked into canonical path on non-POSIX host: {result!r}"
    )
    assert "/" in result
