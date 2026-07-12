"""execution/run_decision_nudge.py — the point-of-intent decision nudge (PR2).

Scans for owner decisions still missing conviction/falsifier, made in the
last N days, never nudged before, and sends one Telegram message per stub
with [Fill in now / Skip] buttons. No LLM leg — the scan and message text are
both deterministic (Layer 3 discipline: a single-purpose CLI over the
already-tested ``capture.decision_nudge`` core).

    python execution/run_decision_nudge.py
    python execution/run_decision_nudge.py --lookback-days 3
    python execution/run_decision_nudge.py --repo-root . --db-path /tmp/x.db

Exit 0 when the scan ran (including zero stubs found or capture unconfigured
— a missing bot token is "not set up", not a failure). Exit 2 only on a
missing DB (configuration, not silence).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from capture import decision_nudge, token_store  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument("--lookback-days", type=int, default=decision_nudge.DEFAULT_LOOKBACK_DAYS)
    args = parser.parse_args(argv)

    repo_root: Path = args.repo_root.resolve()
    db_path: Path = (
        args.db_path if args.db_path is not None else repo_root / "data" / "portfolio.db"
    )
    if not db_path.exists():
        sys.stderr.write(f"FATAL: no DB at {db_path}\n")
        return 2

    try:
        token = token_store.load_token(repo_root / "data" / "secrets" / "telegram_bot_token")
    except token_store.CaptureSetupError as exc:
        print(f"run_decision_nudge: not configured ({exc}); exiting cleanly", file=sys.stderr)
        return 0

    chat_id = token_store.load_chat_id(repo_root / "data" / "capture" / "telegram_chat_id.json")
    if chat_id is None:
        print(
            "run_decision_nudge: no chat id on file yet (owner hasn't messaged the bot); "
            "exiting cleanly",
            file=sys.stderr,
        )
        return 0

    sent = decision_nudge.send_nudges(
        token, chat_id, lookback_days=args.lookback_days, db_path=db_path
    )
    print(f"run_decision_nudge: sent={sent}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
