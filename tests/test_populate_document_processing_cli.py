from __future__ import annotations

import hashlib
import sqlite3
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import provenance.population_document_processing as population
from execution import populate_document_processing as cli
from provenance.population_document_processing import (
    DocumentProcessingOperationReceipt,
    DocumentProcessingPopulationRequest,
    DocumentProcessingPopulationResult,
    persist_document_processing_receipt,
    verify_document_processing_receipt_current,
    verify_document_processing_receipt_current_result,
)
from schema_compat import expected_head
from tests.test_population_document_processing import (
    build_test_document_processing_receipt as build_document_processing_receipt,
)
from tests.test_population_document_processing import (
    receipt_result,
)

_DATABASE_INSTANCE_ID = "database-instance:" + "1" * 32
_HEAD_REVISION = expected_head()


def _artifact_sha(receipt: DocumentProcessingOperationReceipt) -> str:
    return hashlib.sha256((receipt.model_dump_json() + "\n").encode()).hexdigest()


@pytest.fixture(autouse=True)
def _coherent_document_receipt_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "build_document_processing_receipt", build_document_processing_receipt)


def _database(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE alembic_version (version_num TEXT NOT NULL);
            INSERT INTO alembic_version VALUES ('0264_document_processing_operation_ledger');
            CREATE TABLE database_runtime_identity (
                singleton INTEGER PRIMARY KEY,
                database_instance_id TEXT NOT NULL UNIQUE
            );
            INSERT INTO database_runtime_identity VALUES (1, 'database-instance:11111111111111111111111111111111');
            CREATE TABLE document_processing_operation_ledger (
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
        conn.commit()
    finally:
        conn.close()


def _planned_receipt(database: Path):
    cutoff = datetime(2026, 7, 29, tzinfo=UTC)
    return build_document_processing_receipt(
        database_path=str(database.resolve()),
        database_instance_id=_DATABASE_INSTANCE_ID,
        alembic_revision=_HEAD_REVISION,
        request=DocumentProcessingPopulationRequest(
            cutoff_at=cutoff,
            operation_recorded_at=cutoff,
        ),
        result=receipt_result(),
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256=None,
    )


def test_loader_rejects_reformatted_prior_receipt(tmp_path: Path) -> None:
    receipt = _planned_receipt(tmp_path / "candidate.db")
    path = tmp_path / "reformatted-prior.json"
    path.write_text(receipt.model_dump_json() + " \n", encoding="utf-8")

    with pytest.raises(cli.ImmutableArtifactConflictError, match="canonically serialized"):
        cli.load_document_processing_receipt_artifact(path)


def test_apply_request_is_derived_from_exact_dry_run_admission(tmp_path: Path) -> None:
    database = tmp_path / "candidate.db"
    receipt = _planned_receipt(database)
    admitted = cli.admitted_apply_request(
        receipt,
        database=database,
        cutoff_at=receipt.request.cutoff_at,
        operation_recorded_at=receipt.request.operation_recorded_at,
        phase="all",
        after_obligation_id=None,
        max_obligations=None,
    )

    assert admitted.apply is True
    assert admitted.input_commitment_sha256 == receipt.result.input_commitment_sha256
    assert admitted.plan_commitment_sha256 == receipt.result.plan_commitment_sha256
    with pytest.raises(ValueError, match="database"):
        cli.admitted_apply_request(
            receipt,
            database=tmp_path / "other.db",
            cutoff_at=receipt.request.cutoff_at,
            operation_recorded_at=receipt.request.operation_recorded_at,
            phase="all",
            after_obligation_id=None,
            max_obligations=None,
        )


def test_receipt_destination_cannot_alias_database_or_sqlite_sidecars(
    tmp_path: Path,
) -> None:
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
    cutoff = datetime(2026, 7, 29, tzinfo=UTC)
    checkpoint = build_document_processing_receipt(
        database_path=str(database.resolve()),
        database_instance_id=_DATABASE_INSTANCE_ID,
        alembic_revision=_HEAD_REVISION,
        request=DocumentProcessingPopulationRequest(
            cutoff_at=cutoff,
            operation_recorded_at=cutoff,
            apply=True,
            max_obligations=1,
            input_commitment_sha256="b" * 64,
            plan_commitment_sha256="c" * 64,
        ),
        result=receipt_result(mode="apply", bounded=True, remaining=0),
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256="e" * 64,
    )
    terminal_current = receipt_result()

    verify_document_processing_receipt_current_result(
        checkpoint,
        terminal_current,
        historical_checkpoint=True,
    )
    with pytest.raises(ValueError, match="stable source universe"):
        verify_document_processing_receipt_current_result(
            checkpoint,
            terminal_current.model_copy(update={"selection_commitment_sha256": "f" * 64}),
            historical_checkpoint=True,
        )
    with pytest.raises(ValueError, match="current planes"):
        verify_document_processing_receipt_current_result(
            checkpoint,
            receipt_result(bounded=True, remaining=1).model_copy(
                update={"output_commitment_sha256": "f" * 64}
            ),
        )

    terminal = build_document_processing_receipt(
        database_path=str(database.resolve()),
        database_instance_id=_DATABASE_INSTANCE_ID,
        alembic_revision=_HEAD_REVISION,
        request=DocumentProcessingPopulationRequest(
            cutoff_at=cutoff,
            operation_recorded_at=cutoff,
            apply=True,
            input_commitment_sha256="b" * 64,
            plan_commitment_sha256="c" * 64,
        ),
        result=receipt_result(mode="apply"),
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256="e" * 64,
    )
    verify_document_processing_receipt_current_result(terminal, terminal_current)
    with pytest.raises(ValueError, match="current planes"):
        verify_document_processing_receipt_current_result(
            terminal,
            terminal_current.model_copy(update={"output_commitment_sha256": "f" * 64}),
        )


def test_checkpoint_replay_requires_a_terminal_ledger_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    _database(database)
    cutoff = datetime(2026, 7, 29, tzinfo=UTC)
    checkpoint = build_document_processing_receipt(
        database_path=str(database.resolve()),
        database_instance_id=_DATABASE_INSTANCE_ID,
        alembic_revision=_HEAD_REVISION,
        request=DocumentProcessingPopulationRequest(
            cutoff_at=cutoff,
            operation_recorded_at=cutoff,
            apply=True,
            max_obligations=1,
            input_commitment_sha256="b" * 64,
            plan_commitment_sha256="c" * 64,
        ),
        result=receipt_result(mode="apply", bounded=True, remaining=0),
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256="e" * 64,
    )
    rolled_back = receipt_result(bounded=True, remaining=1).model_copy(
        update={"output_commitment_sha256": "f" * 64}
    )

    def rolled_back_operator(
        _connection: sqlite3.Connection,
        _request: DocumentProcessingPopulationRequest,
    ) -> DocumentProcessingPopulationResult:
        return rolled_back

    monkeypatch.setattr(population, "populate_document_processing", rolled_back_operator)
    with sqlite3.connect(database) as connection:
        persist_document_processing_receipt(connection, checkpoint)
        with pytest.raises(ValueError, match="current planes"):
            verify_document_processing_receipt_current(connection, checkpoint)

        terminal = build_document_processing_receipt(
            database_path=str(database.resolve()),
            database_instance_id=_DATABASE_INSTANCE_ID,
            alembic_revision=_HEAD_REVISION,
            request=DocumentProcessingPopulationRequest(
                cutoff_at=cutoff,
                operation_recorded_at=cutoff,
                apply=True,
                input_commitment_sha256="b" * 64,
                plan_commitment_sha256="c" * 64,
            ),
            result=receipt_result(mode="apply"),
            prior_checkpoint_receipt_sha256=_artifact_sha(checkpoint),
            admission_receipt_sha256="e" * 64,
        )
        persist_document_processing_receipt(connection, terminal)

        def current_operator(
            _connection: sqlite3.Connection,
            _request: DocumentProcessingPopulationRequest,
        ) -> DocumentProcessingPopulationResult:
            return receipt_result()

        monkeypatch.setattr(
            population,
            "populate_document_processing",
            current_operator,
        )
        verify_document_processing_receipt_current(connection, checkpoint)


def test_bounded_document_receipt_cannot_masquerade_as_terminal(tmp_path: Path) -> None:
    database = tmp_path / "candidate.db"
    cutoff = datetime(2026, 7, 29, tzinfo=UTC)

    with pytest.raises(ValueError, match="bounded result"):
        build_document_processing_receipt(
            database_path=str(database.resolve()),
            database_instance_id=_DATABASE_INSTANCE_ID,
            alembic_revision=_HEAD_REVISION,
            request=DocumentProcessingPopulationRequest(
                cutoff_at=cutoff,
                operation_recorded_at=cutoff,
                apply=True,
                max_obligations=1,
                input_commitment_sha256="b" * 64,
                plan_commitment_sha256="c" * 64,
            ),
            result=receipt_result(mode="apply"),
            prior_checkpoint_receipt_sha256=None,
            admission_receipt_sha256="e" * 64,
        )


def test_terminal_document_replay_requires_canonical_checkpoint_parent(tmp_path: Path) -> None:
    database = tmp_path / "candidate.db"
    _database(database)
    cutoff = datetime(2026, 7, 29, tzinfo=UTC)

    def terminal(prior: str | None, admission: str = "e" * 64):
        return build_document_processing_receipt(
            database_path=str(database.resolve()),
            database_instance_id=_DATABASE_INSTANCE_ID,
            alembic_revision=_HEAD_REVISION,
            request=DocumentProcessingPopulationRequest(
                cutoff_at=cutoff,
                operation_recorded_at=cutoff,
                apply=True,
                input_commitment_sha256="b" * 64,
                plan_commitment_sha256="c" * 64,
            ),
            result=receipt_result(mode="apply"),
            prior_checkpoint_receipt_sha256=prior,
            admission_receipt_sha256=admission,
        )

    with sqlite3.connect(database) as connection:
        orphan = terminal("f" * 64)
        persist_document_processing_receipt(connection, orphan)
        with pytest.raises(ValueError, match="parent is missing"):
            verify_document_processing_receipt_current(connection, orphan)

        noncheckpoint = terminal(None)
        child = terminal(_artifact_sha(noncheckpoint))
        persist_document_processing_receipt(connection, noncheckpoint)
        persist_document_processing_receipt(connection, child)
        with pytest.raises(ValueError, match="parent is not a checkpoint"):
            verify_document_processing_receipt_current(connection, child)

        checkpoint = build_document_processing_receipt(
            database_path=str(database.resolve()),
            database_instance_id=_DATABASE_INSTANCE_ID,
            alembic_revision=_HEAD_REVISION,
            request=DocumentProcessingPopulationRequest(
                cutoff_at=cutoff,
                operation_recorded_at=cutoff,
                apply=True,
                max_obligations=1,
                input_commitment_sha256="b" * 64,
                plan_commitment_sha256="c" * 64,
            ),
            result=receipt_result(mode="apply", bounded=True, remaining=0),
            prior_checkpoint_receipt_sha256=None,
            admission_receipt_sha256="a" * 64,
        )
        first = terminal(_artifact_sha(checkpoint), "1" * 64)
        sibling = terminal(_artifact_sha(checkpoint), "2" * 64)
        persist_document_processing_receipt(connection, checkpoint)
        persist_document_processing_receipt(connection, first)
        persist_document_processing_receipt(connection, sibling)
        with pytest.raises(ValueError, match="successor is ambiguous"):
            verify_document_processing_receipt_current(connection, first)


def test_checkpoint_resume_requires_exact_last_successful_cursor(tmp_path: Path) -> None:
    database = tmp_path / "candidate.db"
    cutoff = datetime(2026, 7, 29, tzinfo=UTC)
    request = DocumentProcessingPopulationRequest(
        cutoff_at=cutoff,
        operation_recorded_at=cutoff,
        apply=True,
        max_obligations=1,
        input_commitment_sha256="b" * 64,
        plan_commitment_sha256="c" * 64,
    )
    checkpoint = build_document_processing_receipt(
        database_path=str(database.resolve()),
        database_instance_id=_DATABASE_INSTANCE_ID,
        alembic_revision="0264_document_processing_operation_ledger",
        request=request,
        result=receipt_result(mode="apply", bounded=True, remaining=1),
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256="e" * 64,
    )

    cli.validate_checkpoint_resume(
        checkpoint,
        database=database,
        cutoff_at=cutoff,
        operation_recorded_at=cutoff,
        phase="all",
        after_obligation_id="obligation-1",
        max_obligations=1,
    )
    with pytest.raises(ValueError, match="cursor"):
        cli.validate_checkpoint_resume(
            checkpoint,
            database=database,
            cutoff_at=cutoff,
            operation_recorded_at=cutoff,
            phase="all",
            after_obligation_id="obligation-2",
            max_obligations=1,
        )


def test_checkpoint_successor_rejects_selection_output_and_revision_drift(
    tmp_path: Path,
) -> None:
    database = tmp_path / "candidate.db"
    cutoff = datetime(2026, 7, 29, tzinfo=UTC)
    request = DocumentProcessingPopulationRequest(
        cutoff_at=cutoff,
        operation_recorded_at=cutoff,
        apply=True,
        phase="all",
        max_obligations=1,
        input_commitment_sha256="b" * 64,
        plan_commitment_sha256="c" * 64,
    )
    prior = build_document_processing_receipt(
        database_path=str(database.resolve()),
        database_instance_id=_DATABASE_INSTANCE_ID,
        alembic_revision="0264_document_processing_operation_ledger",
        request=request,
        result=receipt_result(mode="apply", bounded=True, remaining=1),
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256="e" * 64,
    )
    successor_request = DocumentProcessingPopulationRequest(
        cutoff_at=cutoff,
        operation_recorded_at=cutoff,
        phase="all",
        after_processing_obligation_revision_id="obligation-1",
        max_obligations=1,
    )
    current = receipt_result(bounded=True, remaining=1)

    cli.validate_checkpoint_successor(
        prior,
        request=successor_request,
        result=current,
        alembic_revision="0264_document_processing_operation_ledger",
    )
    with pytest.raises(ValueError, match="selection"):
        cli.validate_checkpoint_successor(
            prior,
            request=successor_request,
            result=current.model_copy(update={"selection_commitment_sha256": "f" * 64}),
            alembic_revision="0264_document_processing_operation_ledger",
        )
    with pytest.raises(ValueError, match="output"):
        cli.validate_checkpoint_successor(
            prior,
            request=successor_request,
            result=current.model_copy(update={"output_commitment_sha256": "f" * 64}),
            alembic_revision="0264_document_processing_operation_ledger",
        )
    with pytest.raises(ValueError, match="revision"):
        cli.validate_checkpoint_successor(
            prior,
            request=successor_request,
            result=current,
            alembic_revision="0264_other",
        )


def test_checkpoint_resume_rejects_phase_and_batch_shape_drift(tmp_path: Path) -> None:
    database = tmp_path / "candidate.db"
    cutoff = datetime(2026, 7, 29, tzinfo=UTC)
    prior = build_document_processing_receipt(
        database_path=str(database.resolve()),
        database_instance_id=_DATABASE_INSTANCE_ID,
        alembic_revision="0264_document_processing_operation_ledger",
        request=DocumentProcessingPopulationRequest(
            cutoff_at=cutoff,
            operation_recorded_at=cutoff,
            apply=True,
            phase="dispositions",
            max_obligations=1,
            input_commitment_sha256="b" * 64,
            plan_commitment_sha256="c" * 64,
        ),
        result=receipt_result(mode="apply", bounded=True, remaining=1).model_copy(
            update={"phase": "dispositions"}
        ),
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256="e" * 64,
    )

    with pytest.raises(ValueError, match="phase"):
        cli.validate_checkpoint_resume(
            prior,
            database=database,
            cutoff_at=cutoff,
            operation_recorded_at=cutoff,
            phase="all",
            after_obligation_id="obligation-1",
            max_obligations=1,
        )
    with pytest.raises(ValueError, match="batch"):
        cli.validate_checkpoint_resume(
            prior,
            database=database,
            cutoff_at=cutoff,
            operation_recorded_at=cutoff,
            phase="dispositions",
            after_obligation_id="obligation-1",
            max_obligations=2,
        )


def test_dry_run_receipt_publication_is_no_clobber_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    _database(database)
    receipt_path = tmp_path / "dry-run-receipt.json"

    def run_stub(
        _connection: sqlite3.Connection,
        _args: Namespace,
        _request: DocumentProcessingPopulationRequest,
    ) -> DocumentProcessingPopulationResult:
        assert _connection.in_transaction
        return receipt_result()

    monkeypatch.setattr(cli, "_run_operator", run_stub)
    argv = [
        "--db",
        str(database),
        "--cutoff-at",
        "2026-07-29T00:00:00Z",
        "--operation-recorded-at",
        "2026-07-29T00:00:00Z",
        "--receipt",
        str(receipt_path),
    ]

    assert cli.main(argv) == 0
    original = receipt_path.read_bytes()
    assert cli.main(argv) == 0
    assert receipt_path.read_bytes() == original
    receipt_path.write_bytes(b'{"tampered":true}\n')
    assert cli.main(argv) == 2
    assert receipt_path.read_bytes() == b'{"tampered":true}\n'


def test_apply_conflicting_destination_refuses_before_operator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    _database(database)
    admission_path = tmp_path / "admission.json"
    receipt_path = tmp_path / "apply.json"
    admission = _planned_receipt(database)
    admission_path.write_bytes((admission.model_dump_json() + "\n").encode())
    receipt_path.write_text('{"conflict":true}', encoding="utf-8")
    called = False

    def run_stub(*_args: object, **_kwargs: object) -> tuple[object, str]:
        nonlocal called
        called = True
        raise AssertionError("operator must not run")

    monkeypatch.setattr(cli, "_run_operator", run_stub)
    argv = [
        "--db",
        str(database),
        "--cutoff-at",
        "2026-07-29T00:00:00Z",
        "--operation-recorded-at",
        "2026-07-29T00:00:00Z",
        "--apply",
        "--admission-receipt",
        str(admission_path),
        "--receipt",
        str(receipt_path),
    ]

    assert cli.main(argv) == 2
    assert called is False


def test_apply_publication_failure_preserves_recoverable_ledger_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    _database(database)
    admission_path = tmp_path / "admission.json"
    receipt_path = tmp_path / "apply.json"
    admission = _planned_receipt(database)
    admission_path.write_bytes((admission.model_dump_json() + "\n").encode())

    def run_stub(
        connection: sqlite3.Connection,
        _args: Namespace,
        _request: DocumentProcessingPopulationRequest,
    ) -> DocumentProcessingPopulationResult:
        assert connection.in_transaction
        connection.execute("INSERT INTO operation_probe VALUES ('applied')")
        return receipt_result(mode="apply")

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
        "--operation-recorded-at",
        "2026-07-29T00:00:00Z",
        "--apply",
        "--admission-receipt",
        str(admission_path),
        "--receipt",
        str(receipt_path),
    ]

    assert cli.main(argv) == 2
    assert not receipt_path.exists()
    conn = sqlite3.connect(database)
    try:
        stored = conn.execute(
            "SELECT receipt_json FROM document_processing_operation_ledger"
        ).fetchone()
        probe_count = conn.execute("SELECT COUNT(*) FROM operation_probe").fetchone()[0]
    finally:
        conn.close()
    assert stored is not None
    assert probe_count == 1
    receipt = DocumentProcessingOperationReceipt.model_validate_json(str(stored[0]))
    assert receipt.request.apply is True
    assert receipt.database_instance_id == _DATABASE_INSTANCE_ID


def test_apply_exact_replay_exports_the_original_ledger_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    _database(database)
    admission_path = tmp_path / "admission.json"
    receipt_path = tmp_path / "apply.json"
    admission_path.write_bytes((_planned_receipt(database).model_dump_json() + "\n").encode())
    calls = 0

    def run_stub(
        connection: sqlite3.Connection,
        _args: Namespace,
        request: DocumentProcessingPopulationRequest,
    ) -> DocumentProcessingPopulationResult:
        nonlocal calls
        calls += 1
        if request.apply:
            connection.execute("INSERT INTO operation_probe VALUES ('applied')")
            return receipt_result(mode="apply")
        return receipt_result()

    def verify_current(
        connection: sqlite3.Connection,
        receipt: DocumentProcessingOperationReceipt,
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
        verify_document_processing_receipt_current_result(receipt, current)

    monkeypatch.setattr(cli, "_run_operator", run_stub)
    monkeypatch.setattr(cli, "verify_document_processing_receipt_current", verify_current)
    argv = [
        "--db",
        str(database),
        "--cutoff-at",
        "2026-07-29T00:00:00Z",
        "--operation-recorded-at",
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


def test_apply_exact_replay_refuses_rolled_back_document_plane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    _database(database)
    admission_path = tmp_path / "admission.json"
    receipt_path = tmp_path / "apply.json"
    admission_path.write_bytes((_planned_receipt(database).model_dump_json() + "\n").encode())

    def run_stub(
        connection: sqlite3.Connection,
        _args: Namespace,
        request: DocumentProcessingPopulationRequest,
    ) -> DocumentProcessingPopulationResult:
        if request.apply:
            connection.execute("INSERT INTO operation_probe VALUES ('applied')")
            return receipt_result(mode="apply")
        current = receipt_result()
        if connection.execute("SELECT COUNT(*) FROM operation_probe").fetchone()[0] == 0:
            return current.model_copy(update={"output_commitment_sha256": "f" * 64})
        return current

    def verify_current(
        connection: sqlite3.Connection,
        receipt: DocumentProcessingOperationReceipt,
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
        verify_document_processing_receipt_current_result(receipt, current)

    monkeypatch.setattr(cli, "_run_operator", run_stub)
    monkeypatch.setattr(cli, "verify_document_processing_receipt_current", verify_current)
    argv = [
        "--db",
        str(database),
        "--cutoff-at",
        "2026-07-29T00:00:00Z",
        "--operation-recorded-at",
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
            connection.execute(
                "SELECT COUNT(*) FROM document_processing_operation_ledger"
            ).fetchone()[0]
            == 1
        )

    assert cli.main(argv) == 2


def test_apply_rechecks_admission_before_committing_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    _database(database)
    admission_path = tmp_path / "admission.json"
    receipt_path = tmp_path / "apply.json"
    admission_path.write_bytes((_planned_receipt(database).model_dump_json() + "\n").encode())

    def run_stub(
        connection: sqlite3.Connection,
        _args: Namespace,
        _request: DocumentProcessingPopulationRequest,
    ) -> DocumentProcessingPopulationResult:
        connection.execute("INSERT INTO operation_probe VALUES ('must-rollback')")
        admission_path.write_text('{"replaced":true}', encoding="utf-8")
        return receipt_result(mode="apply")

    monkeypatch.setattr(cli, "_run_operator", run_stub)
    argv = [
        "--db",
        str(database),
        "--cutoff-at",
        "2026-07-29T00:00:00Z",
        "--operation-recorded-at",
        "2026-07-29T00:00:00Z",
        "--apply",
        "--admission-receipt",
        str(admission_path),
        "--receipt",
        str(receipt_path),
    ]

    assert cli.main(argv) == 2
    conn = sqlite3.connect(database)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM document_processing_operation_ledger"
        ).fetchone()[0]
        probe_count = conn.execute("SELECT COUNT(*) FROM operation_probe").fetchone()[0]
    finally:
        conn.close()
    assert count == 0
    assert probe_count == 0
    assert not receipt_path.exists()


def test_population_uses_canonical_portfolio_lock_when_paths_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    _database(database)
    receipt_path = tmp_path / "dry-run.json"
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(database))
    called = False

    def run_stub(*_args: object, **_kwargs: object) -> DocumentProcessingPopulationResult:
        nonlocal called
        called = True
        return receipt_result()

    monkeypatch.setattr(cli, "_run_operator", run_stub)
    argv = [
        "--db",
        str(database),
        "--cutoff-at",
        "2026-07-29T00:00:00Z",
        "--operation-recorded-at",
        "2026-07-29T00:00:00Z",
        "--receipt",
        str(receipt_path),
    ]
    with cli.JobLock(cli.PROJECT_ROOT, "cutover-test", ["portfolio-db"]):
        assert cli.main(argv) == 75
    assert called is False


def test_resume_rejects_checkpoint_from_replaced_database_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    _database(database)
    cutoff = datetime(2026, 7, 29, tzinfo=UTC)
    prior_path = tmp_path / "prior.json"
    receipt_path = tmp_path / "resume-plan.json"
    prior = build_document_processing_receipt(
        database_path=str(database.resolve()),
        database_instance_id="database-instance:" + "2" * 32,
        alembic_revision="0264_document_processing_operation_ledger",
        request=DocumentProcessingPopulationRequest(
            cutoff_at=cutoff,
            operation_recorded_at=cutoff,
            apply=True,
            max_obligations=1,
            input_commitment_sha256="b" * 64,
            plan_commitment_sha256="c" * 64,
        ),
        result=receipt_result(mode="apply", bounded=True, remaining=1),
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256="e" * 64,
    )
    prior_path.write_bytes((prior.model_dump_json() + "\n").encode())
    called = False

    def run_stub(*_args: object, **_kwargs: object) -> DocumentProcessingPopulationResult:
        nonlocal called
        called = True
        return receipt_result(bounded=True, remaining=1)

    monkeypatch.setattr(cli, "_run_operator", run_stub)
    argv = [
        "--db",
        str(database),
        "--cutoff-at",
        "2026-07-29T00:00:00Z",
        "--operation-recorded-at",
        "2026-07-29T00:00:00Z",
        "--after-obligation-id",
        "obligation-1",
        "--max-obligations",
        "1",
        "--prior-checkpoint-receipt",
        str(prior_path),
        "--receipt",
        str(receipt_path),
    ]

    assert cli.main(argv) == 2
    assert called is False


def test_completed_checkpoint_can_dry_run_and_apply_unbounded_sealing_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    _database(database)
    cutoff = datetime(2026, 7, 29, tzinfo=UTC)
    prior_path = tmp_path / "prior.json"
    admission_path = tmp_path / "seal-plan.json"
    apply_path = tmp_path / "seal-apply.json"
    prior = build_document_processing_receipt(
        database_path=str(database.resolve()),
        database_instance_id=_DATABASE_INSTANCE_ID,
        alembic_revision=_HEAD_REVISION,
        request=DocumentProcessingPopulationRequest(
            cutoff_at=cutoff,
            operation_recorded_at=cutoff,
            apply=True,
            max_obligations=1,
            input_commitment_sha256="b" * 64,
            plan_commitment_sha256="c" * 64,
        ),
        result=receipt_result(mode="apply", bounded=True, remaining=0),
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256="e" * 64,
    )
    prior_path.write_bytes((prior.model_dump_json() + "\n").encode())
    with sqlite3.connect(database) as connection:
        persist_document_processing_receipt(connection, prior)

    def run_stub(
        _connection: sqlite3.Connection,
        _args: Namespace,
        request: DocumentProcessingPopulationRequest,
    ) -> DocumentProcessingPopulationResult:
        return receipt_result(mode="apply" if request.apply else "dry_run")

    monkeypatch.setattr(cli, "_run_operator", run_stub)
    base = [
        "--db",
        str(database),
        "--cutoff-at",
        "2026-07-29T00:00:00Z",
        "--operation-recorded-at",
        "2026-07-29T00:00:00Z",
        "--phase",
        "all",
        "--prior-checkpoint-receipt",
        str(prior_path),
    ]

    assert cli.main([*base, "--receipt", str(admission_path)]) == 0
    admission = DocumentProcessingOperationReceipt.model_validate_json(
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
    applied = DocumentProcessingOperationReceipt.model_validate_json(
        apply_path.read_text(encoding="utf-8")
    )
    assert applied.outcome == "complete"
    assert applied.prior_checkpoint_receipt_sha256 == _artifact_sha(prior)
