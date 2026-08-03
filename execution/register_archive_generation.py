"""Verify and register one sealed archive generation in the operational catalog."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from log_redact import redact  # noqa: E402
from provenance.archive_catalog import (  # noqa: E402
    ArchiveCatalogError,
    ArchiveRegistrationRequest,
    register_archive_generation,
)
from provenance.archive_generation import (  # noqa: E402
    ArchiveGenerationError,
    ArchiveGenerationManifest,
    verify_archive_generation_manifest,
)
from provenance.immutable_artifact import (  # noqa: E402
    ImmutableArtifactConflictError,
    assert_artifact_unchanged,
    path_aliases_any,
    read_stable_artifact,
    require_no_reparse_points,
)
from run_lock import RunLockHeldError, acquire_run_lock  # noqa: E402
from runtime.job_runtime import (  # noqa: E402
    JobAlreadyRunningError,
    JobLock,
    portfolio_db_path,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--ops-database", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--registered-at", type=datetime.fromisoformat, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-live-catalog-registration", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        repo_root = _absolute(args.repo_root)
        ops_database = _absolute(args.ops_database)
        archive_root = _absolute(args.archive_root)
        manifest_path = _absolute(args.manifest)
        live_database = _absolute(portfolio_db_path(repo_root))
        _validate_operational_target(
            ops_database,
            live_database=live_database,
            apply=args.apply,
            confirmed=args.confirm_live_catalog_registration,
        )
        manifest_snapshot, payload = read_stable_artifact(manifest_path)
        manifest = ArchiveGenerationManifest.model_validate_json(payload)
        archive_database = archive_root / manifest.archive_file
        archive_uri = _relative_uri(archive_root, archive_database)
        manifest_uri = _relative_uri(archive_root, manifest_path)
        request = ArchiveRegistrationRequest(
            manifest=manifest,
            archive_uri=archive_uri,
            manifest_uri=manifest_uri,
            registered_at=args.registered_at,
        )
        verify_archive_generation_manifest(archive_database, manifest)
        assert_artifact_unchanged(manifest_snapshot)

        if not args.apply:
            conn = connect_sqlite(ops_database, role=SQLiteConnectionRole.READ_ONLY)
            try:
                conn.execute("SELECT 1 FROM v_archive_generations_verified LIMIT 1")
            finally:
                conn.close()
            status = "ready"
        else:
            resources = [
                f"sqlite:{ops_database}",
                f"sqlite:{archive_database}",
                f"artifact:{manifest_path}",
            ]
            with JobLock(repo_root, "register-archive-generation", resources):
                lock = acquire_run_lock(
                    ops_database,
                    owner="register_archive_generation",
                    timeout_s=0,
                )
                try:
                    manifest_snapshot, payload = read_stable_artifact(manifest_path)
                    locked_manifest = ArchiveGenerationManifest.model_validate_json(payload)
                    if locked_manifest != manifest:
                        raise ArchiveCatalogError(
                            "archive manifest changed before catalog registration"
                        )
                    verify_archive_generation_manifest(archive_database, manifest)
                    with (
                        closing(
                            connect_sqlite(
                                ops_database,
                                role=SQLiteConnectionRole.WRITER,
                                schema_preflight=True,
                            )
                        ) as conn,
                        conn,
                    ):
                        result = register_archive_generation(conn, request)
                        verify_archive_generation_manifest(archive_database, manifest)
                        assert_artifact_unchanged(manifest_snapshot)
                finally:
                    lock.release()
            status = "registered" if result.created else "already_registered"
    except (
        ArchiveCatalogError,
        ArchiveGenerationError,
        ImmutableArtifactConflictError,
        JobAlreadyRunningError,
        RunLockHeldError,
        OSError,
        sqlite3.DatabaseError,
        ValidationError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {"event": "archive_registration_blocked", "error": redact(str(exc))},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "archive_database": str(archive_database),
                "generation_id": manifest.generation_id,
                "manifest": str(manifest_path),
                "manifest_sha256": manifest.manifest_sha256,
                "ops_database": str(ops_database),
                "status": status,
            },
            sort_keys=True,
        )
    )
    return 0


def _validate_operational_target(
    ops_database: Path,
    *,
    live_database: Path,
    apply: bool,
    confirmed: bool,
) -> None:
    require_no_reparse_points(ops_database)
    require_no_reparse_points(live_database)
    if apply and path_aliases_any(ops_database, {live_database}) and not confirmed:
        raise ValueError("live catalog registration requires --confirm-live-catalog-registration")


def _relative_uri(root: Path, candidate: Path) -> str:
    require_no_reparse_points(root)
    require_no_reparse_points(candidate)
    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve()
    try:
        relative = resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("archive artifact is outside --archive-root") from exc
    return relative.as_posix()


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


if __name__ == "__main__":
    raise SystemExit(main())
