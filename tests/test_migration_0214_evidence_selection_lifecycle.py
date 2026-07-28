"""Schema contract for the evidence-selection lifecycle migration (0214)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0213_evidence_ledger_foundation"
HEAD = "0214_evidence_selection_lifecycle"


def _config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _seed_prior_schema(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE transcripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL UNIQUE,
                ticker TEXT NOT NULL,
                call_date TIMESTAMP,
                fiscal_period_type TEXT,
                period_end TIMESTAMP,
                source_url TEXT,
                has_qa_section INTEGER,
                source TEXT
            );
            CREATE UNIQUE INDEX uq_transcripts_ticker_period_type_end
                ON transcripts (ticker, fiscal_period_type, period_end);
            CREATE TABLE filing_sections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL, source TEXT NOT NULL, source_ref TEXT NOT NULL,
                doc_id INTEGER, accession_number TEXT, form TEXT NOT NULL,
                fiscal_year INTEGER, fiscal_period TEXT NOT NULL, period_end TIMESTAMP,
                filing_date TEXT, section_key_raw TEXT NOT NULL, section_stem TEXT NOT NULL,
                canonical_id TEXT, title TEXT, ordinal INTEGER NOT NULL, text TEXT NOT NULL,
                text_sha256 TEXT NOT NULL, char_len INTEGER NOT NULL,
                key_truncated INTEGER NOT NULL DEFAULT 0, extractor_version TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                CONSTRAINT uq_filing_sections_key
                    UNIQUE (source, source_ref, section_key_raw, ordinal)
            );
            CREATE INDEX ix_filing_sections_ticker_period
                ON filing_sections (ticker, form, fiscal_year, fiscal_period);
            CREATE TABLE filing_section_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                section_id INTEGER NOT NULL, text TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO transcripts (document_id, ticker, fiscal_period_type, period_end) "
            "VALUES (1, 'NVDA', 'Q1', '2025-03-31')"
        )
        conn.execute(
            "INSERT INTO filing_sections (id, ticker, source, source_ref, form, fiscal_period, "
            "section_key_raw, section_stem, ordinal, text, text_sha256, char_len, "
            "extractor_version, created_at) VALUES "
            "(7, 'NVDA', 'edgar_text', 'acc-1', '10-Q', 'Q1', 'Item 1A', "
            "'item 1a', 0, 'old text', 'a', 8, 'v1', '2026-01-01')"
        )
        conn.execute("INSERT INTO filing_section_items (section_id, text) VALUES (7, 'child')")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "lifecycle.db"
    _seed_prior_schema(path)
    command.stamp(_config(path), PRIOR_HEAD)
    return path


def test_upgrade_preserves_rows_and_enforces_one_active_selection(db_path: Path) -> None:
    command.upgrade(_config(db_path), HEAD)
    conn = sqlite3.connect(str(db_path))
    try:
        transcript_columns = {row[1] for row in conn.execute("PRAGMA table_info(transcripts)")}
        assert {
            "is_active",
            "superseded_by_id",
            "superseded_at",
            "selection_reason",
        } <= transcript_columns
        filing_columns = {row[1] for row in conn.execute("PRAGMA table_info(filing_sections)")}
        assert {
            "is_active",
            "superseded_by_id",
            "superseded_at",
            "retirement_reason",
        } <= filing_columns
        assert conn.execute("SELECT id, is_active FROM filing_sections").fetchone() == (7, 1)
        assert conn.execute("SELECT section_id FROM filing_section_items").fetchone() == (7,)
        assert conn.execute("SELECT id FROM v_active_transcripts").fetchone() == (1,)
        assert conn.execute("SELECT id FROM v_active_filing_sections").fetchone() == (7,)

        conn.execute(
            "INSERT INTO transcripts (document_id, ticker, fiscal_period_type, period_end, is_active) "
            "VALUES (2, 'NVDA', 'Q1', '2025-03-31', 0)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO transcripts (document_id, ticker, fiscal_period_type, period_end, is_active) "
                "VALUES (3, 'NVDA', 'Q1', '2025-03-31', 1)"
            )
        conn.execute("UPDATE transcripts SET is_active = 0 WHERE id = 1")
        conn.execute("UPDATE transcripts SET is_active = 1 WHERE document_id = 2")
        assert conn.execute("SELECT document_id FROM v_active_transcripts").fetchone() == (2,)

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO filing_sections (ticker, source, source_ref, form, fiscal_period, "
                "section_key_raw, section_stem, ordinal, text, text_sha256, char_len, "
                "extractor_version, created_at, is_active) VALUES "
                "('NVDA', 'edgar_text', 'acc-1', '10-Q', 'Q1', 'Item 1A', "
                "'item 1a', 0, 'new text', 'b', 8, 'v2', '2026-01-02', 1)"
            )
        conn.execute("UPDATE filing_sections SET is_active = 0 WHERE id = 7")
        conn.execute(
            "INSERT INTO filing_sections (ticker, source, source_ref, form, fiscal_period, "
            "section_key_raw, section_stem, ordinal, text, text_sha256, char_len, "
            "extractor_version, created_at, is_active) VALUES "
            "('NVDA', 'edgar_text', 'acc-1', '10-Q', 'Q1', 'Item 1A', "
            "'item 1a', 0, 'new text', 'b', 8, 'v2', '2026-01-02', 1)"
        )
        assert conn.execute("SELECT text FROM v_active_filing_sections").fetchone() == ("new text",)
    finally:
        conn.close()


def test_downgrade_round_trip_restores_pre_lifecycle_schema(db_path: Path) -> None:
    cfg = _config(db_path)
    command.upgrade(cfg, HEAD)
    command.downgrade(cfg, PRIOR_HEAD)
    conn = sqlite3.connect(str(db_path))
    try:
        views = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'")
        }
        assert "v_active_transcripts" not in views
        assert "v_active_filing_sections" not in views
    finally:
        conn.close()
    command.upgrade(cfg, HEAD)
    conn = sqlite3.connect(str(db_path))
    try:
        assert "is_active" in {row[1] for row in conn.execute("PRAGMA table_info(transcripts)")}
        assert "is_active" in {row[1] for row in conn.execute("PRAGMA table_info(filing_sections)")}
        assert conn.execute("SELECT id FROM filing_sections").fetchone() == (7,)
    finally:
        conn.close()
