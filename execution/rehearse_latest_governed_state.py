"""Advance one bounded stage of the sealed latest-governed-state rehearsal."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal, TypeVar, cast

from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import execution.audit_latest_state_candidate as audit_cli  # noqa: E402
import execution.audit_latest_state_candidate_coverage as coverage_cli  # noqa: E402
import execution.build_latest_state_eligibility_manifest as eligibility_cli  # noqa: E402
import execution.populate_canonical_resolution as canonical_cli  # noqa: E402
import execution.populate_document_processing as document_cli  # noqa: E402
import execution.populate_latest_governed_state as latest_cli  # noqa: E402
import execution.populate_metric_ontology as ontology_cli  # noqa: E402
import execution.seal_latest_state_rehearsal_candidate as seal_cli  # noqa: E402
from log_redact import redact  # noqa: E402
from provenance.cutover_preflight import (  # noqa: E402
    ExistingCloneUpgradeRequest,
    upgrade_existing_isolated_clone,
)
from provenance.immutable_artifact import (  # noqa: E402
    ImmutableArtifactConflictError,
    ImmutableArtifactSnapshot,
    assert_artifact_unchanged,
    path_aliases_any,
    publish_text_no_clobber,
    read_stable_artifact,
    require_no_reparse_points,
)
from provenance.latest_governed_population import (  # noqa: E402
    LatestGovernedPopulationReceipt,
)
from provenance.latest_governed_state import (  # noqa: E402
    LatestGovernedCohortAudit,
    audit_latest_governed_cohort,
)
from provenance.latest_state_activation import (  # noqa: E402
    BoundLatestStateEligibilityManifest,
)
from provenance.latest_state_rehearsal import (  # noqa: E402
    ActivationBoundaryRequirements,
    AdmissionBundle,
    ArtifactCommitment,
    CandidatePerformanceEvidence,
    DatabaseFileState,
    ExactReplayEvidence,
    PopulationCheckpointEvidence,
    RehearsalCheckpoint,
    RehearsalPlan,
    RehearsalStage,
    RestoreRoundtripEvidence,
    SemanticQualificationEvidence,
    TerminalReadinessBundle,
    build_rehearsal_checkpoint,
    build_rehearsal_readiness_receipt,
    verify_rehearsal_checkpoint,
)
from provenance.population_canonical_resolution import (  # noqa: E402
    CanonicalResolutionOperationReceipt,
    database_instance_id,
)
from provenance.population_document_processing import (  # noqa: E402
    DocumentProcessingOperationReceipt,
)
from provenance.population_metric_ontology import MetricOntologyOperationReceipt  # noqa: E402
from runtime.job_runtime import (  # noqa: E402
    JobAlreadyRunningError,
    JobLock,
    allow_nested_job_locks,
    portfolio_db_path,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

ModelT = TypeVar("ModelT", bound=BaseModel)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--prior-checkpoint", type=Path)
    parser.add_argument("--stage-evidence", type=Path)
    parser.add_argument("--activation-boundary-requirements", type=Path)
    parser.add_argument("--receipt", type=Path, required=True)
    return parser


def _load_model(
    path: Path,
    model_type: type[ModelT],
) -> tuple[ArtifactCommitment, ImmutableArtifactSnapshot, ModelT]:
    snapshot, payload = read_stable_artifact(path)
    artifact = ArtifactCommitment(
        path=str(snapshot.path),
        device=snapshot.device,
        inode=snapshot.inode,
        size_bytes=snapshot.size_bytes,
        modified_time_ns=snapshot.modified_time_ns,
        changed_time_ns=snapshot.changed_time_ns,
        file_sha256=snapshot.file_sha256,
    )
    return artifact, snapshot, model_type.model_validate_json(payload)


def _load_prior(
    path: Path | None,
) -> tuple[ArtifactCommitment, RehearsalCheckpoint] | None:
    if path is None:
        return None
    artifact, _snapshot, receipt = _load_model(path, RehearsalCheckpoint)
    if not verify_rehearsal_checkpoint(receipt):
        raise ValueError("prior rehearsal checkpoint commitment is invalid")
    return artifact, receipt


def _history(path: Path | None) -> tuple[tuple[ArtifactCommitment, RehearsalCheckpoint], ...]:
    items: list[tuple[ArtifactCommitment, RehearsalCheckpoint]] = []
    current = path
    expected_file_sha: str | None = None
    expected_receipt_sha: str | None = None
    while current is not None:
        loaded = _load_prior(current)
        if loaded is None:
            break
        artifact, receipt = loaded
        if expected_file_sha is not None and (
            artifact.file_sha256 != expected_file_sha
            or receipt.receipt_sha256 != expected_receipt_sha
        ):
            raise ValueError("rehearsal checkpoint chain commitment is broken")
        items.append(loaded)
        expected_file_sha = receipt.prior_checkpoint_sha256
        expected_receipt_sha = receipt.prior_receipt_sha256
        current = (
            None if receipt.prior_checkpoint_path is None else Path(receipt.prior_checkpoint_path)
        )
    return tuple(reversed(items))


def _stage_artifact(
    history: tuple[tuple[ArtifactCommitment, RehearsalCheckpoint], ...],
    stage: RehearsalStage,
) -> ArtifactCommitment:
    for _checkpoint_artifact, receipt in reversed(history):
        if receipt.stage is stage and receipt.stage_complete:
            return receipt.output_artifacts[-1]
    raise ValueError(f"required rehearsal stage is missing: {stage.value}")


def _output_path(plan: RehearsalPlan, ordinal: int, label: str) -> Path:
    return Path(plan.evidence_directory) / f"{ordinal:04d}-{label}.json"


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _run_population(
    *,
    plan: RehearsalPlan,
    stage: RehearsalStage,
    prior: RehearsalCheckpoint,
    history: tuple[tuple[ArtifactCommitment, RehearsalCheckpoint], ...],
    ordinal: int,
    database_before: DatabaseFileState,
) -> tuple[ArtifactCommitment, PopulationCheckpointEvidence, bool]:
    previous = prior.population_checkpoint if prior.stage is stage else None
    apply = previous is not None and previous.mode == "dry_run"
    prior_apply = (
        previous.prior_operator_receipt
        if apply and previous is not None
        else (
            previous.operator_receipt
            if previous is not None and previous.mode == "apply" and previous.exit_code == 3
            else None
        )
    )
    prior_apply_sha = (
        previous.prior_operator_receipt_sha256
        if apply and previous is not None
        else (
            previous.operator_receipt_sha256
            if previous is not None and previous.mode == "apply" and previous.exit_code == 3
            else None
        )
    )
    request_cursor = (
        previous.request_cursor
        if apply and previous is not None
        else (
            previous.result_cursor
            if previous is not None and previous.mode == "apply" and previous.exit_code == 3
            else None
        )
    )
    prior_apply_path = None if prior_apply is None else prior_apply.path
    admission_path = previous.operator_receipt.path if apply and previous is not None else None
    mode = "apply" if apply else "dry_run"
    output = _output_path(plan, ordinal, f"{stage.value}-{mode}")
    common = [
        "--cutoff-at",
        _iso(plan.cutoff_at),
    ]
    if stage is RehearsalStage.DOCUMENT:
        argv = [
            "--db",
            plan.database_path,
            *common,
            "--recorded-at",
            _iso(plan.operation_recorded_at),
            "--phase",
            "all",
            "--max-obligations",
            str(plan.max_document_obligations),
            "--receipt",
            str(output),
        ]
        if request_cursor is not None:
            argv.extend(["--after-obligation-id", request_cursor])
        if prior_apply_path is not None:
            argv.extend(["--prior-checkpoint-receipt", prior_apply_path])
        if admission_path is not None:
            argv.extend(["--apply", "--admission-receipt", admission_path])
        exit_code = document_cli.main(argv)
        if exit_code not in {0, 3}:
            raise ValueError(f"document operator blocked with exit {exit_code}")
        artifact, _snapshot, receipt = _load_model(output, DocumentProcessingOperationReceipt)
        receipt_apply = receipt.request.apply
        internal_sha = receipt.receipt_sha256
        next_cursor: str | None = receipt.result.checkpoint.last_processing_obligation_revision_id
    elif stage is RehearsalStage.ONTOLOGY:
        argv = [
            "--db",
            plan.database_path,
            *common,
            "--recorded-at",
            _iso(plan.operation_recorded_at),
            "--phase",
            "all",
            "--max-observations",
            str(plan.max_ontology_observations),
            "--receipt",
            str(output),
        ]
        if request_cursor is not None:
            argv.extend(["--after-observation-id", request_cursor])
        if prior_apply_path is not None:
            argv.extend(["--prior-checkpoint-receipt", prior_apply_path])
        if admission_path is not None:
            argv.extend(["--apply", "--admission-receipt", admission_path])
        exit_code = ontology_cli.main(argv)
        if exit_code not in {0, 3}:
            raise ValueError(f"ontology operator blocked with exit {exit_code}")
        artifact, _snapshot, receipt = _load_model(output, MetricOntologyOperationReceipt)
        receipt_apply = receipt.request.apply
        internal_sha = receipt.receipt_sha256
        next_cursor = receipt.result.last_observation_id
    elif stage is RehearsalStage.CANONICAL:
        document = _stage_artifact(history, RehearsalStage.DOCUMENT)
        argv = [
            "--db",
            plan.database_path,
            *common,
            "--recorded-at",
            _iso(plan.operation_recorded_at),
            "--phase",
            "all",
            "--max-cells",
            str(plan.max_canonical_cells),
            "--document-prerequisite-receipt",
            document.path,
            "--receipt",
            str(output),
        ]
        if request_cursor is not None:
            argv.extend(["--after-cell-id", request_cursor])
        if prior_apply_path is not None:
            argv.extend(["--prior-checkpoint-receipt", prior_apply_path])
        if admission_path is not None:
            argv.extend(["--apply", "--admission-receipt", admission_path])
        exit_code = canonical_cli.main(argv)
        if exit_code not in {0, 3}:
            raise ValueError(f"canonical operator blocked with exit {exit_code}")
        artifact, _snapshot, receipt = _load_model(output, CanonicalResolutionOperationReceipt)
        receipt_apply = receipt.request.apply
        internal_sha = receipt.receipt_sha256
        next_cursor = receipt.result.last_canonical_metric_cell_id
    elif stage is RehearsalStage.LATEST:
        eligibility = _stage_artifact(history, RehearsalStage.ADMISSION_ELIGIBILITY)
        argv = [
            "--database",
            plan.database_path,
            "--eligibility",
            eligibility.path,
            "--scope-registry",
            plan.production_scope_registry,
            "--expected-revision",
            plan.expected_target_revision,
            "--operation-recorded-at",
            _iso(plan.operation_recorded_at),
            "--max-scopes",
            "1",
            "--max-batch-rows",
            str(plan.max_latest_batch_rows),
            "--receipt",
            str(output),
        ]
        if request_cursor is not None:
            argv.extend(["--after-scope-id", request_cursor])
        if prior_apply_path is not None:
            argv.extend(["--prior-checkpoint-receipt", prior_apply_path])
        if apply:
            if admission_path is None:
                raise ValueError("latest apply lacks its exact dry-run admission receipt")
            argv.extend(["--apply", "--admission-receipt", admission_path])
        exit_code = latest_cli.main(argv)
        if exit_code not in {0, 3}:
            raise ValueError(f"latest operator blocked with exit {exit_code}")
        artifact, _snapshot, receipt = _load_model(output, LatestGovernedPopulationReceipt)
        receipt_apply = receipt.request.apply
        internal_sha = receipt.receipt_sha256
        next_cursor = (
            receipt.result.processed_scope_ids[-1]
            if exit_code == 3 and receipt.result.processed_scope_ids
            else None
        )
    else:
        raise ValueError("stage is not a population operator")
    bounded_exit = cast(Literal[0, 3], exit_code)
    if bool(receipt_apply) != apply:
        raise ValueError("population operator receipt mode differs from invocation")
    if bounded_exit == 0:
        next_cursor = None
    database_after = DatabaseFileState.from_path(Path(plan.database_path))
    admission = previous.operator_receipt if apply and previous is not None else None
    admission_sha = previous.operator_receipt_sha256 if apply and previous is not None else None
    evidence = PopulationCheckpointEvidence(
        operator=stage.value,
        mode=mode,
        exit_code=bounded_exit,
        request_cursor=request_cursor,
        result_cursor=next_cursor,
        operator_receipt=artifact,
        operator_receipt_sha256=internal_sha,
        prior_operator_receipt=prior_apply,
        prior_operator_receipt_sha256=prior_apply_sha,
        admission_receipt=admission,
        admission_receipt_sha256=admission_sha,
        database_before=database_before,
        database_after=database_after,
    )
    return artifact, evidence, apply and bounded_exit == 0


def _run_nonpopulation(
    *,
    plan: RehearsalPlan,
    stage: RehearsalStage,
    history: tuple[tuple[ArtifactCommitment, RehearsalCheckpoint], ...],
    ordinal: int,
    stage_evidence: Path | None,
    activation_requirements: Path | None,
) -> ArtifactCommitment:
    database = Path(plan.database_path)
    output = _output_path(plan, ordinal, stage.value)
    if stage is RehearsalStage.UPGRADE:
        clone = ArtifactCommitment.from_path(Path(plan.compressed_clone_receipt))
        _snapshot, payload = read_stable_artifact(Path(plan.compressed_clone_receipt))
        clone_payload = json.loads(payload)
        upgrade_existing_isolated_clone(
            ExistingCloneUpgradeRequest(
                repo_root=Path(plan.repo_root),
                database_path=database,
                compressed_clone_receipt=Path(plan.compressed_clone_receipt),
                receipt_path=output,
                expected_source_revision=plan.expected_source_revision,
                expected_target_revision=plan.expected_target_revision,
                operation_recorded_at=plan.operation_recorded_at,
                minimum_free_bytes=int(clone_payload["minimum_free_bytes"]),
            )
        )
        assert clone.verify()
        if not output.is_file():
            raise ValueError("clone upgrade owner did not publish its canonical receipt")
    elif stage in {RehearsalStage.ADMISSION_SEAL, RehearsalStage.TERMINAL_SEAL}:
        if seal_cli.main(
            [
                "--repo-root",
                plan.repo_root,
                "--database",
                plan.database_path,
                "--expected-revision",
                plan.expected_target_revision,
                "--seal",
                str(output),
            ]
        ):
            raise ValueError("candidate sealing blocked")
    elif stage in {RehearsalStage.ADMISSION_AUDIT, RehearsalStage.TERMINAL_AUDIT}:
        seal_stage = (
            RehearsalStage.ADMISSION_SEAL
            if stage is RehearsalStage.ADMISSION_AUDIT
            else RehearsalStage.TERMINAL_SEAL
        )
        seal = _stage_artifact(history, seal_stage)
        if audit_cli.main(
            [
                "--database",
                plan.database_path,
                "--seal",
                seal.path,
                "--expected-revision",
                plan.expected_target_revision,
                "--output",
                str(output),
            ]
        ):
            raise ValueError("candidate structural audit blocked")
    elif stage in {RehearsalStage.ADMISSION_COVERAGE, RehearsalStage.TERMINAL_COVERAGE}:
        audit_stage = (
            RehearsalStage.ADMISSION_AUDIT
            if stage is RehearsalStage.ADMISSION_COVERAGE
            else RehearsalStage.TERMINAL_AUDIT
        )
        audit = _stage_artifact(history, audit_stage)
        if coverage_cli.main(
            [
                "--database",
                plan.database_path,
                "--candidate-audit-receipt",
                audit.path,
                "--output",
                str(output),
            ]
        ):
            raise ValueError("candidate coverage audit blocked")
    elif stage in {
        RehearsalStage.ADMISSION_ELIGIBILITY,
        RehearsalStage.TERMINAL_ELIGIBILITY,
    }:
        admission = stage is RehearsalStage.ADMISSION_ELIGIBILITY
        audit = _stage_artifact(
            history,
            RehearsalStage.ADMISSION_AUDIT if admission else RehearsalStage.TERMINAL_AUDIT,
        )
        coverage = _stage_artifact(
            history,
            RehearsalStage.ADMISSION_COVERAGE if admission else RehearsalStage.TERMINAL_COVERAGE,
        )
        if eligibility_cli.main(
            [
                "--database",
                plan.database_path,
                "--candidate-audit-receipt",
                audit.path,
                "--candidate-coverage-receipt",
                coverage.path,
                "--scope-registry",
                plan.production_scope_registry,
                "--expected-revision",
                plan.expected_target_revision,
                "--operation-recorded-at",
                _iso(plan.operation_recorded_at),
                "--output",
                str(output),
            ]
        ):
            raise ValueError("candidate eligibility binding blocked")
    elif stage is RehearsalStage.COHORT_AUDIT:
        terminal = _stage_artifact(history, RehearsalStage.TERMINAL_ELIGIBILITY)
        _artifact, _snapshot, bound = _load_model(
            Path(terminal.path), BoundLatestStateEligibilityManifest
        )
        scope_ids = tuple(bound.expected_scope_ids)
        conn = connect_sqlite(
            database,
            role=SQLiteConnectionRole.QUIESCED_IMMUTABLE_READ_ONLY,
            schema_preflight=False,
        )
        try:
            audit = audit_latest_governed_cohort(
                conn,
                scope_ids,
                operation_recorded_at=plan.operation_recorded_at,
                high_risk_sample_size=plan.high_risk_sample_size,
            )
        finally:
            conn.close()
        publish_text_no_clobber(output, audit.model_dump_json())
    elif stage in {
        RehearsalStage.SEMANTIC,
        RehearsalStage.REPLAY,
        RehearsalStage.RESTORE,
        RehearsalStage.PERFORMANCE,
    }:
        del stage_evidence
        raise ValueError(
            f"{stage.value} has no authoritative generator; terminal readiness is disabled"
        )
    elif stage is RehearsalStage.COMPOSITE:
        if activation_requirements is None:
            raise ValueError("composite requires future activation-boundary requirements")
        _requirements_artifact, _snapshot, requirements = _load_model(
            activation_requirements, ActivationBoundaryRequirements
        )
        admission_eligibility = _stage_artifact(history, RehearsalStage.ADMISSION_ELIGIBILITY)
        terminal_eligibility = _stage_artifact(history, RehearsalStage.TERMINAL_ELIGIBILITY)
        _a, _s, admitted = _load_model(
            Path(admission_eligibility.path), BoundLatestStateEligibilityManifest
        )
        _a, _s, terminal = _load_model(
            Path(terminal_eligibility.path), BoundLatestStateEligibilityManifest
        )
        _a, _s, cohort = _load_model(
            Path(_stage_artifact(history, RehearsalStage.COHORT_AUDIT).path),
            LatestGovernedCohortAudit,
        )
        _a, _s, semantics = _load_model(
            Path(_stage_artifact(history, RehearsalStage.SEMANTIC).path),
            SemanticQualificationEvidence,
        )
        _a, _s, replay = _load_model(
            Path(_stage_artifact(history, RehearsalStage.REPLAY).path),
            ExactReplayEvidence,
        )
        _a, _s, restore = _load_model(
            Path(_stage_artifact(history, RehearsalStage.RESTORE).path),
            RestoreRoundtripEvidence,
        )
        _a, _s, performance = _load_model(
            Path(_stage_artifact(history, RehearsalStage.PERFORMANCE).path),
            CandidatePerformanceEvidence,
        )
        admitted_commitments = {
            item.scope_id: str(item.terminal_commitment)
            for item in admitted.eligibility.scopes
            if item.status == "eligible" and item.terminal_commitment is not None
        }
        terminal_commitments = {
            item.scope_id: str(item.terminal_commitment)
            for item in terminal.eligibility.scopes
            if item.status == "eligible" and item.terminal_commitment is not None
        }
        scope_ids = tuple(admitted.expected_scope_ids)
        readiness = build_rehearsal_readiness_receipt(
            plan=plan,
            database_instance_id=_database_instance(database),
            database_sha256=DatabaseFileState.from_path(database).file_sha256,
            alembic_revision=plan.expected_target_revision,
            production_scope_ids=scope_ids,
            table_counts=cohort.table_counts,
            stage_artifacts=tuple(
                artifact for _checkpoint, item in history for artifact in item.output_artifacts
            ),
            admission_bundle=AdmissionBundle(
                candidate_audit=_stage_artifact(history, RehearsalStage.ADMISSION_AUDIT),
                candidate_coverage=_stage_artifact(history, RehearsalStage.ADMISSION_COVERAGE),
                bound_eligibility=admission_eligibility,
                production_scope_ids=scope_ids,
                terminal_commitments=admitted_commitments,
            ),
            terminal_bundle=TerminalReadinessBundle(
                candidate_seal=_stage_artifact(history, RehearsalStage.TERMINAL_SEAL),
                candidate_audit=_stage_artifact(history, RehearsalStage.TERMINAL_AUDIT),
                candidate_coverage=_stage_artifact(history, RehearsalStage.TERMINAL_COVERAGE),
                bound_eligibility=terminal_eligibility,
                latest_population_receipt=_stage_artifact(history, RehearsalStage.LATEST),
                cohort_audit=_stage_artifact(history, RehearsalStage.COHORT_AUDIT),
                production_scope_ids=scope_ids,
                terminal_commitments=terminal_commitments,
            ),
            semantic_qualification=semantics,
            activation_boundary_requirements=requirements,
            restore_roundtrip=restore,
            exact_replay_verified=(replay.database_sha256_before == replay.database_sha256_after),
            candidate_performance_passed=performance.synthetic_benchmark_passed,
            exhaustive_parity_failure_count=0,
            cross_scope_leakage_count=0,
            retrieval_canary_failure_count=0,
            fts_failure_count=0,
        )
        publish_text_no_clobber(output, readiness.model_dump_json())
    else:
        raise ValueError(f"unsupported rehearsal stage: {stage.value}")
    return ArtifactCommitment.from_path(output)


def _database_instance(database: Path) -> str:
    conn = connect_sqlite(database, role=SQLiteConnectionRole.READ_ONLY)
    try:
        return database_instance_id(conn)
    finally:
        conn.close()


def _safe_outer_receipt(
    path: Path,
    *,
    plan: RehearsalPlan,
    inputs: tuple[Path, ...],
) -> Path:
    destination = Path(os.path.abspath(path))
    protected = {
        Path(plan.database_path),
        Path(plan.compressed_clone_receipt),
        Path(plan.production_scope_registry),
        portfolio_db_path(Path(plan.repo_root)).resolve(),
        *(Path(f"{plan.database_path}{suffix}") for suffix in ("-wal", "-shm", "-journal")),
        *(Path(os.path.abspath(item)) for item in inputs),
    }
    for item in (destination, *protected):
        require_no_reparse_points(item)
    if path_aliases_any(destination, protected):
        raise ValueError("outer rehearsal receipt aliases a protected artifact")
    return destination


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        plan_artifact, plan_snapshot, plan = _load_model(args.plan, RehearsalPlan)
        if not plan.verify_commitment():
            raise ValueError("rehearsal plan commitment is invalid")
        if Path(plan.repo_root) != PROJECT_ROOT.resolve():
            raise ValueError("rehearsal plan repo root differs from the executing checkout")
        prior = _load_prior(args.prior_checkpoint)
        history = _history(args.prior_checkpoint)
        if prior is None:
            stage = RehearsalStage.UPGRADE
            ordinal = 0
        else:
            stage = prior[1].next_stage
            if stage is None:
                raise ValueError("rehearsal chain is already terminal")
            ordinal = prior[1].transition_ordinal + 1
        destination = _safe_outer_receipt(
            args.receipt,
            plan=plan,
            inputs=tuple(
                item
                for item in (
                    args.plan,
                    args.prior_checkpoint,
                    args.stage_evidence,
                    args.activation_boundary_requirements,
                )
                if item is not None
            ),
        )
        locked_database = Path(plan.database_path).expanduser().resolve()
        resources = [
            f"rehearsal-chain:{plan.plan_sha256}",
            f"artifact:{destination}",
            f"sqlite:{locked_database}",
            "portfolio-db",
        ]
        with (
            JobLock(Path(plan.repo_root), "rehearse-latest-governed-state", resources),
            allow_nested_job_locks(),
        ):
            assert_artifact_unchanged(plan_snapshot)
            database_path = Path(plan.database_path)
            live = portfolio_db_path(Path(plan.repo_root)).resolve()
            for protected_path in (database_path, live):
                require_no_reparse_points(protected_path)
            if path_aliases_any(database_path, {live}):
                raise ValueError("rehearsal database aliases the configured live database")
            database_before = DatabaseFileState.from_path(database_path)
            if prior is not None and prior[1].database_after != database_before:
                raise ValueError("rehearsal database changed since the prior checkpoint")
            if stage in {
                RehearsalStage.DOCUMENT,
                RehearsalStage.ONTOLOGY,
                RehearsalStage.CANONICAL,
                RehearsalStage.LATEST,
            }:
                if prior is None:
                    raise ValueError("population stage requires a prior outer checkpoint")
                output, population, stage_complete = _run_population(
                    plan=plan,
                    stage=stage,
                    prior=prior[1],
                    history=history,
                    ordinal=ordinal,
                    database_before=database_before,
                )
            else:
                output = _run_nonpopulation(
                    plan=plan,
                    stage=stage,
                    history=history,
                    ordinal=ordinal,
                    stage_evidence=args.stage_evidence,
                    activation_requirements=args.activation_boundary_requirements,
                )
                population = None
                stage_complete = True
            database_after = DatabaseFileState.from_path(Path(plan.database_path))
            if (
                stage
                not in {
                    RehearsalStage.UPGRADE,
                    RehearsalStage.DOCUMENT,
                    RehearsalStage.ONTOLOGY,
                    RehearsalStage.CANONICAL,
                    RehearsalStage.LATEST,
                }
                and database_before != database_after
            ):
                raise ValueError(f"{stage.value} changed the rehearsal database")
            if (
                population is not None
                and population.mode == "dry_run"
                and (database_before != database_after)
            ):
                raise ValueError("population dry run changed the rehearsal database")
            checkpoint = build_rehearsal_checkpoint(
                plan=plan,
                stage=stage,
                database_instance_id=_database_instance(Path(plan.database_path)),
                alembic_revision=plan.expected_target_revision,
                database_before=database_before,
                database_after=database_after,
                output_artifacts=(output,),
                prior_checkpoint=prior,
                stage_complete=stage_complete,
                population_checkpoint=population,
            )
            publish_text_no_clobber(destination, checkpoint.model_dump_json())
            assert_artifact_unchanged(plan_snapshot)
            if not plan_artifact.verify():
                raise ValueError("rehearsal plan changed during stage execution")
    except (
        ImmutableArtifactConflictError,
        JobAlreadyRunningError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {"event": "sealed_rehearsal_stage_blocked", "error": redact(str(exc))},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "next_stage": None if checkpoint.next_stage is None else checkpoint.next_stage,
                "receipt": str(destination),
                "receipt_sha256": checkpoint.receipt_sha256,
                "stage": checkpoint.stage,
                "stage_complete": checkpoint.stage_complete,
                "status": "checkpointed",
            },
            sort_keys=True,
        )
    )
    return 0 if checkpoint.stage_complete else 3


if __name__ == "__main__":
    raise SystemExit(main())
