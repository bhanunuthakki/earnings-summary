from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
REVISION = "0248_native_processing_closure_adapters"
PARENT = "0247_bounded_canonical_retrieval"
TABLES = {
    "document_processing_evidence_headers",
    "document_processing_evidence_members",
    "document_processing_evidence_seals",
}


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _sha(value: object) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


def test_0248_staged_seal_append_only_freezes_native_rows_and_downgrades(
    tmp_path: Path,
) -> None:
    path = tmp_path / "native-processing-evidence-migration.db"
    config = _config(path)
    seed = sqlite3.connect(path)
    seed.executescript(
        """
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY, source_doc_id INTEGER NOT NULL
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY, source_doc_id INTEGER NOT NULL
        );
        """
    )
    seed.close()
    base_revision = "0213_decision_draft_provider_id"
    command.stamp(config, base_revision)
    command.upgrade(config, REVISION)

    revision = ScriptDirectory.from_config(config).get_revision(REVISION)
    assert revision is not None
    assert revision.down_revision == PARENT

    conn = sqlite3.connect(path)
    conn.create_function("fact_sha256", 1, _sha)
    conn.execute("PRAGMA foreign_keys=ON")
    existing = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert existing >= TABLES
    for table in TABLES:
        triggers = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
                (table,),
            )
        }
        assert f"trg_{table}_update_append_only" in triggers
        assert f"trg_{table}_delete_append_only" in triggers

    with pytest.raises(sqlite3.IntegrityError, match="native run"):
        conn.execute(
            "INSERT INTO document_processing_evidence_headers ("
            "evidence_seal_id,idempotency_key,document_version_id,"
            "processing_lane,extraction_run_id,adapter_name,adapter_version,"
            "adapter_config_sha256,input_blob_sha256,native_output_sha256,"
            "native_scope_json,native_scope_sha256,canonical_header_json,"
            "header_sha256,cutoff_at,knowledge_at,recorded_at) VALUES ("
            "'bad','bad','missing','pdf_text','missing','adapter','v1',"
            "?,?,?,?,?,?,?,?,?,?)",
            (
                "a" * 64,
                "b" * 64,
                "c" * 64,
                "{}",
                "0" * 64,
                "{}",
                _sha("{}"),
                "2026-07-27 12:00:00",
                "2026-07-27 12:00:00",
                "2026-07-27 12:00:00",
            ),
        )
    conn.rollback()
    conn.close()

    command.downgrade(config, PARENT)
    conn = sqlite3.connect(path)
    remaining = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert not (TABLES & remaining)
    assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (PARENT,)
    conn.close()
