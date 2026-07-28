from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
BASE_REVISION = "0213_decision_draft_provider_id"
REVISION = "0240_fact_plane_v2_hardening"
PARENT = "0239_structured_fact_search_projection"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _base_database(path: Path) -> Config:
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
    config = _config(path)
    command.stamp(config, BASE_REVISION)
    return config


def test_hardening_migration_installs_one_linear_investor_grade_boundary(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fact-hardening.db"
    config = _base_database(path)
    command.upgrade(config, REVISION)

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (REVISION,)
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {
            "fact_dimensions_normalized_v2",
            "fact_cell_identity_seals_v2",
            "fact_reported_observation_anchors_v2",
            "fact_observation_payload_commitments_v2",
            "fact_derivation_basis_commitments_v2",
            "fact_extraction_run_completeness_seals_v2",
        } <= tables
        views = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'")
        }
        assert {
            "v_fact_cells_hardened_v2",
            "v_fact_reported_anchors_selected_v2",
            "v_fact_observations_committed_v2",
            "v_fact_extraction_runs_complete_v2",
        } <= views
        triggers = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        }
        assert {
            "trg_fact_reported_anchors_v2_exact",
            "trg_fact_observation_payload_commitments_v2_exact",
            "trg_fact_resolution_candidates_v2_payload_commitment",
            "trg_fact_derivation_basis_v2_exact",
            "trg_fact_extraction_seals_v2_complete",
            "trg_search_fact_membership_hardened_v2",
        } <= triggers
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()

    command.downgrade(config, PARENT)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (PARENT,)
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' "
            "AND name = 'fact_cell_identity_seals_v2'"
        ).fetchone() == (0,)
    finally:
        conn.close()


def test_hardening_migration_refuses_to_reinterpret_existing_v2_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fact-hardening-nonempty.db"
    config = _base_database(path)
    command.upgrade(config, PARENT)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO fact_cells_v2 ("
            "fact_cell_id,idempotency_key,reporting_entity_id,"
            "semantic_key_version,semantic_key_sha256,concept_namespace,"
            "concept_name,taxonomy_name,accounting_basis,"
            "consolidation_scope,period_kind,period_end,"
            "canonical_dimensions_json,canonical_dimensions_sha256,"
            "unit_key,effective_at,knowledge_at,recorded_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "existing-cell",
                "existing-cell-key",
                "missing-with-fk-disabled",
                "fact_cell_semantic_key.v2",
                "a" * 64,
                "us-gaap",
                "Revenue",
                "US GAAP",
                "us_gaap",
                "consolidated",
                "instant",
                "2026-01-01",
                "[]",
                "b" * 64,
                "USD",
                "2026-01-01",
                "2026-01-01",
                "2026-01-01",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(
        RuntimeError,
        match="refuses to reinterpret existing v2 evidence",
    ):
        command.upgrade(config, REVISION)

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (PARENT,)
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' "
            "AND name = 'fact_cell_identity_seals_v2'"
        ).fetchone() == (0,)
    finally:
        conn.close()
