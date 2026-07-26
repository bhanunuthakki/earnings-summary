"""Tests for the raw→processed auto-promote step in `execution/ingest_transcripts.py`.

Covers `_promote_raw_to_processed` directly (the five unit cases) plus one
end-to-end run through `main()` to confirm the wiring stays connected.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

import index_manager
from compute.transcript_ingest import IngestResult, ParsedFilename, QASectionStatus, sha256_of

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_ingest_module():
    """Import `execution/ingest_transcripts.py` by file path.

    The script lives under `execution/`, which isn't a package and is not on
    `sys.path`. Loading via importlib lets the tests exercise the real module.
    """
    spec = importlib.util.spec_from_file_location(
        "ingest_transcripts_under_test",
        _REPO_ROOT / "execution" / "ingest_transcripts.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stand up a project layout the promoter can run inside.

    Mirrors the dirs the real script touches (`transcripts/{raw,processed}`,
    `.tmp/` for the on-disk indexes) and points `index_manager` at them so
    promotion side-effects land in the temp tree instead of the real repo.
    """
    root = tmp_path / "proj"
    (root / "transcripts" / "raw").mkdir(parents=True)
    (root / "transcripts" / "processed").mkdir(parents=True)
    (root / ".tmp").mkdir()

    monkeypatch.setattr(index_manager, "PROJECT_ROOT", str(root))
    monkeypatch.setattr(index_manager, "CACHE_DIR", str(root / ".tmp"))
    monkeypatch.setattr(
        index_manager, "TRANSCRIPT_INDEX_PATH", str(root / ".tmp" / "transcript_index.json")
    )
    monkeypatch.setattr(
        index_manager, "DOCUMENT_INDEX_PATH", str(root / ".tmp" / "document_index.json")
    )
    monkeypatch.setattr(index_manager, "TRANSCRIPTS_RAW_DIR", str(root / "transcripts" / "raw"))
    monkeypatch.setattr(
        index_manager,
        "TRANSCRIPTS_PROCESSED_DIR",
        str(root / "transcripts" / "processed"),
    )
    return root


@pytest.fixture
def conn() -> sqlite3.Connection:
    """In-memory DB with just enough schema for the promoter to UPDATE."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            source_type TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            period_start TIMESTAMP,
            period_end TIMESTAMP,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            fetched_at TIMESTAMP NOT NULL,
            fetch_status TEXT NOT NULL,
            http_code INTEGER,
            raw_bytes_size INTEGER NOT NULL,
            source_url TEXT,
            parent_document_id INTEGER
        );
        """
    )
    return c


def _insert_doc(conn: sqlite3.Connection, ticker: str, rel_path: str, sha: str) -> int:
    cur = conn.execute(
        "INSERT INTO documents "
        "(ticker, source_type, doc_type, period_end, file_path, sha256, fetched_at, "
        " fetch_status, raw_bytes_size) "
        "VALUES (?, 'transcript_audio', 'earnings_call_transcript', ?, ?, ?, ?, 'ok', 100)",
        (ticker, datetime(2025, 3, 31), rel_path, sha, datetime.now()),
    )
    conn.commit()
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def _result(
    file_path: Path, *, doc_id: int, skipped: bool = False, ticker: str = "GOOG"
) -> IngestResult:
    return IngestResult(
        file_path=file_path,
        ticker=ticker,
        period_end=datetime(2025, 3, 31),
        document_id=doc_id,
        transcript_id=None if skipped else 1,
        segment_count=0 if skipped else 5,
        skipped_existing=skipped,
        qa_status=QASectionStatus.PRESENT,
        qa_signals=("qa_header",),
    )


def _parsed(ticker: str = "GOOG", q: int = 1, year: int = 2025) -> ParsedFilename:
    return ParsedFilename(ticker=ticker, quarter_idx=q, fiscal_year_label=year)


def test_fresh_raw_ingest_moves_file_updates_db(
    fake_project: Path, conn: sqlite3.Connection
) -> None:
    """The happy path: raw/ file moves to processed/, DB is repointed."""
    mod = _load_ingest_module()

    raw_path = fake_project / "transcripts" / "raw" / "GOOG_Q1_2025.txt"
    raw_path.write_text("hello transcript", encoding="utf-8")
    sha = sha256_of(raw_path)
    doc_id = _insert_doc(conn, "GOOG", "transcripts/raw/GOOG_Q1_2025.txt", sha)

    new_path = mod._promote_raw_to_processed(
        _result(raw_path, doc_id=doc_id), _parsed(), conn, fake_project
    )

    assert new_path == fake_project / "transcripts" / "processed" / "GOOG_Q1_2025.txt"
    assert new_path.exists()
    assert not raw_path.exists()

    db_path = conn.execute("SELECT file_path FROM documents WHERE id = ?", (doc_id,)).fetchone()[0]
    assert db_path == "transcripts/processed/GOOG_Q1_2025.txt"


def test_skipped_ingest_is_noop(fake_project: Path, conn: sqlite3.Connection) -> None:
    """`skipped_existing=True` means no fresh ingest — leave everything alone."""
    mod = _load_ingest_module()

    raw_path = fake_project / "transcripts" / "raw" / "AMZN_Q2_2025.txt"
    raw_path.write_text("already-ingested bytes", encoding="utf-8")
    sha = sha256_of(raw_path)
    doc_id = _insert_doc(conn, "AMZN", "transcripts/raw/AMZN_Q2_2025.txt", sha)

    new_path = mod._promote_raw_to_processed(
        _result(raw_path, doc_id=doc_id, skipped=True, ticker="AMZN"),
        _parsed("AMZN", 2, 2025),
        conn,
        fake_project,
    )

    assert new_path == raw_path
    assert raw_path.exists()  # untouched
    db_path = conn.execute("SELECT file_path FROM documents WHERE id = ?", (doc_id,)).fetchone()[0]
    assert db_path == "transcripts/raw/AMZN_Q2_2025.txt"


def test_already_processed_file_is_noop(fake_project: Path, conn: sqlite3.Connection) -> None:
    """File coming in from `processed/` already — nothing to promote."""
    mod = _load_ingest_module()

    processed_path = fake_project / "transcripts" / "processed" / "META_Q3_2024.txt"
    processed_path.write_text("processed-canonical", encoding="utf-8")
    sha = sha256_of(processed_path)
    doc_id = _insert_doc(conn, "META", "transcripts/processed/META_Q3_2024.txt", sha)

    new_path = mod._promote_raw_to_processed(
        _result(processed_path, doc_id=doc_id, ticker="META"),
        _parsed("META", 3, 2024),
        conn,
        fake_project,
    )

    assert new_path == processed_path
    assert processed_path.exists()
    db_path = conn.execute("SELECT file_path FROM documents WHERE id = ?", (doc_id,)).fetchone()[0]
    assert db_path == "transcripts/processed/META_Q3_2024.txt"


def test_sha_match_conflict_deletes_raw_keeps_processed(
    fake_project: Path, conn: sqlite3.Connection
) -> None:
    """If processed/ already has the same bytes, drop the raw/ duplicate."""
    mod = _load_ingest_module()

    body = "duplicate transcript content"
    raw_path = fake_project / "transcripts" / "raw" / "NU_Q1_2026.txt"
    processed_path = fake_project / "transcripts" / "processed" / "NU_Q1_2026.txt"
    raw_path.write_text(body, encoding="utf-8")
    processed_path.write_text(body, encoding="utf-8")

    sha = sha256_of(raw_path)
    # DB row was inserted at fresh-ingest time pointing at the raw/ path even
    # though processed/ was already there — this is the state the promoter
    # has to repair.
    doc_id = _insert_doc(conn, "NU", "transcripts/raw/NU_Q1_2026.txt", sha)

    new_path = mod._promote_raw_to_processed(
        _result(raw_path, doc_id=doc_id, ticker="NU"),
        _parsed("NU", 1, 2026),
        conn,
        fake_project,
    )

    assert new_path == processed_path
    assert processed_path.exists()
    assert not raw_path.exists()
    assert processed_path.read_text(encoding="utf-8") == body  # surviving copy intact
    db_path = conn.execute("SELECT file_path FROM documents WHERE id = ?", (doc_id,)).fetchone()[0]
    assert db_path == "transcripts/processed/NU_Q1_2026.txt"


def test_sha_mismatch_conflict_preserves_both_logs_event(
    fake_project: Path,
    conn: sqlite3.Connection,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Different bytes at the target slot = surface, don't clobber."""
    mod = _load_ingest_module()

    raw_path = fake_project / "transcripts" / "raw" / "WIX_Q4_2025.txt"
    processed_path = fake_project / "transcripts" / "processed" / "WIX_Q4_2025.txt"
    raw_path.write_text("VERSION A — newer aggregator pull", encoding="utf-8")
    processed_path.write_text("VERSION B — older manual drop", encoding="utf-8")

    sha_raw = sha256_of(raw_path)
    doc_id = _insert_doc(conn, "WIX", "transcripts/raw/WIX_Q4_2025.txt", sha_raw)

    new_path = mod._promote_raw_to_processed(
        _result(raw_path, doc_id=doc_id, ticker="WIX"),
        _parsed("WIX", 4, 2025),
        conn,
        fake_project,
    )

    # Both files preserved; DB untouched.
    assert new_path == raw_path
    assert raw_path.exists()
    assert processed_path.exists()
    db_path = conn.execute("SELECT file_path FROM documents WHERE id = ?", (doc_id,)).fetchone()[0]
    assert db_path == "transcripts/raw/WIX_Q4_2025.txt"

    # Structured event emitted to stderr for investigation.
    err = capsys.readouterr().err
    event = json.loads(err.strip().splitlines()[-1])
    assert event["event"] == "transcript_promotion_conflict"
    assert event["src"] == "transcripts/raw/WIX_Q4_2025.txt"
    assert event["target"] == "transcripts/processed/WIX_Q4_2025.txt"
    assert event["src_sha256"] != event["target_sha256"]
    assert event["document_id"] == doc_id


def test_index_entries_are_updated_after_promotion(
    fake_project: Path, conn: sqlite3.Connection
) -> None:
    """The on-disk indexes must learn about the new processed/ location too."""
    mod = _load_ingest_module()

    raw_path = fake_project / "transcripts" / "raw" / "GOOG_Q1_2025.txt"
    raw_path.write_text("transcript text", encoding="utf-8")
    sha = sha256_of(raw_path)
    doc_id = _insert_doc(conn, "GOOG", "transcripts/raw/GOOG_Q1_2025.txt", sha)

    # Seed both indexes with raw/ entries — the state a freshly-fetched file
    # would leave behind after fetch_qa_transcript ran.
    index_manager.register_transcript(
        "GOOG",
        2025,
        "Q1",
        source="aggregator_roic",
        filepath="GOOG_Q1_2025.txt",  # canonicalizes to raw/ since the file is there
        has_qa=True,
    )

    mod._promote_raw_to_processed(_result(raw_path, doc_id=doc_id), _parsed(), conn, fake_project)

    t_entry = index_manager.has_transcript("GOOG", 2025, "Q1")
    assert t_entry is not None
    assert t_entry["filepath"] == "transcripts/processed/GOOG_Q1_2025.txt"

    d_entry = index_manager.has_document("GOOG", 2025, "Q1", "transcript")
    assert d_entry is not None
    assert d_entry["local_path"] == "transcripts/processed/GOOG_Q1_2025.txt"


# ---------------------------------------------------------------------------
# Integration: drive `main()` end-to-end across mixed raw/ + processed/ files
# ---------------------------------------------------------------------------


def test_main_promotes_after_fresh_ingest(
    fake_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a fresh-raw file is in processed/ after the script exits."""
    mod = _load_ingest_module()

    # Stand up a temp portfolio.db with the minimum schema main() touches.
    db_path = fake_project / "data" / "portfolio.db"
    db_path.parent.mkdir(parents=True)
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE tracked_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER DEFAULT 1,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            list_type TEXT NOT NULL,
            added_at TIMESTAMP,
            archived_at TIMESTAMP
        );
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            source_type TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            period_start TIMESTAMP,
            period_end TIMESTAMP,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            fetched_at TIMESTAMP NOT NULL,
            fetch_status TEXT NOT NULL,
            http_code INTEGER,
            raw_bytes_size INTEGER NOT NULL,
            source_url TEXT,
            parent_document_id INTEGER
        );
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            call_date TIMESTAMP,
            fiscal_period_type TEXT,
            period_end TIMESTAMP,
            source_url TEXT,
            has_qa_section INTEGER,
            source TEXT
        );
        CREATE TABLE transcript_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transcript_id INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            speaker TEXT,
            speaker_role TEXT,
            time_code_start TEXT,
            time_code_end TEXT,
            text TEXT NOT NULL
        );
        CREATE TABLE ingestion_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL UNIQUE,
            started_at TIMESTAMP NOT NULL,
            ended_at TIMESTAMP,
            directive TEXT NOT NULL,
            ticker_scope TEXT NOT NULL,
            status TEXT NOT NULL,
            error_summary TEXT
        );
        CREATE TABLE stage_transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            period_end TIMESTAMP,
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TIMESTAMP NOT NULL,
            ended_at TIMESTAMP,
            error_msg TEXT
        );
        """
    )
    c.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type) VALUES (?, ?, ?)",
        ("GOOG", "Alphabet", "portfolio"),
    )
    c.commit()
    c.close()

    # Drop a real-enough transcript into raw/. The text needs >=2 speaker
    # turns so `ingest_one` returns a non-skipped result. Add the QA header
    # so detect_qa_section also fires structurally instead of guessing.
    raw_path = fake_project / "transcripts" / "raw" / "GOOG_Q1_2025.txt"
    raw_path.write_text(
        (
            "Operator\n"
            "Good afternoon, and welcome to the GOOG Q1 2025 call.\n"
            "Sundar Pichai\n\nThanks, Jim. Hi, everyone. "
            + "Lorem ipsum dolor sit amet. "
            * 200
            + "\n\n"
            "Anant Ashkenazi\n\nThanks, Sundar. Q1 revenue grew. "
            + "Consectetur adipiscing elit. " * 200
            + "\n\nQUESTION AND ANSWER SECTION\n"
        ),
        encoding="utf-8",
    )

    # Point the module's PROJECT_ROOT + transcript dirs at the fake tree.
    monkeypatch.setattr(mod, "PROJECT_ROOT", fake_project)
    monkeypatch.setattr(
        mod,
        "_TRANSCRIPT_DIRS",
        (
            fake_project / "transcripts" / "processed",
            fake_project / "transcripts" / "raw",
        ),
    )
    monkeypatch.setattr(sys, "argv", ["ingest_transcripts.py", "--db", str(db_path)])

    rc = mod.main()
    assert rc == 0

    # File promoted, DB updated.
    assert (fake_project / "transcripts" / "processed" / "GOOG_Q1_2025.txt").exists()
    assert not raw_path.exists()
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    row = c.execute(
        "SELECT file_path FROM documents WHERE ticker = 'GOOG' "
        "AND doc_type = 'earnings_call_transcript'"
    ).fetchone()
    c.close()
    assert row["file_path"] == "transcripts/processed/GOOG_Q1_2025.txt"
