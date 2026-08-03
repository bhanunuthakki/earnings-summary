"""Seal or verify one quiesced, non-live SQLite archive generation."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from log_redact import redact  # noqa: E402
from provenance.archive_generation import (  # noqa: E402
    ArchiveGenerationError,
    ArchiveGenerationManifest,
    ArchiveGenerationRequest,
    build_archive_generation_manifest,
    verify_archive_generation_manifest,
)
from provenance.immutable_artifact import (  # noqa: E402
    ImmutableArtifactConflictError,
    assert_artifact_unchanged,
    path_aliases_any,
    publish_text_no_clobber,
    read_stable_artifact,
    require_no_reparse_points,
)
from runtime.job_runtime import (  # noqa: E402
    JobAlreadyRunningError,
    JobLock,
    portfolio_db_path,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    seal = commands.add_parser("seal", help="build and publish a no-clobber manifest")
    verify = commands.add_parser("verify", help="recompute and verify an existing manifest")
    for command in (seal, verify):
        command.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
        command.add_argument("--database", type=Path, required=True)
        command.add_argument("--manifest", type=Path, required=True)
    seal.add_argument("--generation-id", required=True)
    seal.add_argument("--publication-sequence-start", type=int, required=True)
    seal.add_argument("--publication-sequence-end", type=int, required=True)
    seal.add_argument("--recorded-at-start", type=datetime.fromisoformat, required=True)
    seal.add_argument("--recorded-at-end", type=datetime.fromisoformat, required=True)
    seal.add_argument("--predecessor-manifest-sha256")
    seal.add_argument("--external-reference-count", type=int, required=True)
    seal.add_argument("--external-reference-set-sha256", required=True)
    seal.add_argument("--sealed-at", type=datetime.fromisoformat, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        repo_root = _absolute(args.repo_root)
        database = _absolute(args.database)
        destination = _absolute(args.manifest)
        live = _absolute(portfolio_db_path(repo_root))
        _validate_paths(database=database, destination=destination, live=live)
        resources = [f"sqlite:{database}", f"artifact:{destination}"]
        with JobLock(repo_root, "seal-archive-generation", resources):
            _validate_paths(database=database, destination=destination, live=live)
            if args.command == "seal":
                request = ArchiveGenerationRequest(
                    generation_id=args.generation_id,
                    archive_file=database.name,
                    publication_sequence_start=args.publication_sequence_start,
                    publication_sequence_end=args.publication_sequence_end,
                    recorded_at_start=args.recorded_at_start,
                    recorded_at_end=args.recorded_at_end,
                    predecessor_manifest_sha256=args.predecessor_manifest_sha256,
                    external_reference_count=args.external_reference_count,
                    external_reference_set_sha256=args.external_reference_set_sha256,
                    sealed_at=args.sealed_at,
                )
                manifest = build_archive_generation_manifest(database, request)
                created = publish_text_no_clobber(
                    destination,
                    manifest.model_dump_json(),
                )
                status = "sealed" if created else "already_sealed"
            else:
                snapshot, payload = read_stable_artifact(destination)
                manifest = ArchiveGenerationManifest.model_validate_json(payload)
                verify_archive_generation_manifest(database, manifest)
                assert_artifact_unchanged(snapshot)
                status = "verified"
    except (
        ArchiveGenerationError,
        ImmutableArtifactConflictError,
        JobAlreadyRunningError,
        OSError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {"event": "archive_generation_blocked", "error": redact(str(exc))},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "database": str(database),
                "database_sha256": manifest.database_sha256,
                "generation_id": manifest.generation_id,
                "manifest": str(destination),
                "manifest_sha256": manifest.manifest_sha256,
                "status": status,
            },
            sort_keys=True,
        )
    )
    return 0


def _validate_paths(*, database: Path, destination: Path, live: Path) -> None:
    for path in (database, destination, live):
        require_no_reparse_points(path)
    if path_aliases_any(database, {live}):
        raise ValueError("archive sealer refuses the configured live database")
    protected = {
        database,
        *(Path(f"{database}{suffix}") for suffix in ("-wal", "-shm", "-journal")),
        live,
    }
    if path_aliases_any(destination, protected):
        raise ValueError("archive manifest aliases a protected database artifact")


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


if __name__ == "__main__":
    raise SystemExit(main())
