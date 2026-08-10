"""Active squashed migration-chain invariants."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from alembic import command
from execution.evaluate_deletion_catalog import Catalog

ROOT = Path(__file__).resolve().parents[1]
HEAD = "0006_add_ask_proposal_approval"
RETAINED_TABLES = {
    "archive_generations",
    "ask_exchange_artifacts",
    "ask_exchanges",
    "ask_session_contexts",
    "canonical_resolution_operation_ledger",
    "document_processing_operation_ledger",
    "evidence_content_blobs",
    "metric_ontology_operation_ledger",
    "llm_circuit_breakers",
    "research_snapshot_headers",
    "search_corpus_manifests",
}


def _cataloged_deleted_tables() -> set[str]:
    catalog = Catalog.model_validate_json(
        (ROOT / "docs" / "design" / "deletion_catalog_2026_08.json").read_text(encoding="utf-8")
    )
    return {target for candidate in catalog.candidates for target in candidate.schema_targets}


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def test_squashed_chain_has_one_head_and_preserves_active_schema(tmp_path: Path) -> None:
    path = tmp_path / "migration-chain.db"
    config = _config(path)
    assert ScriptDirectory.from_config(config).get_heads() == [HEAD]

    command.upgrade(config, "head")

    with sqlite3.connect(path) as conn:
        revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        tables = {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        identity = conn.execute(
            "SELECT database_instance_id FROM database_runtime_identity WHERE singleton=1"
        ).fetchone()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()

    assert revision == (HEAD,)
    assert tables >= RETAINED_TABLES
    assert tables.isdisjoint(_cataloged_deleted_tables())
    assert identity is not None and str(identity[0]).startswith("database-instance:")
    assert integrity == ("ok",)
    assert foreign_keys == []
