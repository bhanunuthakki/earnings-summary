"""Plan or atomically materialize candidates from a sealed IR observation bundle."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from log_redact import redact  # noqa: E402
from pipeline.ir_candidate_caller import (  # noqa: E402
    IrCandidateCallerRequest,
    apply_ir_candidate_plan,
    load_ir_observation_artifact,
    plan_ir_candidates,
)
from runtime.job_runtime import JobLock  # noqa: E402


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--issuer", required=True, choices=("WIX", "RBRK"))
    parser.add_argument("--recorded-by", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--blob-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    mode = "apply" if args.apply else "dry_run"
    try:
        _event("ir_candidate_materialization_started", mode=mode, issuer=str(args.issuer))
        bundle_bytes = load_ir_observation_artifact(args.bundle)
        plan = plan_ir_candidates(
            bundle_bytes,
            IrCandidateCallerRequest(
                issuer_identifier=str(args.issuer),
                recorded_by=str(args.recorded_by),
                recorded_at=datetime.now(UTC).replace(tzinfo=None),
                reason=str(args.reason),
            ),
        )
        if args.apply:
            with JobLock(
                PROJECT_ROOT,
                "ir-candidate-materialization",
                [
                    f"sqlite:{args.db.resolve()}",
                    f"evidence-blobs:{args.blob_root.resolve()}",
                ],
            ):
                result = apply_ir_candidate_plan(args.db, args.blob_root, bundle_bytes, plan)
            payload = result.model_dump_json()
            created, replayed = result.created, result.replayed
        else:
            payload = plan.model_dump_json(exclude={"catalog"})
            created, replayed = 0, 0
    except Exception as exc:
        _event(
            "ir_candidate_materialization_failed",
            mode=mode,
            issuer=str(args.issuer),
            error_type=type(exc).__name__,
            error=redact(exc),
        )
        return 1
    sys.stdout.write(payload + "\n")
    _event(
        "ir_candidate_materialization_completed",
        mode=mode,
        issuer=str(args.issuer),
        candidate_count=plan.candidate_count,
        created=created,
        replayed=replayed,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
