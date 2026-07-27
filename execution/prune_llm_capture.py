"""Prune expired private LLM capture shards from configured and legacy roots."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from llm.capture import (  # noqa: E402
    DEFAULT_CAPTURE_RETENTION_DAYS,
    default_capture_archive_dir,
    prune_capture_archive,
)


class ArchivePruneResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    archive_count: int = Field(ge=0)
    deleted_files: int = Field(ge=0)
    retention_days: int = Field(gt=0)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--retention-days",
        type=int,
        default=DEFAULT_CAPTURE_RETENTION_DAYS,
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.retention_days <= 0:
        print("ERROR: --retention-days must be positive", file=sys.stderr)
        return 1
    repo_root = args.repo_root.resolve()
    archives = {
        default_capture_archive_dir(repo_root).resolve(),
        (repo_root / "data" / "llm_capture").resolve(),
    }
    try:
        deleted = sum(
            prune_capture_archive(
                path,
                retention_days=args.retention_days,
                strict=True,
            )
            for path in archives
        )
    except OSError as exc:
        print(
            f"ERROR: capture retention could not delete an expired shard ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1
    result = ArchivePruneResult(
        archive_count=len(archives),
        deleted_files=deleted,
        retention_days=args.retention_days,
    )
    print(json.dumps(result.model_dump(mode="json"), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
