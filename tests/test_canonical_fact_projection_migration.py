from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
BASE_REVISION = "0213_decision_draft_provider_id"
HEAD = "0247_bounded_canonical_retrieval"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("version_locations", str(ROOT / "alembic" / "versions_archived"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def test_0247_is_reversible_single_head_with_exact_schema(tmp_path: Path) -> None:
    path = tmp_path / "bounded-canonical-retrieval.db"
    config = _config(path)
    script = ScriptDirectory.from_config(config)
    assert script.get_revision(HEAD).down_revision == ("0246_source_fact_publication_stream")
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
            "canonical_fact_projection_generations",
            "canonical_fact_projection_entries",
            "canonical_fact_projection_batches",
            "canonical_fact_projection_buckets",
            "canonical_fact_projection_seals",
            "canonical_fact_projection_audit_receipts",
            "research_snapshot_admission_receipts",
            "heterogeneous_retrieval_trace_headers",
            "heterogeneous_retrieval_trace_candidates",
            "heterogeneous_retrieval_trace_results",
            "heterogeneous_retrieval_trace_seals",
        } <= tables
        seal_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(canonical_fact_projection_seals)")
        }
        assert {
            "change_count",
            "upsert_count",
            "tombstone_count",
            "effective_entry_count",
            "stored_bucket_count",
            "logical_bucket_count",
            "projection_seal_sha256",
        } <= seal_columns
        entry_foreign_keys = {
            str(row[2])
            for row in conn.execute("PRAGMA foreign_key_list(canonical_fact_projection_entries)")
        }
        assert {
            "canonical_fact_resolution_revisions",
            "fact_observations_v2",
            "fact_cells_v2",
            "source_fact_publications",
            "source_fact_publication_seals",
            "source_fact_publication_members",
            "fact_cell_canonical_binding_revisions",
            "metric_mapping_revisions",
            "canonical_metric_definition_revisions",
            "evidence_document_versions",
            "evidence_nodes",
        } <= entry_foreign_keys
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()

    command.downgrade(config, "0246_source_fact_publication_stream")
    conn = sqlite3.connect(path)
    try:
        tables = {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "canonical_fact_projection_generations" not in tables
        assert "heterogeneous_retrieval_trace_headers" not in tables
    finally:
        conn.close()
