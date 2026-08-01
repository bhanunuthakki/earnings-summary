"""Populate metric ontology with immutable admission and checkpoint receipts."""

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
    path_aliases_any,
    publish_text_no_clobber,
    read_stable_artifact,
    require_no_reparse_points,
)
from provenance.population_metric_ontology import (  # noqa: E402
    MetricOntologyOperationReceipt,
    MetricOntologyPopulationRequest,
    MetricOntologyPopulationResult,
    build_metric_ontology_receipt,
    database_instance_id,
    load_metric_ontology_receipt,
    metric_ontology_operation_id,
    persist_metric_ontology_receipt,
    populate_metric_ontology,
    verify_metric_ontology_receipt,
)
from runtime.job_runtime import (  # noqa: E402
    JobAlreadyRunningError,
    JobLock,
    portfolio_db_path,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

OntologyPhase = Literal["registry", "assertions", "bindings", "snapshot", "all"]


def _datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("datetime must include a timezone")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--cutoff-at", type=_datetime, required=True)
    parser.add_argument("--recorded-at", type=_datetime, required=True)
    parser.add_argument(
        "--phase",
        choices=("registry", "assertions", "bindings", "snapshot", "all"),
        default="all",
    )
    parser.add_argument("--after-observation-id")
    parser.add_argument("--max-observations", type=int)
    parser.add_argument("--input-commitment-sha256")
    parser.add_argument("--plan-commitment-sha256")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--admission-receipt", type=Path)
    parser.add_argument("--prior-checkpoint-receipt", type=Path)
    return parser


def admitted_apply_request(
    receipt: MetricOntologyOperationReceipt,
    *,
    database: Path,
    knowledge_cutoff: datetime,
    operation_recorded_at: datetime,
    phase: OntologyPhase,
    after_observation_id: str | None,
    max_observations: int | None,
) -> MetricOntologyPopulationRequest:
    """Derive a write request only from one exact dry-run admission."""

    if not verify_metric_ontology_receipt(receipt):
        raise ValueError("ontology admission receipt is invalid")
    if receipt.request.apply or receipt.outcome != "planned":
        raise ValueError("ontology apply requires a planned dry-run admission")
    if receipt.database_path != str(database.resolve()):
        raise ValueError("ontology admission database does not match")
    expected = receipt.request
    if (
        expected.knowledge_cutoff != knowledge_cutoff
        or expected.operation_recorded_at != operation_recorded_at
        or expected.phase != phase
        or expected.after_observation_id != after_observation_id
        or expected.max_observations != max_observations
    ):
        raise ValueError("ontology admission request does not match")
    return expected.model_copy(
        update={
            "apply": True,
            "input_commitment_sha256": receipt.result.input_commitment_sha256,
            "plan_commitment_sha256": receipt.result.plan_commitment_sha256,
        }
    )


def validate_checkpoint_resume(
    receipt: MetricOntologyOperationReceipt,
    *,
    database: Path,
    knowledge_cutoff: datetime,
    operation_recorded_at: datetime,
    phase: OntologyPhase,
    after_observation_id: str | None,
    max_observations: int | None,
) -> None:
    """Require a resume to start at the exact last successful cursor."""

    if not verify_metric_ontology_receipt(receipt) or receipt.outcome != "checkpoint":
        raise ValueError("prior ontology receipt is not a checkpoint")
    if receipt.database_path != str(database.resolve()):
        raise ValueError("ontology checkpoint database does not match")
    if (
        receipt.request.knowledge_cutoff != knowledge_cutoff
        or receipt.request.operation_recorded_at != operation_recorded_at
    ):
        raise ValueError("ontology checkpoint temporal scope does not match")
    if receipt.request.phase != phase or receipt.result.phase != phase:
        raise ValueError("ontology checkpoint phase does not match")
    if receipt.request.max_observations != max_observations:
        raise ValueError("ontology checkpoint batch shape does not match")
    if receipt.result.last_observation_id != after_observation_id:
        raise ValueError("ontology resume cursor does not match the checkpoint")


def validate_checkpoint_successor(
    receipt: MetricOntologyOperationReceipt,
    *,
    request: MetricOntologyPopulationRequest,
    result: MetricOntologyPopulationResult,
    alembic_revision: str,
) -> None:
    """Bind a resumed plan to the exact state left by its parent checkpoint."""

    if alembic_revision != receipt.alembic_revision:
        raise ValueError("ontology checkpoint database revision changed")
    if request.phase != receipt.request.phase or result.phase != receipt.result.phase:
        raise ValueError("ontology checkpoint successor phase changed")
    if request.max_observations != receipt.request.max_observations:
        raise ValueError("ontology checkpoint successor batch shape changed")
    if result.output_commitment_sha256 != receipt.result.post_state_commitment_sha256:
        raise ValueError("ontology state changed since prior checkpoint")
    if result.input_commitment_sha256 != receipt.result.input_commitment_sha256:
        raise ValueError("ontology source input changed since prior checkpoint")


def validate_receipt_path(
    receipt: Path,
    *,
    database: Path,
    protected_receipts: tuple[Path, ...],
) -> Path:
    """Reject output aliases to the database, sidecars, or input receipts."""

    for path in (receipt, database, *protected_receipts):
        require_no_reparse_points(path)
    destination = Path(os.path.abspath(receipt))
    database_path = Path(os.path.abspath(database))
    protected = {
        database_path,
        *(
            Path(os.path.abspath(f"{database_path}{suffix}"))
            for suffix in ("-wal", "-shm", "-journal")
        ),
        *(Path(os.path.abspath(path)) for path in protected_receipts),
    }
    if path_aliases_any(destination, protected):
        raise ValueError("ontology receipt aliases a protected artifact")
    return destination


def _load_receipt(
    path: Path,
) -> tuple[ImmutableArtifactSnapshot, MetricOntologyOperationReceipt]:
    snapshot, payload = read_stable_artifact(path)
    try:
        receipt = MetricOntologyOperationReceipt.model_validate_json(payload)
    except ValueError as exc:
        raise ValueError("ontology receipt payload is invalid") from exc
    if not verify_metric_ontology_receipt(receipt):
        raise ValueError("ontology receipt commitment is invalid")
    return snapshot, receipt


def _load_existing_output(
    path: Path,
) -> tuple[ImmutableArtifactSnapshot, MetricOntologyOperationReceipt] | None:
    if not path.exists():
        return None
    return _load_receipt(path)


def _request_from_args(args: argparse.Namespace) -> MetricOntologyPopulationRequest:
    return MetricOntologyPopulationRequest(
        apply=False,
        knowledge_cutoff=args.cutoff_at,
        operation_recorded_at=args.recorded_at,
        phase=cast(OntologyPhase, args.phase),
        after_observation_id=args.after_observation_id,
        max_observations=args.max_observations,
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
        raise ValueError("ontology database must have one Alembic revision")
    return str(rows[0][0])


def _run_operator(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    request: MetricOntologyPopulationRequest,
) -> MetricOntologyPopulationResult:
    del args
    return populate_metric_ontology(conn, request)


def _execute(args: argparse.Namespace) -> MetricOntologyOperationReceipt:
    if args.apply and args.admission_receipt is None:
        raise ValueError("--apply requires --admission-receipt")
    if not args.apply and args.admission_receipt is not None:
        raise ValueError("--admission-receipt is valid only with --apply")
    if args.apply and (
        args.input_commitment_sha256 is not None or args.plan_commitment_sha256 is not None
    ):
        raise ValueError("apply commitments must come only from the admission receipt")
    if args.after_observation_id is not None and not args.apply:
        if args.prior_checkpoint_receipt is None:
            raise ValueError("an ontology resume dry-run requires --prior-checkpoint-receipt")
    elif args.prior_checkpoint_receipt is not None:
        raise ValueError("--prior-checkpoint-receipt requires a resume dry-run")

    input_paths = tuple(
        path for path in (args.admission_receipt, args.prior_checkpoint_receipt) if path is not None
    )
    receipt_path = validate_receipt_path(
        args.receipt,
        database=args.db,
        protected_receipts=input_paths,
    )
    resources = [
        f"sqlite:{Path(os.path.abspath(args.db))}",
        f"artifact:{receipt_path}",
        *(f"artifact:{Path(os.path.abspath(path))}" for path in input_paths),
    ]
    if Path(os.path.abspath(args.db)) == portfolio_db_path(PROJECT_ROOT):
        resources.append("portfolio-db")
    with JobLock(PROJECT_ROOT, "populate-metric-ontology", resources):
        admission_snapshot: ImmutableArtifactSnapshot | None = None
        prior_snapshot: ImmutableArtifactSnapshot | None = None
        admission: MetricOntologyOperationReceipt | None = None
        prior: MetricOntologyOperationReceipt | None = None
        if args.admission_receipt is not None:
            admission_snapshot, admission = _load_receipt(args.admission_receipt)
        if args.prior_checkpoint_receipt is not None:
            prior_snapshot, prior = _load_receipt(args.prior_checkpoint_receipt)
        if admission is not None:
            request = admitted_apply_request(
                admission,
                database=args.db,
                knowledge_cutoff=args.cutoff_at,
                operation_recorded_at=args.recorded_at,
                phase=cast(OntologyPhase, args.phase),
                after_observation_id=args.after_observation_id,
                max_observations=args.max_observations,
            )
            expected_revision = admission.alembic_revision
        else:
            request = _request_from_args(args)
            expected_revision = None
        if prior is not None:
            validate_checkpoint_resume(
                prior,
                database=args.db,
                knowledge_cutoff=args.cutoff_at,
                operation_recorded_at=args.recorded_at,
                phase=cast(OntologyPhase, args.phase),
                after_observation_id=args.after_observation_id,
                max_observations=args.max_observations,
            )
        existing_output = _load_existing_output(receipt_path)
        database_path = Path(os.path.abspath(args.db))
        file_identity = _database_file_identity(database_path)
        role = SQLiteConnectionRole.WRITER if request.apply else SQLiteConnectionRole.READ_ONLY
        conn = connect_sqlite(database_path, role=role, schema_preflight=request.apply)
        try:
            conn.execute("BEGIN IMMEDIATE" if request.apply else "BEGIN")
            revision = _revision(conn)
            if expected_revision is not None and revision != expected_revision:
                raise ValueError("ontology admission revision does not match")
            instance_id = database_instance_id(conn)
            if admission is not None and instance_id != admission.database_instance_id:
                raise ValueError("ontology admission database identity changed")
            if prior is not None and instance_id != prior.database_instance_id:
                raise ValueError("ontology checkpoint database identity changed")
            admission_sha = None if admission_snapshot is None else admission_snapshot.file_sha256
            prior_sha = (
                prior_snapshot.file_sha256
                if prior_snapshot is not None
                else (None if admission is None else admission.prior_checkpoint_receipt_sha256)
            )
            operation_id = metric_ontology_operation_id(
                database_instance_id=instance_id,
                request=request,
                admission_receipt_sha256=admission_sha,
                prior_checkpoint_receipt_sha256=prior_sha,
            )
            if existing_output is not None and existing_output[1].operation_id != operation_id:
                raise ImmutableArtifactConflictError(
                    "immutable ontology receipt belongs to another operation"
                )
            stored = load_metric_ontology_receipt(conn, operation_id) if request.apply else None
            if stored is not None:
                if existing_output is not None and existing_output[1] != stored:
                    raise ImmutableArtifactConflictError(
                        "exported ontology receipt differs from its ledger"
                    )
                if admission_snapshot is not None:
                    assert_artifact_unchanged(admission_snapshot)
                if prior_snapshot is not None:
                    assert_artifact_unchanged(prior_snapshot)
                conn.rollback()
                receipt = stored
            else:
                if request.apply and existing_output is not None:
                    raise ImmutableArtifactConflictError(
                        "ontology apply receipt exists without its database ledger"
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
                    raise ValueError("ontology database file identity changed")
                if database_instance_id(conn) != instance_id:
                    raise ValueError("ontology database identity changed")
                receipt = build_metric_ontology_receipt(
                    database_path=str(database_path),
                    database_instance_id=instance_id,
                    alembic_revision=revision,
                    request=request,
                    result=result,
                    prior_checkpoint_receipt_sha256=prior_sha,
                    admission_receipt_sha256=admission_sha,
                )
                if request.apply:
                    persist_metric_ontology_receipt(conn, receipt)
                    conn.commit()
                else:
                    conn.rollback()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        publish_text_no_clobber(receipt_path, receipt.model_dump_json())
        exported_snapshot, exported = _load_receipt(receipt_path)
        if exported != receipt:
            raise ImmutableArtifactConflictError(
                "exported ontology receipt differs from the canonical receipt"
            )
        assert_artifact_unchanged(exported_snapshot)
        return receipt


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        receipt = _execute(args)
    except JobAlreadyRunningError:
        _event("metric_ontology_population_deferred", reason="job_lock_held")
        return 75
    except Exception as exc:
        _event(
            "metric_ontology_population_refused",
            error_type=type(exc).__name__,
            detail=redact(exc),
        )
        return 2
    _event(
        "metric_ontology_population_completed",
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
