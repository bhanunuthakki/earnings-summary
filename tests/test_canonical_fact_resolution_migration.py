"""0244 is a strict successor to the sealed admission and ontology layers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
BASE_REVISION = "0213_decision_draft_provider_id"
REVISION = "0244_canonical_fact_resolution"
PARENT = "0243_metric_ontology"


def test_canonical_fact_resolution_revision_has_the_exact_ontology_parent() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("version_locations", str(ROOT / "alembic" / "versions_archived"))
    script = ScriptDirectory.from_config(config)
    revision = script.get_revision("0244_canonical_fact_resolution")
    assert revision is not None
    assert revision.down_revision == "0243_metric_ontology"


def test_canonical_resolution_migration_declares_sealed_cross_cell_tables() -> None:
    source = (ROOT / "alembic/versions_archived/0244_canonical_fact_resolution.py").read_text(
        encoding="utf-8"
    )
    for table in (
        "canonical_fact_candidate_universe_revisions",
        "canonical_fact_candidate_dispositions",
        "canonical_fact_candidate_universe_seals",
        "canonical_fact_relation_set_revisions",
        "canonical_fact_relation_assertions",
        "canonical_fact_relation_set_seals",
        "canonical_fact_resolution_revisions",
        "canonical_fact_resolution_snapshot_seals",
    ):
        assert table in source
    assert "trg_canonical_fact_resolution_selected" in source
    assert "trg_canonical_fact_universe_seal_exact" in source


def test_0244_installs_and_downgrades_staged_exact_boundaries(
    tmp_path: Path,
) -> None:
    path = tmp_path / "canonical-resolution.db"
    conn = sqlite3.connect(path)
    conn.executescript(
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
    conn.commit()
    conn.close()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("version_locations", str(ROOT / "alembic" / "versions_archived"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.stamp(config, BASE_REVISION)
    command.upgrade(config, REVISION)
    conn = sqlite3.connect(path)
    try:
        tables = {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "canonical_fact_candidate_universe_seals",
            "canonical_fact_relation_set_seals",
            "canonical_fact_resolution_snapshot_members",
        } <= tables
        triggers = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        assert {
            "trg_canonical_fact_universe_final_exact",
            "trg_canonical_fact_relation_final_exact",
            "trg_canonical_fact_snapshot_exact",
        } <= triggers
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
    command.downgrade(config, PARENT)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (PARENT,)
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'canonical_fact_%resolution%'"
        ).fetchone() == (0,)
    finally:
        conn.close()
