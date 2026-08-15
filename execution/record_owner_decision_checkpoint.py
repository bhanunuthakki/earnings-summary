"""Confirm one typed owner-decision checkpoint from a JSON payload.

The payload is validated before the writer opens.  Confirmation is atomic and
idempotent; the command prints a schema-validated JSON receipt to stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from research.owner_decision_checkpoint import (  # noqa: E402
    CheckpointConflictError,
    OwnerDecisionCheckpointPayload,
    confirm_owner_decision_checkpoint,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        payload = OwnerDecisionCheckpointPayload.model_validate_json(
            args.payload.read_text(encoding="utf-8")
        )
        receipt = confirm_owner_decision_checkpoint(payload, db_path=args.db)
    except (OSError, ValidationError, CheckpointConflictError, RuntimeError, ValueError) as exc:
        print(
            json.dumps(
                {"event": "owner_decision_checkpoint_failed", "error": str(exc)},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(receipt.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
