"""Tests for Wave 2 schema retirement migration (0080)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from provenance.integrity_audit import AuditOptions, audit_connection

ROOT = Path(__file__).resolve().parents[1]
PRIOR_REV = "0079_simplify_transcript_intake_and_scan_log"
TARGET_REV = "0080_retire_unused_ask_audit_search_fact_archive_population_tables"

RETIRED_TABLES = (
    "archive_generation_registration_receipts",
    "archive_generation_table_commitments",
    "archive_generations",
    "ask_answer_audit_claim_citations",
    "ask_answer_audit_citations",
    "ask_answer_audit_claims",
    "ask_answer_audit_records",
    "ask_answer_audit_retrievals",
    "ask_answer_audit_seals",
    "ask_answer_groundings",
    "ask_retrieval_scope_promotions",
    "ask_retrieval_trace_hits",
    "ask_retrieval_trace_items",
    "ask_retrieval_traces",
    "heterogeneous_retrieval_trace_candidates",
    "heterogeneous_retrieval_trace_headers",
    "heterogeneous_retrieval_trace_results",
    "heterogeneous_retrieval_trace_seals",
    "population_cutover_audit_receipts",
    "population_cutover_receipts",
    "population_parity_receipts",
    "population_plane_receipts",
    "population_run_headers",
    "search_fact_projection_memberships",
    "search_fact_projection_rows",
    "search_fact_projection_runs",
    "search_fact_projection_seals",
)

RETIRED_VIEWS = (
    "v_archive_generations_verified",
    "v_ask_retrieval_scope_current",
    "v_population_cutover_current",
    "v_search_fact_hits_current",
    "v_search_fact_projection_current_sealed",
)


def _alembic_config(db_path: Path) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


def test_0080_drops_all_27_empty_tables_and_5_views_cleanly(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "wave2.db", target=PRIOR_REV)
    with sqlite3.connect(db_path) as conn:
        before_tables = {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert set(RETIRED_TABLES) <= before_tables

    cfg = _alembic_config(db_path)
    command.upgrade(cfg, TARGET_REV)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (TARGET_REV,)
        after_tables = {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        after_views = {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")
        }
        assert after_tables.isdisjoint(RETIRED_TABLES)
        assert after_views.isdisjoint(RETIRED_VIEWS)
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

        summary = audit_connection(conn, AuditOptions(deep_sqlite_checks=False))
        unexpected = [
            finding
            for finding in summary.findings
            if finding.code != "EMBEDDING_MODEL_NOT_PROMOTED"
        ]
        assert unexpected == []


def test_0080_refuses_to_drop_non_empty_retired_table(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "nonempty.db", target=PRIOR_REV)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO ask_retrieval_traces "
            "(trace_id,idempotency_key,question_sha256,scope_sha256,"
            "retrieval_config_sha256,outcome,reason_code,manifest_ids_json,filters_json,"
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "trace-1",
                "trace-1",
                "a" * 64,
                "b" * 64,
                "c" * 64,
                "ready",
                "ok",
                "[]",
                "{}",
                "2026-11-28T00:00:00Z",
            ),
        )
        conn.commit()

    cfg = _alembic_config(db_path)
    with pytest.raises(RuntimeError, match="refusing to drop non-empty retired tables"):
        command.upgrade(cfg, TARGET_REV)


def test_0080_downgrade_fails_closed_with_snapshot_guidance(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "downgrade.db", target=TARGET_REV)
    cfg = _alembic_config(db_path)
    with pytest.raises(RuntimeError, match="Restore a reviewed predecessor database snapshot"):
        command.downgrade(cfg, PRIOR_REV)
