"""Audit one sealed latest-state candidate without mutating SQLite or sidecars."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from provenance.immutable_artifact import (  # noqa: E402
    ImmutableArtifactConflictError,
    path_aliases_any,
    publish_text_no_clobber,
    require_no_reparse_points,
)
from provenance.latest_state_activation import (  # noqa: E402
    LatestStateActivationError,
    audit_governed_candidate,
)
from runtime.job_runtime import JobAlreadyRunningError, JobLock  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--seal", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        output = receipt_destination(args.database, args.seal, args.output)
        database = args.database.resolve()
        with JobLock(
            PROJECT_ROOT,
            "audit-latest-state-candidate",
            [f"sqlite:{database}", f"artifact:{output}"],
        ):
            report = audit_governed_candidate(
                database,
                seal_path=args.seal,
                expected_revision=args.expected_revision,
            )
            published = publish_text_no_clobber(output, report.model_dump_json())
    except JobAlreadyRunningError:
        _event("latest_state_candidate_audit_deferred", reason="job_lock_held")
        return 75
    except (ImmutableArtifactConflictError, LatestStateActivationError, OSError) as exc:
        _event(
            "latest_state_candidate_audit_refused",
            error_type=type(exc).__name__,
            reason=str(exc),
        )
        return 2
    _event(
        "latest_state_candidate_audit_completed",
        database_sha256=report.database_sha256,
        outcome="published" if published else "exact_replay",
        report_sha256=report.report_sha256,
        output=str(output),
    )
    print(
        json.dumps(
            {
                "database_sha256": report.database_sha256,
                "output": str(output),
                "report_sha256": report.report_sha256,
            },
            sort_keys=True,
        )
    )
    return 0


def receipt_destination(database: Path, seal: Path, output: Path) -> Path:
    database_path = database.resolve()
    seal_path = seal.resolve()
    destination = output.resolve()
    forbidden = {
        database_path,
        seal_path,
        *(Path(f"{database_path}{suffix}").resolve() for suffix in ("-wal", "-shm", "-journal")),
    }
    require_no_reparse_points(database_path)
    require_no_reparse_points(seal_path)
    require_no_reparse_points(destination)
    if path_aliases_any(destination, forbidden):
        raise LatestStateActivationError("receipt output aliases a protected candidate artifact")
    return destination


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
