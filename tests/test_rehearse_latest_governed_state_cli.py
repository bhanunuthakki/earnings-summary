# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

import execution.rehearse_latest_governed_state as rehearsal_cli
from provenance.compressed_candidate_clone import MINIMUM_SAFE_FREE_BYTES
from provenance.cutover_preflight import ExistingCloneUpgradeRequest
from provenance.immutable_artifact import read_stable_artifact
from provenance.latest_governed_population import LatestGovernedPopulationReceipt
from provenance.latest_state_activation import BoundLatestStateEligibilityManifest
from provenance.latest_state_rehearsal import (
    ArtifactCommitment,
    DatabaseFileState,
    PopulationCheckpointEvidence,
    RehearsalCheckpoint,
    RehearsalPlan,
    RehearsalStage,
    verify_rehearsal_checkpoint,
)
from provenance.latest_state_rehearsal_evidence import CandidatePerformanceRequest
from provenance.latest_state_semantic_qualification import SemanticQualificationRequest
from provenance.population_document_processing import (
    DocumentProcessingPopulationRequest,
)
from runtime.job_runtime import JobAlreadyRunningError, JobLock
from schema_compat import expected_head
from tests.test_population_document_processing import (
    build_test_document_processing_receipt as build_document_processing_receipt,
)
from tests.test_population_document_processing import (
    receipt_result,
)

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
_HEAD_REVISION = expected_head()


@pytest.fixture(autouse=True)
def _coherent_document_receipt_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        rehearsal_cli.document_cli,
        "build_document_processing_receipt",
        build_document_processing_receipt,
    )


def _plan(tmp_path: Path) -> tuple[RehearsalPlan, Path]:
    database = tmp_path / "candidate.db"
    database.write_bytes(b"isolated-candidate")
    clone = tmp_path / "compressed-clone.json"
    clone.write_text(
        json.dumps({"minimum_free_bytes": MINIMUM_SAFE_FREE_BYTES}),
        encoding="utf-8",
    )
    registry = tmp_path / "registry.json"
    registry.write_text("{}", encoding="utf-8")
    plan = RehearsalPlan.create(
        repo_root=tmp_path,
        database_path=database,
        evidence_directory=tmp_path / "evidence",
        compressed_clone_receipt=clone,
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
    plan_path = tmp_path / "rehearsal-plan.json"
    plan_path.write_text(plan.model_dump_json(), encoding="utf-8")
    return plan, plan_path


def test_dispatcher_advances_exactly_one_upgrade_stage_and_is_no_clobber_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, plan_path = _plan(tmp_path)
    monkeypatch.setattr(rehearsal_cli, "PROJECT_ROOT", tmp_path)
    contender_blocked: list[bool] = []

    def upgrade(_request: ExistingCloneUpgradeRequest) -> SimpleNamespace:
        def contender() -> None:
            try:
                with JobLock(
                    _request.repo_root,
                    "foreign-candidate-writer",
                    [f"sqlite:{_request.database_path}"],
                ):
                    contender_blocked.append(False)
            except JobAlreadyRunningError:
                contender_blocked.append(True)

        thread = threading.Thread(target=contender)
        thread.start()
        thread.join()
        with JobLock(
            _request.repo_root,
            "nested-upgrade-owner",
            ["portfolio-db", f"sqlite:{_request.database_path}"],
        ):
            _request.receipt_path.parent.mkdir(parents=True, exist_ok=True)
            if not _request.receipt_path.exists():
                _request.receipt_path.write_text('{"upgraded":true}', encoding="utf-8")
        return SimpleNamespace(model_dump_json=lambda: '{"upgraded":true}')

    def database_instance(_database: Path) -> str:
        return "database-instance:" + "1" * 32

    monkeypatch.setattr(rehearsal_cli, "upgrade_existing_isolated_clone", upgrade)
    monkeypatch.setattr(rehearsal_cli, "_database_instance", database_instance)
    receipt_path = tmp_path / "checkpoint-0000.json"
    argv = ["--plan", str(plan_path), "--receipt", str(receipt_path)]

    assert rehearsal_cli.main(argv) == 0
    assert rehearsal_cli.main(argv) == 0

    checkpoint = RehearsalCheckpoint.model_validate_json(receipt_path.read_text(encoding="utf-8"))
    assert checkpoint.stage is RehearsalStage.UPGRADE
    assert checkpoint.next_stage is RehearsalStage.DOCUMENT
    assert checkpoint.transition_ordinal == 0
    assert checkpoint.database_path == plan.database_path
    assert verify_rehearsal_checkpoint(checkpoint)
    assert len(list((tmp_path / "evidence").glob("0000-upgrade.json"))) == 1
    assert contender_blocked == [True, True]


def test_dispatcher_refuses_outer_receipt_alias_to_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plan_value, plan_path = _plan(tmp_path)
    monkeypatch.setattr(rehearsal_cli, "PROJECT_ROOT", tmp_path)
    called = False

    def upgrade(_request: ExistingCloneUpgradeRequest) -> SimpleNamespace:
        nonlocal called
        called = True
        return SimpleNamespace(model_dump_json=lambda: "{}")

    monkeypatch.setattr(rehearsal_cli, "upgrade_existing_isolated_clone", upgrade)

    assert rehearsal_cli.main(["--plan", str(plan_path), "--receipt", str(plan_path)]) == 2
    assert not called


def test_dispatcher_refuses_plan_from_a_different_checkout_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plan_value, plan_path = _plan(tmp_path)
    called = False

    def upgrade(_request: ExistingCloneUpgradeRequest) -> SimpleNamespace:
        nonlocal called
        called = True
        return SimpleNamespace(model_dump_json=lambda: "{}")

    monkeypatch.setattr(rehearsal_cli, "upgrade_existing_isolated_clone", upgrade)
    receipt = tmp_path / "foreign-root-checkpoint.json"

    assert rehearsal_cli.main(["--plan", str(plan_path), "--receipt", str(receipt)]) == 2
    assert not called
    assert not receipt.exists()


def test_population_dispatcher_runs_one_dry_call_and_checkpoints_before_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, _plan_path = _plan(tmp_path)
    database = Path(plan.database_path)
    upgrade_artifact_path = tmp_path / "upgrade.json"
    upgrade_artifact_path.write_text("{}", encoding="utf-8")
    upgrade_artifact = ArtifactCommitment.from_path(upgrade_artifact_path)
    upgrade = rehearsal_cli.build_rehearsal_checkpoint(
        plan=plan,
        stage=RehearsalStage.UPGRADE,
        database_instance_id="database-instance:" + "1" * 32,
        alembic_revision=plan.expected_target_revision,
        database_before=DatabaseFileState.from_path(database),
        database_after=DatabaseFileState.from_path(database),
        output_artifacts=(upgrade_artifact,),
        prior_checkpoint=None,
        stage_complete=True,
    )
    calls: list[list[str]] = []

    def document_main(argv: list[str] | None = None) -> int:
        assert argv is not None
        calls.append(argv)
        parsed = rehearsal_cli.document_cli.build_parser().parse_args(argv)
        request = DocumentProcessingPopulationRequest(
            cutoff_at=parsed.cutoff_at,
            operation_recorded_at=parsed.operation_recorded_at,
            phase=parsed.phase,
            max_obligations=parsed.max_obligations,
        )
        receipt = build_document_processing_receipt(
            database_path=str(database.resolve()),
            database_instance_id="database-instance:" + "1" * 32,
            alembic_revision=plan.expected_target_revision,
            request=request,
            result=receipt_result(
                bounded=parsed.max_obligations is not None,
                remaining=1 if parsed.max_obligations is not None else 0,
            ),
            prior_checkpoint_receipt_sha256=None,
            admission_receipt_sha256=None,
        )
        parsed.receipt.parent.mkdir(parents=True, exist_ok=True)
        parsed.receipt.write_text(receipt.model_dump_json(), encoding="utf-8")
        return 0

    monkeypatch.setattr(rehearsal_cli.document_cli, "main", document_main)

    artifact, evidence, stage_complete = rehearsal_cli._run_population(
        plan=plan,
        stage=RehearsalStage.DOCUMENT,
        prior=upgrade,
        history=(),
        ordinal=1,
        database_before=DatabaseFileState.from_path(database),
    )

    assert len(calls) == 1
    assert "--apply" not in calls[0]
    assert artifact == evidence.operator_receipt
    assert evidence.mode == "dry_run"
    assert evidence.exit_code == 0
    assert stage_complete is False


@pytest.mark.parametrize(
    ("stage", "cursor_flag", "cli_name"),
    [
        (RehearsalStage.DOCUMENT, "--after-obligation-id", "document_cli"),
        (RehearsalStage.ONTOLOGY, "--after-observation-id", "ontology_cli"),
        (RehearsalStage.CANONICAL, "--after-cell-id", "canonical_cli"),
        (RehearsalStage.LATEST, "--after-scope-id", "latest_cli"),
    ],
)
def test_population_dispatcher_preserves_resume_cursor_across_two_dry_apply_batches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: RehearsalStage,
    cursor_flag: str,
    cli_name: str,
) -> None:
    plan, _plan_path = _plan(tmp_path)
    database = Path(plan.database_path)
    prerequisite = tmp_path / "prerequisite.json"
    prerequisite.write_text("{}", encoding="utf-8")
    prerequisite_artifact = ArtifactCommitment.from_path(prerequisite)

    def stage_artifact(
        _history: tuple[tuple[ArtifactCommitment, RehearsalCheckpoint], ...],
        _stage: RehearsalStage,
    ) -> ArtifactCommitment:
        return prerequisite_artifact

    monkeypatch.setattr(rehearsal_cli, "_stage_artifact", stage_artifact)
    calls: list[list[str]] = []
    exits = iter((0, 3, 0, 0))

    def operator_main(argv: list[str] | None = None) -> int:
        assert argv is not None
        calls.append(argv)
        output = Path(argv[argv.index("--receipt") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"call": len(calls)}), encoding="utf-8")
        return next(exits)

    monkeypatch.setattr(getattr(rehearsal_cli, cli_name), "main", operator_main)

    def load_receipt(
        path: Path,
        _model_type: type[BaseModel],
    ) -> tuple[ArtifactCommitment, object, object]:
        snapshot, _payload = read_stable_artifact(path)
        apply = "--apply" in calls[-1]
        terminal = len(calls) == 4
        checkpoint = SimpleNamespace(
            last_processing_obligation_revision_id=(None if terminal else "cursor-1")
        )
        result = SimpleNamespace(
            checkpoint=checkpoint,
            last_observation_id=(None if terminal else "cursor-1"),
            last_canonical_metric_cell_id=(None if terminal else "cursor-1"),
            processed_scope_ids=(() if terminal else ("cursor-1",)),
        )
        receipt = SimpleNamespace(
            request=SimpleNamespace(apply=apply),
            result=result,
            receipt_sha256="a" * 64,
        )
        return ArtifactCommitment.from_path(path), snapshot, receipt

    monkeypatch.setattr(rehearsal_cli, "_load_model", load_receipt)
    prior = RehearsalCheckpoint.model_construct(
        stage=RehearsalStage.UPGRADE,
        population_checkpoint=None,
    )
    evidences: list[PopulationCheckpointEvidence] = []
    for ordinal in range(1, 5):
        artifact, evidence, complete = rehearsal_cli._run_population(
            plan=plan,
            stage=stage,
            prior=prior,
            history=(),
            ordinal=ordinal,
            database_before=DatabaseFileState.from_path(database),
        )
        assert artifact == evidence.operator_receipt
        evidences.append(evidence)
        prior = RehearsalCheckpoint.model_construct(
            stage=stage,
            population_checkpoint=evidence,
        )
        assert complete is (ordinal == 4)

    assert [evidence.mode for evidence in evidences] == [
        "dry_run",
        "apply",
        "dry_run",
        "apply",
    ]
    assert cursor_flag not in calls[0]
    assert cursor_flag not in calls[1]
    assert calls[2][calls[2].index(cursor_flag) + 1] == "cursor-1"
    assert calls[3][calls[3].index(cursor_flag) + 1] == "cursor-1"
    assert evidences[1].admission_receipt == evidences[0].operator_receipt
    assert evidences[2].prior_operator_receipt == evidences[1].operator_receipt
    assert evidences[3].admission_receipt == evidences[2].operator_receipt


def test_semantic_stage_requires_typed_generator_request(tmp_path: Path) -> None:
    plan, _plan_path = _plan(tmp_path)

    with pytest.raises(ValueError, match="typed qualification request"):
        rehearsal_cli._run_nonpopulation(
            plan=plan,
            stage=RehearsalStage.SEMANTIC,
            history=(),
            ordinal=1,
            stage_evidence=None,
            activation_requirements=None,
        )


def test_semantic_stage_generates_and_publishes_authoritative_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, _plan_path = _plan(tmp_path)
    index_root = tmp_path / "indexes"
    runtime_root = tmp_path / "runtime"
    index_root.mkdir()
    runtime_root.mkdir()
    request = SemanticQualificationRequest(
        index_root=index_root.resolve(),
        runtime_root=runtime_root.resolve(),
        exact_row_cap=100,
        fact_canary_limit=10,
        max_fact_canary_milliseconds=1_000,
        max_issuer_qualification_milliseconds=1_000,
    )
    request_path = tmp_path / "semantic-request.json"
    request_path.write_text(request.model_dump_json(), encoding="utf-8")
    observed: dict[str, object] = {}

    def generate(**kwargs: object) -> SimpleNamespace:
        observed.update(kwargs)
        return SimpleNamespace(model_dump_json=lambda: '{"semantic":"ready"}')

    monkeypatch.setattr(rehearsal_cli, "generate_semantic_qualification_evidence", generate)

    artifact = rehearsal_cli._run_nonpopulation(
        plan=plan,
        stage=RehearsalStage.SEMANTIC,
        history=(),
        ordinal=1,
        stage_evidence=request_path,
        activation_requirements=None,
    )

    assert Path(artifact.path).read_text(encoding="utf-8").strip() == '{"semantic":"ready"}'
    assert observed["database_path"] == Path(plan.database_path)
    assert observed["registry_path"] == Path(plan.production_scope_registry)
    assert observed["request"] == request
    assert observed["request_artifact"] == ArtifactCommitment.from_path(request_path)


@pytest.mark.parametrize(
    ("stage", "label"),
    [
        (RehearsalStage.RESTORE, "restore"),
        (RehearsalStage.PERFORMANCE, "performance"),
        (RehearsalStage.SEMANTIC, "semantic"),
    ],
)
def test_authoritative_stage_refuses_uncheckpointed_orphan_output(
    tmp_path: Path,
    stage: RehearsalStage,
    label: str,
) -> None:
    plan, _plan_path = _plan(tmp_path)
    output = Path(plan.evidence_directory) / f"0001-{label}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("{}", encoding="utf-8")
    stage_evidence = tmp_path / "performance-request.json"
    stage_evidence.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="uncheckpointed"):
        rehearsal_cli._run_nonpopulation(
            plan=plan,
            stage=stage,
            history=(),
            ordinal=1,
            stage_evidence=(
                stage_evidence
                if stage in {RehearsalStage.PERFORMANCE, RehearsalStage.SEMANTIC}
                else None
            ),
            activation_requirements=None,
        )


def test_outer_receipt_refuses_configured_live_sidecars(tmp_path: Path) -> None:
    plan, _plan_path = _plan(tmp_path)
    live_wal = tmp_path / "data" / "portfolio.db-wal"

    with pytest.raises(ValueError, match="aliases a protected artifact"):
        rehearsal_cli._safe_outer_receipt(live_wal, plan=plan, inputs=())


def test_ledger_replay_requires_document_receipt_to_match_current_planes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, _plan_path = _plan(tmp_path)
    receipt_path = tmp_path / "document-apply.json"
    request = DocumentProcessingPopulationRequest(
        cutoff_at=NOW,
        operation_recorded_at=NOW,
        apply=True,
        input_commitment_sha256="b" * 64,
        plan_commitment_sha256="c" * 64,
    )
    receipt = build_document_processing_receipt(
        database_path=plan.database_path,
        database_instance_id="database-instance:" + "1" * 32,
        alembic_revision=plan.expected_target_revision,
        request=request,
        result=receipt_result(mode="apply"),
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256="e" * 64,
    )
    receipt_path.write_text(receipt.model_dump_json(), encoding="utf-8")
    population = PopulationCheckpointEvidence.model_construct(
        operator="document",
        mode="apply",
        operator_receipt=ArtifactCommitment.from_path(receipt_path),
        operator_receipt_sha256=receipt.receipt_sha256,
    )
    checkpoint = RehearsalCheckpoint.model_construct(
        stage=RehearsalStage.DOCUMENT,
        population_checkpoint=population,
    )

    def connect_stub(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        return sqlite3.connect(":memory:")

    def load_stub(
        _conn: sqlite3.Connection,
        _operation_id: str,
    ) -> object:
        return receipt

    monkeypatch.setattr(rehearsal_cli, "connect_sqlite", connect_stub)
    monkeypatch.setattr(rehearsal_cli, "load_document_processing_receipt", load_stub)
    verified: list[str] = []

    def verify_current(_conn: sqlite3.Connection, stored: object) -> None:
        assert stored == receipt
        verified.append(receipt.receipt_sha256)

    monkeypatch.setattr(
        rehearsal_cli,
        "verify_document_processing_receipt_current",
        verify_current,
    )

    assert rehearsal_cli._ledger_receipt_sha256(Path(plan.database_path), checkpoint) == (
        receipt.receipt_sha256
    )
    assert verified == [receipt.receipt_sha256]


def test_exact_replay_generator_replays_every_population_receipt_and_matches_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, _plan_path = _plan(tmp_path)
    database = Path(plan.database_path)
    history: list[tuple[ArtifactCommitment, RehearsalCheckpoint]] = []
    expected_shas: list[str] = []
    for ordinal, stage in enumerate(
        (
            RehearsalStage.DOCUMENT,
            RehearsalStage.ONTOLOGY,
            RehearsalStage.CANONICAL,
            RehearsalStage.LATEST,
        ),
        start=1,
    ):
        dry_path = tmp_path / f"{stage.value}-dry.json"
        dry_path.write_text(json.dumps({"stage": stage.value, "mode": "dry"}), encoding="utf-8")
        dry_artifact = ArtifactCommitment.from_path(dry_path)
        dry_population = PopulationCheckpointEvidence.model_construct(
            operator=stage.value,
            mode="dry_run",
            exit_code=0,
            operator_receipt=dry_artifact,
            operator_receipt_sha256="f" * 64,
        )
        dry_checkpoint_path = tmp_path / f"checkpoint-{ordinal}-dry.json"
        dry_checkpoint_path.write_text("{}", encoding="utf-8")
        history.append(
            (
                ArtifactCommitment.from_path(dry_checkpoint_path),
                RehearsalCheckpoint.model_construct(
                    stage=stage,
                    population_checkpoint=dry_population,
                ),
            )
        )
        operator_path = tmp_path / f"{stage.value}.json"
        operator_path.write_text(json.dumps({"stage": stage.value}), encoding="utf-8")
        operator_artifact = ArtifactCommitment.from_path(operator_path)
        receipt_sha = f"{ordinal:x}" * 64
        receipt_sha = receipt_sha[:64]
        expected_shas.append(receipt_sha)
        population = PopulationCheckpointEvidence.model_construct(
            operator=stage.value,
            mode="apply",
            exit_code=0,
            request_cursor=None,
            result_cursor=None,
            operator_receipt=operator_artifact,
            operator_receipt_sha256=receipt_sha,
            prior_operator_receipt=None,
            prior_operator_receipt_sha256=None,
            admission_receipt=dry_artifact,
            admission_receipt_sha256="f" * 64,
            database_before=DatabaseFileState.from_path(database),
            database_after=DatabaseFileState.from_path(database),
        )
        checkpoint_path = tmp_path / f"checkpoint-{ordinal}.json"
        checkpoint_path.write_text("{}", encoding="utf-8")
        history.append(
            (
                ArtifactCommitment.from_path(checkpoint_path),
                RehearsalCheckpoint.model_construct(
                    stage=stage,
                    population_checkpoint=population,
                ),
            )
        )
    replayed: list[str] = []

    def replay(
        _plan: RehearsalPlan,
        _history: tuple[tuple[ArtifactCommitment, RehearsalCheckpoint], ...],
        checkpoint: RehearsalCheckpoint,
    ) -> None:
        assert checkpoint.population_checkpoint is not None
        replayed.append(checkpoint.population_checkpoint.operator)

    monkeypatch.setattr(rehearsal_cli, "_replay_population_checkpoint", replay)

    def ledger_receipt_sha256(
        _database: Path,
        checkpoint: RehearsalCheckpoint,
    ) -> str:
        assert checkpoint.population_checkpoint is not None
        return checkpoint.population_checkpoint.operator_receipt_sha256

    monkeypatch.setattr(
        rehearsal_cli,
        "_ledger_receipt_sha256",
        ledger_receipt_sha256,
    )

    evidence = rehearsal_cli._generate_exact_replay_evidence(
        plan=plan,
        history=tuple(history),
    )

    assert replayed == ["document", "ontology", "canonical", "latest"]
    assert evidence.operator_receipt_sha256s == tuple(expected_shas)
    assert evidence.database_sha256_before == evidence.database_sha256_after
    assert evidence.database_ledger_match_count == 4
    assert evidence.no_clobber_replay_count == 4


def test_exact_replay_generator_fails_if_operator_changes_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, _plan_path = _plan(tmp_path)
    database = Path(plan.database_path)
    history_items: list[tuple[ArtifactCommitment, RehearsalCheckpoint]] = []
    for stage in (
        RehearsalStage.DOCUMENT,
        RehearsalStage.ONTOLOGY,
        RehearsalStage.CANONICAL,
        RehearsalStage.LATEST,
    ):
        operator_path = tmp_path / f"{stage.value}.json"
        operator_path.write_text("{}", encoding="utf-8")
        operator_artifact = ArtifactCommitment.from_path(operator_path)
        population = PopulationCheckpointEvidence.model_construct(
            operator=stage.value,
            mode="apply",
            exit_code=0,
            operator_receipt=operator_artifact,
            operator_receipt_sha256="a" * 64,
        )
        checkpoint_path = tmp_path / f"checkpoint-{stage.value}.json"
        checkpoint_path.write_text("{}", encoding="utf-8")
        history_items.append(
            (
                ArtifactCommitment.from_path(checkpoint_path),
                RehearsalCheckpoint.model_construct(
                    stage=stage,
                    population_checkpoint=population,
                ),
            )
        )
    history = tuple(history_items)

    def replay(
        _plan: RehearsalPlan,
        _history: tuple[tuple[ArtifactCommitment, RehearsalCheckpoint], ...],
        _checkpoint: RehearsalCheckpoint,
    ) -> None:
        database.write_bytes(b"changed")

    monkeypatch.setattr(rehearsal_cli, "_replay_population_checkpoint", replay)

    with pytest.raises(ValueError, match="changed the rehearsal database"):
        rehearsal_cli._generate_exact_replay_evidence(plan=plan, history=history)


def test_performance_stage_generates_full_cohort_noop_before_measurement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, _plan_path = _plan(tmp_path)
    benchmark = tmp_path / "benchmark.json"
    benchmark.write_text("{}", encoding="utf-8")
    request = CandidatePerformanceRequest(
        synthetic_benchmark_report=benchmark.resolve(),
        read_samples=3,
        read_limit=10,
        max_fact_read_p95_milliseconds=100,
        max_narrative_read_p95_milliseconds=100,
        max_history_scale_ratio=1.5,
    )
    request_path = tmp_path / "performance-request.json"
    request_path.write_text(request.model_dump_json(), encoding="utf-8")
    terminal_path = tmp_path / "terminal-eligibility.json"
    terminal_path.write_text("{}", encoding="utf-8")
    terminal_artifact = ArtifactCommitment.from_path(terminal_path)

    def stage_artifact(
        _history: tuple[tuple[ArtifactCommitment, RehearsalCheckpoint], ...],
        stage: RehearsalStage,
    ) -> ArtifactCommitment:
        if stage is not RehearsalStage.TERMINAL_ELIGIBILITY:
            raise AssertionError(stage)
        return terminal_artifact

    monkeypatch.setattr(
        rehearsal_cli,
        "_stage_artifact",
        stage_artifact,
    )
    real_load = rehearsal_cli._load_model
    no_op_receipt = LatestGovernedPopulationReceipt.model_construct()

    def load_model(path: Path, model_type: type[BaseModel]) -> tuple[object, object, object]:
        if model_type is BoundLatestStateEligibilityManifest:
            snapshot, _payload = read_stable_artifact(path)
            return (
                ArtifactCommitment.from_path(path),
                snapshot,
                BoundLatestStateEligibilityManifest.model_construct(
                    expected_scope_ids=("scope-a", "scope-b")
                ),
            )
        if model_type is LatestGovernedPopulationReceipt:
            snapshot, _payload = read_stable_artifact(path)
            return ArtifactCommitment.from_path(path), snapshot, no_op_receipt
        return real_load(path, model_type)

    monkeypatch.setattr(rehearsal_cli, "_load_model", load_model)
    calls: list[list[str]] = []

    def latest_main(argv: list[str] | None = None) -> int:
        assert argv is not None
        calls.append(argv)
        receipt = Path(argv[argv.index("--receipt") + 1])
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text("{}", encoding="utf-8")
        return 0

    monkeypatch.setattr(rehearsal_cli.latest_cli, "main", latest_main)
    captured: dict[str, object] = {}

    def generate(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(model_dump_json=lambda: '{"performance":true}')

    monkeypatch.setattr(rehearsal_cli, "generate_candidate_performance_evidence", generate)

    artifact = rehearsal_cli._run_nonpopulation(
        plan=plan,
        stage=RehearsalStage.PERFORMANCE,
        history=(),
        ordinal=12,
        stage_evidence=request_path,
        activation_requirements=None,
    )

    assert artifact.verify()
    assert calls[0][calls[0].index("--max-scopes") + 1] == "2"
    assert "--apply" not in calls[0]
    assert captured["production_scope_ids"] == ("scope-a", "scope-b")
    assert captured["no_op_receipt"] is no_op_receipt
