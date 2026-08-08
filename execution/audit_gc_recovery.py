"""Publish a fail-closed, read-only recovery receipt for facts-depth GC."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from provenance.gc_recovery import (  # noqa: E402
    GcRecoveryError,
    canonical_runtime_git_commit,
    publish_gc_recovery_audit,
)
from provenance.immutable_artifact import (  # noqa: E402
    ImmutableArtifactConflictError,
    canonical_text_artifact_sha256,
    path_aliases_any,
    require_no_reparse_points,
)
from runtime.job_runtime import JobAlreadyRunningError, JobLock  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--baseline-database", type=Path, required=True)
    parser.add_argument("--archive-database", type=Path, required=True)
    parser.add_argument("--admission-receipt", type=Path, required=True)
    parser.add_argument("--expected-admission-receipt-sha256", required=True)
    parser.add_argument("--expected-activation-receipt-sha256", required=True)
    parser.add_argument("--expected-current-revision", required=True)
    parser.add_argument("--expected-baseline-revision", required=True)
    parser.add_argument("--expected-runtime-git-commit", required=True)
    parser.add_argument("--expected-recovery-receipt-sha256")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        canonical_runtime_git_commit(args.expected_runtime_git_commit)
        current, baseline, archive, output = admitted_paths(
            args.database,
            args.baseline_database,
            args.archive_database,
            args.admission_receipt,
            args.output,
        )
        resources = [
            "portfolio-db",
            f"sqlite:{current}",
            f"sqlite:{baseline}",
            f"sqlite:{archive}",
            f"artifact:{args.admission_receipt.resolve()}",
            f"artifact:{output}",
        ]
        with JobLock(PROJECT_ROOT, "audit-gc-recovery", resources):
            receipt, published = publish_gc_recovery_audit(
                current,
                baseline_database=baseline,
                archive_database=archive,
                admission_receipt_path=args.admission_receipt,
                expected_admission_receipt_sha256=(args.expected_admission_receipt_sha256),
                expected_activation_receipt_sha256=(args.expected_activation_receipt_sha256),
                expected_current_revision=args.expected_current_revision,
                expected_baseline_revision=args.expected_baseline_revision,
                output=output,
                expected_recovery_receipt_sha256=args.expected_recovery_receipt_sha256,
            )
    except JobAlreadyRunningError:
        _event("gc_recovery_audit_deferred", reason="job_lock_held")
        return 75
    except (
        GcRecoveryError,
        ImmutableArtifactConflictError,
        OSError,
        ValueError,
    ) as exc:
        _event(
            "gc_recovery_audit_refused",
            error_type=type(exc).__name__,
            reason=str(exc),
        )
        return 2
    receipt_file_sha256 = canonical_text_artifact_sha256(receipt.model_dump_json())
    _event(
        "gc_recovery_audit_completed",
        outcome="published" if published else "exact_replay",
        recovery_outcome=receipt.outcome.value,
        recovery_ready=receipt.recovery_ready,
        report_sha256=receipt.report_sha256,
        receipt_file_sha256=receipt_file_sha256,
        output=str(output),
    )
    print(
        json.dumps(
            {
                "blockers": list(receipt.blockers),
                "outcome": receipt.outcome.value,
                "output": str(output),
                "recovery_ready": receipt.recovery_ready,
                "report_sha256": receipt.report_sha256,
                "receipt_file_sha256": receipt_file_sha256,
            },
            sort_keys=True,
        )
    )
    return 0 if receipt.recovery_ready else 2


def admitted_paths(
    database: Path,
    baseline: Path,
    archive: Path,
    admission: Path,
    output: Path,
) -> tuple[Path, Path, Path, Path]:
    inputs = tuple(Path(os.path.abspath(os.fspath(path))) for path in (database, baseline, archive))
    destination = Path(os.path.abspath(os.fspath(output)))
    for path in (*inputs, destination):
        require_no_reparse_points(path)
    protected = {
        *inputs,
        Path(os.path.abspath(os.fspath(admission))),
        *(Path(f"{path}{suffix}") for path in inputs for suffix in ("-wal", "-shm", "-journal")),
    }
    if path_aliases_any(destination, protected):
        raise GcRecoveryError("recovery receipt aliases protected SQLite evidence")
    return inputs[0], inputs[1], inputs[2], destination


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
