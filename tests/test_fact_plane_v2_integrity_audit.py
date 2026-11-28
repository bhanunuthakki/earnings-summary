"""Read-only integrity coverage for the hardened v2 fact/search plane."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Generator
from pathlib import Path

import pytest

from provenance.integrity_audit import AuditOptions, audit_connection

T0 = "2026-07-27T12:00:00"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def conn(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> Generator[sqlite3.Connection, None, None]:
    path = tmp_path / "fact-plane-v2-audit.db"
    migrated_db(path)
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
