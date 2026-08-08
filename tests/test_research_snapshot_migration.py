from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
BASE_REVISION = "0213_decision_draft_provider_id"
REVISION = "0245_document_processing_research_snapshots"
PARENT = "0244_canonical_fact_resolution"
TABLES = {
    "document_processing_obligation_revisions",
    "document_processing_disposition_headers",
    "document_processing_disposition_members",
    "document_processing_disposition_seals",
    "document_processing_snapshot_headers",
    "document_processing_snapshot_members",
    "document_processing_snapshot_seals",
    "research_snapshot_headers",
    "research_snapshot_members",
    "research_snapshot_seals",
}
T1 = "2026-07-27 13:00:00"
T2 = "2026-07-28 13:00:00"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("version_locations", str(ROOT / "alembic" / "versions_archived"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _sha(value: object) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


def test_0245_exact_tables_final_seal_append_only_and_downgrade(
    tmp_path: Path,
) -> None:
    path = tmp_path / "research-snapshot-migration.db"
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
    command.stamp(config, BASE_REVISION)
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

    request = "{}"
    conn.execute(
        "INSERT INTO research_snapshot_headers VALUES (?,?,?,?,?,?)",
        ("research:backdated", "research:backdated", request, _sha(request), T1, T2),
    )
    with pytest.raises(sqlite3.IntegrityError, match="final seal mismatch"):
        conn.execute(
            "INSERT INTO research_snapshot_seals VALUES (?,?,?,?,?)",
            ("research:backdated", 0, "[]", _sha("[]"), T1),
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE research_snapshot_headers SET recorded_at=? "
            "WHERE research_snapshot_id='research:backdated'",
            (T1,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="commitment mismatch"):
        conn.execute(
            "INSERT INTO research_snapshot_headers VALUES (?,?,?,?,?,?)",
            ("research:bad", "research:bad", "{}", "0" * 64, T1, T1),
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
