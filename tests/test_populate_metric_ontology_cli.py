from __future__ import annotations

import hashlib
import sqlite3
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import provenance.population_metric_ontology as population
from execution import populate_metric_ontology as cli
from provenance.population_metric_ontology import (
    MetricOntologyOperationReceipt,
    MetricOntologyPopulationRequest,
    MetricOntologyPopulationResult,
    build_metric_ontology_receipt,
    persist_metric_ontology_receipt,
    verify_metric_ontology_receipt_current,
    verify_metric_ontology_receipt_current_result,
)

_STAMP = datetime(2026, 7, 29, tzinfo=UTC)
_DATABASE_INSTANCE_ID = "database-instance:" + "1" * 32
_HEAD_REVISION = "0269_latest_governed_population_receipt_v2"


def _artifact_sha(receipt: MetricOntologyOperationReceipt) -> str:
    return hashlib.sha256((receipt.model_dump_json() + "\n").encode()).hexdigest()


def _database(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE alembic_version (version_num TEXT NOT NULL);
            INSERT INTO alembic_version VALUES ('0267_source_definition_taxonomy_identity');
            CREATE TABLE database_runtime_identity (
                singleton INTEGER PRIMARY KEY,
                database_instance_id TEXT NOT NULL UNIQUE
            );
            INSERT INTO database_runtime_identity VALUES
                (1, 'database-instance:11111111111111111111111111111111');
            CREATE TABLE metric_ontology_operation_ledger (
                operation_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                database_instance_id TEXT NOT NULL,
                request_sha256 TEXT NOT NULL,
                result_sha256 TEXT NOT NULL,
                receipt_sha256 TEXT NOT NULL UNIQUE,
                receipt_json TEXT NOT NULL
            );
            CREATE TABLE operation_probe (value TEXT NOT NULL);
            """
        )
        conn.execute("UPDATE alembic_version SET version_num=?", (_HEAD_REVISION,))


def _result(
    *,
    mode: str = "dry_run",
    outcome: str = "planned",
    last_observation_id: str | None = None,
    missing: int = 0,
    post_state: str = "d" * 64,
) -> MetricOntologyPopulationResult:
    return MetricOntologyPopulationResult.model_validate(
        {
            "mode": mode,
            "phase": "all",
            "outcome": outcome,
            "reason_codes": (
                ()
                if missing == 0
                else ("ontology_assertions_incomplete", "ontology_bindings_incomplete")
            ),
            "snapshot_eligible": missing == 0 and outcome != "checkpoint",
            "source_cell_count": 2,
            "source_observation_count": 2,
            "metric_count": 2,
            "source_component_count": 2,
            "canonical_cell_count": 2,
            "assertion_count": 2 - missing,
            "binding_count": 2 - missing,
            "missing_assertion_count": missing,
            "missing_binding_count": missing,
            "processed_observation_count": 2 - missing,
            "last_observation_id": last_observation_id,
            "remaining_observation_count": 1 if outcome == "checkpoint" else 0,
            "safe_to_seal": missing == 0 and outcome != "checkpoint",
            "snapshot_id": None,
            "policy_config_sha256": "a" * 64,
            "plan_commitment_sha256": "b" * 64,
            "input_commitment_sha256": "c" * 64,
            "post_state_commitment_sha256": post_state,
            "output_commitment_sha256": post_state,
        }
    )


def _planned_receipt(database: Path) -> MetricOntologyOperationReceipt:
    return build_metric_ontology_receipt(
        database_path=str(database.resolve()),
        database_instance_id=_DATABASE_INSTANCE_ID,
        alembic_revision=_HEAD_REVISION,
        request=MetricOntologyPopulationRequest(
            knowledge_cutoff=_STAMP,
            operation_recorded_at=_STAMP,
        ),
        result=_result(),
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256=None,
    )


def test_loader_rejects_reformatted_prior_receipt(tmp_path: Path) -> None:
    receipt = _planned_receipt(tmp_path / "candidate.db")
    path = tmp_path / "reformatted-prior.json"
    path.write_text(receipt.model_dump_json() + " \n", encoding="utf-8")

    with pytest.raises(cli.ImmutableArtifactConflictError, match="canonically serialized"):
        cli.load_metric_ontology_receipt_artifact(path)


def test_apply_request_is_derived_only_from_exact_admission(tmp_path: Path) -> None:
    database = tmp_path / "candidate.db"
    receipt = _planned_receipt(database)

    admitted = cli.admitted_apply_request(
        receipt,
        database=database,
        knowledge_cutoff=_STAMP,
        operation_recorded_at=_STAMP,
        phase="all",
        after_observation_id=None,
        max_observations=None,
    )

    assert admitted.apply is True
    assert admitted.input_commitment_sha256 == receipt.result.input_commitment_sha256
    assert admitted.plan_commitment_sha256 == receipt.result.plan_commitment_sha256
    with pytest.raises(ValueError, match="database"):
        cli.admitted_apply_request(
            receipt,
            database=tmp_path / "other.db",
            knowledge_cutoff=_STAMP,
            operation_recorded_at=_STAMP,
            phase="all",
            after_observation_id=None,
            max_observations=None,
        )


def test_receipt_destination_cannot_alias_database_or_sidecars(tmp_path: Path) -> None:
    database = tmp_path / "candidate.db"
    database.write_bytes(b"db")
    with pytest.raises(ValueError, match="protected"):
        cli.validate_receipt_path(database, database=database, protected_receipts=())
    with pytest.raises(ValueError, match="protected"):
        cli.validate_receipt_path(
            Path(f"{database}-wal"),
            database=database,
            protected_receipts=(),
        )


def test_population_database_lock_rejects_hardlink_alias_to_portfolio(
    tmp_path: Path,
) -> None:
    portfolio = tmp_path / "portfolio.db"
    alias = tmp_path / "candidate.db"
    portfolio.write_bytes(b"sqlite")
    alias.write_bytes(b"candidate")

    resources = cli.population_database_lock_resources(alias, portfolio)

    assert "portfolio-db" in resources
    alias.unlink()
    alias.hardlink_to(portfolio)

    with pytest.raises(ValueError, match="aliases the portfolio database"):
        cli.validate_population_database_target(alias, portfolio)


def test_checkpoint_replay_uses_stable_universe_then_terminal_current_planes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "candidate.db"
    checkpoint = build_metric_ontology_receipt(
        database_path=str(database.resolve()),
        database_instance_id=_DATABASE_INSTANCE_ID,
        alembic_revision=_HEAD_REVISION,
        request=MetricOntologyPopulationRequest(
            knowledge_cutoff=_STAMP,
            operation_recorded_at=_STAMP,
            apply=True,
            max_observations=1,
            input_commitment_sha256="c" * 64,
            plan_commitment_sha256="b" * 64,
        ),
        result=_result(
            mode="apply",
            outcome="checkpoint",
            last_observation_id="observation-1",
        ).model_copy(update={"remaining_observation_count": 0}),
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256="e" * 64,
    )
    terminal_current = _result()

    verify_metric_ontology_receipt_current_result(
        checkpoint,
        terminal_current,
        historical_checkpoint=True,
    )
    with pytest.raises(ValueError, match="stable source universe"):
        verify_metric_ontology_receipt_current_result(
            checkpoint,
            terminal_current.model_copy(update={"source_observation_count": 3}),
            historical_checkpoint=True,
        )
    with pytest.raises(ValueError, match="current planes"):
        verify_metric_ontology_receipt_current_result(
            checkpoint,
            _result(
                outcome="checkpoint",
                last_observation_id="observation-1",
                missing=1,
                post_state="f" * 64,
            ),
        )

    terminal = build_metric_ontology_receipt(
        database_path=str(database.resolve()),
        database_instance_id=_DATABASE_INSTANCE_ID,
        alembic_revision=_HEAD_REVISION,
        request=MetricOntologyPopulationRequest(
            knowledge_cutoff=_STAMP,
            operation_recorded_at=_STAMP,
            apply=True,
            input_commitment_sha256="c" * 64,
            plan_commitment_sha256="b" * 64,
        ),
        result=_result(mode="apply", outcome="applied"),
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256="e" * 64,
    )
    verify_metric_ontology_receipt_current_result(terminal, terminal_current)
    with pytest.raises(ValueError, match="current planes"):
        verify_metric_ontology_receipt_current_result(
            terminal,
            terminal_current.model_copy(update={"post_state_commitment_sha256": "f" * 64}),
        )


def test_checkpoint_replay_requires_a_terminal_ledger_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    _database(database)
    checkpoint = build_metric_ontology_receipt(
        database_path=str(database.resolve()),
        database_instance_id=_DATABASE_INSTANCE_ID,
        alembic_revision=_HEAD_REVISION,
        request=MetricOntologyPopulationRequest(
            knowledge_cutoff=_STAMP,
            operation_recorded_at=_STAMP,
            apply=True,
            max_observations=1,
            input_commitment_sha256="c" * 64,
            plan_commitment_sha256="b" * 64,
        ),
        result=_result(
            mode="apply",
            outcome="checkpoint",
            last_observation_id="observation-1",
        ).model_copy(update={"remaining_observation_count": 0}),
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256="e" * 64,
    )

    def rolled_back_operator(
        _connection: sqlite3.Connection,
        _request: MetricOntologyPopulationRequest,
    ) -> MetricOntologyPopulationResult:
        return _result(
            outcome="checkpoint",
            last_observation_id="observation-1",
            missing=1,
            post_state="f" * 64,
        )

    monkeypatch.setattr(population, "populate_metric_ontology", rolled_back_operator)
    with sqlite3.connect(database) as connection:
        persist_metric_ontology_receipt(connection, checkpoint)
        with pytest.raises(ValueError, match="current planes"):
            verify_metric_ontology_receipt_current(connection, checkpoint)

        terminal = build_metric_ontology_receipt(
            database_path=str(database.resolve()),
            database_instance_id=_DATABASE_INSTANCE_ID,
            alembic_revision=_HEAD_REVISION,
            request=MetricOntologyPopulationRequest(
                knowledge_cutoff=_STAMP,
                operation_recorded_at=_STAMP,
                apply=True,
                input_commitment_sha256="c" * 64,
                plan_commitment_sha256="b" * 64,
            ),
            result=_result(mode="apply", outcome="applied"),
            prior_checkpoint_receipt_sha256=_artifact_sha(checkpoint),
            admission_receipt_sha256="e" * 64,
        )
        persist_metric_ontology_receipt(connection, terminal)

        def current_operator(
            _connection: sqlite3.Connection,
            _request: MetricOntologyPopulationRequest,
        ) -> MetricOntologyPopulationResult:
            return _result()

        monkeypatch.setattr(population, "populate_metric_ontology", current_operator)
        verify_metric_ontology_receipt_current(connection, checkpoint)


def test_terminal_ontology_replay_requires_canonical_checkpoint_parent(tmp_path: Path) -> None:
    database = tmp_path / "candidate.db"
    _database(database)

    def terminal(prior: str | None, admission: str = "e" * 64):
        return build_metric_ontology_receipt(
            database_path=str(database.resolve()),
            database_instance_id=_DATABASE_INSTANCE_ID,
            alembic_revision=_HEAD_REVISION,
            request=MetricOntologyPopulationRequest(
                knowledge_cutoff=_STAMP,
                operation_recorded_at=_STAMP,
                apply=True,
                input_commitment_sha256="c" * 64,
                plan_commitment_sha256="b" * 64,
            ),
            result=_result(mode="apply", outcome="applied"),
            prior_checkpoint_receipt_sha256=prior,
            admission_receipt_sha256=admission,
        )

    with sqlite3.connect(database) as connection:
        orphan = terminal("f" * 64)
        persist_metric_ontology_receipt(connection, orphan)
        with pytest.raises(ValueError, match="parent is missing"):
            verify_metric_ontology_receipt_current(connection, orphan)

        noncheckpoint = terminal(None)
        child = terminal(_artifact_sha(noncheckpoint))
        persist_metric_ontology_receipt(connection, noncheckpoint)
        persist_metric_ontology_receipt(connection, child)
        with pytest.raises(ValueError, match="parent is not a checkpoint"):
            verify_metric_ontology_receipt_current(connection, child)

        checkpoint = build_metric_ontology_receipt(
            database_path=str(database.resolve()),
            database_instance_id=_DATABASE_INSTANCE_ID,
            alembic_revision=_HEAD_REVISION,
            request=MetricOntologyPopulationRequest(
                knowledge_cutoff=_STAMP,
                operation_recorded_at=_STAMP,
                apply=True,
                max_observations=1,
                input_commitment_sha256="c" * 64,
                plan_commitment_sha256="b" * 64,
            ),
            result=_result(
                mode="apply",
                outcome="checkpoint",
                last_observation_id="observation-1",
            ).model_copy(update={"remaining_observation_count": 0}),
            prior_checkpoint_receipt_sha256=None,
            admission_receipt_sha256="a" * 64,
        )
        first = terminal(_artifact_sha(checkpoint), "1" * 64)
        sibling = terminal(_artifact_sha(checkpoint), "2" * 64)
        persist_metric_ontology_receipt(connection, checkpoint)
        persist_metric_ontology_receipt(connection, first)
        persist_metric_ontology_receipt(connection, sibling)
        with pytest.raises(ValueError, match="successor is ambiguous"):
            verify_metric_ontology_receipt_current(connection, first)


def test_dry_run_receipt_is_no_clobber_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    receipt_path = tmp_path / "dry-run.json"
    _database(database)

    def run_stub(
        connection: sqlite3.Connection,
        _args: Namespace,
        _request: MetricOntologyPopulationRequest,
    ) -> MetricOntologyPopulationResult:
        assert connection.in_transaction
        return _result()

    monkeypatch.setattr(cli, "_run_operator", run_stub)
    argv = [
        "--db",
        str(database),
        "--cutoff-at",
        "2026-07-29T00:00:00Z",
        "--recorded-at",
        "2026-07-29T00:00:00Z",
        "--receipt",
        str(receipt_path),
    ]

    assert cli.main(argv) == 0
    original = receipt_path.read_bytes()
    assert cli.main(argv) == 0
    assert receipt_path.read_bytes() == original
    receipt_path.write_text('{"tampered":true}', encoding="utf-8")
    assert cli.main(argv) == 2


def test_apply_ledger_is_atomic_and_exact_replay_does_not_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    admission_path = tmp_path / "admission.json"
    receipt_path = tmp_path / "apply.json"
    _database(database)
    admission_path.write_bytes((_planned_receipt(database).model_dump_json() + "\n").encode())
    calls = 0

    def run_stub(
        connection: sqlite3.Connection,
        _args: Namespace,
        request: MetricOntologyPopulationRequest,
    ) -> MetricOntologyPopulationResult:
        nonlocal calls
        calls += 1
        assert connection.in_transaction
        if request.apply:
            connection.execute("INSERT INTO operation_probe VALUES ('applied')")
            return _result(mode="apply", outcome="applied")
        return _result()

    def verify_current(
        connection: sqlite3.Connection,
        receipt: MetricOntologyOperationReceipt,
    ) -> None:
        current = run_stub(
            connection,
            Namespace(),
            receipt.request.model_copy(
                update={
                    "apply": False,
                    "input_commitment_sha256": None,
                    "plan_commitment_sha256": None,
                }
            ),
        )
        verify_metric_ontology_receipt_current_result(receipt, current)

    monkeypatch.setattr(cli, "_run_operator", run_stub)
    monkeypatch.setattr(cli, "verify_metric_ontology_receipt_current", verify_current)
    argv = [
        "--db",
        str(database),
        "--cutoff-at",
        "2026-07-29T00:00:00Z",
        "--recorded-at",
        "2026-07-29T00:00:00Z",
        "--apply",
        "--admission-receipt",
        str(admission_path),
        "--receipt",
        str(receipt_path),
    ]

    assert cli.main(argv) == 0
    original = receipt_path.read_bytes()
    assert cli.main(argv) == 0
    assert receipt_path.read_bytes() == original
    assert calls == 2
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM operation_probe").fetchone()[0] == 1
        stored = conn.execute(
            "SELECT receipt_json FROM metric_ontology_operation_ledger"
        ).fetchone()
    assert stored is not None
    assert MetricOntologyOperationReceipt.model_validate_json(str(stored[0])).request.apply


def test_apply_exact_replay_refuses_rolled_back_ontology_plane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    admission_path = tmp_path / "admission.json"
    receipt_path = tmp_path / "apply.json"
    _database(database)
    admission_path.write_bytes((_planned_receipt(database).model_dump_json() + "\n").encode())

    def run_stub(
        connection: sqlite3.Connection,
        _args: Namespace,
        request: MetricOntologyPopulationRequest,
    ) -> MetricOntologyPopulationResult:
        if request.apply:
            connection.execute("INSERT INTO operation_probe VALUES ('applied')")
            return _result(mode="apply", outcome="applied")
        if connection.execute("SELECT COUNT(*) FROM operation_probe").fetchone()[0] == 0:
            return _result(post_state="f" * 64)
        return _result()

    def verify_current(
        connection: sqlite3.Connection,
        receipt: MetricOntologyOperationReceipt,
    ) -> None:
        current = run_stub(
            connection,
            Namespace(),
            receipt.request.model_copy(
                update={
                    "apply": False,
                    "input_commitment_sha256": None,
                    "plan_commitment_sha256": None,
                }
            ),
        )
        verify_metric_ontology_receipt_current_result(receipt, current)

    monkeypatch.setattr(cli, "_run_operator", run_stub)
    monkeypatch.setattr(cli, "verify_metric_ontology_receipt_current", verify_current)
    argv = [
        "--db",
        str(database),
        "--cutoff-at",
        "2026-07-29T00:00:00Z",
        "--recorded-at",
        "2026-07-29T00:00:00Z",
        "--apply",
        "--admission-receipt",
        str(admission_path),
        "--receipt",
        str(receipt_path),
    ]

    assert cli.main(argv) == 0
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM operation_probe")
        assert (
            connection.execute("SELECT COUNT(*) FROM metric_ontology_operation_ledger").fetchone()[
                0
            ]
            == 1
        )

    assert cli.main(argv) == 2


def test_stored_ontology_checkpoint_current_verification_ignores_mode_only_reason(
    tmp_path: Path,
) -> None:
    database = tmp_path / "candidate.db"
    request = MetricOntologyPopulationRequest(
        knowledge_cutoff=_STAMP,
        operation_recorded_at=_STAMP,
        apply=True,
        max_observations=1,
        input_commitment_sha256="c" * 64,
        plan_commitment_sha256="b" * 64,
    )
    receipt = build_metric_ontology_receipt(
        database_path=str(database.resolve()),
        database_instance_id=_DATABASE_INSTANCE_ID,
        alembic_revision=_HEAD_REVISION,
        request=request,
        result=_result(
            mode="apply",
            outcome="checkpoint",
            last_observation_id="observation-1",
            missing=1,
        ).model_copy(
            update={
                "reason_codes": (
                    "bounded_population_checkpoint",
                    "ontology_assertions_incomplete",
                    "ontology_bindings_incomplete",
                )
            }
        ),
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256="e" * 64,
    )
    verify_metric_ontology_receipt_current_result(
        receipt,
        _result(missing=1),
        historical_checkpoint=True,
    )


def test_bounded_ontology_receipt_cannot_masquerade_as_terminal(
    tmp_path: Path,
) -> None:
    database = tmp_path / "candidate.db"
    request = MetricOntologyPopulationRequest(
        knowledge_cutoff=_STAMP,
        operation_recorded_at=_STAMP,
        apply=True,
        max_observations=1,
        input_commitment_sha256="c" * 64,
        plan_commitment_sha256="b" * 64,
    )
    with pytest.raises(ValueError, match="bounded ontology apply"):
        build_metric_ontology_receipt(
            database_path=str(database.resolve()),
            database_instance_id=_DATABASE_INSTANCE_ID,
            alembic_revision=_HEAD_REVISION,
            request=request,
            result=_result(mode="apply", outcome="applied"),
            prior_checkpoint_receipt_sha256=None,
            admission_receipt_sha256="e" * 64,
        )


def test_checkpoint_successor_rejects_post_state_drift(tmp_path: Path) -> None:
    database = tmp_path / "candidate.db"
    request = MetricOntologyPopulationRequest(
        knowledge_cutoff=_STAMP,
        operation_recorded_at=_STAMP,
        apply=True,
        max_observations=1,
        input_commitment_sha256="c" * 64,
        plan_commitment_sha256="b" * 64,
    )
    checkpoint = build_metric_ontology_receipt(
        database_path=str(database.resolve()),
        database_instance_id=_DATABASE_INSTANCE_ID,
        alembic_revision="0265_metric_ontology_operation_ledger",
        request=request,
        result=_result(
            mode="apply",
            outcome="checkpoint",
            last_observation_id="observation-1",
            missing=1,
        ),
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256="e" * 64,
    )
    successor = _result(missing=1, post_state="f" * 64)

    with pytest.raises(ValueError, match="state changed"):
        cli.validate_checkpoint_successor(
            checkpoint,
            request=request.model_copy(
                update={"apply": False, "after_observation_id": "observation-1"}
            ),
            result=successor,
            alembic_revision="0265_metric_ontology_operation_ledger",
        )


def test_terminal_bounded_apply_remains_checkpoint_until_unbounded_seal(tmp_path: Path) -> None:
    database = tmp_path / "candidate.db"
    receipt = build_metric_ontology_receipt(
        database_path=str(database.resolve()),
        database_instance_id=_DATABASE_INSTANCE_ID,
        alembic_revision="0267_source_definition_taxonomy_identity",
        request=MetricOntologyPopulationRequest(
            knowledge_cutoff=_STAMP,
            operation_recorded_at=_STAMP,
            apply=True,
            after_observation_id="observation-final",
            max_observations=10,
            input_commitment_sha256="c" * 64,
            plan_commitment_sha256="b" * 64,
        ),
        result=_result(
            mode="apply",
            outcome="checkpoint",
            last_observation_id="observation-final",
        ).model_copy(update={"remaining_observation_count": 0}),
        prior_checkpoint_receipt_sha256="e" * 64,
        admission_receipt_sha256="f" * 64,
    )

    assert receipt.result.remaining_observation_count == 0
    assert receipt.result.safe_to_seal is False
    assert receipt.outcome == "checkpoint"


def test_completed_checkpoint_can_dry_run_and_apply_unbounded_sealing_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    _database(database)
    prior_path = tmp_path / "prior.json"
    admission_path = tmp_path / "seal-plan.json"
    apply_path = tmp_path / "seal-apply.json"
    prior = build_metric_ontology_receipt(
        database_path=str(database.resolve()),
        database_instance_id=_DATABASE_INSTANCE_ID,
        alembic_revision=_HEAD_REVISION,
        request=MetricOntologyPopulationRequest(
            knowledge_cutoff=_STAMP,
            operation_recorded_at=_STAMP,
            apply=True,
            max_observations=1,
            input_commitment_sha256="c" * 64,
            plan_commitment_sha256="b" * 64,
        ),
        result=_result(
            mode="apply",
            outcome="checkpoint",
            last_observation_id="observation-1",
        ).model_copy(update={"remaining_observation_count": 0}),
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256="e" * 64,
    )
    prior_path.write_bytes((prior.model_dump_json() + "\n").encode())
    with sqlite3.connect(database) as connection:
        persist_metric_ontology_receipt(connection, prior)

    def run_stub(
        _connection: sqlite3.Connection,
        _args: Namespace,
        request: MetricOntologyPopulationRequest,
    ) -> MetricOntologyPopulationResult:
        return _result(
            mode="apply" if request.apply else "dry_run",
            outcome="applied" if request.apply else "planned",
        )

    monkeypatch.setattr(cli, "_run_operator", run_stub)
    base = [
        "--db",
        str(database),
        "--cutoff-at",
        "2026-07-29T00:00:00Z",
        "--recorded-at",
        "2026-07-29T00:00:00Z",
        "--phase",
        "all",
        "--prior-checkpoint-receipt",
        str(prior_path),
    ]

    assert cli.main([*base, "--receipt", str(admission_path)]) == 0
    admission = MetricOntologyOperationReceipt.model_validate_json(
        admission_path.read_text(encoding="utf-8")
    )
    assert admission.prior_checkpoint_receipt_sha256 == _artifact_sha(prior)
    assert (
        cli.main(
            [
                *base,
                "--apply",
                "--admission-receipt",
                str(admission_path),
                "--receipt",
                str(apply_path),
            ]
        )
        == 0
    )
    applied = MetricOntologyOperationReceipt.model_validate_json(
        apply_path.read_text(encoding="utf-8")
    )
    assert applied.outcome == "complete"
    assert applied.prior_checkpoint_receipt_sha256 == _artifact_sha(prior)
