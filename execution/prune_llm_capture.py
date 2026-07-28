"""Prune expired private LLM capture shards, then enforce an alert-only byte cap."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from llm.capture import (  # noqa: E402
    DEFAULT_CAPTURE_MAX_BYTES,
    DEFAULT_CAPTURE_RETENTION_DAYS,
    capture_archive_bytes,
    default_capture_archive_dir,
    prune_capture_archive,
)


class ArchivePruneResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    archive_count: int = Field(ge=0)
    deleted_files: int = Field(ge=0)
    retention_days: int = Field(gt=0)
    remaining_bytes: int = Field(ge=0)
    max_total_bytes: int = Field(gt=0)
    over_limit: bool


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--retention-days",
        type=int,
        default=DEFAULT_CAPTURE_RETENTION_DAYS,
    )
    parser.add_argument(
        "--max-total-bytes",
        type=int,
        default=DEFAULT_CAPTURE_MAX_BYTES,
        help=(
            "Fail after age-based pruning when recognized capture shards exceed this total. "
            "Recent shards are never deleted to satisfy the byte ceiling."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.retention_days <= 0:
        print("ERROR: --retention-days must be positive", file=sys.stderr)
        return 1
    if args.max_total_bytes <= 0:
        print("ERROR: --max-total-bytes must be positive", file=sys.stderr)
        return 1
    repo_root = args.repo_root.resolve()
    configured_archive = default_capture_archive_dir(repo_root).resolve()
    archives = {
        configured_archive,
        (repo_root / "data" / "llm_capture").resolve(),
    }
    try:
        deleted = sum(
            prune_capture_archive(
                path,
                retention_days=args.retention_days,
                strict=True,
                require_directory=path == configured_archive,
            )
            for path in archives
        )
        remaining_bytes = sum(
            capture_archive_bytes(
                path,
                strict=True,
                require_directory=path == configured_archive,
            )
            for path in archives
        )
    except OSError as exc:
        print(
            f"ERROR: capture retention/size audit failed ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1
    over_limit = remaining_bytes > args.max_total_bytes
    result = ArchivePruneResult(
        archive_count=len(archives),
        deleted_files=deleted,
        retention_days=args.retention_days,
        remaining_bytes=remaining_bytes,
        max_total_bytes=args.max_total_bytes,
        over_limit=over_limit,
    )
    print(json.dumps(result.model_dump(mode="json"), indent=2))
    if over_limit:
        print(
            "ERROR: capture archive exceeds configured byte ceiling after retention pruning; "
            "no in-window files were deleted",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
