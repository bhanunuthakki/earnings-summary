"""Bind an exhaustive governed-plane census to a structural candidate audit."""

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
    audit_candidate_coverage,
)
from runtime.job_runtime import JobAlreadyRunningError, JobLock  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--candidate-audit-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        output = output_path_for(
            args.database,
            args.candidate_audit_receipt,
            args.output,
        )
        database = args.database.resolve()
        with JobLock(
            PROJECT_ROOT,
            "audit-latest-state-candidate-coverage",
            [f"sqlite:{database}", f"artifact:{output}"],
        ):
            report = audit_candidate_coverage(
                database,
                candidate_audit_receipt=args.candidate_audit_receipt,
            )
            published = publish_text_no_clobber(output, report.model_dump_json())
    except JobAlreadyRunningError:
        _event("latest_state_candidate_coverage_deferred", reason="job_lock_held")
        return 75
    except (ImmutableArtifactConflictError, LatestStateActivationError, OSError) as exc:
        _event(
            "latest_state_candidate_coverage_refused",
            error_type=type(exc).__name__,
            reason=str(exc),
        )
        return 2
    _event(
        "latest_state_candidate_coverage_completed",
        outcome="published" if published else "exact_replay",
        output=str(output),
        report_sha256=report.report_sha256,
    )
    print(
        json.dumps(
            {"output": str(output), "report_sha256": report.report_sha256},
            sort_keys=True,
        )
    )
    return 0


def output_path_for(database: Path, audit: Path, output: Path) -> Path:
    source = database.resolve()
    destination = output.resolve()
    protected = {
        source,
        audit.resolve(),
        *(Path(f"{source}{suffix}").resolve() for suffix in ("-wal", "-shm", "-journal")),
    }
    require_no_reparse_points(source)
    require_no_reparse_points(audit.resolve())
    require_no_reparse_points(destination)
    if path_aliases_any(destination, protected):
        raise LatestStateActivationError("coverage output aliases a protected artifact")
    return destination


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
