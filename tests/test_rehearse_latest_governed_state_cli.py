# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
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
from provenance.latest_state_rehearsal import (
    ArtifactCommitment,
    DatabaseFileState,
    PopulationCheckpointEvidence,
    RehearsalCheckpoint,
    RehearsalPlan,
    RehearsalStage,
    verify_rehearsal_checkpoint,
)
from provenance.population_document_processing import (
    DocumentProcessingPopulationRequest,
    build_document_processing_receipt,
)
from runtime.job_runtime import JobAlreadyRunningError, JobLock
from tests.test_population_document_processing import receipt_result

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


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
        expected_target_revision="0269_latest_governed_population_receipt_v2",
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
            result=receipt_result(),
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


@pytest.mark.parametrize(
    "stage",
    [
        RehearsalStage.SEMANTIC,
        RehearsalStage.REPLAY,
        RehearsalStage.RESTORE,
        RehearsalStage.PERFORMANCE,
    ],
)
def test_terminal_stage_refuses_caller_authored_evidence_without_an_authoritative_generator(
    tmp_path: Path,
    stage: RehearsalStage,
) -> None:
    plan, _plan_path = _plan(tmp_path)
    supplied = tmp_path / f"fabricated-{stage.value}.json"
    supplied.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="no authoritative generator"):
        rehearsal_cli._run_nonpopulation(
            plan=plan,
            stage=stage,
            history=(),
            ordinal=1,
            stage_evidence=supplied,
            activation_requirements=None,
        )
