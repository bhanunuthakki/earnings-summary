"""Tests for the one-off `scratch/repoint_stale_raw_rows_2026_05_25.py` sweep.

The sweep finds documents rows still pointing at `transcripts/raw/<name>`
even though the bytes now live at `transcripts/processed/<name>` (residue
from earlier manual hand-moves), and rewrites the DB + on-disk indexes to
match.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

import index_manager


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_sweep_module():
    """Load the scratch script by file path (scratch/ isn't on sys.path)."""
    spec = importlib.util.spec_from_file_location(
        "repoint_stale_raw_rows_under_test",
        _REPO_ROOT / "scratch" / "repoint_stale_raw_rows_2026_05_25.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_project(tmp_path: Path) -> Path:
    """Project layout the sweep needs: transcripts/{raw,processed} + .tmp/."""
    root = tmp_path / "proj"
    (root / "transcripts" / "raw").mkdir(parents=True)
    (root / "transcripts" / "processed").mkdir(parents=True)
    (root / ".tmp").mkdir()
    (root / "data").mkdir()
    return root


def _make_db(db_path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(db_path))
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


def _insert_doc(c: sqlite3.Connection, ticker: str, rel_path: str) -> int:
    cur = c.execute(
        "INSERT INTO documents "
        "(ticker, source_type, doc_type, period_end, file_path, sha256, fetched_at, "
        " fetch_status, raw_bytes_size) "
        "VALUES (?, 'transcript_audio', 'earnings_call_transcript', ?, ?, ?, ?, 'ok', 100)",
        (ticker, datetime(2025, 3, 31), rel_path, f"sha-{rel_path}", datetime.now()),
    )
    c.commit()
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def test_sweep_repoints_db_when_processed_file_exists(
    fake_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The happy path: raw/-pointing row + processed/<basename> on disk ==> DB repointed."""
    # File lives ONLY in processed/ — this models the residue state (raw/ was
    # hand-moved earlier but the DB pointer was left stale).
    (fake_project / "transcripts" / "processed" / "GOOG_Q1_2025.txt").write_text(
        "transcript bytes", encoding="utf-8"
    )

    db_path = fake_project / "data" / "portfolio.db"
    c = _make_db(db_path)
    doc_id = _insert_doc(c, "GOOG", "transcripts/raw/GOOG_Q1_2025.txt")
    c.close()

    # Seed both indexes with the stale raw/ pointer so the index-rewrite path
    # actually has something to update.
    index_manager.PROJECT_ROOT = str(fake_project)
    index_manager.CACHE_DIR = str(fake_project / ".tmp")
    index_manager.TRANSCRIPT_INDEX_PATH = str(fake_project / ".tmp" / "transcript_index.json")
    index_manager.DOCUMENT_INDEX_PATH = str(fake_project / ".tmp" / "document_index.json")
    index_manager.TRANSCRIPTS_RAW_DIR = str(fake_project / "transcripts" / "raw")
    index_manager.TRANSCRIPTS_PROCESSED_DIR = str(fake_project / "transcripts" / "processed")
    index_manager.register_transcript(
        "GOOG", 2025, "Q1",
        source="aggregator_roic",
        filepath="transcripts/raw/GOOG_Q1_2025.txt",
        has_qa=True,
    )

    mod = _load_sweep_module()
    monkeypatch.setattr(
        sys, "argv",
        ["repoint.py", "--project-root", str(fake_project), "--db", str(db_path)],
    )
    rc = mod.main()
    assert rc == 0

    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    db_path_value = c.execute(
        "SELECT file_path FROM documents WHERE id = ?", (doc_id,)
    ).fetchone()[0]
    c.close()
    assert db_path_value == "transcripts/processed/GOOG_Q1_2025.txt"

    t_entry = index_manager.has_transcript("GOOG", 2025, "Q1")
    assert t_entry is not None
    assert t_entry["filepath"] == "transcripts/processed/GOOG_Q1_2025.txt"
    d_entry = index_manager.has_document("GOOG", 2025, "Q1", "transcript")
    assert d_entry is not None
    assert d_entry["local_path"] == "transcripts/processed/GOOG_Q1_2025.txt"


def test_sweep_leaves_row_alone_when_processed_file_missing(
    fake_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Truly-orphaned rows (no processed/<basename>) get a warning, not a rewrite."""
    # Note: nothing written under processed/ — the file is genuinely gone.
    db_path = fake_project / "data" / "portfolio.db"
    c = _make_db(db_path)
    doc_id = _insert_doc(c, "AMZN", "transcripts/raw/AMZN_Q2_2024.txt")
    c.close()

    mod = _load_sweep_module()
    monkeypatch.setattr(
        sys, "argv",
        ["repoint.py", "--project-root", str(fake_project), "--db", str(db_path)],
    )
    rc = mod.main()
    assert rc == 0

    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    db_path_value = c.execute(
        "SELECT file_path FROM documents WHERE id = ?", (doc_id,)
    ).fetchone()[0]
    c.close()
    assert db_path_value == "transcripts/raw/AMZN_Q2_2024.txt"
