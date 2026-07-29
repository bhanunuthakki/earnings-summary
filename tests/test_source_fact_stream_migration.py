from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
BASE_REVISION = "0213_decision_draft_provider_id"
HEAD = "0246_source_fact_publication_stream"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def test_0246_is_reversible_single_head_with_exact_schema(
    tmp_path: Path,
) -> None:
    path = tmp_path / "source-fact-stream-migration.db"
    config = _config(path)
    script = ScriptDirectory.from_config(config)
    assert script.get_revision(HEAD).down_revision == (
        "0245_document_processing_research_snapshots"
    )
    assert len(script.get_heads()) == 1

    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY,
            source_doc_id INTEGER NOT NULL
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY,
            source_doc_id INTEGER NOT NULL
        );
        """
    )
    legacy.commit()
    legacy.close()
    command.stamp(config, BASE_REVISION)
    command.upgrade(config, HEAD)

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD,)
        tables = {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "source_fact_publication_stream_clock",
            "source_fact_publication_stream",
            "canonical_fact_resolution_snapshot_watermarks",
        } <= tables
        indexes = {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert {
            "ix_source_fact_publication_stream_sealed",
            "ix_canonical_resolution_snapshot_watermark_stream",
        } <= indexes
        triggers = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        assert {
            "trg_source_fact_publication_stream_exact",
            "trg_source_fact_publication_stream_append_only",
            "trg_source_fact_publication_stream_append_only_delete",
            "trg_canonical_resolution_snapshot_watermark_exact",
        } <= triggers
        assert conn.execute(
            "SELECT singleton_key,next_sequence FROM source_fact_publication_stream_clock"
        ).fetchone() == (1, 1)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()

    command.downgrade(
        config,
        "0245_document_processing_research_snapshots",
    )
    conn = sqlite3.connect(path)
    try:
        tables = {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "source_fact_publication_stream" not in tables
        assert "canonical_fact_resolution_snapshot_watermarks" not in tables
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0245_document_processing_research_snapshots",
        )
    finally:
        conn.close()
