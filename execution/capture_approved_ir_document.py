"""Capture one explicitly approved IR candidate and admit its exact bytes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import cast

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ir_pipeline.evidence_capture import SessionLike  # noqa: E402
from log_redact import redact  # noqa: E402
from pipeline.ir_approval_capture import (  # noqa: E402
    ExactIrCaptureActionInput,
    capture_and_admit_exact_ir_document,
)
from runtime.job_runtime import JobLock  # noqa: E402


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--owner-actor", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--checkpoint-root", type=Path, required=True)
    parser.add_argument("--blob-root", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--user-agent", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _event("approved_ir_capture_started", candidate_id=str(args.candidate_id))
        with (
            JobLock(
                PROJECT_ROOT,
                "approved-ir-capture",
                [
                    f"sqlite:{args.db.resolve()}",
                    f"evidence-blobs:{args.blob_root.resolve()}",
                ],
            ),
            requests.Session() as session,
        ):
            receipt = capture_and_admit_exact_ir_document(
                args.db,
                ExactIrCaptureActionInput(
                    candidate_id=str(args.candidate_id),
                    reason=str(args.reason),
                ),
                owner_actor=str(args.owner_actor),
                checkpoint_root=args.checkpoint_root,
                blob_root=args.blob_root,
                task_id=str(args.task_id),
                user_agent=str(args.user_agent),
                session=cast(SessionLike, session),
            )
    except Exception as exc:
        _event(
            "approved_ir_capture_failed",
            candidate_id=str(args.candidate_id),
            error_type=type(exc).__name__,
            error=redact(exc),
        )
        return 1
    sys.stdout.write(receipt.model_dump_json() + "\n")
    _event(
        "approved_ir_capture_completed",
        candidate_id=receipt.candidate_id,
        outcome=receipt.outcome,
        network_fetched=receipt.network_fetched,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
