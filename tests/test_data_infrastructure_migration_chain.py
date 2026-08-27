"""Active squashed migration-chain invariants."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from alembic import command
from execution.evaluate_deletion_catalog import Catalog

ROOT = Path(__file__).resolve().parents[1]
HEAD = "0029_retire_podcast_prototype"
RETAINED_TABLES = {
    "archive_generations",
    "ask_exchange_artifacts",
    "ask_exchanges",
    "ask_grounding_traces",
    "ask_session_contexts",
    "canonical_resolution_operation_ledger",
    "document_processing_operation_ledger",
    "evidence_content_blobs",
    "metric_ontology_operation_ledger",
    "llm_circuit_breakers",
    "ir_approval_candidates",
    "ir_approval_decisions",
    "research_snapshot_headers",
    "search_corpus_manifests",
    "managed_ir_publications",
    "managed_ir_inventory_evidence",
    "operations_attention_findings",
    "operations_attention_lifecycle_events",
    "operations_attention_action_receipts",
    "operations_attention_repair_references",
    "position_sizing_intent_withdrawals",
    "position_sizing_intent_supersessions",
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


def test_managed_ir_publication_migration_is_reversible(tmp_path: Path) -> None:
    path = tmp_path / "managed-ir-publications.db"
    config = _config(path)
    command.upgrade(config, "0020_kpi_fact_currency")
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='managed_ir_publications'"
            ).fetchone()
            is None
        )

    command.upgrade(config, HEAD)
    with sqlite3.connect(path) as conn:
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(managed_ir_publications)")
        }
        assert {
            "attempt_id",
            "created_paths_json",
            "staging_receipt_path",
            "inventory_receipt_path",
            "publication_result_path",
            "intent_sha256",
            "payload_sha256",
        } <= columns
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD,)
        conn.execute(
            "INSERT INTO managed_ir_publications VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "attempt-0001",
                "a" * 64,
                "b" * 64,
                "[]",
                "[]",
                "[]",
                "[]",
                "data/managed_ir_publications/attempt-0001/staging_receipt.json",
                "data/managed_ir_publications/attempt-0001/inventory_receipt.json",
                "data/managed_ir_publications/attempt-0001/publication_result.json",
                "c" * 64,
                "d" * 64,
                "now",
                "committed",
                "e" * 64,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM managed_ir_publications")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE managed_ir_publications SET state='committed'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "INSERT OR REPLACE INTO managed_ir_publications VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "attempt-0001",
                    "a" * 64,
                    "b" * 64,
                    "[]",
                    "[]",
                    "[]",
                    "[]",
                    "data/managed_ir_publications/attempt-0001/staging_receipt.json",
                    "data/managed_ir_publications/attempt-0001/inventory_receipt.json",
                    "data/managed_ir_publications/attempt-0001/publication_result.json",
                    "c" * 64,
                    "d" * 64,
                    "now",
                    "committed",
                    "e" * 64,
                ),
            )
        conn.execute(
            "INSERT INTO managed_ir_inventory_evidence VALUES (?,?,?,?,?,?)",
            (
                "attempt-0001",
                "e" * 64,
                "data/managed_ir_publications/attempt-0001/inventory_receipt.json",
                "f" * 64,
                "d" * 64,
                "a" * 64,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "INSERT OR REPLACE INTO managed_ir_inventory_evidence VALUES (?,?,?,?,?,?)",
                (
                    "attempt-0001",
                    "e" * 64,
                    "data/managed_ir_publications/attempt-0001/inventory_receipt.json",
                    "f" * 64,
                    "d" * 64,
                    "a" * 64,
                ),
            )

    command.downgrade(config, "0020_kpi_fact_currency")
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='managed_ir_publications'"
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='managed_ir_inventory_evidence'"
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'managed_ir_publications_%'"
            ).fetchone()
            is None
        )
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0020_kpi_fact_currency",
        )


def test_schedule_class_migration_preserves_research_storage_and_derives_downgrade_tiers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "schedule-class.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE alembic_version (version_num VARCHAR(64) NOT NULL);
            INSERT INTO alembic_version VALUES ('0027_add_sizing_intent_supersessions');
            CREATE TABLE tracked_companies (
                ticker TEXT, list_type TEXT, last_built_at TEXT, archived_at TEXT,
                processing_tier TEXT DEFAULT 'P3'
            );
            INSERT INTO tracked_companies VALUES
                ('A', 'portfolio', NULL, NULL, 'P1'),
                ('B', 'watchlist', NULL, NULL, 'P2'),
                ('C', 'evaluation', NULL, NULL, 'P2'),
                ('D', 'index_member', NULL, NULL, 'P3'),
                ('E', 'none', NULL, NULL, 'P3');
            CREATE TABLE research_tasks (id INTEGER, cost_usd FLOAT, run_id TEXT);
            INSERT INTO research_tasks VALUES (1, 12.5, '{"run":"r1"}');
            CREATE INDEX idx_tracked_processing_tier
                ON tracked_companies(processing_tier,last_built_at);
            CREATE INDEX ix_tracked_companies_processing_tier
                ON tracked_companies(processing_tier);
            """
        )

    config = _config(path)
    command.upgrade(config, "head")
    with sqlite3.connect(path) as conn:
        tracked_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(tracked_companies)")
        }
        research_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(research_tasks)")
        }
        assert "processing_tier" not in tracked_columns
        assert {"cost_usd", "run_id"} <= research_columns
        assert conn.execute("SELECT cost_usd, run_id FROM research_tasks").fetchone() == (
            12.5,
            '{"run":"r1"}',
        )

    command.downgrade(config, "0027_add_sizing_intent_supersessions")
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT ticker, processing_tier FROM tracked_companies ORDER BY ticker"
        ).fetchall() == [
            ("A", "P1"),
            ("B", "P2"),
            ("C", "P2"),
            ("D", "P3"),
            ("E", "P3"),
        ]
