"""Publish one immutable exhaustive-health receipt for a fenced cutover source."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from provenance.immutable_artifact import (  # noqa: E402
    ImmutableArtifactConflictError,
    path_aliases_any,
    publish_text_no_clobber,
    require_no_reparse_points,
)
from provenance.live_cutover_merge import (  # noqa: E402
    LiveCutoverMergeError,
    audit_cutover_source_health,
)
from runtime.job_runtime import JobAlreadyRunningError, JobLock  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def receipt_destination(database: Path, output: Path) -> Path:
    source = database.resolve()
    destination = output.resolve()
    protected = {
        source,
        *(Path(f"{source}{suffix}").resolve() for suffix in ("-wal", "-shm", "-journal")),
    }
    require_no_reparse_points(source)
    require_no_reparse_points(destination)
    if path_aliases_any(destination, protected):
        raise LiveCutoverMergeError("health receipt aliases a protected SQLite source")
    return destination


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        database = args.database.resolve()
        output = receipt_destination(database, args.output)
        with JobLock(
            PROJECT_ROOT,
            "audit-live-cutover-source-health",
            [f"sqlite:{database}", f"artifact:{output}"],
        ):
            receipt = audit_cutover_source_health(database)
            published = publish_text_no_clobber(output, receipt.model_dump_json())
    except JobAlreadyRunningError:
        _event("live_cutover_source_health_deferred", reason="job_lock_held")
        return 75
    except (ImmutableArtifactConflictError, LiveCutoverMergeError, OSError) as exc:
        _event(
            "live_cutover_source_health_refused",
            error_type=type(exc).__name__,
            reason=str(exc),
        )
        return 2
    _event(
        "live_cutover_source_health_completed",
        database_sha256=receipt.source_sha256,
        outcome="published" if published else "exact_replay",
        output=str(output),
        receipt_sha256=receipt.receipt_sha256,
    )
    print(receipt.model_dump_json())
    return 0


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
