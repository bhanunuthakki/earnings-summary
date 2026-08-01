from __future__ import annotations

import sqlite3
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from execution import populate_canonical_resolution as cli
from provenance.population_canonical_resolution import (
    CanonicalResolutionCheckpoint,
    CanonicalResolutionOperationReceipt,
    CanonicalResolutionPopulationRequest,
    CanonicalResolutionPopulationResult,
    build_canonical_resolution_receipt,
)
from provenance.population_document_processing import (
    DocumentProcessingPopulationRequest,
    build_document_processing_receipt,
    persist_document_processing_receipt,
)
from tests.test_population_document_processing import receipt_result

_STAMP = datetime(2026, 7, 29, tzinfo=UTC)
_DATABASE_INSTANCE_ID = "database-instance:" + "1" * 32


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


def _document_prerequisite(database: Path, path: Path) -> None:
    receipt = build_document_processing_receipt(
        database_path=str(database.resolve()),
        database_instance_id=_DATABASE_INSTANCE_ID,
        alembic_revision="0264_document_processing_operation_ledger",
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
    path.write_text(receipt.model_dump_json(), encoding="utf-8")


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


def _planned_receipt(
    database: Path,
    document_sha: str,
) -> CanonicalResolutionOperationReceipt:
    return build_canonical_resolution_receipt(
        database_path=str(database.resolve()),
        database_instance_id=_DATABASE_INSTANCE_ID,
        alembic_revision="0267_source_definition_taxonomy_identity",
        request=CanonicalResolutionPopulationRequest(
            cutoff_at=_STAMP,
            operation_recorded_at=_STAMP,
        ),
        result=_result(),
        document_prerequisite_receipt_sha256=document_sha,
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256=None,
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
    admission_path.write_text(
        _planned_receipt(database, document_sha).model_dump_json(),
        encoding="utf-8",
    )
    calls = 0

    def run_stub(
        connection: sqlite3.Connection,
        _args: Namespace,
        _request: CanonicalResolutionPopulationRequest,
    ) -> CanonicalResolutionPopulationResult:
        nonlocal calls
        calls += 1
        connection.execute("INSERT INTO operation_probe VALUES ('applied')")
        return _result(mode="apply", state="complete")

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
        assert (
            conn.execute("SELECT COUNT(*) FROM canonical_resolution_operation_ledger").fetchone()[0]
            == 1
        )


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
    admission_path.write_text(
        _planned_receipt(database, document_sha).model_dump_json(),
        encoding="utf-8",
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
    admission_path.write_text(
        _planned_receipt(database, document_sha).model_dump_json(),
        encoding="utf-8",
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
    admission_path.write_text(
        _planned_receipt(database, document_sha).model_dump_json(),
        encoding="utf-8",
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
