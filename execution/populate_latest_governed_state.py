"""Populate the exact admitted production cohort into the 0261 current-state planes."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ask.sealed_retrieval import load_production_scopes  # noqa: E402
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
from provenance.latest_governed_population import (  # noqa: E402
    LatestGovernedPopulationPersistence,
    LatestGovernedPopulationReceipt,
    LatestGovernedPopulationRequest,
    admit_latest_governed_population,
    build_latest_governed_population_receipt,
    latest_governed_population_operation_id,
    load_latest_governed_population_receipt,
    populate_latest_governed_cohort,
    verify_latest_governed_population_receipt,
)
from provenance.latest_state_activation import (  # noqa: E402
    BoundLatestStateEligibilityManifest,
    verify_bound_eligibility_manifest,
)
from provenance.population_canonical_resolution import database_instance_id  # noqa: E402
from runtime.job_runtime import JobAlreadyRunningError, JobLock, portfolio_db_path  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("datetime must include a timezone")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--eligibility", type=Path, required=True)
    parser.add_argument("--scope-registry", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--operation-recorded-at", type=_datetime, required=True)
    parser.add_argument("--after-scope-id")
    parser.add_argument("--prior-checkpoint-receipt", type=Path)
    parser.add_argument("--max-scopes", type=int, default=1)
    parser.add_argument("--max-batch-rows", type=int, default=1_000)
    parser.add_argument("--document-checkpoint", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--receipt", type=Path, required=True)
    return parser


def safe_receipt_path(
    output: Path,
    *,
    database: Path,
    inputs: tuple[Path, ...],
) -> Path:
    for path in (output, database, *inputs):
        require_no_reparse_points(path)
    destination = Path(os.path.abspath(output))
    db = Path(os.path.abspath(database))
    protected = {
        db,
        *(Path(os.path.abspath(f"{db}{suffix}")) for suffix in ("-wal", "-shm", "-journal")),
        *(Path(os.path.abspath(path)) for path in inputs),
    }
    if path_aliases_any(destination, protected):
        raise ValueError("population receipt aliases a protected artifact")
    return destination


def population_database_lock_resources(
    database: Path,
    portfolio_database: Path,
) -> tuple[str, ...]:
    """Bind the canonical portfolio lock and reject unsafe same-file aliases."""

    candidate = Path(os.path.abspath(database))
    portfolio = Path(os.path.abspath(portfolio_database))
    resources = [f"sqlite:{candidate}"]
    if path_aliases_any(candidate, {portfolio}):
        if candidate != portfolio:
            raise ValueError("latest governed database aliases the portfolio database")
        resources.append("portfolio-db")
    return tuple(resources)


def validate_existing_export_database_evidence(
    *,
    apply: bool,
    existing: LatestGovernedPopulationReceipt | None,
    stored: LatestGovernedPopulationReceipt | None,
) -> None:
    """Require DB evidence before replaying apply output; defer dry-run equality."""

    if apply and existing is not None and stored != existing:
        raise ImmutableArtifactConflictError(
            "exported latest governed receipt lacks matching database evidence"
        )


def _load_bound(
    path: Path,
) -> tuple[ImmutableArtifactSnapshot, BoundLatestStateEligibilityManifest]:
    snapshot, payload = read_stable_artifact(path)
    manifest = BoundLatestStateEligibilityManifest.model_validate_json(payload)
    if not verify_bound_eligibility_manifest(manifest):
        raise ValueError("latest governed eligibility receipt is invalid")
    return snapshot, manifest


def _load_receipt(
    path: Path,
) -> tuple[ImmutableArtifactSnapshot, LatestGovernedPopulationReceipt]:
    snapshot, payload = read_stable_artifact(path)
    receipt = LatestGovernedPopulationReceipt.model_validate_json(payload)
    if not verify_latest_governed_population_receipt(receipt):
        raise ValueError("latest governed population receipt is invalid")
    return snapshot, receipt


def _execute(args: argparse.Namespace) -> LatestGovernedPopulationReceipt:
    if args.apply and args.max_scopes != 1:
        raise ValueError("apply checkpoint publication requires --max-scopes 1")
    if (args.after_scope_id is None) != (args.prior_checkpoint_receipt is None):
        raise ValueError("resume requires both --after-scope-id and --prior-checkpoint-receipt")
    database = Path(os.path.abspath(args.database))
    eligibility_path = Path(os.path.abspath(args.eligibility))
    registry_path = Path(os.path.abspath(args.scope_registry))
    prior_path = (
        None
        if args.prior_checkpoint_receipt is None
        else Path(os.path.abspath(args.prior_checkpoint_receipt))
    )
    inputs = tuple(
        path for path in (eligibility_path, registry_path, prior_path) if path is not None
    )
    output = safe_receipt_path(args.receipt, database=database, inputs=inputs)
    resources = [
        *population_database_lock_resources(database, portfolio_db_path(PROJECT_ROOT)),
        *(f"artifact:{path}" for path in inputs),
        f"artifact:{output}",
    ]
    with JobLock(PROJECT_ROOT, "populate-latest-governed-state", resources):
        eligibility_snapshot, manifest = _load_bound(eligibility_path)
        registry_snapshot, registry_bytes = read_stable_artifact(registry_path)
        prior_snapshot: ImmutableArtifactSnapshot | None = None
        prior: LatestGovernedPopulationReceipt | None = None
        if prior_path is not None:
            prior_snapshot, prior = _load_receipt(prior_path)
        role = SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY
        conn = connect_sqlite(database, role=role, schema_preflight=args.apply)
        try:
            revision_rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
            if len(revision_rows) != 1 or str(revision_rows[0][0]) != args.expected_revision:
                raise ValueError("latest governed database revision differs from expectation")
            revision = str(revision_rows[0][0])
            if (
                Path(manifest.database_path).resolve() != database
                or manifest.alembic_revision != revision
            ):
                raise ValueError("latest governed eligibility names another database or revision")
            instance_id = database_instance_id(conn)
            scopes = load_production_scopes(
                conn,
                registry_path,
                registry_payload=registry_bytes,
            )
            admission = admit_latest_governed_population(manifest, scopes)
            request = LatestGovernedPopulationRequest(
                operation_recorded_at=args.operation_recorded_at,
                admission_sha256=admission.commitment_sha256,
                apply=args.apply,
                after_scope_id=args.after_scope_id,
                max_scopes=args.max_scopes,
                max_batch_rows=args.max_batch_rows,
                document_checkpoint=args.document_checkpoint,
            )
            prior_stored = (
                None
                if prior is None
                else load_latest_governed_population_receipt(conn, prior.operation_id)
            )
            expected_remaining = (
                ()
                if args.after_scope_id is None
                else tuple(
                    item.scope_id
                    for item in admission.scopes[
                        tuple(scope.scope_id for scope in admission.scopes).index(
                            args.after_scope_id
                        )
                        + 1 :
                    ]
                )
            )
            if prior is not None and (
                prior_stored != prior
                or prior.result.outcome != "checkpoint"
                or not prior.request.apply
                or prior.result.last_scope_id != args.after_scope_id
                or prior.admission != admission
                or prior.database_instance_id != instance_id
                or prior.alembic_revision != revision
                or prior.result.processed_scope_ids != (args.after_scope_id,)
                or prior.result.remaining_scope_ids != expected_remaining
                or args.operation_recorded_at < prior.request.operation_recorded_at
            ):
                raise ValueError("prior latest governed checkpoint does not bind this resume")
            existing = None if not output.exists() else _load_receipt(output)[1]
            prior_sha = None if prior_snapshot is None else prior_snapshot.file_sha256
            persistence = LatestGovernedPopulationPersistence(
                database_path=str(database),
                database_instance_id=instance_id,
                alembic_revision=revision,
                eligibility_artifact_sha256=eligibility_snapshot.file_sha256,
                registry_artifact_sha256=registry_snapshot.file_sha256,
                prior_checkpoint_receipt_sha256=prior_sha,
            )
            operation_id = latest_governed_population_operation_id(
                persistence=persistence,
                admission_sha256=admission.commitment_sha256,
                request=request,
            )
            stored_before = (
                load_latest_governed_population_receipt(conn, operation_id) if args.apply else None
            )
            if prior is not None:
                validate_population_resume_heads(
                    conn,
                    prior_heads=prior.result.heads_after,
                    stored_successor_heads=(
                        None if stored_before is None else stored_before.result.heads_after
                    ),
                )
            validate_existing_export_database_evidence(
                apply=args.apply,
                existing=existing,
                stored=stored_before,
            )
            result = populate_latest_governed_cohort(
                conn,
                admission,
                request,
                persistence=persistence if args.apply else None,
                prior_checkpoint=prior if args.apply else None,
                input_stability_check=(
                    None
                    if not args.apply
                    else lambda: _assert_inputs_unchanged(
                        eligibility_snapshot,
                        registry_snapshot,
                        prior_snapshot,
                    )
                ),
            )
            if args.apply:
                stored = load_latest_governed_population_receipt(conn, operation_id)
                if stored is None:
                    raise ValueError("atomic latest governed population receipt is missing")
                receipt = stored
            else:
                assert_artifact_unchanged(eligibility_snapshot)
                assert_artifact_unchanged(registry_snapshot)
                if prior_snapshot is not None:
                    assert_artifact_unchanged(prior_snapshot)
                if database_instance_id(conn) != instance_id:
                    raise ValueError("latest governed database identity changed")
                receipt = build_latest_governed_population_receipt(
                    database_path=str(database),
                    database_instance_id=instance_id,
                    alembic_revision=revision,
                    eligibility_artifact_sha256=eligibility_snapshot.file_sha256,
                    registry_artifact_sha256=registry_snapshot.file_sha256,
                    admission=admission,
                    request=request,
                    result=result,
                    prior_checkpoint_receipt_sha256=prior_sha,
                )
            if existing is not None and existing != receipt:
                raise ImmutableArtifactConflictError(
                    "exported latest governed receipt differs from its database ledger"
                )
        finally:
            conn.close()
        publish_text_no_clobber(output, receipt.model_dump_json())
        exported_snapshot, exported = _load_receipt(output)
        if exported != receipt:
            raise ImmutableArtifactConflictError(
                "exported latest governed receipt differs from the canonical receipt"
            )
        assert_artifact_unchanged(exported_snapshot)
        assert_artifact_unchanged(eligibility_snapshot)
        assert_artifact_unchanged(registry_snapshot)
        if prior_snapshot is not None:
            assert_artifact_unchanged(prior_snapshot)
        return receipt


def validate_population_resume_heads(
    conn: sqlite3.Connection,
    *,
    prior_heads: Mapping[str, tuple[str, str] | None],
    stored_successor_heads: Mapping[str, tuple[str, str] | None] | None,
) -> None:
    """Accept a committed successor before export, otherwise require the prior boundary."""

    expected = prior_heads if stored_successor_heads is None else stored_successor_heads
    actual: dict[str, tuple[str, str] | None] = {}
    for scope_id in expected:
        rows = conn.execute(
            "SELECT refresh_receipt_id,state_sha256 FROM latest_governed_scope_heads "
            "WHERE scope_key=?",
            (scope_id,),
        ).fetchall()
        if len(rows) > 1:
            raise ValueError("latest governed scope head is ambiguous")
        actual[scope_id] = None if not rows else (str(rows[0][0]), str(rows[0][1]))
    if actual != expected:
        label = "prior checkpoint" if stored_successor_heads is None else "stored successor"
        raise ValueError(f"latest governed database heads differ from the {label}")


def _assert_inputs_unchanged(
    eligibility: ImmutableArtifactSnapshot,
    registry: ImmutableArtifactSnapshot,
    prior: ImmutableArtifactSnapshot | None,
) -> None:
    assert_artifact_unchanged(eligibility)
    assert_artifact_unchanged(registry)
    if prior is not None:
        assert_artifact_unchanged(prior)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        receipt = _execute(args)
    except JobAlreadyRunningError:
        _event("latest_governed_population_deferred", reason="job_lock_held")
        return 75
    except Exception as exc:
        _event(
            "latest_governed_population_refused",
            error_type=type(exc).__name__,
            detail=redact(exc),
        )
        return 2
    _event(
        "latest_governed_population_completed",
        outcome=receipt.result.outcome,
        receipt=str(args.receipt.resolve()),
        receipt_sha256=receipt.receipt_sha256,
    )
    sys.stdout.write(receipt.model_dump_json() + "\n")
    return 3 if receipt.result.outcome == "checkpoint" else 0


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
