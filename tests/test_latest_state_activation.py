from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

import provenance.latest_state_activation as activation_module
from execution.audit_latest_state_candidate import receipt_destination
from provenance.immutable_artifact import (
    ImmutableArtifactConflictError,
    path_aliases_any,
    publish_text_no_clobber,
)
from provenance.latest_governed_state import (
    LatestGovernedRefreshRequest,
    LatestGovernedRefreshResult,
)
from provenance.latest_state_activation import (
    LatestStateActivationError,
    audit_candidate_coverage,
    audit_governed_candidate,
    bind_scope_eligibility_manifest,
    build_scope_eligibility_manifest,
    candidate_file_identity,
    read_candidate_artifact,
    verify_bound_eligibility_manifest,
    verify_candidate_coverage_receipt,
)


def _candidate(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY);
            INSERT INTO alembic_version VALUES ('0261_latest_governed_state');
            CREATE TABLE source_fact_publications (publication_id TEXT PRIMARY KEY);
            INSERT INTO source_fact_publications VALUES ('publication-1');
            CREATE TABLE source_fact_publication_members (
              publication_id TEXT NOT NULL,
              ordinal INTEGER NOT NULL,
              PRIMARY KEY (publication_id, ordinal)
            );
            INSERT INTO source_fact_publication_members VALUES ('publication-1', 0);
            CREATE TABLE latest_governed_scope_heads (scope_key TEXT PRIMARY KEY);
            CREATE VIRTUAL TABLE latest_governed_narrative_fts USING fts5(text);
            """
        )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seal(path: Path, database: Path) -> None:
    payload = {
        "canonical_bindings": 0,
        "database": str(database.resolve()),
        "foreign_key_violations": 0,
        "quick_check": "ok",
        "revision": ["0261_latest_governed_state"],
        "sha256": _sha256(database),
        "size_bytes": database.stat().st_size,
        "source_taxonomy_components": 0,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_candidate_audit_is_hash_bound_counted_and_sidecar_safe(tmp_path: Path) -> None:
    database = tmp_path / "candidate.db"
    _candidate(database)
    seal = tmp_path / "candidate-seal.json"
    _seal(seal, database)
    expected_sha256 = _sha256(database)

    report = audit_governed_candidate(
        database,
        seal_path=seal,
        expected_revision="0261_latest_governed_state",
    )

    assert report.database_sha256 == expected_sha256
    assert report.seal_path == str(seal.resolve())
    assert report.seal_sha256 == _sha256(seal)
    assert report.alembic_revision == "0261_latest_governed_state"
    assert report.quick_check == "ok"
    assert report.integrity_check == "ok"
    assert report.foreign_key_violation_count == 0
    assert len(report.schema_fingerprint_sha256) == 64
    assert report.database_identity_before == report.database_identity_after
    assert report.schema_version == "latest-governed-activation-candidate/v2"
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()


def test_candidate_audit_refuses_hash_or_revision_drift(tmp_path: Path) -> None:
    database = tmp_path / "candidate.db"
    _candidate(database)
    seal = tmp_path / "candidate-seal.json"
    _seal(seal, database)

    with pytest.raises(LatestStateActivationError, match="SHA-256"):
        payload = json.loads(seal.read_text(encoding="utf-8"))
        payload["sha256"] = "0" * 64
        seal.write_text(json.dumps(payload), encoding="utf-8")
        audit_governed_candidate(
            database,
            seal_path=seal,
            expected_revision="0261_latest_governed_state",
        )
    _seal(seal, database)
    with pytest.raises(LatestStateActivationError, match="revision"):
        audit_governed_candidate(
            database,
            seal_path=seal,
            expected_revision="0259_source_definition_identity",
        )


def test_candidate_audit_refuses_database_revision_different_from_seal(
    tmp_path: Path,
) -> None:
    database = tmp_path / "candidate.db"
    _candidate(database)
    seal = tmp_path / "candidate-seal.json"
    _seal(seal, database)
    payload = json.loads(seal.read_text(encoding="utf-8"))
    payload["revision"] = ["0259_source_definition_identity"]
    seal.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(LatestStateActivationError, match="differs from its seal"):
        audit_governed_candidate(
            database,
            seal_path=seal,
            expected_revision="0261_latest_governed_state",
        )


def test_candidate_audit_refuses_nonempty_wal_before_immutable_open(tmp_path: Path) -> None:
    database = tmp_path / "candidate.db"
    _candidate(database)
    seal = tmp_path / "candidate-seal.json"
    _seal(seal, database)
    Path(f"{database}-wal").write_bytes(b"not-checkpointed")

    with pytest.raises(LatestStateActivationError, match="non-empty WAL"):
        audit_governed_candidate(
            database,
            seal_path=seal,
            expected_revision="0261_latest_governed_state",
        )


def test_candidate_audit_refuses_wal_created_during_immutable_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    _candidate(database)
    seal = tmp_path / "candidate-seal.json"
    _seal(seal, database)
    checkpoint_calls = 0
    original_checkpoint = activation_module.require_checkpointed_sidecars

    def create_late_wal(path: Path) -> None:
        nonlocal checkpoint_calls
        checkpoint_calls += 1
        original_checkpoint(path)
        if checkpoint_calls == 1:
            Path(f"{database}-wal").write_bytes(b"late-writer")

    monkeypatch.setattr(
        activation_module,
        "require_checkpointed_sidecars",
        create_late_wal,
    )

    with pytest.raises(LatestStateActivationError, match="non-empty WAL"):
        audit_governed_candidate(
            database,
            seal_path=seal,
            expected_revision="0261_latest_governed_state",
        )


def test_candidate_coverage_binds_structural_receipt_and_all_fixed_planes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "candidate.db"
    _candidate(database)
    seal = tmp_path / "candidate-seal.json"
    _seal(seal, database)
    structural = audit_governed_candidate(
        database,
        seal_path=seal,
        expected_revision="0261_latest_governed_state",
    )
    audit_receipt = tmp_path / "candidate-audit.json"
    audit_receipt.write_text(structural.model_dump_json(), encoding="utf-8")

    coverage = audit_candidate_coverage(
        database,
        candidate_audit_receipt=audit_receipt,
    )

    counts = {item.plane_name: item.row_count for item in coverage.planes}
    assert counts["source_fact_publications"] == 1
    assert counts["source_fact_publication_seals"] is None
    assert counts["filing_xbrl_extraction_disposition_seals"] is None
    assert counts["heterogeneous_retrieval_trace_seals"] is None
    assert counts["latest_governed_narrative_fts"] == 0
    assert coverage.schema_version == "latest-governed-candidate-coverage/v2"
    assert verify_candidate_coverage_receipt(coverage)


def test_candidate_coverage_refuses_audit_receipt_change_during_census(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    _candidate(database)
    seal = tmp_path / "candidate-seal.json"
    _seal(seal, database)
    structural = audit_governed_candidate(
        database,
        seal_path=seal,
        expected_revision="0261_latest_governed_state",
    )
    audit_path = tmp_path / "candidate-audit.json"
    audit_path.write_text(structural.model_dump_json(), encoding="utf-8")

    def mutate_audit_receipt() -> None:
        audit_path.write_text(
            audit_path.read_text(encoding="utf-8") + " ",
            encoding="utf-8",
        )

    monkeypatch.setattr(
        activation_module,
        "_before_coverage_artifact_recheck",
        mutate_audit_receipt,
    )

    with pytest.raises(LatestStateActivationError, match="changed during coverage census"):
        audit_candidate_coverage(database, candidate_audit_receipt=audit_path)


@pytest.mark.parametrize("suffix", ("", "-wal", "-shm", "-journal"))
def test_receipt_destination_refuses_candidate_and_sidecar_aliases(
    tmp_path: Path,
    suffix: str,
) -> None:
    database = tmp_path / "candidate.db"
    seal = tmp_path / "candidate-seal.json"

    with pytest.raises(LatestStateActivationError, match="output aliases"):
        receipt_destination(database, seal, Path(f"{database}{suffix}"))
    with pytest.raises(LatestStateActivationError, match="output aliases"):
        receipt_destination(database, seal, seal)


def test_receipt_publication_is_no_clobber_and_exact_replay(tmp_path: Path) -> None:
    destination = tmp_path / "receipt.json"
    assert publish_text_no_clobber(destination, "first") is True
    assert publish_text_no_clobber(destination, "first") is False
    with pytest.raises(ImmutableArtifactConflictError, match="different content"):
        publish_text_no_clobber(destination, "second")
    assert destination.read_text(encoding="utf-8") == "first\n"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_protected_artifact_alias_detects_existing_hardlink(tmp_path: Path) -> None:
    protected = tmp_path / "candidate.db"
    alias = tmp_path / "receipt.json"
    protected.write_bytes(b"candidate")
    alias.hardlink_to(protected)

    assert path_aliases_any(alias, {protected})


def _eligibility_database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE v_issuer_reporting_scope_current (
          scope_revision_id TEXT, scope_key TEXT, issuer_id TEXT,
          inclusion_state TEXT
        );
        CREATE TABLE issuer_entities (issuer_id TEXT, entity_kind TEXT);
        CREATE TABLE reporting_entities (
          reporting_entity_id TEXT, issuer_id TEXT, reporting_entity_kind TEXT
        );
        CREATE TABLE v_security_listings_canonical (
          issuer_id TEXT, normalized_ticker TEXT, status TEXT
        );
        CREATE TABLE v_population_cutover_current (
          population_run_id TEXT, receipt_set_sha256 TEXT
        );
        CREATE TABLE v_ask_retrieval_scope_current (
          scope_key TEXT, promotion_id TEXT, status TEXT,
          population_receipt_set_sha256 TEXT, fact_projection_seal_sha256 TEXT,
          source_inventory_set_json TEXT, source_inventory_set_sha256 TEXT,
          narrative_bundles_json TEXT, narrative_bundles_sha256 TEXT,
          issuer_id TEXT, reporting_entity_id TEXT
        );
        INSERT INTO v_population_cutover_current VALUES ('population-1', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
        INSERT INTO v_issuer_reporting_scope_current VALUES
          ('revision-a', 'issuer:a', 'issuer-a', 'core'),
          ('revision-b', 'issuer:b', 'issuer-b', 'monitored'),
          ('revision-c', 'issuer:c', 'issuer-c', 'core');
        INSERT INTO issuer_entities VALUES
          ('issuer-a', 'operating_company'),
          ('issuer-b', 'operating_company'),
          ('issuer-c', 'operating_company');
        INSERT INTO reporting_entities VALUES
          ('reporting-a', 'issuer-a', 'legal_registrant'),
          ('reporting-b', 'issuer-b', 'legal_registrant'),
          ('reporting-c', 'issuer-c', 'legal_registrant');
        INSERT INTO v_security_listings_canonical VALUES
          ('issuer-a', 'AAA', 'listed'),
          ('issuer-b', 'BBB', 'listed'),
          ('issuer-c', 'CCC', 'listed');
        INSERT INTO v_ask_retrieval_scope_current VALUES (
          'issuer:a', 'promotion-a', 'promoted',
          'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
          'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
          '["inventory-a"]', NULL, '[]', NULL, 'issuer-a', 'reporting-a'
        );
        """
    )
    conn.execute(
        "UPDATE v_ask_retrieval_scope_current SET "
        "source_inventory_set_sha256=?,narrative_bundles_sha256=?",
        (
            hashlib.sha256(b'["inventory-a"]').hexdigest(),
            hashlib.sha256(b"[]").hexdigest(),
        ),
    )
    return conn


def _dry_run_result() -> LatestGovernedRefreshResult:
    return LatestGovernedRefreshResult(
        mode="dry_run",
        outcome="changed",
        refresh_id="refresh-a",
        head_id=None,
        created_count=0,
        replayed_count=0,
        source_event_count=0,
        fact_change_count=1,
        document_change_count=1,
        narrative_change_count=1,
        source_read_count=3,
        current_read_count=0,
        current_write_count=0,
        receipt_write_count=0,
        terminal_commitment="c" * 64,
    )


def test_scope_manifest_classifies_every_scope_and_commits_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _eligibility_database()

    def dry_run(
        connection: sqlite3.Connection,
        request: LatestGovernedRefreshRequest,
    ) -> LatestGovernedRefreshResult:
        del connection, request
        return _dry_run_result()

    monkeypatch.setattr(
        "provenance.latest_state_activation.refresh_latest_governed_state",
        dry_run,
    )

    manifest = build_scope_eligibility_manifest(
        conn,
        operation_recorded_at=datetime(2026, 7, 31, tzinfo=UTC),
    )

    assert manifest.scope_count == 3
    assert manifest.eligible_count == 1
    assert manifest.blocked_count == 1
    assert manifest.excluded_count == 1
    by_scope = {item.scope_key: item for item in manifest.scopes}
    assert by_scope["issuer:a"].status == "eligible"
    assert by_scope["issuer:a"].reason_codes == ("eligible",)
    assert by_scope["issuer:a"].promotion_id == "promotion-a"
    assert by_scope["issuer:a"].terminal_commitment == "c" * 64
    assert by_scope["issuer:b"].status == "intentionally_excluded"
    assert by_scope["issuer:b"].reason_codes == ("scope_not_core",)
    assert by_scope["issuer:c"].status == "blocked"
    assert by_scope["issuer:c"].reason_codes == ("promotion_missing",)
    assert (
        manifest.manifest_sha256
        == build_scope_eligibility_manifest(
            conn,
            operation_recorded_at=datetime(2026, 7, 31, tzinfo=UTC),
        ).manifest_sha256
    )


def test_scope_manifest_fails_closed_on_materializer_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _eligibility_database()

    def fail(connection: sqlite3.Connection, request: object) -> object:
        raise LatestStateActivationError("tampered governed input")

    monkeypatch.setattr(
        "provenance.latest_state_activation.refresh_latest_governed_state",
        fail,
    )

    manifest = build_scope_eligibility_manifest(
        conn,
        operation_recorded_at=datetime(2026, 7, 31, tzinfo=UTC),
    )

    first = manifest.scopes[0]
    assert first.status == "blocked"
    assert first.reason_codes == ("materializer_validation_failed",)
    assert first.blocker_detail == "tampered governed input"
    assert first.blocker_detail_sha256 is not None


def test_scope_manifest_blocks_duplicate_current_scope_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _eligibility_database()
    conn.execute(
        "INSERT INTO v_issuer_reporting_scope_current VALUES (?,?,?,?)",
        ("revision-a-duplicate", "issuer:a", "issuer-a", "core"),
    )

    def dry_run(
        connection: sqlite3.Connection,
        request: LatestGovernedRefreshRequest,
    ) -> LatestGovernedRefreshResult:
        del connection, request
        return _dry_run_result()

    monkeypatch.setattr(
        "provenance.latest_state_activation.refresh_latest_governed_state",
        dry_run,
    )

    manifest = build_scope_eligibility_manifest(
        conn,
        operation_recorded_at=datetime(2026, 7, 31, tzinfo=UTC),
    )

    duplicates = [item for item in manifest.scopes if item.scope_key == "issuer:a"]
    assert len(duplicates) == 2
    assert all(item.status == "blocked" for item in duplicates)
    assert all(item.reason_codes == ("scope_registry_ambiguous",) for item in duplicates)


def test_scope_manifest_blocks_nonterminal_materializer_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _eligibility_database()

    def staged(
        connection: sqlite3.Connection,
        request: LatestGovernedRefreshRequest,
    ) -> LatestGovernedRefreshResult:
        del connection, request
        return _dry_run_result().model_copy(update={"outcome": "staged"})

    monkeypatch.setattr(
        "provenance.latest_state_activation.refresh_latest_governed_state",
        staged,
    )

    manifest = build_scope_eligibility_manifest(
        conn,
        operation_recorded_at=datetime(2026, 7, 31, tzinfo=UTC),
    )

    first = manifest.scopes[0]
    assert first.status == "blocked"
    assert first.reason_codes == ("materializer_result_invalid",)
    assert first.blocker_detail == "mode=dry_run;outcome=staged"


def test_scope_manifest_refuses_empty_or_excluded_only_core_cohort() -> None:
    conn = _eligibility_database()
    conn.execute("UPDATE v_issuer_reporting_scope_current SET inclusion_state='monitored'")

    with pytest.raises(LatestStateActivationError, match="core scope cohort is empty"):
        build_scope_eligibility_manifest(
            conn,
            operation_recorded_at=datetime(2026, 7, 31, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "commitment_column", ("source_inventory_set_sha256", "narrative_bundles_sha256")
)
def test_scope_manifest_blocks_tampered_raw_text_commitments(
    commitment_column: str,
) -> None:
    conn = _eligibility_database()
    conn.execute(
        f"UPDATE v_ask_retrieval_scope_current SET {commitment_column}=?",  # nosec B608 -- test allowlist
        ("0" * 64,),
    )

    manifest = build_scope_eligibility_manifest(
        conn,
        operation_recorded_at=datetime(2026, 7, 31, tzinfo=UTC),
    )

    first = manifest.scopes[0]
    assert first.status == "blocked"
    assert first.reason_codes == ("promotion_evidence_commitment_mismatch",)


def test_bound_manifest_commits_candidate_receipts_registry_and_scope_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    _candidate(database)
    seal = tmp_path / "candidate-seal.json"
    _seal(seal, database)
    structural = audit_governed_candidate(
        database,
        seal_path=seal,
        expected_revision="0261_latest_governed_state",
    )
    audit_path = tmp_path / "candidate-audit.json"
    audit_path.write_text(structural.model_dump_json(), encoding="utf-8")
    coverage = audit_candidate_coverage(database, candidate_audit_receipt=audit_path)
    coverage_path = tmp_path / "candidate-coverage.json"
    coverage_path.write_text(coverage.model_dump_json(), encoding="utf-8")
    registry_path = tmp_path / "production-scopes.json"
    registry_path.write_text('{"registry_sha256":"registry-sha"}', encoding="utf-8")
    conn = _eligibility_database()

    def dry_run(
        connection: sqlite3.Connection,
        request: LatestGovernedRefreshRequest,
    ) -> LatestGovernedRefreshResult:
        del connection, request
        return _dry_run_result()

    monkeypatch.setattr(
        "provenance.latest_state_activation.refresh_latest_governed_state",
        dry_run,
    )
    eligibility = build_scope_eligibility_manifest(
        conn,
        operation_recorded_at=datetime(2026, 7, 31, tzinfo=UTC),
    )
    identity = candidate_file_identity(database)
    audit_snapshot, _ = read_candidate_artifact(audit_path)
    coverage_snapshot, _ = read_candidate_artifact(coverage_path)
    registry_snapshot, _ = read_candidate_artifact(registry_path)

    bound = bind_scope_eligibility_manifest(
        database_path=database,
        audit_path=audit_path,
        coverage_path=coverage_path,
        scope_registry_path=registry_path,
        scope_registry_sha256="registry-sha",
        audit_snapshot=audit_snapshot,
        coverage_snapshot=coverage_snapshot,
        registry_snapshot=registry_snapshot,
        expected_scope_revision_ids=("revision-a", "revision-c"),
        identity_before=identity,
        identity_after=identity,
        eligibility=eligibility,
        expected_revision="0261_latest_governed_state",
    )

    assert bound.expected_scope_count == 2
    assert bound.database_sha256 == structural.database_sha256
    assert bound.candidate_coverage_report_sha256 == coverage.report_sha256
    assert bound.candidate_audit_identity_before == bound.candidate_audit_identity_after
    assert bound.candidate_coverage_identity_before == bound.candidate_coverage_identity_after
    assert (
        bound.production_scope_registry_identity_before
        == bound.production_scope_registry_identity_after
    )
    assert verify_bound_eligibility_manifest(bound)

    with pytest.raises(LatestStateActivationError, match="differ from production registry"):
        bind_scope_eligibility_manifest(
            database_path=database,
            audit_path=audit_path,
            coverage_path=coverage_path,
            scope_registry_path=registry_path,
            scope_registry_sha256="registry-sha",
            audit_snapshot=audit_snapshot,
            coverage_snapshot=coverage_snapshot,
            registry_snapshot=registry_snapshot,
            expected_scope_revision_ids=("revision-a",),
            identity_before=identity,
            identity_after=identity,
            eligibility=eligibility,
            expected_revision="0261_latest_governed_state",
        )

    original_payloads = {
        audit_path: audit_path.read_bytes(),
        coverage_path: coverage_path.read_bytes(),
        registry_path: registry_path.read_bytes(),
    }
    for mutation_path in (audit_path, coverage_path, registry_path):
        audit_snapshot, _ = read_candidate_artifact(audit_path)
        coverage_snapshot, _ = read_candidate_artifact(coverage_path)
        registry_snapshot, _ = read_candidate_artifact(registry_path)

        def mutate_input(path: Path = mutation_path) -> None:
            path.write_bytes(path.read_bytes() + b" ")

        monkeypatch.setattr(
            activation_module,
            "_before_bound_artifact_recheck",
            mutate_input,
        )
        with pytest.raises(LatestStateActivationError, match="changed during binding"):
            bind_scope_eligibility_manifest(
                database_path=database,
                audit_path=audit_path,
                coverage_path=coverage_path,
                scope_registry_path=registry_path,
                scope_registry_sha256="registry-sha",
                audit_snapshot=audit_snapshot,
                coverage_snapshot=coverage_snapshot,
                registry_snapshot=registry_snapshot,
                expected_scope_revision_ids=("revision-a", "revision-c"),
                identity_before=identity,
                identity_after=identity,
                eligibility=eligibility,
                expected_revision="0261_latest_governed_state",
            )
        mutation_path.write_bytes(original_payloads[mutation_path])
