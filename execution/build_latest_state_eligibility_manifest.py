"""Build a candidate- and registry-bound latest-governed eligibility manifest."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ask.audit_store import canonical_json  # noqa: E402
from ask.sealed_retrieval import (  # noqa: E402
    derive_production_scope_registry,
    load_production_scopes,
)
from provenance.immutable_artifact import (  # noqa: E402
    ImmutableArtifactConflictError,
    path_aliases_any,
    publish_text_no_clobber,
    require_no_reparse_points,
)
from provenance.latest_state_activation import (  # noqa: E402
    LatestStateActivationError,
    bind_scope_eligibility_manifest,
    build_scope_eligibility_manifest,
    candidate_file_identity,
    read_candidate_artifact,
    require_checkpointed_sidecars,
)
from runtime.job_runtime import JobAlreadyRunningError, JobLock  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("datetime must include a timezone")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--candidate-audit-receipt", type=Path, required=True)
    parser.add_argument("--candidate-coverage-receipt", type=Path, required=True)
    parser.add_argument("--scope-registry", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--operation-recorded-at", type=_datetime, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        database = args.database.resolve()
        audit_path = args.candidate_audit_receipt.resolve()
        coverage_path = args.candidate_coverage_receipt.resolve()
        registry_path = args.scope_registry.resolve()
        output = _output_path(
            database,
            audit_path,
            coverage_path,
            registry_path,
            args.output,
        )
        with JobLock(
            PROJECT_ROOT,
            "build-latest-state-eligibility-manifest",
            [
                f"sqlite:{database}",
                f"artifact:{audit_path}",
                f"artifact:{coverage_path}",
                f"artifact:{registry_path}",
                f"artifact:{output}",
            ],
        ):
            require_checkpointed_sidecars(database)
            identity_before = candidate_file_identity(database)
            audit_snapshot, _ = read_candidate_artifact(audit_path)
            coverage_snapshot, _ = read_candidate_artifact(coverage_path)
            registry_snapshot, registry_bytes = read_candidate_artifact(registry_path)
            conn = connect_sqlite(
                database,
                role=SQLiteConnectionRole.QUIESCED_IMMUTABLE_READ_ONLY,
                schema_preflight=False,
            )
            try:
                registry = _validated_registry(conn, registry_path, registry_bytes)
                eligibility = build_scope_eligibility_manifest(
                    conn,
                    operation_recorded_at=args.operation_recorded_at,
                )
            finally:
                conn.close()
            require_checkpointed_sidecars(database)
            identity_after = candidate_file_identity(database)
            raw_revision_ids = registry["source_scope_revision_ids"]
            if not isinstance(raw_revision_ids, list):
                raise LatestStateActivationError("production scope revision IDs are malformed")
            expected_ids = tuple(str(value) for value in cast(list[object], raw_revision_ids))
            bound = bind_scope_eligibility_manifest(
                database_path=database,
                audit_path=audit_path,
                coverage_path=coverage_path,
                scope_registry_path=registry_path,
                scope_registry_sha256=str(registry["registry_sha256"]),
                audit_snapshot=audit_snapshot,
                coverage_snapshot=coverage_snapshot,
                registry_snapshot=registry_snapshot,
                expected_scope_revision_ids=expected_ids,
                identity_before=identity_before,
                identity_after=identity_after,
                eligibility=eligibility,
                expected_revision=args.expected_revision,
            )
            published = publish_text_no_clobber(output, bound.model_dump_json())
    except JobAlreadyRunningError:
        _event("latest_state_eligibility_manifest_deferred", reason="job_lock_held")
        return 75
    except (
        ImmutableArtifactConflictError,
        LatestStateActivationError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        _event(
            "latest_state_eligibility_manifest_refused",
            error_type=type(exc).__name__,
            reason=str(exc),
        )
        return 2
    inner = bound.eligibility
    _event(
        "latest_state_eligibility_manifest_completed",
        blocked_count=inner.blocked_count,
        eligible_count=inner.eligible_count,
        excluded_count=inner.excluded_count,
        outcome="published" if published else "exact_replay",
        output=str(output),
        report_sha256=bound.report_sha256,
    )
    print(
        json.dumps(
            {
                "blocked_count": inner.blocked_count,
                "eligible_count": inner.eligible_count,
                "excluded_count": inner.excluded_count,
                "output": str(output),
                "report_sha256": bound.report_sha256,
            },
            sort_keys=True,
        )
    )
    return 0 if inner.blocked_count == 0 else 2


def _validated_registry(
    conn: sqlite3.Connection,
    path: Path,
    payload: bytes,
) -> dict[str, object]:
    if not path.is_file():
        raise LatestStateActivationError("committed production scope registry is missing")
    try:
        decoded_raw: object = json.loads(payload)
    except (OSError, json.JSONDecodeError) as exc:
        raise LatestStateActivationError("production scope registry is malformed") from exc
    if not isinstance(decoded_raw, dict):
        raise LatestStateActivationError("production scope registry must be a JSON object")
    decoded = cast(dict[str, object], decoded_raw)
    load_production_scopes(conn, path)
    derived = derive_production_scope_registry(conn)
    if canonical_json(decoded) != canonical_json(derived):
        raise LatestStateActivationError("production scope registry differs from candidate")
    revision_ids = decoded.get("source_scope_revision_ids")
    if not isinstance(revision_ids, list) or not revision_ids:
        raise LatestStateActivationError("production scope registry is empty")
    revision_values = cast(list[object], revision_ids)
    if any(not isinstance(value, str) or not value for value in revision_values):
        raise LatestStateActivationError("production scope revision IDs are malformed")
    return decoded


def _output_path(
    database: Path,
    audit: Path,
    coverage: Path,
    registry: Path,
    output: Path,
) -> Path:
    destination = output.resolve()
    protected = {
        database,
        audit,
        coverage,
        registry,
        *(Path(f"{database}{suffix}").resolve() for suffix in ("-wal", "-shm", "-journal")),
    }
    for path in protected | {destination}:
        require_no_reparse_points(path)
    if path_aliases_any(destination, protected):
        raise LatestStateActivationError("manifest output aliases a protected artifact")
    return destination


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
