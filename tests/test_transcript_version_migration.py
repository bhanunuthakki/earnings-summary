"""Focused upgrade coverage for immutable transcript evidence versions."""

from __future__ import annotations

import importlib.util
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from compute.transcript_ingest import _insert_transcript, supersede_transcripts
from models.facts import FiscalPeriodType


def _legacy_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY,
            fetched_at TEXT,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL
        );
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            call_date TEXT,
            fiscal_period_type TEXT,
            period_end TEXT,
            source_url TEXT,
            has_qa_section INTEGER,
            source TEXT
        );
        CREATE TABLE transcript_segments (
            id INTEGER PRIMARY KEY,
            transcript_id INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            text TEXT NOT NULL
        );
        INSERT INTO documents VALUES
            (1, '2026-01-01T00:00:00', 'old.txt', 'old-sha'),
            (2, '2026-02-01T00:00:00', 'new.txt', 'new-sha');
        INSERT INTO transcripts VALUES
            (10, 1, 'NVDA', NULL, 'Q1', '2025-03-31', NULL, 1, 'legacy'),
            (20, 2, 'NVDA', NULL, 'Q1', '2025-03-31', NULL, 1, 'manual_pdf');
        INSERT INTO transcript_segments VALUES
            (100, 10, 0, 'old evidence'),
            (200, 20, 0, 'new evidence');
        """
    )
    conn.commit()
    conn.close()


def _upgrade(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions_archived"
        / "0250_immutable_transcript_versions.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0250", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = sa.create_engine(f"sqlite:///{path.as_posix()}")
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(module, "op", operations)
        module.upgrade()
    engine.dispose()


def _selected_lifecycle_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY,
            fetched_at TEXT,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL
        );
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            call_date TEXT,
            fiscal_period_type TEXT,
            period_end TEXT,
            source_url TEXT,
            has_qa_section INTEGER,
            source TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            superseded_by_id INTEGER,
            superseded_at TEXT,
            selection_reason TEXT
        );
        INSERT INTO documents VALUES
            (1, '2026-01-01T00:00:00', 'old.txt', 'old-sha'),
            (2, '2026-02-01T00:00:00', 'new.txt', 'new-sha');
        INSERT INTO transcripts VALUES
            (10, 1, 'NVDA', NULL, 'Q1', '2025-03-31 00:00:00', NULL, 1, 'legacy',
             1, NULL, NULL, 'initial');
        CREATE UNIQUE INDEX uq_transcripts_active_ticker_period_type_end
            ON transcripts(ticker, fiscal_period_type, period_end)
            WHERE is_active = 1;
        CREATE VIEW v_active_transcripts AS
            SELECT * FROM transcripts WHERE is_active = 1;
        """
    )
    conn.commit()
    conn.close()


def test_upgrade_normalizes_legacy_duplicates_before_unique_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "legacy.db"
    _legacy_database(db_path)

    _upgrade(db_path, monkeypatch)

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT id, version_number, is_current, superseded_by_transcript_id "
        "FROM transcripts ORDER BY id"
    ).fetchall()
    assert rows == [(10, 1, 0, 20), (20, 2, 1, None)]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO transcripts "
            "(id, document_id, ticker, fiscal_period_type, period_end, source, recorded_at) "
            "VALUES (30, 2, 'NVDA', 'Q1', '2025-03-31', 'other', CURRENT_TIMESTAMP)"
        )
    conn.close()


def test_upgrade_makes_evidence_append_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "legacy.db"
    _legacy_database(db_path)
    _upgrade(db_path, monkeypatch)

    conn = sqlite3.connect(db_path)
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        conn.execute("DELETE FROM transcripts WHERE id = 10")
    with pytest.raises(sqlite3.IntegrityError, match="segments are immutable"):
        conn.execute("UPDATE transcript_segments SET text = 'rewritten' WHERE id = 100")
    with pytest.raises(sqlite3.IntegrityError, match="source provenance is immutable"):
        conn.execute("UPDATE transcripts SET source = 'changed' WHERE id = 10")
    conn.close()


def test_upgrade_keeps_0214_selection_lifecycle_in_lockstep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "selected.db"
    _selected_lifecycle_database(db_path)
    _upgrade(db_path, monkeypatch)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    new_id = _insert_transcript(
        conn,
        document_id=2,
        ticker="NVDA",
        fiscal_period_type=FiscalPeriodType.Q1,
        period_end=datetime(2025, 3, 31),
        source="manual_pdf",
        is_current=False,
        superseded_by_transcript_id=10,
    )
    supersede_transcripts(
        conn,
        winner_transcript_id=new_id,
        loser_transcript_ids=[10],
        superseded_at=datetime(2026, 2, 1),
    )
    conn.commit()

    rows = conn.execute(
        "SELECT id, is_active, is_current, superseded_by_id, "
        "superseded_by_transcript_id FROM transcripts ORDER BY id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (10, 0, 0, new_id, new_id),
        (new_id, 1, 1, None, None),
    ]
    assert conn.execute("SELECT id FROM v_active_transcripts").fetchone()[0] == new_id
    with pytest.raises(sqlite3.IntegrityError, match="invalid transcript lifecycle"):
        conn.execute("UPDATE transcripts SET is_active = 0 WHERE id = ?", (new_id,))
    conn.close()
