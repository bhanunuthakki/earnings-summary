"""Upgrade one admitted compressed rehearsal clone to the exact repository head."""

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
from provenance.compressed_candidate_clone import MINIMUM_SAFE_FREE_BYTES  # noqa: E402
from provenance.cutover_preflight import (  # noqa: E402
    CutoverPreflightError,
    ExistingCloneUpgradeRequest,
    upgrade_existing_isolated_clone,
)
from provenance.immutable_artifact import (  # noqa: E402
    ImmutableArtifactConflictError,
    path_aliases_any,
    require_no_reparse_points,
)


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("datetime must include a timezone")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--compressed-clone-receipt", type=Path, required=True)
    parser.add_argument("--expected-source-revision", required=True)
    parser.add_argument("--expected-target-revision", required=True)
    parser.add_argument("--operation-recorded-at", type=_datetime, required=True)
    parser.add_argument(
        "--minimum-free-bytes",
        type=int,
        default=MINIMUM_SAFE_FREE_BYTES,
    )
    parser.add_argument("--receipt", type=Path, required=True)
    return parser


def safe_receipt_path(output: Path, *, database: Path, inputs: tuple[Path, ...]) -> Path:
    destination = Path(os.path.abspath(output))
    db = Path(os.path.abspath(database))
    protected = {
        db,
        *(Path(os.path.abspath(f"{db}{suffix}")) for suffix in ("-wal", "-shm", "-journal")),
        *(Path(os.path.abspath(path)) for path in inputs),
    }
    for path in (destination, *protected):
        require_no_reparse_points(path)
    if path_aliases_any(destination, protected):
        raise ValueError("upgrade receipt aliases a protected artifact")
    return destination


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        destination = safe_receipt_path(
            args.receipt,
            database=args.database,
            inputs=(args.compressed_clone_receipt,),
        )
        receipt = upgrade_existing_isolated_clone(
            ExistingCloneUpgradeRequest(
                repo_root=args.repo_root,
                database_path=args.database,
                compressed_clone_receipt=args.compressed_clone_receipt,
                receipt_path=destination,
                expected_source_revision=args.expected_source_revision,
                expected_target_revision=args.expected_target_revision,
                operation_recorded_at=args.operation_recorded_at,
                minimum_free_bytes=args.minimum_free_bytes,
            )
        )
    except (
        CutoverPreflightError,
        ImmutableArtifactConflictError,
        OSError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {"event": "rehearsal_clone_upgrade_blocked", "error": redact(str(exc))},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "database": receipt.database_path,
                "receipt": str(destination),
                "receipt_sha256": receipt.receipt_sha256,
                "revision": receipt.database_after.alembic_revision,
                "status": "upgraded",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
