"""Read-only integrity coverage for the hardened v2 fact/search plane."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from provenance.integrity_audit import AuditOptions, audit_connection

ROOT = Path(__file__).resolve().parents[1]
BASE_REVISION = "0213_decision_draft_provider_id"
T0 = "2026-07-27T12:00:00"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


@pytest.fixture
def conn(tmp_path: Path) -> Generator[sqlite3.Connection, None, None]:
    path = tmp_path / "fact-plane-v2-audit.db"
    database = sqlite3.connect(path)
    database.executescript(
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
    database.commit()
    database.close()
    config = _config(path)
    command.stamp(config, BASE_REVISION)
    command.upgrade(config, "head")
    upgraded = sqlite3.connect(path)
    try:
        yield upgraded
    finally:
        upgraded.close()


def _fact_codes(conn: sqlite3.Connection) -> set[str]:
    summary = audit_connection(
        conn,
        AuditOptions(deep_sqlite_checks=False),
    )
    return {
        finding.code
        for finding in summary.findings
        if finding.code.startswith(("FACT_PLANE_V2_", "FACT_SEARCH_V2_"))
    }


def test_clean_empty_hardened_plane_has_no_v2_integrity_findings(
    conn: sqlite3.Connection,
) -> None:
    assert _fact_codes(conn) == set()


def test_partial_hardening_schema_is_a_blocker(
    conn: sqlite3.Connection,
) -> None:
    conn.execute("DROP TABLE fact_derivation_basis_commitments_v2")

    codes = _fact_codes(conn)

    assert "FACT_PLANE_V2_HARDENING_SCHEMA_PARTIAL" in codes


def test_corrupt_observation_commitment_is_recomputed_and_detected(
    conn: sqlite3.Connection,
) -> None:
    conn.execute("DROP TRIGGER trg_fact_observation_payload_commitments_v2_exact")
    conn.execute(
        "INSERT INTO fact_observation_payload_commitments_v2 "
        "(observation_id,idempotency_key,payload_version,"
        "canonical_payload_json,observation_payload_sha256,committed_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            "ghost-observation",
            "ghost-observation:payload",
            "fact_observation_payload.v1",
            "{}",
            "a" * 64,
            T0,
        ),
    )

    codes = _fact_codes(conn)

    assert "FACT_PLANE_V2_OBSERVATION_PAYLOAD_DIGEST_MISMATCH" in codes
    assert "FACT_PLANE_V2_OBSERVATION_PAYLOAD_MISMATCH" in codes
    assert "FACT_PLANE_V2_HARDENING_TRIGGER_MISSING" in codes


def test_corrupt_fact_projection_commitments_are_detected(
    conn: sqlite3.Connection,
) -> None:
    for trigger in (
        "trg_search_fact_projection_runs_manifest",
        "trg_search_fact_projection_memberships_scope",
        "trg_search_fact_projection_seals_contract",
        "trg_search_fact_projection_seals_counts",
        "trg_search_fact_projection_seals_coverage",
    ):
        conn.execute(f"DROP TRIGGER {trigger}")
    config_sha = _sha("projection-config")
    conn.execute(
        "INSERT INTO search_fact_projection_runs "
        "(projection_run_id,idempotency_key,projection_key,revision,"
        "manifest_id,knowledge_cutoff,config_sha256,code_version,"
        "supersedes_projection_run_id,recorded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "projection-corrupt",
            "projection-corrupt:key",
            "projection-corrupt",
            1,
            "missing-manifest",
            T0,
            config_sha,
            "test-v1",
            None,
            T0,
        ),
    )
    conn.execute(
        "INSERT INTO search_fact_projection_memberships "
        "(membership_id,projection_run_id,fact_cell_id,disposition,"
        "resolution_revision_id,reason_code,reason_details_json,"
        "membership_bundle_sha256,recorded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "membership-corrupt",
            "projection-corrupt",
            "missing-cell",
            "quarantined",
            None,
            "test_corruption",
            "{}",
            "b" * 64,
            T0,
        ),
    )
    conn.execute(
        "INSERT INTO search_fact_projection_seals "
        "(projection_seal_id,idempotency_key,projection_run_id,manifest_id,"
        "eligible_fact_cell_count,membership_count,included_count,"
        "unresolved_material_count,missing_provenance_count,quarantined_count,"
        "row_count,membership_set_sha256,row_set_sha256,config_sha256,sealed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "seal-corrupt",
            "seal-corrupt:key",
            "projection-corrupt",
            "missing-manifest",
            0,
            1,
            0,
            0,
            0,
            1,
            0,
            "c" * 64,
            "d" * 64,
            config_sha,
            T0,
        ),
    )

    codes = _fact_codes(conn)

    assert "FACT_SEARCH_V2_MEMBERSHIP_BUNDLE_MISMATCH" in codes
    assert "FACT_SEARCH_V2_PROJECTION_SEAL_MISMATCH" in codes
    assert "FACT_SEARCH_V2_RUN_CORPUS_MISMATCH" in codes
