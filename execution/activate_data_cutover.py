"""Dry-run or atomically activate one exact, reviewed SQLite cutover candidate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from provenance.atomic_cutover import (  # noqa: E402
    ActivationMode,
    ActivationRequest,
    ActivationRolledBackError,
    AtomicCutoverError,
    activate_data_cutover,
    canonical_activation_json,
)


def _event(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--live-db", type=Path, required=True)
    parser.add_argument("--candidate-db", type=Path, required=True)
    parser.add_argument("--rollback-db", type=Path, required=True)
    parser.add_argument("--failed-candidate-db", type=Path, required=True)
    parser.add_argument("--receipt-path", type=Path, required=True)
    parser.add_argument("--quiescence-receipt", type=Path, required=True)
    parser.add_argument("--expected-quiescence-receipt-sha256", required=True)
    parser.add_argument("--expected-live-sha256", required=True)
    parser.add_argument("--expected-candidate-sha256", required=True)
    parser.add_argument("--expected-alembic-head", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the recoverable rename sequence; default is a read-only dry run",
    )
    args = parser.parse_args(argv)
    try:
        receipt = activate_data_cutover(
            ActivationRequest(
                repo_root=args.repo_root,
                live_database=args.live_db,
                candidate_database=args.candidate_db,
                rollback_database=args.rollback_db,
                failed_candidate_database=args.failed_candidate_db,
                receipt_path=args.receipt_path,
                quiescence_receipt_path=args.quiescence_receipt,
                expected_quiescence_receipt_sha256=(args.expected_quiescence_receipt_sha256),
                expected_live_sha256=args.expected_live_sha256,
                expected_candidate_sha256=args.expected_candidate_sha256,
                expected_alembic_head=args.expected_alembic_head,
                mode=ActivationMode.APPLY if args.apply else ActivationMode.DRY_RUN,
            )
        )
    except ActivationRolledBackError as exc:
        print(canonical_activation_json(exc.receipt), end="")
        _event(
            "data_cutover_activation_rolled_back",
            failure=exc.receipt.failure,
            receipt_sha256=exc.receipt.receipt_sha256,
        )
        return 1
    except (AtomicCutoverError, ValidationError, ValueError) as exc:
        _event(
            "data_cutover_activation_refused",
            error_type=type(exc).__name__,
            detail=str(exc),
        )
        return 2
    print(canonical_activation_json(receipt), end="")
    _event(
        "data_cutover_activation_ready"
        if receipt.mode is ActivationMode.DRY_RUN
        else "data_cutover_activation_applied",
        receipt_sha256=receipt.receipt_sha256,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
