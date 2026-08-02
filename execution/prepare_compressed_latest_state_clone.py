"""Create one receipted NTFS-compressed clone from an admitted candidate."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from provenance.compressed_candidate_clone import (  # noqa: E402
    MINIMUM_SAFE_FREE_BYTES,
    CompressedCloneRequest,
    prepare_compressed_clone,
    verify_compressed_clone_receipt,
)
from provenance.immutable_artifact import (  # noqa: E402
    ImmutableArtifactConflictError,
    path_aliases_any,
    publish_text_no_clobber,
    require_no_reparse_points,
)
from provenance.latest_state_activation import LatestStateActivationError  # noqa: E402
from runtime.job_runtime import JobAlreadyRunningError, JobLock  # noqa: E402


def _datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("datetime must include a timezone")
    return parsed


def _minimum_free_bytes(value: str) -> int:
    parsed = int(value)
    if parsed < MINIMUM_SAFE_FREE_BYTES:
        raise argparse.ArgumentTypeError("minimum free bytes must be at least 5 GiB")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-database", type=Path, required=True)
    parser.add_argument("--candidate-audit-receipt", type=Path, required=True)
    parser.add_argument("--candidate-coverage-receipt", type=Path, required=True)
    parser.add_argument("--destination-database", type=Path, required=True)
    parser.add_argument("--operation-recorded-at", type=_datetime, required=True)
    parser.add_argument("--minimum-free-bytes", type=_minimum_free_bytes, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    clone_receipt = None
    try:
        receipt_path = receipt_path_for(args)
        with JobLock(
            PROJECT_ROOT,
            "prepare-compressed-latest-state-clone",
            [
                f"sqlite:{args.source_database.resolve()}",
                f"sqlite:{args.destination_database.resolve()}",
                f"artifact:{receipt_path}",
            ],
        ):
            receipt = prepare_compressed_clone(
                CompressedCloneRequest(
                    source_database=args.source_database,
                    candidate_audit_receipt=args.candidate_audit_receipt,
                    candidate_coverage_receipt=args.candidate_coverage_receipt,
                    destination_database=args.destination_database,
                    operation_recorded_at=args.operation_recorded_at,
                    minimum_free_bytes=args.minimum_free_bytes,
                )
            )
            clone_receipt = receipt
            if not verify_compressed_clone_receipt(receipt):
                raise LatestStateActivationError("compressed clone receipt commitment is invalid")
            publish_text_no_clobber(receipt_path, receipt.model_dump_json())
    except JobAlreadyRunningError:
        _event("compressed_latest_state_clone_deferred", reason="job_lock_held")
        return 75
    except (ImmutableArtifactConflictError, LatestStateActivationError, OSError, ValueError) as exc:
        _event(
            "compressed_latest_state_clone_refused",
            error_type=type(exc).__name__,
            preserved_clone=(None if clone_receipt is None else clone_receipt.destination_database),
            preserved_clone_expected_sha256=(
                None if clone_receipt is None else clone_receipt.destination_database_sha256
            ),
            reason=str(exc),
        )
        return 2
    _event(
        "compressed_latest_state_clone_completed",
        compressed_size_bytes=receipt.compressed_size_bytes,
        destination_database=receipt.destination_database,
        free_bytes_after=receipt.free_bytes_after,
        receipt=str(receipt_path),
    )
    print(
        json.dumps(
            {
                "compressed_size_bytes": receipt.compressed_size_bytes,
                "destination_database": receipt.destination_database,
                "free_bytes_after": receipt.free_bytes_after,
                "receipt": str(receipt_path),
            },
            sort_keys=True,
        )
    )
    return 0


def receipt_path_for(args: argparse.Namespace) -> Path:
    receipt = args.receipt.resolve()
    protected = {
        args.source_database.resolve(),
        args.candidate_audit_receipt.resolve(),
        args.candidate_coverage_receipt.resolve(),
        args.destination_database.resolve(),
        *(
            Path(f"{args.destination_database.resolve()}{suffix}").resolve()
            for suffix in ("-wal", "-shm", "-journal")
        ),
        *(
            Path(f"{args.source_database.resolve()}{suffix}").resolve()
            for suffix in ("-wal", "-shm", "-journal")
        ),
    }
    for path in protected | {receipt}:
        require_no_reparse_points(path)
    if path_aliases_any(receipt, protected):
        raise LatestStateActivationError("clone receipt aliases a protected artifact")
    return receipt


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
