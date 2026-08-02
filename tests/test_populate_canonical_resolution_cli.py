from __future__ import annotations

import hashlib
import sqlite3
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import provenance.population_canonical_resolution as population
from execution import populate_canonical_resolution as cli
from provenance.population_canonical_resolution import (
    CanonicalResolutionCheckpoint,
    CanonicalResolutionOperationReceipt,
    CanonicalResolutionPopulationRequest,
    CanonicalResolutionPopulationResult,
    build_canonical_resolution_receipt,
    persist_canonical_resolution_receipt,
    verify_canonical_resolution_receipt_current,
    verify_canonical_resolution_receipt_current_result,
)
from provenance.population_document_processing import (
    DocumentProcessingPopulationRequest,
    persist_document_processing_receipt,
)
from tests.test_population_document_processing import (
    build_test_document_processing_receipt as build_document_processing_receipt,
)
from tests.test_population_document_processing import (
    receipt_result,
)

_STAMP = datetime(2026, 7, 29, tzinfo=UTC)
_DATABASE_INSTANCE_ID = "database-instance:" + "1" * 32
_HEAD_REVISION = "0269_latest_governed_population_receipt_v2"


def _artifact_sha(receipt: CanonicalResolutionOperationReceipt) -> str:
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
            CREATE TABLE document_processing_operation_ledger (
                operation_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                database_instance_id TEXT NOT NULL,
                request_sha256 TEXT NOT NULL,
                result_sha256 TEXT NOT NULL,
                receipt_sha256 TEXT NOT NULL UNIQUE,
                receipt_json TEXT NOT NULL
            );
            CREATE TABLE canonical_resolution_operation_ledger (
                operation_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                database_instance_id TEXT NOT NULL,
                document_prerequisite_receipt_sha256 TEXT NOT NULL,
                request_sha256 TEXT NOT NULL,
                result_sha256 TEXT NOT NULL,
                receipt_sha256 TEXT NOT NULL UNIQUE,
                receipt_json TEXT NOT NULL
            );
            CREATE TABLE operation_probe (value TEXT NOT NULL);
            """
        )
        conn.execute("UPDATE alembic_version SET version_num=?", (_HEAD_REVISION,))


def _document_prerequisite(database: Path, path: Path) -> None:
    receipt = build_document_processing_receipt(
        database_path=str(database.resolve()),
        database_instance_id=_DATABASE_INSTANCE_ID,
        alembic_revision=_HEAD_REVISION,
        request=DocumentProcessingPopulationRequest(
            cutoff_at=_STAMP,
            operation_recorded_at=_STAMP,
            apply=True,
            input_commitment_sha256="b" * 64,
            plan_commitment_sha256="c" * 64,
        ),
        result=receipt_result(mode="apply"),
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256="e" * 64,
    )
    with sqlite3.connect(database) as conn:
        persist_document_processing_receipt(conn, receipt)
    path.write_bytes((receipt.model_dump_json() + "\n").encode())


def test_loader_rejects_reformatted_prior_receipt(tmp_path: Path) -> None:
    database = tmp_path / "candidate.db"
    receipt = build_canonical_resolution_receipt(
        database_path=str(database.resolve()),
        database_instance_id=_DATABASE_INSTANCE_ID,
        alembic_revision=_HEAD_REVISION,
        request=CanonicalResolutionPopulationRequest(
            cutoff_at=_STAMP,
            operation_recorded_at=_STAMP,
        ),
        result=_result(),
        document_prerequisite_receipt_sha256="f" * 64,
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256=None,
    )
    path = tmp_path / "reformatted-prior.json"
    path.write_text(receipt.model_dump_json() + " \n", encoding="utf-8")

    with pytest.raises(cli.ImmutableArtifactConflictError, match="canonically serialized"):
        cli.load_canonical_resolution_receipt_artifact(path)


def _result(
    *,
    mode: str = "dry_run",
    state: str = "planned",
    unresolved: int = 0,
    bounded: bool = False,
    last_cell: str | None = None,
    post_state: str = "d" * 64,
    resolution_plan: str = "a" * 64,
) -> CanonicalResolutionPopulationResult:
    return CanonicalResolutionPopulationResult.model_validate(
        {
            "mode": mode,
            "phase": "all",
            "state": state,
            "expected_cell_count": 1,
            "resolved_cell_count": 0 if mode == "dry_run" else 1 - unresolved,
            "unresolved_cell_count": 0 if mode == "dry_run" else unresolved,
            "retired_cell_count": 0,
            "planned_resolved_cell_count": 1 - unresolved,
            "planned_unresolved_cell_count": unresolved,
            "planned_retired_cell_count": 0,
            "resolution_reason_counts": (
                {"exact_assertion_agreement": 1}
                if unresolved == 0
                else {"materially_conflicting_assertions": 1}
            ),
            "resolution_plan_commitment_sha256": resolution_plan,
            "processed_cell_count": 0 if mode == "dry_run" else 1,
            "last_canonical_metric_cell_id": last_cell,
            "expected_issuer_count": 1,
            "resolution_snapshot_count": 0 if bounded or unresolved else 1,
            "projection_count": 0 if bounded or unresolved else 1,
            "projection_entry_count": 0 if bounded or unresolved else 1,
            "input_commitment_sha256": "b" * 64,
            "plan_commitment_sha256": "c" * 64,
            "post_state_commitment_sha256": post_state,
            "output_commitment_sha256": post_state,
            "checkpoint": CanonicalResolutionCheckpoint(
                bounded=bounded,
                safe_to_seal=not bounded and unresolved == 0,
                after_canonical_metric_cell_id=None,
                last_canonical_metric_cell_id=last_cell,
                processed_cell_count=0 if mode == "dry_run" else 1,
                remaining_cell_count=1 if bounded else 0,
                can_resume=bounded,
            ),
        }
    )


def _current_result(*, post_state: str = "d" * 64) -> CanonicalResolutionPopulationResult:
    applied = _result(mode="apply", state="complete", post_state=post_state)
    return applied.model_copy(
        update={
            "mode": "dry_run",
            "state": "planned",
            "processed_cell_count": 0,
            "last_canonical_metric_cell_id": None,
            "checkpoint": applied.checkpoint.model_copy(
                update={
                    "processed_cell_count": 0,
                    "last_canonical_metric_cell_id": None,
                }
            ),
        }
    )


def _planned_receipt(
    database: Path,
    document_sha: str,
) -> CanonicalResolutionOperationReceipt:
    return build_canonical_resolution_receipt(
        database_path=str(database.resolve()),
        database_instance_id=_DATABASE_INSTANCE_ID,
        alembic_revision=_HEAD_REVISION,
        request=CanonicalResolutionPopulationRequest(
            cutoff_at=_STAMP,
            operation_recorded_at=_STAMP,
        ),
        result=_result(),
        document_prerequisite_receipt_sha256=document_sha,
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256=None,
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
    checkpoint = build_canonical_resolution_receipt(
        database_path=str(database.resolve()),
        database_instance_id=_DATABASE_INSTANCE_ID,
        alembic_revision=_HEAD_REVISION,
        request=CanonicalResolutionPopulationRequest(
            cutoff_at=_STAMP,
            operation_recorded_at=_STAMP,
            apply=True,
            max_cells=1,
            input_commitment_sha256="b" * 64,
            plan_commitment_sha256="c" * 64,
        ),
        result=_result(
            mode="apply",
            state="partial",
            bounded=True,
            last_cell="cell-1",
        ).model_copy(
            update={
                "checkpoint": _result(
                    mode="apply",
                    state="partial",
                    bounded=True,
                    last_cell="cell-1",
                ).checkpoint.model_copy(update={"remaining_cell_count": 0, "can_resume": False})
            }
        ),
        document_prerequisite_receipt_sha256="d" * 64,
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256="e" * 64,
    )
    terminal_current = _current_result()

    verify_canonical_resolution_receipt_current_result(
        checkpoint,
        terminal_current,
        historical_checkpoint=True,
    )
    with pytest.raises(ValueError, match="stable source universe"):
        verify_canonical_resolution_receipt_current_result(
            checkpoint,
            terminal_current.model_copy(update={"expected_cell_count": 2}),
            historical_checkpoint=True,
        )
    with pytest.raises(ValueError, match="current planes"):
        verify_canonical_resolution_receipt_current_result(
            checkpoint,
            _result(
                state="partial",
                bounded=True,
                last_cell="cell-1",
                post_state="f" * 64,
            ),
        )

    terminal = build_canonical_resolution_receipt(
        database_path=str(database.resolve()),
        database_instance_id=_DATABASE_INSTANCE_ID,
        alembic_revision=_HEAD_REVISION,
        request=CanonicalResolutionPopulationRequest(
            cutoff_at=_STAMP,
            operation_recorded_at=_STAMP,
            apply=True,
            input_commitment_sha256="b" * 64,
            plan_commitment_sha256="c" * 64,
        ),
        result=_result(mode="apply", state="complete"),
        document_prerequisite_receipt_sha256="d" * 64,
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256="e" * 64,
    )
    verify_canonical_resolution_receipt_current_result(terminal, terminal_current)
    with pytest.raises(ValueError, match="current planes"):
        verify_canonical_resolution_receipt_current_result(
            terminal,
            terminal_current.model_copy(update={"projection_count": 0}),
        )


def test_checkpoint_replay_requires_a_terminal_ledger_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    _database(database)
    checkpoint = build_canonical_resolution_receipt(
        database_path=str(database.resolve()),
        database_instance_id=_DATABASE_INSTANCE_ID,
        alembic_revision=_HEAD_REVISION,
        request=CanonicalResolutionPopulationRequest(
            cutoff_at=_STAMP,
            operation_recorded_at=_STAMP,
            apply=True,
            max_cells=1,
            input_commitment_sha256="b" * 64,
            plan_commitment_sha256="c" * 64,
        ),
        result=_result(
            mode="apply",
            state="partial",
            bounded=True,
            last_cell="cell-1",
        ).model_copy(
            update={
                "checkpoint": CanonicalResolutionCheckpoint(
                    bounded=True,
                    safe_to_seal=False,
                    after_canonical_metric_cell_id=None,
                    last_canonical_metric_cell_id="cell-1",
                    processed_cell_count=1,
                    remaining_cell_count=0,
                    can_resume=False,
                )
            }
        ),
        document_prerequisite_receipt_sha256="d" * 64,
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256="e" * 64,
    )

    def rolled_back_operator(
        _connection: sqlite3.Connection,
        _request: CanonicalResolutionPopulationRequest,
    ) -> CanonicalResolutionPopulationResult:
        return _result(
            state="partial",
            bounded=True,
            last_cell="cell-1",
            post_state="f" * 64,
        )

    monkeypatch.setattr(population, "populate_canonical_resolution", rolled_back_operator)
    with sqlite3.connect(database) as connection:
        persist_canonical_resolution_receipt(connection, checkpoint)
        with pytest.raises(ValueError, match="current planes"):
            verify_canonical_resolution_receipt_current(connection, checkpoint)

        terminal = build_canonical_resolution_receipt(
            database_path=str(database.resolve()),
            database_instance_id=_DATABASE_INSTANCE_ID,
            alembic_revision=_HEAD_REVISION,
            request=CanonicalResolutionPopulationRequest(
                cutoff_at=_STAMP,
                operation_recorded_at=_STAMP,
                apply=True,
                input_commitment_sha256="b" * 64,
                plan_commitment_sha256="c" * 64,
            ),
            result=_result(mode="apply", state="complete"),
            document_prerequisite_receipt_sha256="d" * 64,
            prior_checkpoint_receipt_sha256=_artifact_sha(checkpoint),
            admission_receipt_sha256="e" * 64,
        )
        persist_canonical_resolution_receipt(connection, terminal)

        def current_operator(
            _connection: sqlite3.Connection,
            _request: CanonicalResolutionPopulationRequest,
        ) -> CanonicalResolutionPopulationResult:
            return _current_result()

        monkeypatch.setattr(
            population,
            "populate_canonical_resolution",
            current_operator,
        )
        verify_canonical_resolution_receipt_current(connection, checkpoint)


def test_bounded_canonical_receipt_cannot_masquerade_as_terminal(tmp_path: Path) -> None:
    database = tmp_path / "candidate.db"

    with pytest.raises(ValueError, match="bounded result"):
        build_canonical_resolution_receipt(
            database_path=str(database.resolve()),
            database_instance_id=_DATABASE_INSTANCE_ID,
            alembic_revision=_HEAD_REVISION,
            request=CanonicalResolutionPopulationRequest(
                cutoff_at=_STAMP,
                operation_recorded_at=_STAMP,
                apply=True,
                max_cells=1,
                input_commitment_sha256="b" * 64,
                plan_commitment_sha256="c" * 64,
            ),
            result=_result(mode="apply", state="complete"),
            document_prerequisite_receipt_sha256="d" * 64,
            prior_checkpoint_receipt_sha256=None,
            admission_receipt_sha256="e" * 64,
        )


def test_terminal_canonical_replay_requires_canonical_checkpoint_parent(tmp_path: Path) -> None:
    database = tmp_path / "candidate.db"
    _database(database)

    def terminal(prior: str | None, admission: str = "e" * 64):
        return build_canonical_resolution_receipt(
            database_path=str(database.resolve()),
            database_instance_id=_DATABASE_INSTANCE_ID,
            alembic_revision=_HEAD_REVISION,
            request=CanonicalResolutionPopulationRequest(
                cutoff_at=_STAMP,
                operation_recorded_at=_STAMP,
                apply=True,
                input_commitment_sha256="b" * 64,
                plan_commitment_sha256="c" * 64,
            ),
            result=_result(mode="apply", state="complete"),
            document_prerequisite_receipt_sha256="d" * 64,
            prior_checkpoint_receipt_sha256=prior,
            admission_receipt_sha256=admission,
        )

    with sqlite3.connect(database) as connection:
        orphan = terminal("f" * 64)
        persist_canonical_resolution_receipt(connection, orphan)
        with pytest.raises(ValueError, match="parent is missing"):
            verify_canonical_resolution_receipt_current(connection, orphan)

        noncheckpoint = terminal(None)
        child = terminal(_artifact_sha(noncheckpoint))
        persist_canonical_resolution_receipt(connection, noncheckpoint)
        persist_canonical_resolution_receipt(connection, child)
        with pytest.raises(ValueError, match="parent is not a checkpoint"):
            verify_canonical_resolution_receipt_current(connection, child)

        checkpoint_result = _result(
            mode="apply",
            state="partial",
            bounded=True,
            last_cell="cell-1",
        )
        checkpoint = build_canonical_resolution_receipt(
            database_path=str(database.resolve()),
            database_instance_id=_DATABASE_INSTANCE_ID,
            alembic_revision=_HEAD_REVISION,
            request=CanonicalResolutionPopulationRequest(
                cutoff_at=_STAMP,
                operation_recorded_at=_STAMP,
                apply=True,
                max_cells=1,
                input_commitment_sha256="b" * 64,
                plan_commitment_sha256="c" * 64,
            ),
            result=checkpoint_result.model_copy(
                update={
                    "checkpoint": checkpoint_result.checkpoint.model_copy(
                        update={"remaining_cell_count": 0, "can_resume": False}
                    )
                }
            ),
            document_prerequisite_receipt_sha256="d" * 64,
            prior_checkpoint_receipt_sha256=None,
            admission_receipt_sha256="a" * 64,
        )
        first = terminal(_artifact_sha(checkpoint), "1" * 64)
        sibling = terminal(_artifact_sha(checkpoint), "2" * 64)
        persist_canonical_resolution_receipt(connection, checkpoint)
        persist_canonical_resolution_receipt(connection, first)
        persist_canonical_resolution_receipt(connection, sibling)
        with pytest.raises(ValueError, match="successor is ambiguous"):
            verify_canonical_resolution_receipt_current(connection, first)


def test_completed_checkpoint_can_dry_run_and_apply_unbounded_sealing_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    _database(database)
    document_path = tmp_path / "document.json"
    prior_path = tmp_path / "prior.json"
    admission_path = tmp_path / "seal-plan.json"
    apply_path = tmp_path / "seal-apply.json"
    _document_prerequisite(database, document_path)
    checkpoint_result = _result(
        mode="apply",
        state="partial",
        bounded=True,
        last_cell="cell-1",
    )
    prior = build_canonical_resolution_receipt(
        database_path=str(database.resolve()),
        database_instance_id=_DATABASE_INSTANCE_ID,
        alembic_revision=_HEAD_REVISION,
        request=CanonicalResolutionPopulationRequest(
            cutoff_at=_STAMP,
            operation_recorded_at=_STAMP,
            apply=True,
            max_cells=1,
            input_commitment_sha256="b" * 64,
            plan_commitment_sha256="c" * 64,
        ),
        result=checkpoint_result.model_copy(
            update={
                "checkpoint": checkpoint_result.checkpoint.model_copy(
                    update={"remaining_cell_count": 0, "can_resume": False}
                )
            }
        ),
        document_prerequisite_receipt_sha256=hashlib.sha256(document_path.read_bytes()).hexdigest(),
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256="e" * 64,
    )
    prior_path.write_bytes((prior.model_dump_json() + "\n").encode())
    with sqlite3.connect(database) as connection:
        persist_canonical_resolution_receipt(connection, prior)

    def run_stub(
        _connection: sqlite3.Connection,
        _args: Namespace,
        request: CanonicalResolutionPopulationRequest,
    ) -> CanonicalResolutionPopulationResult:
        return _result(
            mode="apply" if request.apply else "dry_run",
            state="complete" if request.apply else "planned",
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
        "--document-prerequisite-receipt",
        str(document_path),
        "--prior-checkpoint-receipt",
        str(prior_path),
    ]

    assert cli.main([*base, "--receipt", str(admission_path)]) == 0
    admission = CanonicalResolutionOperationReceipt.model_validate_json(
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
    applied = CanonicalResolutionOperationReceipt.model_validate_json(
        apply_path.read_text(encoding="utf-8")
    )
    assert applied.outcome == "complete"
    assert applied.prior_checkpoint_receipt_sha256 == _artifact_sha(prior)


@pytest.mark.parametrize(
    ("phase", "cursor", "error"),
    (("resolutions", "wrong-cell", "cursor"), ("snapshots", "cell-1", "phase")),
)
def test_public_apply_rejects_checkpoint_cursor_or_phase_drift_before_operator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    phase: cli.CanonicalPhase,
    cursor: str,
    error: str,
) -> None:
    database = tmp_path / "candidate.db"
    document_path = tmp_path / "document.json"
    prior_path = tmp_path / "prior.json"
    admission_path = tmp_path / "admission.json"
    _database(database)
    _document_prerequisite(database, document_path)
    document_sha = cli.read_stable_artifact(document_path)[0].file_sha256
    prior_result = _result(
        mode="apply",
        state="partial",
        bounded=True,
        last_cell="cell-1",
    ).model_copy(update={"phase": "resolutions"})
    prior = build_canonical_resolution_receipt(
        database_path=str(database.resolve()),
        database_instance_id=_DATABASE_INSTANCE_ID,
        alembic_revision=_HEAD_REVISION,
        request=CanonicalResolutionPopulationRequest(
            cutoff_at=_STAMP,
            operation_recorded_at=_STAMP,
            apply=True,
            phase="resolutions",
            after_canonical_metric_cell_id="cell-0",
            input_commitment_sha256="b" * 64,
            plan_commitment_sha256="c" * 64,
        ),
        result=prior_result,
        document_prerequisite_receipt_sha256=document_sha,
        prior_checkpoint_receipt_sha256="d" * 64,
        admission_receipt_sha256="e" * 64,
    )
    prior_path.write_bytes((prior.model_dump_json() + "\n").encode())
    with sqlite3.connect(database) as connection:
        persist_canonical_resolution_receipt(connection, prior)
    admission = build_canonical_resolution_receipt(
        database_path=str(database.resolve()),
        database_instance_id=_DATABASE_INSTANCE_ID,
        alembic_revision=_HEAD_REVISION,
        request=CanonicalResolutionPopulationRequest(
            cutoff_at=_STAMP,
            operation_recorded_at=_STAMP,
            phase=phase,
            after_canonical_metric_cell_id=cursor,
            input_commitment_sha256="b" * 64,
            plan_commitment_sha256="c" * 64,
        ),
        result=_result(bounded=True, last_cell=cursor).model_copy(update={"phase": phase}),
        document_prerequisite_receipt_sha256=document_sha,
        prior_checkpoint_receipt_sha256=_artifact_sha(prior),
        admission_receipt_sha256=None,
    )
    admission_path.write_bytes((admission.model_dump_json() + "\n").encode())
    called = False

    def run_stub(*_args: object, **_kwargs: object) -> CanonicalResolutionPopulationResult:
        nonlocal called
        called = True
        return _result()

    monkeypatch.setattr(cli, "_run_operator", run_stub)
    assert (
        cli.main(
            [
                "--db",
                str(database),
                "--cutoff-at",
                "2026-07-29T00:00:00Z",
                "--recorded-at",
                "2026-07-29T00:00:00Z",
                "--phase",
                phase,
                "--after-cell-id",
                cursor,
                "--document-prerequisite-receipt",
                str(document_path),
                "--prior-checkpoint-receipt",
                str(prior_path),
                "--apply",
                "--admission-receipt",
                str(admission_path),
                "--receipt",
                str(tmp_path / "apply.json"),
            ]
        )
        == 2
    )
    assert error in capsys.readouterr().err
    assert called is False
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM canonical_resolution_operation_ledger"
            ).fetchone()[0]
            == 1
        )


def test_dry_run_requires_complete_document_prerequisite_and_is_no_clobber(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    document_path = tmp_path / "document.json"
    receipt_path = tmp_path / "canonical-dry-run.json"
    _database(database)
    _document_prerequisite(database, document_path)

    def run_stub(
        connection: sqlite3.Connection,
        _args: Namespace,
        _request: CanonicalResolutionPopulationRequest,
    ) -> CanonicalResolutionPopulationResult:
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
        "--document-prerequisite-receipt",
        str(document_path),
        "--receipt",
        str(receipt_path),
    ]

    assert cli.main(argv) == 0
    original = receipt_path.read_bytes()
    assert cli.main(argv) == 0
    assert receipt_path.read_bytes() == original
    missing_prerequisite = list(argv)
    prerequisite_index = missing_prerequisite.index("--document-prerequisite-receipt")
    del missing_prerequisite[prerequisite_index : prerequisite_index + 2]
    with pytest.raises(SystemExit):
        cli.main(missing_prerequisite)


def test_unresolved_admission_is_blocked_before_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    document_path = tmp_path / "document.json"
    receipt_path = tmp_path / "blocked.json"
    _database(database)
    _document_prerequisite(database, document_path)

    def unresolved_stub(
        _connection: sqlite3.Connection,
        _args: Namespace,
        _request: CanonicalResolutionPopulationRequest,
    ) -> CanonicalResolutionPopulationResult:
        return _result(unresolved=1)

    monkeypatch.setattr(cli, "_run_operator", unresolved_stub)

    assert (
        cli.main(
            [
                "--db",
                str(database),
                "--cutoff-at",
                "2026-07-29T00:00:00Z",
                "--recorded-at",
                "2026-07-29T00:00:00Z",
                "--document-prerequisite-receipt",
                str(document_path),
                "--receipt",
                str(receipt_path),
            ]
        )
        == 2
    )
    assert (
        CanonicalResolutionOperationReceipt.model_validate_json(
            receipt_path.read_text(encoding="utf-8")
        ).outcome
        == "blocked"
    )


def test_apply_ledger_is_atomic_and_exact_replay_does_not_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    document_path = tmp_path / "document.json"
    admission_path = tmp_path / "admission.json"
    receipt_path = tmp_path / "apply.json"
    _database(database)
    _document_prerequisite(database, document_path)
    document_sha = cli.read_stable_artifact(document_path)[0].file_sha256
    admission_path.write_bytes(
        (_planned_receipt(database, document_sha).model_dump_json() + "\n").encode()
    )
    calls = 0

    def run_stub(
        connection: sqlite3.Connection,
        _args: Namespace,
        request: CanonicalResolutionPopulationRequest,
    ) -> CanonicalResolutionPopulationResult:
        nonlocal calls
        calls += 1
        if request.apply:
            connection.execute("INSERT INTO operation_probe VALUES ('applied')")
            return _result(mode="apply", state="complete")
        return _current_result()

    def verify_current(
        connection: sqlite3.Connection,
        receipt: CanonicalResolutionOperationReceipt,
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
        verify_canonical_resolution_receipt_current_result(receipt, current)

    monkeypatch.setattr(cli, "_run_operator", run_stub)
    monkeypatch.setattr(cli, "verify_canonical_resolution_receipt_current", verify_current)
    argv = [
        "--db",
        str(database),
        "--cutoff-at",
        "2026-07-29T00:00:00Z",
        "--recorded-at",
        "2026-07-29T00:00:00Z",
        "--document-prerequisite-receipt",
        str(document_path),
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
        assert (
            conn.execute("SELECT COUNT(*) FROM canonical_resolution_operation_ledger").fetchone()[0]
            == 1
        )


def test_apply_exact_replay_refuses_rolled_back_canonical_plane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    document_path = tmp_path / "document.json"
    admission_path = tmp_path / "admission.json"
    receipt_path = tmp_path / "apply.json"
    _database(database)
    _document_prerequisite(database, document_path)
    document_sha = cli.read_stable_artifact(document_path)[0].file_sha256
    admission_path.write_bytes(
        (_planned_receipt(database, document_sha).model_dump_json() + "\n").encode()
    )

    def run_stub(
        connection: sqlite3.Connection,
        _args: Namespace,
        request: CanonicalResolutionPopulationRequest,
    ) -> CanonicalResolutionPopulationResult:
        if request.apply:
            connection.execute("INSERT INTO operation_probe VALUES ('applied')")
            return _result(mode="apply", state="complete")
        if connection.execute("SELECT COUNT(*) FROM operation_probe").fetchone()[0] == 0:
            return _current_result(post_state="f" * 64)
        return _current_result()

    def verify_current(
        connection: sqlite3.Connection,
        receipt: CanonicalResolutionOperationReceipt,
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
        verify_canonical_resolution_receipt_current_result(receipt, current)

    monkeypatch.setattr(cli, "_run_operator", run_stub)
    monkeypatch.setattr(cli, "verify_canonical_resolution_receipt_current", verify_current)
    argv = [
        "--db",
        str(database),
        "--cutoff-at",
        "2026-07-29T00:00:00Z",
        "--recorded-at",
        "2026-07-29T00:00:00Z",
        "--document-prerequisite-receipt",
        str(document_path),
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
            connection.execute(
                "SELECT COUNT(*) FROM canonical_resolution_operation_ledger"
            ).fetchone()[0]
            == 1
        )

    assert cli.main(argv) == 2


def test_apply_publication_failure_preserves_recoverable_ledger_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    document_path = tmp_path / "document.json"
    admission_path = tmp_path / "admission.json"
    receipt_path = tmp_path / "apply.json"
    _database(database)
    _document_prerequisite(database, document_path)
    document_sha = cli.read_stable_artifact(document_path)[0].file_sha256
    admission_path.write_bytes(
        (_planned_receipt(database, document_sha).model_dump_json() + "\n").encode()
    )

    def run_stub(
        connection: sqlite3.Connection,
        _args: Namespace,
        _request: CanonicalResolutionPopulationRequest,
    ) -> CanonicalResolutionPopulationResult:
        connection.execute("INSERT INTO operation_probe VALUES ('applied')")
        return _result(mode="apply", state="complete")

    real_publish = cli.publish_text_no_clobber

    def publish_stub(path: Path, text: str) -> None:
        if path == receipt_path.resolve():
            raise PermissionError("receipt publication denied")
        real_publish(path, text)

    monkeypatch.setattr(cli, "_run_operator", run_stub)
    monkeypatch.setattr(cli, "publish_text_no_clobber", publish_stub)
    argv = [
        "--db",
        str(database),
        "--cutoff-at",
        "2026-07-29T00:00:00Z",
        "--recorded-at",
        "2026-07-29T00:00:00Z",
        "--document-prerequisite-receipt",
        str(document_path),
        "--apply",
        "--admission-receipt",
        str(admission_path),
        "--receipt",
        str(receipt_path),
    ]

    assert cli.main(argv) == 2
    assert not receipt_path.exists()
    with sqlite3.connect(database) as conn:
        stored = conn.execute(
            "SELECT receipt_json FROM canonical_resolution_operation_ledger"
        ).fetchone()
        probe_count = conn.execute("SELECT COUNT(*) FROM operation_probe").fetchone()[0]
    assert stored is not None
    assert probe_count == 1
    receipt = CanonicalResolutionOperationReceipt.model_validate_json(str(stored[0]))
    assert receipt.outcome == "complete"
    assert receipt.document_prerequisite_receipt_sha256 == document_sha


def test_apply_rechecks_input_artifacts_before_committing_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    document_path = tmp_path / "document.json"
    admission_path = tmp_path / "admission.json"
    receipt_path = tmp_path / "apply.json"
    _database(database)
    _document_prerequisite(database, document_path)
    document_sha = cli.read_stable_artifact(document_path)[0].file_sha256
    admission_path.write_bytes(
        (_planned_receipt(database, document_sha).model_dump_json() + "\n").encode()
    )

    def run_stub(
        connection: sqlite3.Connection,
        _args: Namespace,
        _request: CanonicalResolutionPopulationRequest,
    ) -> CanonicalResolutionPopulationResult:
        connection.execute("INSERT INTO operation_probe VALUES ('must-rollback')")
        document_path.write_text('{"replaced":true}', encoding="utf-8")
        return _result(mode="apply", state="complete")

    monkeypatch.setattr(cli, "_run_operator", run_stub)
    assert (
        cli.main(
            [
                "--db",
                str(database),
                "--cutoff-at",
                "2026-07-29T00:00:00Z",
                "--recorded-at",
                "2026-07-29T00:00:00Z",
                "--document-prerequisite-receipt",
                str(document_path),
                "--apply",
                "--admission-receipt",
                str(admission_path),
                "--receipt",
                str(receipt_path),
            ]
        )
        == 2
    )
    with sqlite3.connect(database) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM canonical_resolution_operation_ledger").fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM operation_probe").fetchone()[0] == 0
    assert not receipt_path.exists()


def test_apply_rejects_resolution_plan_drift_before_committing_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    document_path = tmp_path / "document.json"
    admission_path = tmp_path / "admission.json"
    receipt_path = tmp_path / "apply.json"
    _database(database)
    _document_prerequisite(database, document_path)
    document_sha = cli.read_stable_artifact(document_path)[0].file_sha256
    admission_path.write_bytes(
        (_planned_receipt(database, document_sha).model_dump_json() + "\n").encode()
    )

    def drifted_plan_stub(
        connection: sqlite3.Connection,
        _args: Namespace,
        _request: CanonicalResolutionPopulationRequest,
    ) -> CanonicalResolutionPopulationResult:
        connection.execute("INSERT INTO operation_probe VALUES ('must-rollback')")
        return _result(
            mode="apply",
            state="complete",
            resolution_plan="f" * 64,
        )

    monkeypatch.setattr(cli, "_run_operator", drifted_plan_stub)
    assert (
        cli.main(
            [
                "--db",
                str(database),
                "--cutoff-at",
                "2026-07-29T00:00:00Z",
                "--recorded-at",
                "2026-07-29T00:00:00Z",
                "--document-prerequisite-receipt",
                str(document_path),
                "--apply",
                "--admission-receipt",
                str(admission_path),
                "--receipt",
                str(receipt_path),
            ]
        )
        == 2
    )
    with sqlite3.connect(database) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM canonical_resolution_operation_ledger").fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM operation_probe").fetchone()[0] == 0
    assert not receipt_path.exists()


def test_receipt_destination_cannot_alias_database_or_prerequisite(tmp_path: Path) -> None:
    database = tmp_path / "candidate.db"
    prerequisite = tmp_path / "document.json"
    database.write_bytes(b"db")
    prerequisite.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="protected"):
        cli.validate_receipt_path(database, database=database, protected_receipts=(prerequisite,))
    with pytest.raises(ValueError, match="protected"):
        cli.validate_receipt_path(
            prerequisite,
            database=database,
            protected_receipts=(prerequisite,),
        )
