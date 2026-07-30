"""Verify and register inert local embedding-runtime bytes."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from runtime.job_runtime import JobAlreadyRunningError, JobLock  # noqa: E402
from search.embedding_runtime_artifact import (  # noqa: E402
    RuntimeArtifactSource,
    load_runtime_artifact,
    verify_runtime_artifact,
)
from search.embedding_runtime_registration import (  # noqa: E402
    persist_runtime_registration,
    registration_from_artifact,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _timestamp(raw: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO-8601 datetime") from exc
    if value.tzinfo is None:
        raise argparse.ArgumentTypeError("must include an explicit timezone")
    return value.astimezone(UTC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--runtime-artifact", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--registered-at", type=_timestamp, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    artifact = load_runtime_artifact(args.runtime_artifact)
    sources = [
        RuntimeArtifactSource(
            logical_name=item.logical_name,
            role=item.role,
            relative_path=Path(item.logical_name),
        )
        for item in artifact.files
    ]
    verify_runtime_artifact(artifact, args.runtime_root, sources)
    registration = registration_from_artifact(
        artifact,
        registered_at=args.registered_at,
    )
    if not args.apply:
        sys.stdout.write(
            json.dumps(
                {
                    "mode": "dry_run",
                    "runtime_registration_id": registration.runtime_registration_id,
                    "model": registration.model,
                    "runtime_artifact_sha256": registration.runtime_artifact_sha256,
                    "routing_changed": False,
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 0
    try:
        with JobLock(PROJECT_ROOT, "register-embedding-runtime", ["portfolio-db"]):
            conn = connect_sqlite(
                args.db,
                role=SQLiteConnectionRole.WRITER,
                schema_preflight=True,
            )
            try:
                conn.execute("BEGIN IMMEDIATE")
                created = persist_runtime_registration(conn, registration)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
    except JobAlreadyRunningError as exc:
        sys.stderr.write(
            json.dumps(
                {"event": "embedding_runtime_registration_deferred", "reason": str(exc)},
                sort_keys=True,
            )
            + "\n"
        )
        return 75
    sys.stdout.write(
        json.dumps(
            {
                "mode": "apply",
                "runtime_registration_id": registration.runtime_registration_id,
                "created": created,
                "routing_changed": False,
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
