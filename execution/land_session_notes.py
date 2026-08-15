"""execution/land_session_notes.py — the claude_session capture channel.

The owner's thinking often RESOLVES inside Claude Code chats, and the corpus
never hears (the NVDA-LEAP intent stayed 'live' in the seed while a chat had
killed it — the canonical staleness case from the 2026-07-02 callout). This
script is the landing pad the ``/ledger-land`` skill drives at session end:
distilled items land through the SAME LLM-free capture pipeline as Telegram,
with ``channel='claude_session'``.

Four item kinds, one invocation each (the skill loops):

    # a musing / thought worth keeping
    python execution/land_session_notes.py musing --text "..." [--session-ref <id>]

    # close a standing intent the session resolved
    python execution/land_session_notes.py close-intent --ref seed:intent:leap-sleeve \\
        --verdict resolved-rejected --reason "deletes the NVO hedge" --session-ref <id>

    # a position decision stated in the session (typed, atomic, idempotent)
    python execution/land_session_notes.py decision \\
        --checkpoint-payload .tmp/owner-decision-checkpoint.json

    # a WHOLE deep-session transcript bridged for later distillation (B4) — the
    # 18:00 session_distill sweep reads it, NOT this script (LLM-free at land
    # time, same invariant as every other kind here)
    python execution/land_session_notes.py transcript --file transcript.txt \\
        [--session-ref <id>]
    # or via stdin:
    python execution/land_session_notes.py transcript --session-ref <id> < transcript.txt

Words land durably before any fallible step; nothing here fires an LLM.
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("musing", "close-intent", "decision", "transcript"))
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--text", default=None)
    parser.add_argument(
        "--file", type=Path, default=None, help="transcript kind: path to the transcript text"
    )
    parser.add_argument("--session-ref", default=None, help="originating session id/slug")
    parser.add_argument("--ref", default=None, help="intent source_ref to close")
    parser.add_argument("--verdict", default="resolved-rejected")
    parser.add_argument("--reason", default=None)
    parser.add_argument("--ticker", default=None)
    parser.add_argument("--direction", default=None)
    parser.add_argument("--conviction", default=None)
    parser.add_argument("--falsifier", default=None)
    parser.add_argument("--size-usd", type=float, default=None)
    parser.add_argument("--size-pct", type=float, default=None)
    parser.add_argument("--account", default=None)
    parser.add_argument("--instrument", default=None)
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
    db_path = args.repo_root.resolve() / "data" / "portfolio.db"
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

    if args.kind == "transcript":
        if args.file is not None:
            text = args.file.read_text(encoding="utf-8", errors="replace")
        else:
            text = sys.stdin.read()
        text = text.strip()
        if not text:
            print("transcript requires --file <path> or non-empty stdin", file=sys.stderr)
            return 2
        from capture import sessions
        from clock import now_naive_utc

        purge_after = (now_naive_utc() + timedelta(days=30)).isoformat()
        session_id = sessions.new_session(
            channel="claude_session",
            media_kind="text",
            transcript=text,
            external_ref=args.session_ref,
            purge_after=purge_after,
            db_path=db_path,
        )
        if session_id is None:
            print(
                "land_session_notes: transcript duplicate (session-ref already bridged)",
                file=sys.stderr,
            )
            return 1
        # LLM-free at land time (this script never fires an LLM): the 18:00
        # session_distill sweep (execution/run_session_distill.py) reads this
        # row later and does the actual distillation + auto-adopt.
        print(f"land_session_notes: transcript captured session={session_id}", file=sys.stderr)
        return 0

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
