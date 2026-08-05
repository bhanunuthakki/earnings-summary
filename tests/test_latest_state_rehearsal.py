from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

import execution.seal_latest_state_rehearsal_candidate as seal_cli
from provenance.latest_governed_state import (
    LatestGovernedCohortAudit,
    LatestGovernedScopeAudit,
)
from provenance.latest_state_activation import CandidateSeal, build_governed_candidate_seal
from provenance.latest_state_rehearsal import (
    ActivationBoundaryRequirements,
    AdmissionBundle,
    ArtifactCommitment,
    DatabaseFileState,
    GroundedCanaryCommitment,
    PopulationCheckpointEvidence,
    RehearsalPlan,
    RehearsalStage,
    RestoreRoundtripEvidence,
    SemanticIssuerQualification,
    SemanticScopeQualification,
    TerminalReadinessBundle,
    build_rehearsal_checkpoint,
    build_rehearsal_readiness_receipt,
    build_semantic_qualification_evidence,
    verify_rehearsal_checkpoint,
    verify_rehearsal_readiness_receipt,
)
from schema_compat import expected_head

SHA_A = "a" * 64
SHA_B = "b" * 64
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
_HEAD_REVISION = expected_head()


def _seal_candidate(database: Path, revision: str, *, wal: bool = False) -> None:
    with sqlite3.connect(database) as conn:
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY);
            CREATE TABLE source_taxonomy_components (component_id TEXT PRIMARY KEY);
            INSERT INTO source_taxonomy_components VALUES ('component-1');
            CREATE TABLE fact_cell_canonical_binding_revisions (
              binding_revision_id TEXT PRIMARY KEY
            );
            INSERT INTO fact_cell_canonical_binding_revisions VALUES ('binding-1');
            """
        )
        conn.execute("INSERT INTO alembic_version VALUES (?)", (revision,))


def _artifact(path: Path, payload: str = "evidence") -> ArtifactCommitment:
    path.write_text(payload, encoding="utf-8")
    return ArtifactCommitment.from_path(path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _plan(tmp_path: Path) -> RehearsalPlan:
    database = tmp_path / "candidate.db"
    database.touch()
    clone_receipt = tmp_path / "clone.json"
    registry = tmp_path / "registry.json"
    clone_receipt.write_text("{}", encoding="utf-8")
    registry.write_text("{}", encoding="utf-8")
    return RehearsalPlan.create(
        repo_root=tmp_path,
        database_path=database,
        evidence_directory=tmp_path / "evidence",
        compressed_clone_receipt=clone_receipt,
        production_scope_registry=registry,
        expected_source_revision="0261_latest_governed_state",
        expected_target_revision=_HEAD_REVISION,
        cutoff_at=NOW,
        operation_recorded_at=NOW,
        max_document_obligations=100,
        max_ontology_observations=100,
        max_canonical_cells=100,
        max_latest_batch_rows=1_000,
        high_risk_sample_size=32,
    )


def test_plan_is_hash_bound_absolute_and_refuses_live_or_unbounded_inputs(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)

    assert plan.database_path == str((tmp_path / "candidate.db").resolve())
    assert plan.verify_commitment()
    assert plan.stage_order == tuple(RehearsalStage)
    assert plan.stage_order.index(RehearsalStage.REPLAY) < plan.stage_order.index(
        RehearsalStage.SEMANTIC
    )
    assert plan.stage_order.index(RehearsalStage.RESTORE) < plan.stage_order.index(
        RehearsalStage.SEMANTIC
    )
    assert plan.stage_order.index(RehearsalStage.PERFORMANCE) < plan.stage_order.index(
        RehearsalStage.SEMANTIC
    )

    relative = plan.model_dump(mode="json")
    relative["database_path"] = "candidate.db"
    with pytest.raises(ValueError, match="absolute and canonical"):
        RehearsalPlan.model_validate(relative)

    with pytest.raises(ValueError, match="canonical live database"):
        RehearsalPlan.create(
            **{
                **plan.model_dump(exclude={"plan_sha256", "stage_order"}),
                "database_path": tmp_path / "data" / "portfolio.db",
            }
        )


def test_checkpoint_chain_is_exact_ordered_and_prior_artifact_bound(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    database = Path(plan.database_path)
    database.write_bytes(b"stage-zero")
    first_output = _artifact(tmp_path / "upgrade.json")
    database_state = DatabaseFileState.from_path(database)

    first = build_rehearsal_checkpoint(
        plan=plan,
        stage=RehearsalStage.UPGRADE,
        database_instance_id="database-instance:" + "1" * 32,
        alembic_revision=plan.expected_target_revision,
        database_before=database_state,
        database_after=database_state,
        output_artifacts=(first_output,),
        prior_checkpoint=None,
        stage_complete=True,
    )

    assert first.next_stage is RehearsalStage.DOCUMENT
    assert verify_rehearsal_checkpoint(first)

    prior_path = tmp_path / "checkpoint-001.json"
    prior_path.write_text(first.model_dump_json(), encoding="utf-8")
    prior = ArtifactCommitment.from_path(prior_path)
    second_output = _artifact(tmp_path / "document.json")
    second = build_rehearsal_checkpoint(
        plan=plan,
        stage=RehearsalStage.DOCUMENT,
        database_instance_id=first.database_instance_id,
        alembic_revision=first.alembic_revision,
        database_before=database_state,
        database_after=database_state,
        output_artifacts=(second_output,),
        prior_checkpoint=(prior, first),
        stage_complete=False,
        population_checkpoint=PopulationCheckpointEvidence(
            operator="document",
            mode="dry_run",
            exit_code=0,
            request_cursor=None,
            result_cursor="obligation-100",
            operator_receipt=second_output,
            operator_receipt_sha256=SHA_A,
            prior_operator_receipt=None,
            prior_operator_receipt_sha256=None,
            admission_receipt=None,
            admission_receipt_sha256=None,
            database_before=database_state,
            database_after=database_state,
        ),
    )

    assert second.next_stage is RehearsalStage.DOCUMENT
    assert second.prior_checkpoint_sha256 == prior.file_sha256
    assert verify_rehearsal_checkpoint(second)

    with pytest.raises(ValueError, match="stage order"):
        build_rehearsal_checkpoint(
            plan=plan,
            stage=RehearsalStage.ONTOLOGY,
            database_instance_id=first.database_instance_id,
            alembic_revision=first.alembic_revision,
            database_before=database_state,
            database_after=database_state,
            output_artifacts=(second_output,),
            prior_checkpoint=(prior, first),
            stage_complete=True,
            population_checkpoint=None,
        )

    prior_path.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="prior checkpoint artifact changed"):
        build_rehearsal_checkpoint(
            plan=plan,
            stage=RehearsalStage.DOCUMENT,
            database_instance_id=first.database_instance_id,
            alembic_revision=first.alembic_revision,
            database_before=database_state,
            database_after=database_state,
            output_artifacts=(second_output,),
            prior_checkpoint=(prior, first),
            stage_complete=True,
            population_checkpoint=None,
        )


def test_checkpoint_chain_refuses_database_byte_splice_between_invocations(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    database = Path(plan.database_path)
    database.write_bytes(b"before")
    before = DatabaseFileState.from_path(database)
    upgrade_output = _artifact(tmp_path / "upgrade-splice.json")
    first = build_rehearsal_checkpoint(
        plan=plan,
        stage=RehearsalStage.UPGRADE,
        database_instance_id="database-instance:" + "1" * 32,
        alembic_revision=plan.expected_target_revision,
        database_before=before,
        database_after=before,
        output_artifacts=(upgrade_output,),
        prior_checkpoint=None,
        stage_complete=True,
    )
    prior_path = tmp_path / "checkpoint-splice.json"
    prior_path.write_text(first.model_dump_json(), encoding="utf-8")
    prior_artifact = ArtifactCommitment.from_path(prior_path)
    database.write_bytes(b"after")
    after = DatabaseFileState.from_path(database)
    dry_output = _artifact(tmp_path / "document-splice.json")
    population = PopulationCheckpointEvidence(
        operator="document",
        mode="dry_run",
        exit_code=0,
        request_cursor=None,
        result_cursor="obligation-1",
        operator_receipt=dry_output,
        operator_receipt_sha256=SHA_A,
        prior_operator_receipt=None,
        prior_operator_receipt_sha256=None,
        admission_receipt=None,
        admission_receipt_sha256=None,
        database_before=after,
        database_after=after,
    )

    with pytest.raises(ValueError, match="bytes changed"):
        build_rehearsal_checkpoint(
            plan=plan,
            stage=RehearsalStage.DOCUMENT,
            database_instance_id=first.database_instance_id,
            alembic_revision=first.alembic_revision,
            database_before=after,
            database_after=after,
            output_artifacts=(dry_output,),
            prior_checkpoint=(prior_artifact, first),
            stage_complete=False,
            population_checkpoint=population,
        )


def test_post_mutation_seal_is_exact_and_refuses_uncheckpointed_sidecars(
    tmp_path: Path,
) -> None:
    database = tmp_path / "candidate.db"
    _seal_candidate(database, _HEAD_REVISION)


def test_seal_cli_checkpoints_isolated_candidate_and_publishes_no_clobber(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    _seal_candidate(database, _HEAD_REVISION)

    def portfolio_path(_repo_root: Path) -> Path:
        return tmp_path / "never-live.db"

    monkeypatch.setattr(seal_cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(seal_cli, "portfolio_db_path", portfolio_path)
    reparse_checks: list[Path] = []
    real_require_no_reparse_points = seal_cli.require_no_reparse_points

    def record_reparse_check(path: Path) -> None:
        reparse_checks.append(path)
        real_require_no_reparse_points(path)

    monkeypatch.setattr(
        seal_cli,
        "require_no_reparse_points",
        record_reparse_check,
    )
    seal_path = tmp_path / "candidate-seal.json"

    exit_code = seal_cli.main(
        [
            "--repo-root",
            str(tmp_path),
            "--database",
            str(database),
            "--expected-revision",
            _HEAD_REVISION,
            "--seal",
            str(seal_path),
        ]
    )

    assert exit_code == 0
    assert json.loads(seal_path.read_text(encoding="utf-8"))["sha256"] == _sha256(database)
    assert not Path(f"{database}-wal").exists()
    assert reparse_checks.count(Path(f"{database}-wal")) >= 4
    assert (
        seal_cli.main(
            [
                "--repo-root",
                str(tmp_path),
                "--database",
                str(database),
                "--expected-revision",
                _HEAD_REVISION,
                "--seal",
                str(database),
            ]
        )
        == 2
    )

    seal = build_governed_candidate_seal(
        database,
        expected_revision=_HEAD_REVISION,
    )

    assert seal.database == str(database.resolve())
    assert seal.revision == (_HEAD_REVISION,)
    assert seal.canonical_bindings == 1
    assert seal.source_taxonomy_components == 1
    assert seal.sha256 == hashlib.sha256(database.read_bytes()).hexdigest()

    Path(f"{database}-wal").write_bytes(b"not-checkpointed")
    with pytest.raises(RuntimeError, match="WAL sidecar"):
        build_governed_candidate_seal(
            database,
            expected_revision=_HEAD_REVISION,
        )


@pytest.mark.parametrize("target", ["database", "seal"])
def test_seal_cli_refuses_every_live_sqlite_sidecar_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    database = tmp_path / "candidate.db"
    _seal_candidate(database, _HEAD_REVISION)
    live = tmp_path / "live" / "portfolio.db"

    def portfolio_path(_repo_root: Path) -> Path:
        return live

    monkeypatch.setattr(seal_cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(seal_cli, "portfolio_db_path", portfolio_path)
    target_path = {
        "database": Path(f"{live}-wal"),
        "seal": Path(f"{live}-shm"),
    }[target]
    arguments = [
        "--repo-root",
        str(tmp_path),
        "--database",
        str(target_path if target == "database" else database),
        "--expected-revision",
        _HEAD_REVISION,
        "--seal",
        str(target_path if target == "seal" else tmp_path / "candidate.seal.json"),
    ]
    assert seal_cli.main(arguments) == 2
    assert not target_path.exists()


@pytest.mark.parametrize("sidecar_suffix", ["-wal", "-journal"])
def test_seal_cli_refuses_candidate_sidecar_hardlink_to_live_family(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sidecar_suffix: str,
) -> None:
    database = tmp_path / "candidate.db"
    live = tmp_path / "live" / "portfolio.db"
    live.parent.mkdir(parents=True)
    _seal_candidate(database, _HEAD_REVISION)
    _seal_candidate(live, _HEAD_REVISION)
    candidate_sidecar = Path(f"{database}{sidecar_suffix}")
    os.link(live, candidate_sidecar)
    live_sha256 = _sha256(live)

    def portfolio_path(_repo_root: Path) -> Path:
        return live

    monkeypatch.setattr(seal_cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(seal_cli, "portfolio_db_path", portfolio_path)

    assert (
        seal_cli.main(
            [
                "--repo-root",
                str(tmp_path),
                "--database",
                str(database),
                "--expected-revision",
                _HEAD_REVISION,
                "--seal",
                str(tmp_path / "candidate.seal.json"),
            ]
        )
        == 2
    )
    assert _sha256(live) == live_sha256


@pytest.mark.parametrize("shared_member", ["main", "wal"])
def test_seal_cli_refuses_candidate_family_hardlink_to_non_live_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shared_member: str,
) -> None:
    database = tmp_path / "candidate.db"
    backup = tmp_path / "preserved-backup.db"
    _seal_candidate(backup, _HEAD_REVISION)
    if shared_member == "main":
        os.link(backup, database)
    else:
        _seal_candidate(database, _HEAD_REVISION)
        os.link(backup, Path(f"{database}-wal"))
    database_sha256 = _sha256(database)
    backup_sha256 = _sha256(backup)

    def portfolio_path(_repo_root: Path) -> Path:
        return tmp_path / "never-live.db"

    monkeypatch.setattr(seal_cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(seal_cli, "portfolio_db_path", portfolio_path)

    assert (
        seal_cli.main(
            [
                "--repo-root",
                str(tmp_path),
                "--database",
                str(database),
                "--expected-revision",
                _HEAD_REVISION,
                "--seal",
                str(tmp_path / "candidate.seal.json"),
            ]
        )
        == 2
    )
    assert _sha256(database) == database_sha256
    assert _sha256(backup) == backup_sha256


def test_seal_cli_rechecks_candidate_family_after_long_seal_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    backup = tmp_path / "preserved-backup.db"
    _seal_candidate(database, _HEAD_REVISION)
    _seal_candidate(backup, _HEAD_REVISION)
    backup_sha256 = _sha256(backup)
    destination = tmp_path / "candidate.seal.json"
    real_build_seal = seal_cli.build_governed_candidate_seal

    def build_then_substitute_sidecar(
        database_path: Path,
        *,
        expected_revision: str,
    ) -> CandidateSeal:
        seal = real_build_seal(
            database_path,
            expected_revision=expected_revision,
        )
        os.link(backup, Path(f"{database_path}-shm"))
        return seal

    def portfolio_path(_repo_root: Path) -> Path:
        return tmp_path / "never-live.db"

    monkeypatch.setattr(seal_cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(seal_cli, "portfolio_db_path", portfolio_path)
    monkeypatch.setattr(
        seal_cli,
        "build_governed_candidate_seal",
        build_then_substitute_sidecar,
    )

    assert (
        seal_cli.main(
            [
                "--repo-root",
                str(tmp_path),
                "--database",
                str(database),
                "--expected-revision",
                _HEAD_REVISION,
                "--seal",
                str(destination),
            ]
        )
        == 2
    )
    assert not destination.exists()
    assert _sha256(backup) == backup_sha256


def test_seal_cli_refuses_caller_controlled_decoy_repo_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_root = tmp_path / "canonical"
    decoy_root = tmp_path / "decoy"
    actual_live = canonical_root / "data" / "portfolio.db"
    actual_live.parent.mkdir(parents=True)
    _seal_candidate(actual_live, _HEAD_REVISION)
    live_sha256 = _sha256(actual_live)
    monkeypatch.setattr(seal_cli, "PROJECT_ROOT", canonical_root)

    assert (
        seal_cli.main(
            [
                "--repo-root",
                str(decoy_root),
                "--database",
                str(actual_live),
                "--expected-revision",
                _HEAD_REVISION,
                "--seal",
                str(tmp_path / "candidate.seal.json"),
            ]
        )
        == 2
    )
    assert _sha256(actual_live) == live_sha256


def test_seal_cli_allows_explicit_pre_head_isolated_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "pre-head-candidate.db"
    expected_revision = "pre_head_test_revision"
    _seal_candidate(database, expected_revision, wal=True)
    with sqlite3.connect(database) as conn:
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert checkpoint is not None and int(checkpoint[0]) == 0

    def isolated_portfolio_db_path(_repo_root: Path) -> Path:
        return tmp_path / "never-live.db"

    monkeypatch.setattr(seal_cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(seal_cli, "portfolio_db_path", isolated_portfolio_db_path)
    seal_path = tmp_path / "pre-head-candidate.seal.json"

    assert (
        seal_cli.main(
            [
                "--repo-root",
                str(tmp_path),
                "--database",
                str(database),
                "--expected-revision",
                expected_revision,
                "--seal",
                str(seal_path),
            ]
        )
        == 0
    )
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    assert seal["revision"] == [expected_revision]
    assert seal["sha256"] == _sha256(database)


def test_terminal_receipt_separates_rehearsal_from_future_live_boundary(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    evidence = tuple(
        _artifact(tmp_path / f"evidence-{index}.json", str(index))
        for index in range(len(RehearsalStage))
    )
    semantics = build_semantic_qualification_evidence(
        database_sha256=hashlib.sha256(Path(plan.database_path).read_bytes()).hexdigest(),
        request_artifact=evidence[12],
        registry_artifact=evidence[0],
        production_scope_ids=("ask-scope:v1:" + "1" * 64,),
        issuer_qualifications=(
            SemanticIssuerQualification(
                issuer_id="issuer-1",
                corpus_manifest_id="manifest-1",
                corpus_membership_sha256=SHA_A,
                corpus_expected_document_count=1,
                corpus_included_document_count=1,
                corpus_chunk_count=1,
                lexical_index_run_id="lexical-1",
                vector_index_run_id="index-run-1",
                projection_seal_id="projection-seal-1",
                projection_records_sha256=SHA_A,
                projection_artifact_set_sha256=SHA_A,
                embedding_promotion_id="embedding-promotion-1",
                runtime_registration_id="runtime-artifact-1",
                runtime_artifact_sha256=SHA_A,
                lexical_canaries=(
                    GroundedCanaryCommitment(
                        canary_kind="lexical",
                        query_sha256=SHA_A,
                        result_set_sha256=SHA_A,
                        result_count=1,
                        document_family="annual_securities_report",
                    ),
                ),
                semantic_canary=GroundedCanaryCommitment(
                    canary_kind="semantic",
                    query_sha256=SHA_A,
                    result_set_sha256=SHA_A,
                    result_count=1,
                    backend_receipt_sha256=SHA_A,
                    expected_source_id="chunk-1",
                    observed_rank=1,
                ),
                qualification_elapsed_milliseconds=20.0,
            ),
        ),
        scope_qualifications=(
            SemanticScopeQualification(
                scope_id="ask-scope:v1:" + "1" * 64,
                source_scope_key="investor-research",
                source_scope_revision_id="source-scope-revision-1",
                issuer_id="issuer-1",
                reporting_entity_id="entity-1",
                ask_promotion_id="promotion-1",
                research_snapshot_id="research-1",
                fact_generation_id="generation-1",
                fact_projection_seal_sha256=SHA_A,
                source_inventory_count=1,
                source_inventory_set_sha256=SHA_A,
                corpus_manifest_id="manifest-1",
                lexical_index_run_id="lexical-1",
                vector_index_run_id="index-run-1",
                embedding_promotion_id="embedding-promotion-1",
                fact_canary=GroundedCanaryCommitment(
                    canary_kind="fact",
                    query_sha256=SHA_A,
                    result_set_sha256=SHA_A,
                    result_count=1,
                    elapsed_milliseconds=10.0,
                    expected_source_id="cell-1",
                    observed_rank=1,
                ),
            ),
        ),
        corpus_document_count=1,
        grounded_fact_canary_count=1,
        grounded_narrative_canary_count=2,
        failure_count=0,
        max_fact_canary_milliseconds=100.0,
        max_issuer_qualification_milliseconds=100.0,
        observed_max_fact_canary_milliseconds=10.0,
        observed_max_issuer_qualification_milliseconds=20.0,
    )
    activation_boundary = ActivationBoundaryRequirements(
        expected_task_paths=tuple(f"task-{index}" for index in range(45)),
        expected_service_names=("comments", "capture"),
        expected_listener_endpoints=("127.0.0.1:7421", "127.0.0.1:8000"),
        requires_fresh_live_rollback_snapshot=True,
        requires_unexpired_quiescence_receipt=True,
        requires_restoration_receipt=True,
    )
    restore = RestoreRoundtripEvidence(
        rollback_database_sha256=SHA_A,
        mutated_database_sha256=SHA_B,
        restored_database_sha256=SHA_A,
        quick_check="ok",
        integrity_check="ok",
        foreign_key_violation_count=0,
        replay_equivalent=True,
    )
    counts = {
        "latest_governed_refresh_runs": 1,
        "latest_governed_refresh_stage": 0,
        "latest_governed_refresh_receipts": 1,
        "latest_governed_refresh_changes": 0,
        "latest_governed_scope_heads": 1,
        "latest_governed_fact_entries": 1,
        "latest_governed_document_entries": 1,
        "latest_governed_narrative_entries": 1,
        "latest_governed_narrative_fts": 1,
    }
    scope_ids = semantics.production_scope_ids
    commitments = {scope_ids[0]: SHA_A}
    admission = AdmissionBundle(
        candidate_audit=evidence[3],
        candidate_coverage=evidence[4],
        bound_eligibility=evidence[5],
        production_scope_ids=scope_ids,
        terminal_commitments=commitments,
    )
    terminal = TerminalReadinessBundle(
        candidate_seal=evidence[6],
        candidate_audit=evidence[7],
        candidate_coverage=evidence[8],
        bound_eligibility=evidence[9],
        latest_population_receipt=evidence[10],
        cohort_audit=evidence[11],
        production_scope_ids=scope_ids,
        terminal_commitments=commitments,
    )
    scope_audit = LatestGovernedScopeAudit(
        scope_id=scope_ids[0],
        head_receipt_id="head-1",
        terminal_commitment=SHA_A,
        fact_count=1,
        document_count=1,
        narrative_count=1,
        fts_count=1,
        change_count=0,
        finalized_run_count=1,
        fact_canary_coordinate="fact-1",
        narrative_canary_coordinate="narrative-1",
        exhaustive_source_commitment_sha256=SHA_A,
        current_projection_commitment_sha256=SHA_A,
        high_risk_sample_commitment_sha256=SHA_A,
    )
    cohort = LatestGovernedCohortAudit(
        scope_ids=scope_ids,
        scopes=(scope_audit,),
        table_counts=counts,
        cohort_commitment_sha256=SHA_A,
    )

    receipt = build_rehearsal_readiness_receipt(
        plan=plan,
        database_instance_id="database-instance:" + "1" * 32,
        database_sha256=hashlib.sha256(Path(plan.database_path).read_bytes()).hexdigest(),
        alembic_revision=plan.expected_target_revision,
        production_scope_ids=semantics.production_scope_ids,
        table_counts=counts,
        stage_artifacts=evidence,
        admission_bundle=admission,
        terminal_bundle=terminal,
        cohort_audit=cohort,
        semantic_qualification=semantics,
        activation_boundary_requirements=activation_boundary,
        restore_roundtrip=restore,
        exact_replay_verified=True,
        candidate_performance_passed=True,
    )

    assert verify_rehearsal_readiness_receipt(receipt)
    assert receipt.status == "ready"

    broken_counts = dict(counts)
    broken_counts["latest_governed_narrative_entries"] = 0
    broken_cohort = cohort.model_copy(update={"table_counts": broken_counts})
    with pytest.raises(ValueError, match="durable latest-state planes"):
        build_rehearsal_readiness_receipt(
            plan=plan,
            database_instance_id=receipt.database_instance_id,
            database_sha256=receipt.database_sha256,
            alembic_revision=receipt.alembic_revision,
            production_scope_ids=receipt.production_scope_ids,
            table_counts=broken_counts,
            stage_artifacts=evidence,
            admission_bundle=admission,
            terminal_bundle=terminal,
            cohort_audit=broken_cohort,
            semantic_qualification=semantics,
            activation_boundary_requirements=activation_boundary,
            restore_roundtrip=restore,
            exact_replay_verified=True,
            candidate_performance_passed=True,
        )


def test_artifact_commitment_detects_content_or_identity_change(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    artifact = _artifact(path, json.dumps({"ready": True}))

    assert artifact.verify()
    path.write_text(json.dumps({"ready": False}), encoding="utf-8")
    assert not artifact.verify()
