from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

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
            recorded_at TEXT NOT NULL,
            material_dissent INTEGER NOT NULL CHECK(material_dissent IN (0,1))
        );
        CREATE TABLE securities (
            security_id TEXT PRIMARY KEY,
            issuer_id TEXT NOT NULL,
            created_at TEXT NOT NULL
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
        "INSERT INTO legacy_issuer_binding_revisions VALUES (?,?,?,?,?,?,?,?)",
        (
            "legacy-1",
            "recorded-issuer",
            1,
            "canonical-issuer",
            "selected",
            K.isoformat(),
            OBSERVED.isoformat(),
            0,
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
        "INSERT INTO legacy_issuer_binding_revisions VALUES (?,?,?,?,?,?,?,?)",
        (
            "legacy-2",
            "recorded-issuer",
            2,
            None,
            "retired",
            K.isoformat(),
            (OBSERVED + timedelta(hours=1)).isoformat(),
            0,
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


def test_exact_document_scope_avoids_unrelated_issuer_bindings() -> None:
    conn = _connection()
    conn.execute(
        "INSERT INTO evidence_document_versions VALUES (?,?,?,?)",
        ("unrelated-document", "source-1", "unrelated-issuer", OBSERVED.isoformat()),
    )
    conn.commit()
    request = PopulationIdentityRequest(
        apply=True,
        knowledge_cutoff=K,
        operation_recorded_at=OBSERVED,
        document_version_ids=("document-1",),
    )
    result = populate_recorded_subject_bindings(conn, request)
    assert result.expected_count == result.selected_count == 1
    assert result.unresolved_count == 0
    assert (
        conn.execute(
            "SELECT count(*) FROM recorded_subject_binding_revisions WHERE recorded_issuer_id='unrelated-issuer'"
        ).fetchone()[0]
        == 0
    )
    all_result = populate_recorded_subject_bindings(
        conn, request.model_copy(update={"apply": False, "document_version_ids": None})
    )
    assert all_result.expected_count == 2
    assert result.input_commitment_sha256 != all_result.input_commitment_sha256
    conn.close()


def test_missing_exact_document_scope_fails_before_any_identity_write() -> None:
    conn = _connection()
    conn.commit()
    before = list(conn.iterdump())
    with pytest.raises(ValueError, match="selected document unavailable"):
        populate_recorded_subject_bindings(
            conn,
            PopulationIdentityRequest(
                apply=True,
                knowledge_cutoff=K,
                operation_recorded_at=OBSERVED,
                document_version_ids=("absent",),
            ),
        )
    assert list(conn.iterdump()) == before
    conn.close()


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("dissent", ["legacy", "subject", "both"])
def test_material_dissent_cannot_be_preserved_or_laundered(apply: bool, dissent: str) -> None:
    conn = _connection()
    if dissent in {"legacy", "both"}:
        conn.execute("UPDATE legacy_issuer_binding_revisions SET material_dissent=1")
    if dissent in {"subject", "both"}:
        conn.execute("UPDATE recorded_subject_binding_revisions SET material_dissent=1")
    else:
        conn.execute("DELETE FROM recorded_subject_binding_revisions")
    before = list(conn.iterdump())

    result = populate_recorded_subject_bindings(
        conn,
        PopulationIdentityRequest(apply=apply, knowledge_cutoff=K, operation_recorded_at=OBSERVED),
    )

    assert result.selected_count == result.created_count == 0
    assert result.conflict_count == 1
    assert list(conn.iterdump()) == before
    conn.close()


def test_current_subject_must_agree_with_recorded_issuer_authority() -> None:
    conn = _connection()
    conn.execute(
        "INSERT INTO issuer_entities VALUES (?,?)",
        ("other-issuer", (K - timedelta(days=1)).isoformat()),
    )
    conn.execute(
        "INSERT INTO legacy_issuer_binding_revisions VALUES (?,?,?,?,?,?,?,?)",
        (
            "legacy-2",
            "recorded-issuer",
            2,
            "other-issuer",
            "selected",
            K.isoformat(),
            OBSERVED.isoformat(),
            0,
        ),
    )
    before = list(conn.iterdump())

    result = populate_recorded_subject_bindings(
        conn,
        PopulationIdentityRequest(apply=True, knowledge_cutoff=K, operation_recorded_at=OBSERVED),
    )

    assert result.conflict_count == 1
    assert result.selected_count == result.created_count == 0
    assert list(conn.iterdump()) == before
    conn.close()


def test_legacy_target_requires_an_issuer_present_at_cutoff() -> None:
    conn = _connection()
    conn.execute("DELETE FROM recorded_subject_binding_revisions")
    conn.execute(
        "UPDATE issuer_entities SET created_at=?",
        ((K + timedelta(hours=1)).isoformat(),),
    )
    before = list(conn.iterdump())
    result = populate_recorded_subject_bindings(
        conn,
        PopulationIdentityRequest(apply=True, knowledge_cutoff=K, operation_recorded_at=OBSERVED),
    )
    assert result.conflict_count == 1
    assert result.selected_count == result.created_count == 0
    assert list(conn.iterdump()) == before
    conn.close()


@pytest.mark.parametrize("defect", ["dissent", "wrong_issuer", "wrong_security"])
def test_identity_verifier_rejects_conflicting_selected_witnesses(defect: str) -> None:
    conn = _connection()
    scope = PopulationTemporalScope(knowledge_cutoff=K, observed_through=OBSERVED)
    clean = verify_identity_scope(conn, scope)
    if defect == "dissent":
        conn.execute("UPDATE recorded_subject_binding_revisions SET material_dissent=1")
    elif defect == "wrong_issuer":
        conn.execute("UPDATE legacy_issuer_binding_revisions SET issuer_id='wrong-issuer'")
    else:
        conn.execute(
            "INSERT INTO securities VALUES (?,?,?)",
            ("wrong-security", "wrong-issuer", K.isoformat()),
        )
        conn.execute("UPDATE recorded_subject_binding_revisions SET security_id='wrong-security'")

    rejected = verify_identity_scope(conn, scope)

    assert rejected.materialized_count == 0
    assert rejected.failed_count == 1
    assert rejected.output_commitment_sha256 != clean.output_commitment_sha256
    if defect != "wrong_issuer":
        assert rejected.artifact_sets[0].rows_sha256 != clean.artifact_sets[0].rows_sha256
    assert rejected.artifact_sets[0].row_count == 1
    conn.close()


def test_identity_input_commits_issuer_witness_even_when_selected_subject_is_same() -> None:
    conn = _connection()
    request = PopulationIdentityRequest(knowledge_cutoff=K, operation_recorded_at=OBSERVED)
    before = populate_recorded_subject_bindings(conn, request)
    before_plane = verify_identity_scope(
        conn, PopulationTemporalScope(knowledge_cutoff=K, observed_through=OBSERVED)
    )
    conn.execute(
        "INSERT INTO legacy_issuer_binding_revisions VALUES (?,?,?,?,?,?,?,?)",
        (
            "legacy-2",
            "recorded-issuer",
            2,
            "canonical-issuer",
            "selected",
            K.isoformat(),
            OBSERVED.isoformat(),
            0,
        ),
    )
    after = populate_recorded_subject_bindings(conn, request)
    after_plane = verify_identity_scope(
        conn, PopulationTemporalScope(knowledge_cutoff=K, observed_through=OBSERVED)
    )
    assert before.selected_count == after.selected_count == 1
    assert before.input_commitment_sha256 != after.input_commitment_sha256
    assert before_plane.input_commitment_sha256 != after_plane.input_commitment_sha256
    conn.close()


@pytest.mark.parametrize(
    "later_knowledge,later_recording", [(False, False), (True, False), (False, True)]
)
def test_dissent_revision_respects_both_temporal_cutoffs(
    later_knowledge: bool, later_recording: bool
) -> None:
    conn = _connection()
    request = PopulationIdentityRequest(knowledge_cutoff=K, operation_recorded_at=OBSERVED)
    before = populate_recorded_subject_bindings(conn, request)
    conn.execute(
        "INSERT INTO legacy_issuer_binding_revisions VALUES (?,?,?,?,?,?,?,?)",
        (
            "legacy-2",
            "recorded-issuer",
            2,
            "canonical-issuer",
            "selected",
            (K + timedelta(hours=int(later_knowledge))).isoformat(),
            (OBSERVED + timedelta(hours=int(later_recording))).isoformat(),
            1,
        ),
    )
    after = populate_recorded_subject_bindings(conn, request)
    verification = verify_identity_scope(
        conn, PopulationTemporalScope(knowledge_cutoff=K, observed_through=OBSERVED)
    )
    if later_knowledge or later_recording:
        assert after.selected_count == verification.materialized_count == 1
        assert after.input_commitment_sha256 == before.input_commitment_sha256
    else:
        assert after.conflict_count == verification.failed_count == 1
        assert after.selected_count == verification.materialized_count == 0
        assert after.input_commitment_sha256 != before.input_commitment_sha256
    conn.close()


@pytest.mark.parametrize("preserve_current", [False, True])
def test_canonical_recorded_id_needs_no_legacy_self_binding(preserve_current: bool) -> None:
    conn = _connection()
    conn.execute("UPDATE evidence_document_versions SET issuer_id='canonical-issuer'")
    conn.execute("DELETE FROM legacy_issuer_binding_revisions")
    if preserve_current:
        conn.execute(
            "UPDATE recorded_subject_binding_revisions SET recorded_issuer_id='canonical-issuer'"
        )
    else:
        conn.execute("DELETE FROM recorded_subject_binding_revisions")
    request = PopulationIdentityRequest(
        apply=True, knowledge_cutoff=K, operation_recorded_at=OBSERVED
    )
    result = populate_recorded_subject_bindings(conn, request)
    replay = populate_recorded_subject_bindings(conn, request)
    assert result.selected_count == replay.selected_count == 1
    assert result.created_count == int(not preserve_current)
    assert replay.created_count == 0
    assert conn.execute("SELECT count(*) FROM legacy_issuer_binding_revisions").fetchone()[0] == 0
    conn.close()


def test_valid_explicit_entity_and_security_preserved_without_new_registrant_selection() -> None:
    conn = _connection()
    conn.execute(
        "INSERT INTO reporting_entities VALUES (?,?,?,?)",
        ("other-entity", "canonical-issuer", "other", K.isoformat()),
    )
    conn.execute(
        "INSERT INTO securities VALUES (?,?,?)",
        ("security-1", "canonical-issuer", K.isoformat()),
    )
    conn.execute(
        "UPDATE recorded_subject_binding_revisions SET reporting_entity_id='other-entity',security_id='security-1'"
    )
    before = list(conn.iterdump())
    result = populate_recorded_subject_bindings(
        conn,
        PopulationIdentityRequest(apply=True, knowledge_cutoff=K, operation_recorded_at=OBSERVED),
    )
    verification = verify_identity_scope(
        conn, PopulationTemporalScope(knowledge_cutoff=K, observed_through=OBSERVED)
    )
    assert result.selected_count == verification.materialized_count == 1
    assert result.items[0].reporting_entity_id == "other-entity"
    assert result.created_count == 0
    assert list(conn.iterdump()) == before
    conn.close()


@pytest.mark.parametrize("witness", ["missing", "unresolved", "retired"])
def test_existing_subject_requires_current_selected_issuer_witness(witness: str) -> None:
    conn = _connection()
    if witness == "missing":
        conn.execute("DELETE FROM legacy_issuer_binding_revisions")
    else:
        conn.execute(
            "INSERT INTO legacy_issuer_binding_revisions VALUES (?,?,?,?,?,?,?,?)",
            (
                "legacy-2",
                "recorded-issuer",
                2,
                None,
                witness,
                K.isoformat(),
                OBSERVED.isoformat(),
                0,
            ),
        )
    before = list(conn.iterdump())
    result = populate_recorded_subject_bindings(
        conn,
        PopulationIdentityRequest(apply=True, knowledge_cutoff=K, operation_recorded_at=OBSERVED),
    )
    assert result.conflict_count == 1
    assert result.selected_count == result.created_count == 0
    assert list(conn.iterdump()) == before
    conn.close()


@pytest.mark.parametrize("apply", [False, True])
def test_unresolved_subject_dissent_cannot_be_replaced_with_clean_selection(apply: bool) -> None:
    conn = _connection()
    conn.execute(
        "UPDATE recorded_subject_binding_revisions SET outcome='unresolved',issuer_id=NULL,"
        "reporting_entity_id=NULL,material_dissent=1"
    )
    before = list(conn.iterdump())
    result = populate_recorded_subject_bindings(
        conn,
        PopulationIdentityRequest(apply=apply, knowledge_cutoff=K, operation_recorded_at=OBSERVED),
    )
    assert result.conflict_count == 1
    assert result.selected_count == result.created_count == 0
    assert list(conn.iterdump()) == before
    conn.close()


@pytest.mark.parametrize("target", ["missing", "multiple"])
def test_new_subject_requires_exactly_one_legal_registrant(target: str) -> None:
    conn = _connection()
    conn.execute("DELETE FROM recorded_subject_binding_revisions")
    if target == "missing":
        conn.execute("UPDATE reporting_entities SET reporting_entity_kind='other'")
    else:
        conn.execute(
            "INSERT INTO reporting_entities VALUES (?,?,?,?)",
            ("registrant-2", "canonical-issuer", "legal_registrant", K.isoformat()),
        )
    request = PopulationIdentityRequest(
        apply=True, knowledge_cutoff=K, operation_recorded_at=OBSERVED
    )
    result = populate_recorded_subject_bindings(conn, request)
    replay = populate_recorded_subject_bindings(conn, request)
    assert result.unresolved_count == replay.unresolved_count == 1
    assert result.selected_count == 0
    assert result.created_count == 1
    assert replay.created_count == 0
    conn.close()


def test_cannot_append_before_an_already_recorded_future_subject() -> None:
    conn = _connection()
    future = list(conn.execute("SELECT * FROM recorded_subject_binding_revisions").fetchone())
    conn.execute("DELETE FROM recorded_subject_binding_revisions")
    future[13] = (K + timedelta(hours=1)).isoformat()
    future[14] = (OBSERVED + timedelta(hours=1)).isoformat()
    conn.execute(
        "INSERT INTO recorded_subject_binding_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        future,
    )
    before = list(conn.iterdump())
    with pytest.raises(ValueError, match="historical subject binding"):
        populate_recorded_subject_bindings(
            conn,
            PopulationIdentityRequest(
                apply=True, knowledge_cutoff=K, operation_recorded_at=OBSERVED
            ),
        )
    assert list(conn.iterdump()) == before
    conn.close()


@pytest.mark.parametrize("plane", ["legacy", "subject"])
@pytest.mark.parametrize("boundary", ["knowledge", "recorded", "both"])
@pytest.mark.parametrize("microseconds", [1, 500_000])
def test_fractional_future_clean_revision_cannot_mask_current_dissent(
    plane: str, boundary: str, microseconds: int
) -> None:
    conn = _connection()
    table = (
        "legacy_issuer_binding_revisions"
        if plane == "legacy"
        else "recorded_subject_binding_revisions"
    )
    conn.execute(f"UPDATE {table} SET material_dissent=1")
    request = PopulationIdentityRequest(knowledge_cutoff=K, operation_recorded_at=OBSERVED)
    scope = PopulationTemporalScope(knowledge_cutoff=K, observed_through=OBSERVED)
    before = populate_recorded_subject_bindings(conn, request)
    before_plane = verify_identity_scope(conn, scope)
    row = list(conn.execute(f"SELECT * FROM {table}").fetchone())
    row[0] = "future-clean"
    if plane == "legacy":
        row[2] = 2
        row[5] = (
            K + timedelta(microseconds=microseconds if boundary != "recorded" else 0)
        ).isoformat()
        row[6] = (
            OBSERVED + timedelta(microseconds=microseconds if boundary != "knowledge" else 0)
        ).isoformat()
        row[7] = 0
    else:
        row[1] = "future-clean"
        row[3] = 2
        row[11] = 0
        row[13] = (
            K + timedelta(microseconds=microseconds if boundary != "recorded" else 0)
        ).isoformat()
        row[14] = (
            OBSERVED + timedelta(microseconds=microseconds if boundary != "knowledge" else 0)
        ).isoformat()
        row[15] = "subject-1"
    conn.execute(f"INSERT INTO {table} VALUES ({','.join('?' for _ in row)})", row)

    after = populate_recorded_subject_bindings(conn, request)
    after_plane = verify_identity_scope(conn, scope)

    assert after.conflict_count == after_plane.failed_count == 1
    assert after.selected_count == after_plane.materialized_count == 0
    assert after.input_commitment_sha256 == before.input_commitment_sha256
    assert after_plane.input_commitment_sha256 == before_plane.input_commitment_sha256
    assert after_plane.artifact_sets == before_plane.artifact_sets
    conn.close()


@pytest.mark.parametrize("clock", ["observed_at", "retrieved_at", "document_recorded_at"])
@pytest.mark.parametrize("microseconds", [1, 500_000])
def test_fractional_future_document_is_outside_identity_population(
    clock: str, microseconds: int
) -> None:
    conn = _connection()
    cutoff = K if clock == "observed_at" else OBSERVED
    value = (cutoff + timedelta(microseconds=microseconds)).isoformat()
    if clock == "document_recorded_at":
        conn.execute("UPDATE evidence_document_versions SET recorded_at=?", (value,))
    else:
        conn.execute(f"UPDATE evidence_source_observations SET {clock}=?", (value,))
    before = list(conn.iterdump())
    request = PopulationIdentityRequest(
        apply=True, knowledge_cutoff=K, operation_recorded_at=OBSERVED
    )
    result = populate_recorded_subject_bindings(conn, request)
    assert result.expected_count == result.created_count == 0
    with pytest.raises(ValueError, match="identity scope is empty"):
        verify_identity_scope(
            conn, PopulationTemporalScope(knowledge_cutoff=K, observed_through=OBSERVED)
        )
    with pytest.raises(ValueError, match="selected document unavailable"):
        populate_recorded_subject_bindings(
            conn, request.model_copy(update={"document_version_ids": ("document-1",)})
        )
    assert list(conn.iterdump()) == before
    conn.close()


@pytest.mark.parametrize("target", ["issuer", "entity", "new_entity", "security"])
@pytest.mark.parametrize("microseconds", [1, 500_000])
def test_fractional_future_identity_target_cannot_be_selected(
    target: str, microseconds: int
) -> None:
    conn = _connection()
    future = (K + timedelta(microseconds=microseconds)).isoformat()
    if target == "issuer":
        conn.execute("UPDATE issuer_entities SET created_at=?", (future,))
    elif target in {"entity", "new_entity"}:
        conn.execute("UPDATE reporting_entities SET created_at=?", (future,))
        if target == "new_entity":
            conn.execute("DELETE FROM recorded_subject_binding_revisions")
    else:
        conn.execute(
            "INSERT INTO securities VALUES (?,?,?)", ("security-1", "canonical-issuer", future)
        )
        conn.execute("UPDATE recorded_subject_binding_revisions SET security_id='security-1'")
    before = list(conn.iterdump())
    result = populate_recorded_subject_bindings(
        conn, PopulationIdentityRequest(knowledge_cutoff=K, operation_recorded_at=OBSERVED)
    )
    plane = verify_identity_scope(
        conn, PopulationTemporalScope(knowledge_cutoff=K, observed_through=OBSERVED)
    )
    assert result.selected_count == plane.materialized_count == 0
    assert result.unresolved_count == int(target == "new_entity")
    assert result.conflict_count == int(target != "new_entity")
    assert plane.failed_count == 1
    assert plane.artifact_sets[0].row_count == int(target != "new_entity")
    assert list(conn.iterdump()) == before
    conn.close()


def test_identity_cutoffs_accept_equivalent_mixed_timestamp_formats() -> None:
    conn = _connection()
    conn.execute(
        "UPDATE evidence_source_observations SET observed_at=?,retrieved_at=?",
        ("2026-07-29 12:00:00.000000", "2026-07-29T07:00:00-07:00"),
    )
    conn.execute("UPDATE evidence_document_versions SET recorded_at=?", ("2026-07-29T14:00:00Z",))
    conn.execute(
        "UPDATE legacy_issuer_binding_revisions SET knowledge_at=?,recorded_at=?",
        ("2026-07-29T14:00:00+02:00", "2026-07-29T14:00:00.000000+00:00"),
    )
    conn.execute(
        "UPDATE recorded_subject_binding_revisions SET knowledge_at=?,recorded_at=?",
        ("2026-07-29T05:00:00-07:00", "2026-07-29 14:00:00"),
    )
    request = PopulationIdentityRequest(knowledge_cutoff=K, operation_recorded_at=OBSERVED)
    result = populate_recorded_subject_bindings(conn, request)
    plane = verify_identity_scope(
        conn, PopulationTemporalScope(knowledge_cutoff=K, observed_through=OBSERVED)
    )
    assert result.selected_count == plane.materialized_count == 1
    assert result.conflict_count == plane.failed_count == 0
    assert plane.artifact_sets[0].row_count == 1
    conn.close()


def test_repeated_identity_calls_preserve_active_cursor_and_caller_transaction() -> None:
    conn = _connection()
    cursor = conn.execute(
        "SELECT observation_id FROM evidence_source_observations UNION ALL SELECT 'second' UNION ALL SELECT 'third'"
    )
    assert cursor.fetchone()[0] == "source-1"
    assert conn.in_transaction
    request = PopulationIdentityRequest(knowledge_cutoff=K, operation_recorded_at=OBSERVED)
    scope = PopulationTemporalScope(knowledge_cutoff=K, observed_through=OBSERVED)
    for _ in range(2):
        assert populate_recorded_subject_bindings(conn, request).selected_count == 1
        assert conn.in_transaction
    assert cursor.fetchall() == [("second",), ("third",)]
    for _ in range(2):
        assert verify_identity_scope(conn, scope).materialized_count == 1
        assert conn.in_transaction
    conn.rollback()
    conn.close()


def test_invalid_legacy_clock_fails_before_any_identity_write() -> None:
    conn = _connection()
    conn.execute("UPDATE legacy_issuer_binding_revisions SET knowledge_at='invalid-clock'")
    before = list(conn.iterdump())
    with pytest.raises(ValueError):
        populate_recorded_subject_bindings(
            conn,
            PopulationIdentityRequest(
                apply=True, knowledge_cutoff=K, operation_recorded_at=OBSERVED
            ),
        )
    with pytest.raises(ValueError):
        verify_identity_scope(
            conn, PopulationTemporalScope(knowledge_cutoff=K, observed_through=OBSERVED)
        )
    assert list(conn.iterdump()) == before
    conn.close()
