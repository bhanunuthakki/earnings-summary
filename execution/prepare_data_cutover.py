"""Dry-run or prepare one isolated SQLite clone for a data cutover."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from provenance.cutover_preflight import (  # noqa: E402
    CutoverMode,
    CutoverRequest,
    canonical_manifest_json,
    prepare_cutover,
)


def _event(event: str, fields: dict[str, object]) -> None:
    print(
        json.dumps({"event": event, **fields}, default=str, sort_keys=True),
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preflight an isolated SQLite data cutover clone")
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--source-path", type=Path, required=True)
    parser.add_argument("--destination-path", type=Path, required=True)
    parser.add_argument("--live-database-path", type=Path)
    parser.add_argument("--audit-sample-limit", type=int, default=20)
    parser.add_argument(
        "--minimum-space-reserve-bytes",
        type=int,
        default=64 * 1024 * 1024,
    )
    parser.add_argument("--space-multiplier", type=int, default=3)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create and migrate only the explicit isolated destination",
    )
    args = parser.parse_args(argv)
    manifest = prepare_cutover(
        CutoverRequest(
            repo_root=args.repo_root,
            source_path=args.source_path,
            destination_path=args.destination_path,
            live_database_path=args.live_database_path,
            mode=CutoverMode.APPLY if args.apply else CutoverMode.DRY_RUN,
            audit_sample_limit=args.audit_sample_limit,
            minimum_space_reserve_bytes=args.minimum_space_reserve_bytes,
            space_multiplier=args.space_multiplier,
        ),
        logger=_event,
    )
    print(canonical_manifest_json(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
