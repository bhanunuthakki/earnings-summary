from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta

from provenance.population_completeness import PopulationTemporalScope
from provenance.population_identity import (
    PopulationIdentityRequest,
    populate_recorded_subject_bindings,
    verify_identity_scope,
)

K = datetime(2026, 7, 29, 12, tzinfo=UTC)
OBSERVED = K + timedelta(hours=2)


def _sha(value: object) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.create_function("fact_sha256", 1, _sha, deterministic=True)
    conn.executescript(
        """
        CREATE TABLE evidence_source_observations (
            observation_id TEXT PRIMARY KEY,
            observed_at TEXT NOT NULL,
            retrieved_at TEXT NOT NULL
        );
        CREATE TABLE evidence_document_versions (
            document_version_id TEXT PRIMARY KEY,
            observation_id TEXT NOT NULL,
            issuer_id TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE issuer_entities (
            issuer_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        );
        CREATE TABLE reporting_entities (
            reporting_entity_id TEXT PRIMARY KEY,
            issuer_id TEXT NOT NULL,
            reporting_entity_kind TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE legacy_issuer_binding_revisions (
            binding_revision_id TEXT PRIMARY KEY,
            recorded_issuer_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            issuer_id TEXT,
            outcome TEXT NOT NULL,
            knowledge_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE recorded_subject_binding_revisions (
            binding_revision_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL,
            recorded_issuer_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            issuer_id TEXT,
            reporting_entity_id TEXT,
            security_id TEXT,
            outcome TEXT NOT NULL,
            decision_kind TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            reason_details_json TEXT NOT NULL,
            material_dissent INTEGER NOT NULL,
            effective_at TEXT NOT NULL,
            knowledge_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            supersedes_binding_revision_id TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO evidence_source_observations VALUES (?,?,?)",
        ("source-1", (K - timedelta(days=1)).isoformat(), OBSERVED.isoformat()),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions VALUES (?,?,?,?)",
        ("document-1", "source-1", "recorded-issuer", OBSERVED.isoformat()),
    )
    conn.execute(
        "INSERT INTO issuer_entities VALUES (?,?)",
        ("canonical-issuer", (K - timedelta(days=2)).isoformat()),
    )
    conn.execute(
        "INSERT INTO reporting_entities VALUES (?,?,?,?)",
        (
            "registrant-1",
            "canonical-issuer",
            "legal_registrant",
            (K - timedelta(days=2)).isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO legacy_issuer_binding_revisions VALUES (?,?,?,?,?,?,?)",
        (
            "legacy-1",
            "recorded-issuer",
            1,
            "canonical-issuer",
            "selected",
            K.isoformat(),
            OBSERVED.isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO recorded_subject_binding_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "subject-1",
            "subject-1",
            "recorded-issuer",
            1,
            "canonical-issuer",
            "registrant-1",
            None,
            "selected",
            "deterministic",
            "test",
            "{}",
            0,
            K.isoformat(),
            K.isoformat(),
            OBSERVED.isoformat(),
            None,
        ),
    )
    return conn


def test_identity_population_ranks_authority_at_explicit_k_o() -> None:
    conn = _connection()
    conn.execute(
        "INSERT INTO legacy_issuer_binding_revisions VALUES (?,?,?,?,?,?,?)",
        (
            "legacy-2",
            "recorded-issuer",
            2,
            None,
            "retired",
            K.isoformat(),
            (OBSERVED + timedelta(hours=1)).isoformat(),
        ),
    )

    result = populate_recorded_subject_bindings(
        conn,
        PopulationIdentityRequest(
            knowledge_cutoff=K,
            operation_recorded_at=OBSERVED,
        ),
    )

    assert result.selected_count == 1
    assert result.items[0].reporting_entity_id == "registrant-1"


def test_identity_verifier_ignores_post_o_revision_but_commits_actual_clocks() -> None:
    conn = _connection()
    conn.execute(
        "INSERT INTO recorded_subject_binding_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "subject-2",
            "subject-2",
            "recorded-issuer",
            2,
            None,
            None,
            None,
            "retired",
            "deterministic",
            "later",
            "{}",
            0,
            K.isoformat(),
            K.isoformat(),
            (OBSERVED + timedelta(hours=1)).isoformat(),
            "subject-1",
        ),
    )

    verification = verify_identity_scope(
        conn,
        PopulationTemporalScope(knowledge_cutoff=K, observed_through=OBSERVED),
    )

    assert verification.materialized_count == 1
    assert verification.failed_count == 0
    assert verification.artifact_sets[0].row_count == 1
