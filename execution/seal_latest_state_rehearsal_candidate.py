"""Checkpoint and seal one isolated post-mutation latest-state rehearsal DB."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from log_redact import redact  # noqa: E402
from provenance.immutable_artifact import (  # noqa: E402
    ImmutableArtifactConflictError,
    path_aliases_any,
    publish_text_no_clobber,
    require_no_reparse_points,
)
from provenance.latest_state_activation import (  # noqa: E402
    LatestStateActivationError,
    build_governed_candidate_seal,
)
from runtime.job_runtime import JobAlreadyRunningError, JobLock, portfolio_db_path  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _sqlite_family(database: Path) -> set[Path]:
    main = Path(os.path.abspath(database))
    return {
        main,
        *(Path(os.path.abspath(f"{main}{suffix}")) for suffix in ("-wal", "-shm", "-journal")),
    }


def _require_single_link_regular_files(paths: set[Path]) -> None:
    for path in paths:
        if not path.exists():
            continue
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError(
                "isolated candidate database family contains a non-regular or shared-link file"
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--seal", type=Path, required=True)
    return parser


def safe_seal_path(
    output: Path,
    *,
    database: Path,
) -> Path:
    destination = Path(os.path.abspath(output))
    db = Path(os.path.abspath(database))
    protected = _sqlite_family(db)
    for path in (destination, *protected):
        require_no_reparse_points(path)
    if path_aliases_any(destination, protected):
        raise ValueError("candidate seal aliases the database or a sidecar")
    return destination


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        database = Path(os.path.abspath(cast(Path, args.database)))
        requested_repo_root = cast(Path, args.repo_root).expanduser().resolve()
        repo_root = PROJECT_ROOT.resolve()
        if requested_repo_root != repo_root:
            raise ValueError("rehearsal sealer repo root differs from its executable checkout")
        live = portfolio_db_path(repo_root).resolve()
        live_protected = _sqlite_family(live)
        candidate_protected = _sqlite_family(database)
        for path in (*candidate_protected, *live_protected):
            require_no_reparse_points(path)
        _require_single_link_regular_files(candidate_protected)
        if any(path_aliases_any(path, live_protected) for path in candidate_protected):
            raise ValueError("rehearsal sealer refuses the configured live database family")
        destination = safe_seal_path(
            cast(Path, args.seal),
            database=database,
        )
        if path_aliases_any(destination, live_protected):
            raise ValueError("candidate artifact aliases the configured live database family")
        resources = ["portfolio-db", f"sqlite:{database}", f"artifact:{destination}"]
        with JobLock(repo_root, "seal-latest-state-rehearsal", resources):
            for path in (*candidate_protected, destination, *live_protected):
                require_no_reparse_points(path)
            _require_single_link_regular_files(candidate_protected)
            if any(
                path_aliases_any(path, live_protected) for path in candidate_protected
            ) or path_aliases_any(destination, live_protected):
                raise ValueError("rehearsal sealer refuses the configured live database family")
            conn = connect_sqlite(
                database,
                role=SQLiteConnectionRole.WRITER,
                schema_preflight=False,
            )
            try:
                checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint is None or int(checkpoint[0]) != 0 or int(checkpoint[1]) != 0:
                    raise LatestStateActivationError(
                        "candidate WAL checkpoint was busy or incomplete"
                    )
            finally:
                conn.close()
            for path in (*candidate_protected, destination, *live_protected):
                require_no_reparse_points(path)
            _require_single_link_regular_files(candidate_protected)
            if any(
                path_aliases_any(path, live_protected) for path in candidate_protected
            ) or path_aliases_any(destination, live_protected):
                raise ValueError("rehearsal sealer refuses the configured live database family")
            seal = build_governed_candidate_seal(
                database,
                expected_revision=cast(str, args.expected_revision),
            )
            for path in (*candidate_protected, destination, *live_protected):
                require_no_reparse_points(path)
            _require_single_link_regular_files(candidate_protected)
            if any(
                path_aliases_any(path, live_protected) for path in candidate_protected
            ) or path_aliases_any(destination, live_protected):
                raise ValueError("rehearsal sealer refuses the configured live database family")
            publish_text_no_clobber(destination, seal.model_dump_json())
    except (
        ImmutableArtifactConflictError,
        JobAlreadyRunningError,
        LatestStateActivationError,
        OSError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {"event": "rehearsal_candidate_seal_blocked", "error": redact(str(exc))},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "database": seal.database,
                "database_sha256": seal.sha256,
                "seal": str(destination),
                "status": "sealed",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
