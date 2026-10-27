"""Record an explicit owner note, intent resolution, or typed decision checkpoint.

Use --db with the configured canonical database or an explicit disposable test
fixture. This command does not infer a database from the checkout and makes no
LLM call. Whole-session transcript ingestion and automatic distillation are retired.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("musing", "close-intent", "decision"))
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--text", default=None)
    parser.add_argument("--session-ref", default=None, help="originating session id/slug")
    parser.add_argument("--ref", default=None, help="intent source_ref to close")
    parser.add_argument("--verdict", default="resolved-rejected")
    parser.add_argument("--reason", default=None)
    parser.add_argument(
        "--checkpoint-payload",
        type=Path,
        default=None,
        help=(
            "decision kind: typed owner-decision checkpoint JSON; confirmation "
            "atomically writes/links its decisions and sizing intents"
        ),
    )
    args = parser.parse_args()
    db_path = args.db.expanduser().resolve()
    if not db_path.is_file():
        parser.error("--db must name an existing configured database or explicit test fixture")
    closed_by = f"claude_session:{args.session_ref or 'unattributed'}"

    if args.kind == "musing":
        if not (args.text or "").strip():
            print("musing requires --text", file=sys.stderr)
            return 2
        from capture.ingest import ingest_capture

        result = ingest_capture(
            channel="claude_session", media_kind="text", text=args.text, db_path=db_path
        )
        print(
            f"land_session_notes: musing {result.status} note={result.note_id} "
            f"ticker={result.ticker}",
            file=sys.stderr,
        )
        return 0 if result.status == "landed" else 1

    if args.kind == "close-intent":
        if not args.ref or not (args.reason or "").strip():
            print("close-intent requires --ref and --reason", file=sys.stderr)
            return 2
        from synthesis.reconcile import close_intent

        ok = close_intent(
            args.ref,
            args.verdict,
            reason=args.reason,
            closed_by=closed_by,
            db_path=db_path,
        )
        print(
            f"land_session_notes: close-intent {args.ref} → "
            f"{args.verdict if ok else 'NOT FOUND / already resolved'}",
            file=sys.stderr,
        )
        return 0 if ok else 1

    # decision
    if args.checkpoint_payload is not None:
        from research.owner_decision_checkpoint import (
            OwnerDecisionCheckpointPayload,
            confirm_owner_decision_checkpoint,
        )

        payload = OwnerDecisionCheckpointPayload.model_validate_json(
            args.checkpoint_payload.read_text(encoding="utf-8")
        )
        if payload.source_channel != "claude_session":
            raise ValueError("land_session_notes requires source_channel='claude_session'")
        receipt = confirm_owner_decision_checkpoint(payload, db_path=db_path)
        print(
            "land_session_notes: owner checkpoint "
            f"{receipt.checkpoint_id} confirmed decisions={list(receipt.decision_ids)} "
            f"created={receipt.created}",
            file=sys.stderr,
        )
        return 0
    print("decision requires --checkpoint-payload", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
