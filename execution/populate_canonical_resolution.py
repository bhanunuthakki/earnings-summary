"""Populate canonical resolution with immutable prerequisite and operation receipts."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from log_redact import redact  # noqa: E402
from provenance.immutable_artifact import (  # noqa: E402
    ImmutableArtifactConflictError,
    ImmutableArtifactSnapshot,
    assert_artifact_unchanged,
    population_database_lock_resources,
    publish_text_no_clobber,
    read_stable_artifact,
    require_canonical_text_artifact,
    require_no_reparse_points,
    validate_population_database_target,
)
from provenance.population_canonical_resolution import (  # noqa: E402
    CanonicalResolutionOperationReceipt,
    CanonicalResolutionPopulationRequest,
    CanonicalResolutionPopulationResult,
    build_canonical_resolution_receipt,
    canonical_resolution_operation_id,
    database_instance_id,
    load_canonical_resolution_receipt,
    persist_canonical_resolution_receipt,
    populate_canonical_resolution,
    verify_canonical_resolution_receipt,
    verify_canonical_resolution_receipt_current,
)
from provenance.population_cli_harness import (  # noqa: E402
    parse_timezone_aware_datetime,
    validate_protected_receipt_path,
)
from provenance.population_document_processing import (  # noqa: E402
    DocumentProcessingOperationReceipt,
    verify_document_processing_receipt,
)
from runtime.job_runtime import (  # noqa: E402
    JobAlreadyRunningError,
    JobLock,
    portfolio_db_path,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

CanonicalPhase = Literal["resolutions", "snapshots", "projections", "all"]


def _datetime(value: str) -> datetime:
    return parse_timezone_aware_datetime(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--cutoff-at", type=_datetime, required=True)
    parser.add_argument(
        "--operation-recorded-at",
        "--recorded-at",
        dest="operation_recorded_at",
        type=_datetime,
        required=True,
    )
    parser.add_argument(
        "--phase",
        choices=("resolutions", "snapshots", "projections", "all"),
        default="all",
    )
    parser.add_argument("--after-cell-id")
    parser.add_argument("--max-cells", type=int)
    parser.add_argument("--input-commitment-sha256")
    parser.add_argument("--plan-commitment-sha256")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--document-prerequisite-receipt", type=Path, required=True)
    parser.add_argument("--admission-receipt", type=Path)
    parser.add_argument("--prior-checkpoint-receipt", type=Path)
    return parser


def admitted_apply_request(
    receipt: CanonicalResolutionOperationReceipt,
    *,
    database: Path,
    cutoff_at: datetime,
    operation_recorded_at: datetime,
    phase: CanonicalPhase,
    after_cell_id: str | None,
    max_cells: int | None,
    document_prerequisite_sha256: str,
) -> CanonicalResolutionPopulationRequest:
    """Derive a write request only from one exact dry-run admission."""

    if not verify_canonical_resolution_receipt(receipt):
        raise ValueError("canonical admission receipt is invalid")
    if receipt.request.apply or receipt.outcome != "planned":
        raise ValueError("canonical apply requires a planned dry-run admission")
    if receipt.database_path != str(database.resolve()):
        raise ValueError("canonical admission database does not match")
    if receipt.document_prerequisite_receipt_sha256 != document_prerequisite_sha256:
        raise ValueError("canonical admission document prerequisite changed")
    expected = receipt.request
    if (
        expected.cutoff_at != cutoff_at
        or expected.operation_recorded_at != operation_recorded_at
        or expected.phase != phase
        or expected.after_canonical_metric_cell_id != after_cell_id
        or expected.max_cells != max_cells
    ):
        raise ValueError("canonical admission request does not match")
    return expected.model_copy(
        update={
            "apply": True,
            "input_commitment_sha256": receipt.result.input_commitment_sha256,
            "plan_commitment_sha256": receipt.result.plan_commitment_sha256,
        }
    )


def validate_checkpoint_resume(
    receipt: CanonicalResolutionOperationReceipt,
    *,
    database: Path,
    cutoff_at: datetime,
    operation_recorded_at: datetime,
    phase: CanonicalPhase,
    after_cell_id: str | None,
    max_cells: int | None,
) -> None:
    """Require a resume or sealing handoff from one exact checkpoint."""

    if not verify_canonical_resolution_receipt(receipt) or receipt.outcome != "checkpoint":
        raise ValueError("prior canonical receipt is not a checkpoint")
    if receipt.database_path != str(database.resolve()):
        raise ValueError("canonical checkpoint database does not match")
    if (
        receipt.request.cutoff_at != cutoff_at
        or receipt.request.operation_recorded_at != operation_recorded_at
    ):
        raise ValueError("canonical checkpoint temporal scope does not match")
    bounded = after_cell_id is not None or max_cells is not None
    if bounded:
        if not receipt.result.checkpoint.can_resume:
            raise ValueError("canonical prior receipt is not a resumable checkpoint")
        if receipt.request.phase != phase or receipt.result.phase != phase:
            raise ValueError("canonical checkpoint phase does not match")
        if receipt.request.max_cells != max_cells:
            raise ValueError("canonical checkpoint batch shape does not match")
        if receipt.result.last_canonical_metric_cell_id != after_cell_id:
            raise ValueError("canonical resume cursor does not match the checkpoint")
    elif phase not in {"snapshots", "projections", "all"} or (
        receipt.result.checkpoint.remaining_cell_count != 0
    ):
        raise ValueError("canonical sealing handoff requires a completed checkpoint")


def validate_checkpoint_successor(
    receipt: CanonicalResolutionOperationReceipt,
    *,
    request: CanonicalResolutionPopulationRequest,
    result: CanonicalResolutionPopulationResult,
    alembic_revision: str,
) -> None:
    """Bind a resumed plan to the exact state left by its parent checkpoint."""

    if alembic_revision != receipt.alembic_revision:
        raise ValueError("canonical checkpoint database revision changed")
    bounded = request.after_canonical_metric_cell_id is not None or request.max_cells is not None
    if bounded:
        if not receipt.result.checkpoint.can_resume:
            raise ValueError("canonical checkpoint successor parent cannot resume")
        if request.phase != receipt.request.phase or result.phase != receipt.result.phase:
            raise ValueError("canonical checkpoint successor phase changed")
        if request.max_cells != receipt.request.max_cells:
            raise ValueError("canonical checkpoint successor batch shape changed")
        if request.after_canonical_metric_cell_id != receipt.result.last_canonical_metric_cell_id:
            raise ValueError("canonical checkpoint successor cursor changed")
    elif (
        request.phase not in {"snapshots", "projections", "all"}
        or receipt.result.checkpoint.remaining_cell_count != 0
    ):
        raise ValueError("canonical checkpoint is not ready for sealing")
    if result.output_commitment_sha256 != receipt.result.post_state_commitment_sha256:
        raise ValueError("canonical state changed since prior checkpoint")
    if result.input_commitment_sha256 != receipt.result.input_commitment_sha256:
        raise ValueError("canonical input changed since prior checkpoint")
    if result.resolution_plan_commitment_sha256 != receipt.result.resolution_plan_commitment_sha256:
        raise ValueError("canonical resolution plan changed since prior checkpoint")


def validate_receipt_path(
    receipt: Path,
    *,
    database: Path,
    protected_receipts: tuple[Path, ...],
) -> Path:
    """Reject output aliases to the database, sidecars, or input receipts."""

    return validate_protected_receipt_path(
        receipt,
        database=database,
        protected_receipts=protected_receipts,
        conflict_message="canonical receipt aliases a protected artifact",
    )


def load_canonical_resolution_receipt_artifact(
    path: Path,
) -> tuple[ImmutableArtifactSnapshot, CanonicalResolutionOperationReceipt]:
    snapshot, payload = read_stable_artifact(path)
    try:
        receipt = CanonicalResolutionOperationReceipt.model_validate_json(payload)
    except ValueError as exc:
        raise ValueError("canonical receipt payload is invalid") from exc
    if not verify_canonical_resolution_receipt(receipt):
        raise ValueError("canonical receipt commitment is invalid")
    require_canonical_text_artifact(snapshot, receipt.model_dump_json())
    return snapshot, receipt


def _load_document_receipt(
    path: Path,
) -> tuple[ImmutableArtifactSnapshot, DocumentProcessingOperationReceipt]:
    snapshot, payload = read_stable_artifact(path)
    try:
        receipt = DocumentProcessingOperationReceipt.model_validate_json(payload)
    except ValueError as exc:
        raise ValueError("document prerequisite payload is invalid") from exc
    if not verify_document_processing_receipt(receipt):
        raise ValueError("document prerequisite commitment is invalid")
    require_canonical_text_artifact(snapshot, receipt.model_dump_json())
    return snapshot, receipt


def _validate_document_prerequisite(
    conn: sqlite3.Connection,
    receipt: DocumentProcessingOperationReceipt,
    *,
    database: Path,
    database_instance: str,
    cutoff_at: datetime,
    operation_recorded_at: datetime,
) -> None:
    if receipt.database_path != str(database.resolve()):
        raise ValueError("document prerequisite database does not match")
    if receipt.database_instance_id != database_instance:
        raise ValueError("document prerequisite database identity changed")
    if (
        receipt.request.cutoff_at != cutoff_at
        or receipt.request.operation_recorded_at != operation_recorded_at
    ):
        raise ValueError("document prerequisite temporal scope does not match")
    if (
        receipt.outcome != "complete"
        or any(receipt.blocker_counts.values())
        or receipt.result.processing_snapshot_count < 1
        or not receipt.result.checkpoint.safe_to_seal
    ):
        raise ValueError("document prerequisite is missing, unresolved, or incomplete")
    stored = conn.execute(
        "SELECT receipt_json FROM document_processing_operation_ledger WHERE operation_id=?",
        (receipt.operation_id,),
    ).fetchone()
    if stored is None or str(stored[0]) != receipt.model_dump_json():
        raise ValueError("document prerequisite is not the canonical database receipt")


def _request_from_args(args: argparse.Namespace) -> CanonicalResolutionPopulationRequest:
    return CanonicalResolutionPopulationRequest(
        cutoff_at=args.cutoff_at,
        operation_recorded_at=args.operation_recorded_at,
        apply=False,
        phase=cast(CanonicalPhase, args.phase),
        after_canonical_metric_cell_id=args.after_cell_id,
        max_cells=args.max_cells,
        input_commitment_sha256=args.input_commitment_sha256,
        plan_commitment_sha256=args.plan_commitment_sha256,
    )


def _database_file_identity(path: Path) -> tuple[int, int]:
    require_no_reparse_points(path)
    metadata = os.stat(path, follow_symlinks=False)
    return int(metadata.st_dev), int(metadata.st_ino)


def _revision(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT version_num FROM alembic_version ORDER BY version_num").fetchall()
    if len(rows) != 1:
        raise ValueError("canonical database must have one Alembic revision")
    return str(rows[0][0])


def _run_operator(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    request: CanonicalResolutionPopulationRequest,
) -> CanonicalResolutionPopulationResult:
    del args
    return populate_canonical_resolution(conn, request)


def _execute(args: argparse.Namespace) -> CanonicalResolutionOperationReceipt:
    if args.apply and args.admission_receipt is None:
        raise ValueError("--apply requires --admission-receipt")
    if not args.apply and args.admission_receipt is not None:
        raise ValueError("--admission-receipt is valid only with --apply")
    if args.apply and (
        args.input_commitment_sha256 is not None or args.plan_commitment_sha256 is not None
    ):
        raise ValueError("apply commitments must come only from the admission receipt")
    if (
        args.after_cell_id is not None
        and not args.apply
        and (args.prior_checkpoint_receipt is None)
    ):
        raise ValueError("a canonical resume dry-run requires --prior-checkpoint-receipt")

    input_paths = tuple(
        path
        for path in (
            args.document_prerequisite_receipt,
            args.admission_receipt,
            args.prior_checkpoint_receipt,
        )
        if path is not None
    )
    receipt_path = validate_receipt_path(
        args.receipt,
        database=args.db,
        protected_receipts=input_paths,
    )
    resources = [
        *population_database_lock_resources(args.db, portfolio_db_path(PROJECT_ROOT)),
        f"artifact:{receipt_path}",
        *(f"artifact:{Path(os.path.abspath(path))}" for path in input_paths),
    ]
    with JobLock(PROJECT_ROOT, "populate-canonical-resolution", resources):
        database_path = validate_population_database_target(
            args.db, portfolio_db_path(PROJECT_ROOT)
        )
        document_snapshot, document = _load_document_receipt(args.document_prerequisite_receipt)
        admission_snapshot: ImmutableArtifactSnapshot | None = None
        prior_snapshot: ImmutableArtifactSnapshot | None = None
        admission: CanonicalResolutionOperationReceipt | None = None
        prior: CanonicalResolutionOperationReceipt | None = None
        if args.admission_receipt is not None:
            admission_snapshot, admission = load_canonical_resolution_receipt_artifact(
                args.admission_receipt
            )
        if args.prior_checkpoint_receipt is not None:
            prior_snapshot, prior = load_canonical_resolution_receipt_artifact(
                args.prior_checkpoint_receipt
            )
        if admission is not None:
            expected_prior_sha = admission.prior_checkpoint_receipt_sha256
            actual_prior_sha = None if prior_snapshot is None else prior_snapshot.file_sha256
            if actual_prior_sha != expected_prior_sha:
                raise ValueError("canonical apply prior checkpoint differs from its admission")
            request = admitted_apply_request(
                admission,
                database=args.db,
                cutoff_at=args.cutoff_at,
                operation_recorded_at=args.operation_recorded_at,
                phase=cast(CanonicalPhase, args.phase),
                after_cell_id=args.after_cell_id,
                max_cells=args.max_cells,
                document_prerequisite_sha256=document_snapshot.file_sha256,
            )
            expected_revision = admission.alembic_revision
        else:
            request = _request_from_args(args)
            expected_revision = None
        if prior is not None:
            validate_checkpoint_resume(
                prior,
                database=args.db,
                cutoff_at=args.cutoff_at,
                operation_recorded_at=args.operation_recorded_at,
                phase=cast(CanonicalPhase, args.phase),
                after_cell_id=args.after_cell_id,
                max_cells=args.max_cells,
            )
        existing_output = (
            None
            if not receipt_path.exists()
            else load_canonical_resolution_receipt_artifact(receipt_path)
        )
        file_identity = _database_file_identity(database_path)
        role = SQLiteConnectionRole.WRITER if request.apply else SQLiteConnectionRole.READ_ONLY
        conn = connect_sqlite(database_path, role=role, schema_preflight=request.apply)
        try:
            conn.execute("BEGIN IMMEDIATE" if request.apply else "BEGIN")
            revision = _revision(conn)
            if expected_revision is not None and revision != expected_revision:
                raise ValueError("canonical admission revision does not match")
            instance_id = database_instance_id(conn)
            if admission is not None and instance_id != admission.database_instance_id:
                raise ValueError("canonical admission database identity changed")
            if prior is not None and instance_id != prior.database_instance_id:
                raise ValueError("canonical checkpoint database identity changed")
            if (
                prior is not None
                and load_canonical_resolution_receipt(conn, prior.operation_id) != prior
            ):
                raise ValueError("canonical checkpoint is not canonical in the ledger")
            _validate_document_prerequisite(
                conn,
                document,
                database=database_path,
                database_instance=instance_id,
                cutoff_at=args.cutoff_at,
                operation_recorded_at=args.operation_recorded_at,
            )
            admission_sha = None if admission_snapshot is None else admission_snapshot.file_sha256
            prior_sha = (
                prior_snapshot.file_sha256
                if prior_snapshot is not None
                else (None if admission is None else admission.prior_checkpoint_receipt_sha256)
            )
            operation_id = canonical_resolution_operation_id(
                database_instance_id=instance_id,
                request=request,
                document_prerequisite_receipt_sha256=document_snapshot.file_sha256,
                admission_receipt_sha256=admission_sha,
                prior_checkpoint_receipt_sha256=prior_sha,
            )
            if existing_output is not None and existing_output[1].operation_id != operation_id:
                raise ImmutableArtifactConflictError(
                    "immutable canonical receipt belongs to another operation"
                )
            stored = (
                load_canonical_resolution_receipt(conn, operation_id) if request.apply else None
            )
            if stored is not None:
                if existing_output is not None and existing_output[1] != stored:
                    raise ImmutableArtifactConflictError(
                        "exported canonical receipt differs from its ledger"
                    )
                verify_canonical_resolution_receipt_current(conn, stored)
                assert_artifact_unchanged(document_snapshot)
                if admission_snapshot is not None:
                    assert_artifact_unchanged(admission_snapshot)
                if prior_snapshot is not None:
                    assert_artifact_unchanged(prior_snapshot)
                conn.rollback()
                receipt = stored
            else:
                if request.apply and existing_output is not None:
                    raise ImmutableArtifactConflictError(
                        "canonical apply receipt exists without its database ledger"
                    )
                result = _run_operator(conn, args, request)
                if (
                    admission is not None
                    and result.resolution_plan_commitment_sha256
                    != admission.result.resolution_plan_commitment_sha256
                ):
                    raise ValueError("canonical resolution plan changed since dry-run admission")
                if prior is not None:
                    validate_checkpoint_successor(
                        prior,
                        request=request,
                        result=result,
                        alembic_revision=revision,
                    )
                assert_artifact_unchanged(document_snapshot)
                if admission_snapshot is not None:
                    assert_artifact_unchanged(admission_snapshot)
                if prior_snapshot is not None:
                    assert_artifact_unchanged(prior_snapshot)
                if _database_file_identity(database_path) != file_identity:
                    raise ValueError("canonical database file identity changed")
                if database_instance_id(conn) != instance_id:
                    raise ValueError("canonical database identity changed")
                receipt = build_canonical_resolution_receipt(
                    database_path=str(database_path),
                    database_instance_id=instance_id,
                    alembic_revision=revision,
                    request=request,
                    result=result,
                    document_prerequisite_receipt_sha256=document_snapshot.file_sha256,
                    prior_checkpoint_receipt_sha256=prior_sha,
                    admission_receipt_sha256=admission_sha,
                )
                if request.apply:
                    persist_canonical_resolution_receipt(conn, receipt)
                    conn.commit()
                else:
                    conn.rollback()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        publish_text_no_clobber(receipt_path, receipt.model_dump_json())
        exported_snapshot, exported = load_canonical_resolution_receipt_artifact(receipt_path)
        if exported != receipt:
            raise ImmutableArtifactConflictError(
                "exported canonical receipt differs from the canonical receipt"
            )
        assert_artifact_unchanged(exported_snapshot)
        return receipt


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        receipt = _execute(args)
    except JobAlreadyRunningError:
        _event("canonical_resolution_population_deferred", reason="job_lock_held")
        return 75
    except Exception as exc:
        _event(
            "canonical_resolution_population_refused",
            error_type=type(exc).__name__,
            detail=redact(exc),
        )
        return 2
    _event(
        "canonical_resolution_population_completed",
        outcome=receipt.outcome,
        blocker_counts=receipt.blocker_counts,
        receipt=str(args.receipt.resolve()),
    )
    sys.stdout.write(receipt.model_dump_json() + "\n")
    if receipt.outcome == "blocked":
        return 2
    if receipt.outcome == "checkpoint":
        return 3
    return 0


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
