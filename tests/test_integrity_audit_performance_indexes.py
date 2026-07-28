"""Operational integrity audits must use sparse sentinel indexes."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0227_issuer_reporting_registry"
HEAD = "0228_integrity_audit_sentinel_indexes"
INDEX = "ix_reported_observations_invalid_value_clock"
MEMBERSHIP_INDEX = "ix_observation_resolution_selected_membership_audit"
CHAIN_INDEX = "ix_observation_resolution_chain_audit"
OUTCOME_STATUS_INDEX = "ix_fact_resolution_outcomes_status_resolution"
OBSERVATION_EVIDENCE_INDEX = "ix_reported_observations_evidence_node_observation"

INVALID_VALUE_OR_CLOCK = (
    "(numeric_value IS NULL AND text_value IS NULL) "
    "OR (numeric_value IS NOT NULL AND text_value IS NOT NULL) "
    "OR (numeric_value IS NOT NULL AND unit IS NULL) "
    "OR (numeric_value IS NOT NULL AND unit = 'currency' AND currency IS NULL) "
    "OR recorded_at < available_at"
)


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _create_prior_schema(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE reported_observations (
                observation_id TEXT PRIMARY KEY,
                numeric_value TEXT,
                text_value TEXT,
                unit TEXT,
                currency TEXT,
                available_at DATETIME NOT NULL,
                recorded_at DATETIME NOT NULL,
                evidence_node_id TEXT
            )
            """
        )
        conn.executescript(
            """
            CREATE TABLE observation_resolution_revisions (
                resolution_id TEXT PRIMARY KEY,
                selected_observation_id TEXT NOT NULL,
                logical_key TEXT NOT NULL,
                revision INTEGER NOT NULL,
                supersedes_resolution_id TEXT
            );
            CREATE TABLE observation_resolution_candidates (
                resolution_id TEXT NOT NULL,
                observation_id TEXT NOT NULL,
                PRIMARY KEY (resolution_id, observation_id)
            );
            CREATE TABLE fact_resolution_outcomes (
                resolution_id TEXT PRIMARY KEY,
                resolution_status TEXT NOT NULL
            );
            INSERT INTO observation_resolution_candidates VALUES ('resolution-1', 'valid');
            INSERT INTO observation_resolution_revisions
                (resolution_id, selected_observation_id, logical_key, revision, supersedes_resolution_id)
            VALUES ('resolution-1', 'valid', 'logical-1', 1, NULL);
            INSERT INTO fact_resolution_outcomes VALUES ('resolution-1', 'resolved');
            """
        )
        conn.executemany(
            "INSERT INTO reported_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    "valid",
                    "1",
                    None,
                    "count",
                    None,
                    "2026-01-01",
                    "2026-01-02",
                    "node-1",
                ),
                (
                    "missing-unit",
                    "1",
                    None,
                    None,
                    None,
                    "2026-01-01",
                    "2026-01-02",
                    "node-1",
                ),
                (
                    "bad-clock",
                    None,
                    "reported",
                    None,
                    None,
                    "2026-01-02",
                    "2026-01-01",
                    "node-2",
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_upgrade_adds_sparse_invalid_observation_sentinel_index(tmp_path: Path) -> None:
    path = tmp_path / "audit-sentinel-index.db"
    _create_prior_schema(path)
    config = _config(path)
    command.stamp(config, PRIOR_HEAD)
    command.upgrade(config, HEAD)

    conn = sqlite3.connect(path)
    try:
        index_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (INDEX,),
        ).fetchone()
        assert index_sql is not None
        assert " WHERE " in str(index_sql[0]).upper()

        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT observation_id FROM reported_observations "
            f"WHERE {INVALID_VALUE_OR_CLOCK} ORDER BY observation_id"
        ).fetchall()
        assert any(INDEX in str(row[3]) for row in plan)

        invalid_ids = {
            str(row[0])
            for row in conn.execute(
                f"SELECT observation_id FROM reported_observations WHERE {INVALID_VALUE_OR_CLOCK}"
            )
        }
        assert invalid_ids == {"bad-clock", "missing-unit"}

        membership_plan = conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT revision.resolution_id "
            "FROM observation_resolution_revisions AS revision "
            "LEFT JOIN observation_resolution_candidates AS candidate "
            "ON candidate.resolution_id = revision.resolution_id "
            "AND candidate.observation_id = revision.selected_observation_id "
            "WHERE candidate.observation_id IS NULL "
            "ORDER BY revision.resolution_id"
        ).fetchall()
        assert any(
            MEMBERSHIP_INDEX in str(row[3]) and "COVERING" in str(row[3]).upper()
            for row in membership_plan
        )

        chain_plan = conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT revision.resolution_id "
            "FROM observation_resolution_revisions AS revision "
            "LEFT JOIN observation_resolution_revisions AS prior "
            "ON prior.resolution_id = revision.supersedes_resolution_id "
            "AND prior.logical_key = revision.logical_key "
            "AND prior.revision = revision.revision - 1 "
            "WHERE (revision.revision = 1 AND revision.supersedes_resolution_id IS NOT NULL) "
            "OR (revision.revision > 1 AND prior.resolution_id IS NULL) "
            "ORDER BY revision.resolution_id"
        ).fetchall()
        assert any(
            CHAIN_INDEX in str(row[3]) and "COVERING" in str(row[3]).upper() for row in chain_plan
        )
        assert [
            str(row[2]) for row in conn.execute(f"PRAGMA index_info({OUTCOME_STATUS_INDEX})")
        ] == ["resolution_status", "resolution_id"]
        assert [
            str(row[2]) for row in conn.execute(f"PRAGMA index_info({OBSERVATION_EVIDENCE_INDEX})")
        ] == ["evidence_node_id", "observation_id"]
    finally:
        conn.close()

    command.downgrade(config, PRIOR_HEAD)
    conn = sqlite3.connect(path)
    try:
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
                (INDEX,),
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name IN (?, ?, ?, ?)",
                (
                    MEMBERSHIP_INDEX,
                    CHAIN_INDEX,
                    OUTCOME_STATUS_INDEX,
                    OBSERVATION_EVIDENCE_INDEX,
                ),
            ).fetchone()
            is None
        )
    finally:
        conn.close()
