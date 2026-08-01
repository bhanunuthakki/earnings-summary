"""Checkpoint and seal one isolated post-mutation latest-state rehearsal DB."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--seal", type=Path, required=True)
    return parser


def safe_seal_path(output: Path, *, database: Path) -> Path:
    destination = Path(os.path.abspath(output))
    db = Path(os.path.abspath(database))
    protected = {
        db,
        *(Path(os.path.abspath(f"{db}{suffix}")) for suffix in ("-wal", "-shm", "-journal")),
    }
    for path in (destination, *protected):
        require_no_reparse_points(path)
    if path_aliases_any(destination, protected):
        raise ValueError("candidate seal aliases the database or a sidecar")
    return destination


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        database = Path(os.path.abspath(args.database))
        live = portfolio_db_path(args.repo_root).resolve()
        for path in (database, live):
            require_no_reparse_points(path)
        if path_aliases_any(database, {live}):
            raise ValueError("rehearsal sealer refuses the canonical live database")
        destination = safe_seal_path(args.seal, database=database)
        if path_aliases_any(destination, {live}):
            raise ValueError("candidate seal aliases the configured live database")
        resources = ["portfolio-db", f"sqlite:{database}", f"artifact:{destination}"]
        with JobLock(args.repo_root, "seal-latest-state-rehearsal", resources):
            for path in (database, live):
                require_no_reparse_points(path)
            if path_aliases_any(database, {live}):
                raise ValueError("rehearsal sealer refuses the configured live database")
            conn = connect_sqlite(database, role=SQLiteConnectionRole.WRITER)
            try:
                checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint is None or int(checkpoint[0]) != 0 or int(checkpoint[1]) != 0:
                    raise LatestStateActivationError(
                        "candidate WAL checkpoint was busy or incomplete"
                    )
            finally:
                conn.close()
            seal = build_governed_candidate_seal(
                database,
                expected_revision=args.expected_revision,
            )
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
