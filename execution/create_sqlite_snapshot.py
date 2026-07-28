"""Create one verified SQLite reader snapshot without changing its source."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sqlite_snapshot import SnapshotRequest, create_snapshot  # noqa: E402


def _event(event: str, fields: dict[str, object]) -> None:
    print(json.dumps({"event": event, **fields}, default=str, sort_keys=True), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a verified SQLite reader snapshot")
    parser.add_argument("--source-path", type=Path, required=True)
    parser.add_argument("--destination-path", type=Path, required=True)
    parser.add_argument("--code-config-version", default="sqlite-reader-snapshot/v1")
    args = parser.parse_args(argv)
    result = create_snapshot(
        SnapshotRequest(
            source_path=args.source_path,
            destination_path=args.destination_path,
            code_config_version=args.code_config_version,
        ),
        logger=_event,
    )
    print(result.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
