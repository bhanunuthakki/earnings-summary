from __future__ import annotations

import sqlite3
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from execution import populate_metric_ontology as cli
from provenance.population_metric_ontology import (
    MetricOntologyOperationReceipt,
    MetricOntologyPopulationRequest,
    MetricOntologyPopulationResult,
    build_metric_ontology_receipt,
)

_STAMP = datetime(2026, 7, 29, tzinfo=UTC)
_DATABASE_INSTANCE_ID = "database-instance:" + "1" * 32


def _database(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE alembic_version (version_num TEXT NOT NULL);
            INSERT INTO alembic_version VALUES ('0265_metric_ontology_operation_ledger');
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
        alembic_revision="0265_metric_ontology_operation_ledger",
        request=MetricOntologyPopulationRequest(
            knowledge_cutoff=_STAMP,
            operation_recorded_at=_STAMP,
        ),
        result=_result(),
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256=None,
    )


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
    admission_path.write_text(_planned_receipt(database).model_dump_json(), encoding="utf-8")
    calls = 0

    def run_stub(
        connection: sqlite3.Connection,
        _args: Namespace,
        _request: MetricOntologyPopulationRequest,
    ) -> MetricOntologyPopulationResult:
        nonlocal calls
        calls += 1
        assert connection.in_transaction
        connection.execute("INSERT INTO operation_probe VALUES ('applied')")
        return _result(mode="apply", outcome="applied")

    monkeypatch.setattr(cli, "_run_operator", run_stub)
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
    assert calls == 1
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM operation_probe").fetchone()[0] == 1
        stored = conn.execute(
            "SELECT receipt_json FROM metric_ontology_operation_ledger"
        ).fetchone()
    assert stored is not None
    assert MetricOntologyOperationReceipt.model_validate_json(str(stored[0])).request.apply


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
