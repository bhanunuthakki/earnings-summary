"""Plan or build an additive candidate from live operations and governed data."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from log_redact import redact  # noqa: E402
from provenance.live_cutover_merge import (  # noqa: E402
    apply_live_cutover_merge,
    plan_live_cutover_merge,
)
from runtime.job_runtime import JobLock  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-db", type=Path, required=True)
    parser.add_argument("--governed-db", type=Path, required=True)
    parser.add_argument("--destination-db", type=Path)
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--manifest-path", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.apply:
            if args.destination_db is None or not args.expected_plan_sha256:
                parser.error("--apply requires --destination-db and --expected-plan-sha256")
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
                )
        else:
            result = plan_live_cutover_merge(args.live_db, args.governed_db)
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
    payload = result.model_dump_json()
    if args.manifest_path is not None:
        args.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        args.manifest_path.write_text(payload + "\n", encoding="utf-8")
    sys.stdout.write(payload + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
