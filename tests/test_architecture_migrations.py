from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command


def _config(db_path: Path) -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return config


def test_0209_to_head_adds_governance_and_integrity_foundation(tmp_path: Path) -> None:
    db_path = tmp_path / "migration.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE llm_calls (id INTEGER PRIMARY KEY, model TEXT);
        CREATE TABLE ingestion_runs (
            run_id TEXT PRIMARY KEY, started_at TEXT, ended_at TEXT,
            directive TEXT NOT NULL, ticker_scope TEXT NOT NULL,
            status TEXT NOT NULL, error_summary TEXT
        );
        INSERT INTO ingestion_runs VALUES
            ('attempt-1', '2026-07-26', NULL, 'refresh', '["NU"]', 'running', NULL);
        CREATE TABLE validation_issues (
            id INTEGER PRIMARY KEY, raised_at TEXT
        );
        INSERT INTO validation_issues VALUES (1, '2026-07-26');
        CREATE TABLE dcf_runs (
            id INTEGER PRIMARY KEY, valuation_date TEXT
        );
        INSERT INTO dcf_runs VALUES (1, '2026-07-26');
        CREATE TABLE documents (id INTEGER PRIMARY KEY);
        INSERT INTO documents VALUES (1);
        INSERT INTO documents VALUES (2);
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY, document_id INTEGER REFERENCES documents(id)
        );
        INSERT INTO transcripts VALUES (1, 1);
        """
    )
    conn.commit()
    conn.close()

    config = _config(db_path)
    command.stamp(config, "0209_transcripts_period_unique")
    command.upgrade(config, "head")

    conn = sqlite3.connect(db_path)
    llm_columns = {row[1] for row in conn.execute("PRAGMA table_info(llm_calls)")}
    assert {"provider", "transport", "attempts", "retries", "outcome"} <= llm_columns
    assert conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM pipeline_attempts").fetchone()[0] == 1
    assert conn.execute("SELECT engine_version FROM dcf_runs").fetchone()[0] == "legacy_pre_0211"
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE transcripts SET document_id=2 WHERE id=1")
    conn.close()

    command.downgrade(config, "0210_llm_call_transport_provenance")

    conn = sqlite3.connect(db_path)
    assert (
        conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        == "0210_llm_call_transport_provenance"
    )
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert "pipeline_runs" not in tables
    assert "pipeline_attempts" not in tables
    assert "pipeline_stage_transitions" not in tables
    assert {
        "pipeline_key",
        "attempt_id",
    }.isdisjoint(row[1] for row in conn.execute("PRAGMA table_info(ingestion_runs)"))
    assert {
        "fingerprint",
        "first_seen_at",
        "last_seen_at",
        "occurrence_count",
    }.isdisjoint(row[1] for row in conn.execute("PRAGMA table_info(validation_issues)"))
    assert {
        "input_sha256",
        "workbook_sha256",
        "engine_version",
        "inputs_as_of",
        "provenance_json",
    }.isdisjoint(row[1] for row in conn.execute("PRAGMA table_info(dcf_runs)"))
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'trigger' AND name = 'trg_transcripts_document_immutable'"
        ).fetchone()[0]
        == 0
    )
    assert conn.execute("SELECT run_id FROM ingestion_runs").fetchone()[0] == "attempt-1"
    assert conn.execute("SELECT raised_at FROM validation_issues").fetchone()[0] == "2026-07-26"
    assert conn.execute("SELECT valuation_date FROM dcf_runs").fetchone()[0] == "2026-07-26"
    conn.close()
