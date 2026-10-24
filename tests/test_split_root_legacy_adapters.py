"""Split-root regressions for legacy transcript and NVO adapters."""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Callable, Generator
from pathlib import Path
from typing import cast

import extract_nvo_patent_timeline_state as nvo_adapter
import ingest_transcripts as legacy_ingest
import ingest_transcripts_state as transcript_adapter
import pytest

import db


@pytest.fixture(autouse=True)
def explicit_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Path]:
    from db_paths import db_path_context

    database = tmp_path / "explicit-authority.sqlite"
    database.touch()
    for field in ("DB_PATH", "DATA_DIR", "FMP_DIR", "PROJECT_ROOT", "STATE_ROOT"):
        monkeypatch.setattr(db, field, getattr(db, field))
    with db_path_context(database):
        yield database


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
    ) == getattr(ingester, "_TRANSCRIPT_DIRS")
    assert Path(ingester.index_manager.TRANSCRIPTS_RAW_DIR) == tmp_path / "transcripts/raw"
    assert ingester.seen_argv == [
        "ingest_transcripts.py",
        "--db",
        str(tmp_path / "explicit-authority.sqlite"),
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
        str(tmp_path / "explicit-authority.sqlite"),
        "--ticker",
        "NU",
    ]


def test_transcript_adapter_dry_run_keeps_exact_receipts_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingester = _FakeIngester()
    monkeypatch.setattr(transcript_adapter, "_load_ingester", lambda: ingester)
    state = tmp_path / "state"
    state.mkdir()
    database = tmp_path / "explicit-authority.sqlite"
    before = database.read_bytes()
    receipts = ("a" * 64, "b" * 64)
    assert (
        transcript_adapter.main(
            [
                "--repo-root",
                str(state),
                "--db",
                str(database),
                "--ticker",
                "NU",
                "--receipt-id",
                receipts[0],
                "--receipt-id",
                receipts[1],
                "--dry-run",
            ]
        )
        == 0
    )
    assert state == ingester.PROJECT_ROOT
    assert "--dry-run" in ingester.seen_argv
    assert "--automatic" in ingester.seen_argv
    assert [
        ingester.seen_argv[i + 1]
        for i, item in enumerate(ingester.seen_argv)
        if item == "--receipt-id"
    ] == list(receipts)
    assert database.read_bytes() == before
    assert not (state / "data/portfolio.db").exists()


def test_transcript_adapter_real_child_dry_run_has_no_ingestion_or_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    raw = state / "transcripts/raw"
    raw.mkdir(parents=True)
    selected = raw / "NU_Q1_2026.txt"
    other = raw / "WIX_Q1_2026.txt"
    selected.write_text("selected exact receipt evidence")
    other.write_text("other ticker stays untouched")
    database = tmp_path / "explicit-authority.sqlite"
    before = {path: path.read_bytes() for path in (database, selected, other)}
    for field in ("PROJECT_ROOT", "_TRANSCRIPT_DIRS"):
        monkeypatch.setattr(legacy_ingest, field, getattr(legacy_ingest, field))
    for field in (
        "PROJECT_ROOT",
        "CACHE_DIR",
        "TRANSCRIPT_INDEX_PATH",
        "DOCUMENT_INDEX_PATH",
        "TRANSCRIPTS_RAW_DIR",
        "TRANSCRIPTS_PROCESSED_DIR",
    ):
        monkeypatch.setattr(
            legacy_ingest.index_manager, field, getattr(legacy_ingest.index_manager, field)
        )
    monkeypatch.setattr(transcript_adapter, "_load_ingester", lambda: legacy_ingest)

    def read_only_database(path: str) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    def tracked(_conn: sqlite3.Connection) -> frozenset[str]:
        return frozenset({"NU", "WIX"})

    monkeypatch.setattr(legacy_ingest, "open_db", read_only_database)
    monkeypatch.setattr(legacy_ingest, "_load_tracked_tickers", tracked)
    receipts = ["a" * 64]

    def exact_candidates(
        _conn: sqlite3.Connection,
        receipt_ids: list[str],
        *,
        restrict_ticker: str | None,
        expected_owner_requested: bool,
        project_root: Path,
    ) -> tuple[
        list[tuple[Path, legacy_ingest.ParsedFilename]],
        dict[Path, legacy_ingest.AuthorizedTranscriptArtifact],
    ]:
        assert receipt_ids == receipts
        assert restrict_ticker == "NU"
        assert expected_owner_requested is False
        assert project_root == state
        parsed = legacy_ingest.parse_transcript_filename(selected)
        assert parsed is not None
        return [(selected, parsed)], {}

    def no_ingestion(*_args: object, **_kwargs: object) -> None:
        pytest.fail("dry-run reached financial/document writes")

    monkeypatch.setattr(legacy_ingest, "_receipt_scoped_candidates", exact_candidates)
    monkeypatch.setattr(legacy_ingest, "ingest_evidence_file", no_ingestion)
    assert (
        transcript_adapter.main(
            [
                "--repo-root",
                str(state),
                "--db",
                str(database),
                "--ticker",
                "NU",
                "--receipt-id",
                receipts[0],
                "--dry-run",
            ]
        )
        == 0
    )
    plan = json.loads(capsys.readouterr().out)
    assert plan["receipt_ids"] == receipts
    assert plan["candidates_total"] == 1
    assert len(plan["in_scope"]) == 1
    assert plan["in_scope"][0]["ticker"] == "NU"
    assert Path(plan["in_scope"][0]["file"]) == Path("transcripts/raw/NU_Q1_2026.txt")
    assert {path: path.read_bytes() for path in before} == before
    assert not (state / "transcripts/processed").exists()
    assert not (state / "data/portfolio.db").exists()


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

    transcript_adapter.bind_state(
        cast("transcript_adapter.IngestTranscripts", legacy_ingest),
        state_root,
    )

    candidates = cast(
        Callable[[str | None], list[tuple[Path, legacy_ingest.ParsedFilename]]],
        getattr(legacy_ingest, "_candidate_files"),
    )(None)
    assert [path for path, _parsed in candidates] == [state_file]


def test_nvo_adapter_binds_sources_outputs_env_and_legacy_args_to_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extractor = _FakeNvoExtractor()
    monkeypatch.setattr(nvo_adapter, "_load_extractor", lambda: extractor)
    monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)
    original_db_path = db.DB_PATH
    pdf = tmp_path / "annual.pdf"
    database = tmp_path / "explicit-authority.sqlite"
    database.touch()

    try:
        assert (
            nvo_adapter.main(
                ["--repo-root", str(tmp_path), "--db", str(database), "--pdf", str(pdf), "--force"]
            )
            == 0
        )

        assert tmp_path == extractor.PROJECT_ROOT
        assert tmp_path / ".tmp/nvo_patents" == extractor.OUT_DIR
        assert tmp_path / "ir_documents/NVO" == extractor.IR_DOCS_DIR
        assert tmp_path / "micro_thesis/sources/NVO" == extractor.SOURCES_DIR
        assert extractor.loaded_env_root == tmp_path
        assert Path(db.DB_PATH) == database
        assert extractor.seen_argv == [
            "extract_nvo_patent_timeline.py",
            "--pdf",
            str(pdf),
            "--force",
        ]
    finally:
        db.set_db_path(original_db_path)
