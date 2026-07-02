"""execution/land_session_notes.py — the claude_session capture channel.

The owner's thinking often RESOLVES inside Claude Code chats, and the corpus
never hears (the NVDA-LEAP intent stayed 'live' in the seed while a chat had
killed it — the canonical staleness case from the 2026-07-02 callout). This
script is the landing pad the ``/ledger-land`` skill drives at session end:
distilled items land through the SAME LLM-free capture pipeline as Telegram,
with ``channel='claude_session'``.

Three item kinds, one invocation each (the skill loops):

    # a musing / thought worth keeping
    python execution/land_session_notes.py musing --text "..." [--session-ref <id>]

    # close a standing intent the session resolved
    python execution/land_session_notes.py close-intent --ref seed:intent:leap-sleeve \\
        --verdict resolved-rejected --reason "deletes the NVO hedge" --session-ref <id>

    # a position decision stated in the session (persists via the 0130 adapters)
    python execution/land_session_notes.py decision --ticker NVO --direction buy \\
        --conviction high --falsifier "GLP-1 share loss 2Q" --size-usd 31000

Words land durably before any fallible step; nothing here fires an LLM.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import cast

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("musing", "close-intent", "decision"))
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--text", default=None)
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
    from research.decision_capture import capture_decision
    from research.decision_feed import link_decision_to_note, persist_owner_decision

    def _persist(
        *,
        note_id: object = None,
        db_path: object = None,
        ticker: object = None,
        direction: object = None,
        size_pct: object = None,
        conviction: object = None,
        falsifier: object = None,
    ) -> int:
        """capture_decision's persist_fn contract, closing over the CLI extras
        (size_usd / account / instrument / session provenance)."""
        return persist_owner_decision(
            ticker=str(ticker or ""),
            direction=str(direction or ""),
            size_pct=float(size_pct) if isinstance(size_pct, (int, float)) else None,
            conviction=str(conviction) if conviction else None,
            falsifier=str(falsifier) if falsifier else None,
            size_usd=args.size_usd,
            account=args.account,
            instrument=args.instrument,
            rationale=args.text,
            user_notes=closed_by,
            note_id=int(note_id) if isinstance(note_id, int) else None,
            db_path=cast("Path | str | None", db_path),
        )

    decision_id = capture_decision(
        text=args.text or "",
        ticker=args.ticker,
        direction=args.direction,
        size_pct=args.size_pct,
        conviction=args.conviction,
        falsifier=args.falsifier,
        db_path=db_path,
        persist_fn=_persist,
        link_fn=link_decision_to_note,
    )
    if decision_id is None:
        print(
            "land_session_notes: decision NOT captured (below threshold or "
            "missing ticker/direction)",
            file=sys.stderr,
        )
        return 1
    print(f"land_session_notes: decision {decision_id} captured", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
