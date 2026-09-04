"""Populate document obligations with immutable admission and checkpoint receipts."""

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
from provenance.population_cli_harness import (  # noqa: E402
    parse_timezone_aware_datetime,
    validate_protected_receipt_path,
)
from provenance.population_document_processing import (  # noqa: E402
    DocumentProcessingOperationReceipt,
    DocumentProcessingPopulationRequest,
    DocumentProcessingPopulationResult,
    build_document_processing_receipt,
    database_instance_id,
    document_processing_operation_id,
    load_document_processing_receipt,
    persist_document_processing_receipt,
    populate_document_processing,
    verify_document_processing_receipt,
    verify_document_processing_receipt_current,
)
from runtime.job_runtime import (  # noqa: E402
    JobAlreadyRunningError,
    JobLock,
    portfolio_db_path,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

DocumentPhase = Literal["obligations", "dispositions", "snapshots", "all"]


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
        choices=("obligations", "dispositions", "snapshots", "all"),
        default="all",
    )
    parser.add_argument("--after-obligation-id")
    parser.add_argument("--max-obligations", type=int)
    parser.add_argument("--input-commitment-sha256")
    parser.add_argument("--plan-commitment-sha256")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--admission-receipt", type=Path)
    parser.add_argument("--prior-checkpoint-receipt", type=Path)
    return parser


def admitted_apply_request(
    receipt: DocumentProcessingOperationReceipt,
    *,
    database: Path,
    cutoff_at: datetime,
    operation_recorded_at: datetime,
    phase: DocumentPhase,
    after_obligation_id: str | None,
    max_obligations: int | None,
) -> DocumentProcessingPopulationRequest:
    """Derive the write request only from one exact dry-run admission."""

    if not verify_document_processing_receipt(receipt):
        raise ValueError("document-processing admission receipt is invalid")
    if receipt.request.apply or receipt.outcome != "planned":
        raise ValueError("document-processing apply requires a planned dry-run admission")
    if receipt.database_path != str(database.resolve()):
        raise ValueError("document-processing admission database does not match")
    expected = receipt.request
    if (
        expected.cutoff_at != cutoff_at
        or expected.operation_recorded_at != operation_recorded_at
        or expected.phase != phase
        or expected.after_processing_obligation_revision_id != after_obligation_id
        or expected.max_obligations != max_obligations
    ):
        raise ValueError("document-processing admission request does not match")
    return expected.model_copy(
        update={
            "apply": True,
            "input_commitment_sha256": receipt.result.input_commitment_sha256,
            "plan_commitment_sha256": receipt.result.plan_commitment_sha256,
        }
    )


def validate_checkpoint_resume(
    receipt: DocumentProcessingOperationReceipt,
    *,
    database: Path,
    cutoff_at: datetime,
    operation_recorded_at: datetime,
    phase: DocumentPhase,
    after_obligation_id: str | None,
    max_obligations: int | None,
) -> None:
    """Require a resume to start at one exact last-successful cursor."""

    if not verify_document_processing_receipt(receipt):
        raise ValueError("prior checkpoint receipt is invalid")
    if receipt.outcome != "checkpoint":
        raise ValueError("prior receipt is not a checkpoint")
    if receipt.database_path != str(database.resolve()):
        raise ValueError("checkpoint database does not match")
    if (
        receipt.request.cutoff_at != cutoff_at
        or receipt.request.operation_recorded_at != operation_recorded_at
    ):
        raise ValueError("checkpoint temporal scope does not match")
    if max_obligations is not None:
        if not receipt.result.checkpoint.can_resume:
            raise ValueError("prior receipt is not a resumable checkpoint")
        if receipt.request.phase != phase or receipt.result.phase != phase:
            raise ValueError("checkpoint phase does not match")
        if receipt.request.max_obligations != max_obligations:
            raise ValueError("checkpoint batch shape does not match")
        if receipt.result.checkpoint.last_processing_obligation_revision_id != after_obligation_id:
            raise ValueError("resume cursor does not match the last successful checkpoint")
    elif (
        after_obligation_id is not None
        or phase not in {"snapshots", "all"}
        or receipt.result.checkpoint.remaining_obligation_count != 0
    ):
        raise ValueError("document sealing handoff requires a completed checkpoint")


def validate_checkpoint_successor(
    receipt: DocumentProcessingOperationReceipt,
    *,
    request: DocumentProcessingPopulationRequest,
    result: DocumentProcessingPopulationResult,
    alembic_revision: str,
) -> None:
    """Bind a resumed plan to the exact state left by its parent checkpoint."""

    if alembic_revision != receipt.alembic_revision:
        raise ValueError("checkpoint database revision changed")
    bounded = (
        request.after_processing_obligation_revision_id is not None
        or request.max_obligations is not None
    )
    if bounded:
        if request.phase != receipt.request.phase or result.phase != receipt.result.phase:
            raise ValueError("checkpoint successor phase changed")
        if request.max_obligations != receipt.request.max_obligations:
            raise ValueError("checkpoint successor batch shape changed")
    elif (
        request.phase not in {"snapshots", "all"}
        or receipt.result.checkpoint.remaining_obligation_count != 0
    ):
        raise ValueError("document checkpoint is not ready for sealing")
    if result.input_commitment_sha256 != receipt.result.post_state_commitment_sha256:
        raise ValueError("document-processing input changed since prior checkpoint")
    if result.selection_commitment_sha256 != receipt.result.selection_commitment_sha256:
        raise ValueError("document-processing selection changed since prior checkpoint")
    if (
        not request.apply
        and result.output_commitment_sha256 != receipt.result.output_commitment_sha256
    ):
        raise ValueError("document-processing output changed since prior checkpoint")


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
        conflict_message="document-processing receipt aliases a protected artifact",
    )


def load_document_processing_receipt_artifact(
    path: Path,
) -> tuple[ImmutableArtifactSnapshot, DocumentProcessingOperationReceipt]:
    snapshot, payload = read_stable_artifact(path)
    try:
        receipt = DocumentProcessingOperationReceipt.model_validate_json(payload)
    except ValueError as exc:
        raise ValueError("document-processing receipt payload is invalid") from exc
    if not verify_document_processing_receipt(receipt):
        raise ValueError("document-processing receipt commitment is invalid")
    require_canonical_text_artifact(snapshot, receipt.model_dump_json())
    return snapshot, receipt


def _request_from_args(args: argparse.Namespace) -> DocumentProcessingPopulationRequest:
    return DocumentProcessingPopulationRequest(
        cutoff_at=args.cutoff_at,
        operation_recorded_at=args.operation_recorded_at,
        apply=False,
        phase=cast(DocumentPhase, args.phase),
        after_processing_obligation_revision_id=args.after_obligation_id,
        max_obligations=args.max_obligations,
        input_commitment_sha256=args.input_commitment_sha256,
        plan_commitment_sha256=args.plan_commitment_sha256,
    )


def _load_existing_output(
    path: Path,
) -> tuple[ImmutableArtifactSnapshot, DocumentProcessingOperationReceipt] | None:
    if not path.exists():
        return None
    return load_document_processing_receipt_artifact(path)


def _database_file_identity(path: Path) -> tuple[int, int]:
    require_no_reparse_points(path)
    metadata = os.stat(path, follow_symlinks=False)
    return int(metadata.st_dev), int(metadata.st_ino)


def _run_operator(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    request: DocumentProcessingPopulationRequest,
) -> DocumentProcessingPopulationResult:
    del args
    return populate_document_processing(conn, request)


def _revision(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT version_num FROM alembic_version ORDER BY version_num").fetchall()
    if len(rows) != 1:
        raise ValueError("document-processing database must have one Alembic revision")
    return str(rows[0][0])


def _execute(args: argparse.Namespace) -> DocumentProcessingOperationReceipt:
    if args.apply and args.admission_receipt is None:
        raise ValueError("--apply requires --admission-receipt")
    if not args.apply and args.admission_receipt is not None:
        raise ValueError("--admission-receipt is valid only with --apply")
    if args.apply and (
        args.input_commitment_sha256 is not None or args.plan_commitment_sha256 is not None
    ):
        raise ValueError("apply commitments must come only from the admission receipt")
    if not args.apply:
        if args.after_obligation_id is not None and args.prior_checkpoint_receipt is None:
            raise ValueError("a resume dry-run requires --prior-checkpoint-receipt")
        if (
            args.prior_checkpoint_receipt is not None
            and args.after_obligation_id is None
            and (args.max_obligations is not None or args.phase not in {"snapshots", "all"})
        ):
            raise ValueError(
                "an unbounded prior checkpoint is valid only for a document sealing dry-run"
            )

    input_paths = tuple(
        path for path in (args.admission_receipt, args.prior_checkpoint_receipt) if path is not None
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
    with JobLock(PROJECT_ROOT, "populate-document-processing", resources):
        database_path = validate_population_database_target(
            args.db, portfolio_db_path(PROJECT_ROOT)
        )
        admission_snapshot: ImmutableArtifactSnapshot | None = None
        prior_snapshot: ImmutableArtifactSnapshot | None = None
        admission: DocumentProcessingOperationReceipt | None = None
        prior: DocumentProcessingOperationReceipt | None = None
        if args.admission_receipt is not None:
            admission_snapshot, admission = load_document_processing_receipt_artifact(
                args.admission_receipt
            )
        if args.prior_checkpoint_receipt is not None:
            prior_snapshot, prior = load_document_processing_receipt_artifact(
                args.prior_checkpoint_receipt
            )

        if admission is not None:
            expected_prior_sha = admission.prior_checkpoint_receipt_sha256
            actual_prior_sha = None if prior_snapshot is None else prior_snapshot.file_sha256
            if actual_prior_sha != expected_prior_sha:
                raise ValueError("document apply prior checkpoint differs from its admission")
            request = admitted_apply_request(
                admission,
                database=args.db,
                cutoff_at=args.cutoff_at,
                operation_recorded_at=args.operation_recorded_at,
                phase=cast(DocumentPhase, args.phase),
                after_obligation_id=args.after_obligation_id,
                max_obligations=args.max_obligations,
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
                phase=cast(DocumentPhase, args.phase),
                after_obligation_id=args.after_obligation_id,
                max_obligations=args.max_obligations,
            )
        existing_output = _load_existing_output(receipt_path)
        file_identity = _database_file_identity(database_path)
        role = SQLiteConnectionRole.WRITER if request.apply else SQLiteConnectionRole.READ_ONLY
        conn = connect_sqlite(database_path, role=role, schema_preflight=request.apply)
        try:
            conn.execute("BEGIN IMMEDIATE" if request.apply else "BEGIN")
            revision = _revision(conn)
            if expected_revision is not None and revision != expected_revision:
                raise ValueError("document-processing admission revision does not match")
            instance_id = database_instance_id(conn)
            if admission is not None and instance_id != admission.database_instance_id:
                raise ValueError("document-processing admission database identity changed")
            if prior is not None and instance_id != prior.database_instance_id:
                raise ValueError("document-processing checkpoint database identity changed")
            if (
                prior is not None
                and load_document_processing_receipt(conn, prior.operation_id) != prior
            ):
                raise ValueError("document-processing checkpoint is not canonical in the ledger")
            admission_sha = None if admission_snapshot is None else admission_snapshot.file_sha256
            prior_sha = (
                prior_snapshot.file_sha256
                if prior_snapshot is not None
                else (None if admission is None else admission.prior_checkpoint_receipt_sha256)
            )
            operation_id = document_processing_operation_id(
                database_instance_id=instance_id,
                request=request,
                admission_receipt_sha256=admission_sha,
                prior_checkpoint_receipt_sha256=prior_sha,
            )
            if existing_output is not None and existing_output[1].operation_id != operation_id:
                raise ImmutableArtifactConflictError(
                    "immutable receipt destination belongs to another operation"
                )
            stored = load_document_processing_receipt(conn, operation_id) if request.apply else None
            if stored is not None:
                if existing_output is not None and existing_output[1] != stored:
                    raise ImmutableArtifactConflictError(
                        "exported receipt differs from the operation ledger"
                    )
                verify_document_processing_receipt_current(conn, stored)
                if admission_snapshot is not None:
                    assert_artifact_unchanged(admission_snapshot)
                if prior_snapshot is not None:
                    assert_artifact_unchanged(prior_snapshot)
                conn.rollback()
                receipt = stored
            else:
                if request.apply and existing_output is not None:
                    raise ImmutableArtifactConflictError(
                        "apply receipt exists without its database operation ledger"
                    )
                result = _run_operator(conn, args, request)
                if prior is not None:
                    validate_checkpoint_successor(
                        prior,
                        request=request,
                        result=result,
                        alembic_revision=revision,
                    )
                if admission_snapshot is not None:
                    assert_artifact_unchanged(admission_snapshot)
                if prior_snapshot is not None:
                    assert_artifact_unchanged(prior_snapshot)
                if _database_file_identity(database_path) != file_identity:
                    raise ValueError("document-processing database file identity changed")
                if database_instance_id(conn) != instance_id:
                    raise ValueError("document-processing database identity changed")
                receipt = build_document_processing_receipt(
                    database_path=str(database_path),
                    database_instance_id=instance_id,
                    alembic_revision=revision,
                    request=request,
                    result=result,
                    prior_checkpoint_receipt_sha256=prior_sha,
                    admission_receipt_sha256=admission_sha,
                )
                if request.apply:
                    persist_document_processing_receipt(conn, receipt)
                    conn.commit()
                else:
                    conn.rollback()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        publish_text_no_clobber(receipt_path, receipt.model_dump_json())
        exported_snapshot, exported = load_document_processing_receipt_artifact(receipt_path)
        if exported != receipt:
            raise ImmutableArtifactConflictError(
                "exported receipt differs from the committed operation receipt"
            )
        assert_artifact_unchanged(exported_snapshot)
        return receipt


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        receipt = _execute(args)
    except JobAlreadyRunningError:
        _event("document_processing_population_deferred", reason="job_lock_held")
        return 75
    except Exception as exc:
        _event(
            "document_processing_population_refused",
            error_type=type(exc).__name__,
            detail=redact(exc),
        )
        return 2
    _event(
        "document_processing_population_completed",
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
