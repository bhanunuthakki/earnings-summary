"""Plan or build an additive candidate from live operations and governed data."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from log_redact import redact  # noqa: E402
from provenance.immutable_artifact import (  # noqa: E402
    path_aliases_any,
    publish_text_no_clobber,
    require_no_reparse_points,
)
from provenance.live_cutover_merge import (  # noqa: E402
    apply_live_cutover_merge,
    plan_live_cutover_merge,
)
from runtime.job_runtime import JobLock  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-db", type=Path, required=True)
    parser.add_argument("--governed-db", type=Path, required=True)
    parser.add_argument("--live-health-receipt", type=Path, required=True)
    parser.add_argument("--governed-health-receipt", type=Path, required=True)
    parser.add_argument("--destination-db", type=Path)
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--manifest-path", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        manifest_path = _manifest_destination(
            args.manifest_path,
            protected_paths={
                args.live_db,
                args.governed_db,
                args.live_health_receipt,
                args.governed_health_receipt,
                *(set() if args.destination_db is None else {args.destination_db}),
            },
        )
        if args.apply:
            if (
                args.destination_db is None
                or not args.expected_plan_sha256
                or manifest_path is None
            ):
                parser.error(
                    "--apply requires --destination-db, --expected-plan-sha256, and --manifest-path"
                )
            with JobLock(
                PROJECT_ROOT,
                "build-live-cutover-candidate",
                [
                    f"sqlite:{args.live_db.resolve()}",
                    f"sqlite:{args.governed_db.resolve()}",
                    f"sqlite:{args.destination_db.resolve()}",
                ],
            ):
                result = apply_live_cutover_merge(
                    args.live_db,
                    args.governed_db,
                    args.destination_db,
                    expected_plan_sha256=args.expected_plan_sha256,
                    live_health_receipt=args.live_health_receipt,
                    governed_health_receipt=args.governed_health_receipt,
                    receipt_path=manifest_path,
                )
        else:
            result = plan_live_cutover_merge(
                args.live_db,
                args.governed_db,
                live_health_receipt=args.live_health_receipt,
                governed_health_receipt=args.governed_health_receipt,
            )
        payload = result.model_dump_json()
        if manifest_path is not None and not args.apply:
            publish_text_no_clobber(manifest_path, payload)
    except Exception as exc:
        sys.stderr.write(
            json.dumps(
                {
                    "event": "live_cutover_candidate_failed",
                    "error_type": type(exc).__name__,
                    "detail": redact(exc),
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 1
    sys.stdout.write(payload + "\n")
    return 0


def _manifest_destination(
    manifest_path: Path | None,
    *,
    protected_paths: set[Path],
) -> Path | None:
    if manifest_path is None:
        return None
    destination = manifest_path.resolve()
    protected: set[Path] = set()
    for path in protected_paths:
        resolved = path.resolve()
        protected.add(resolved)
        protected.update(
            Path(f"{resolved}{suffix}").resolve() for suffix in ("-wal", "-shm", "-journal")
        )
    require_no_reparse_points(destination)
    if path_aliases_any(destination, protected):
        raise ValueError("manifest path aliases a protected cutover artifact")
    return destination


if __name__ == "__main__":
    raise SystemExit(main())
