"""execution/run_coach_pings.py — the daily governed-initiation pass (W2).

Collects the three deterministic coaching moments (owner-falsifier breach /
annotation-pending stub / open standing intent), runs them through the nag
governor (freshness gate → frequency caps → auto-mute), and pushes at most
DAILY_CAP Telegram pings with a Dismiss button; overflow waits quietly in the
digest. Zero LLM. Safe to run any number of times a day (a moment is
considered exactly once).

    python execution/run_coach_pings.py
    python execution/run_coach_pings.py --dry-run   # collect + gate, no sends
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
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    db_path = repo_root / "data" / "portfolio.db"

    from collections.abc import Callable

    from research.governor import Moment, run_governor

    send_fn: Callable[[int, Moment], bool] | None = None
    if not args.dry_run:
        from capture import telegram
        from capture.token_store import load_chat_id, load_token

        try:
            token = load_token(repo_root / "data" / "secrets" / "telegram_bot_token")
        except Exception:
            token = None
        chat_id = load_chat_id(repo_root / "data" / "capture" / "telegram_chat_id.json")
        if token and chat_id:
            bot_token, owner_chat = token, chat_id

            def _send(ping_id: int, moment: Moment) -> bool:
                try:
                    telegram.send_message(
                        bot_token,
                        owner_chat,
                        moment.body,
                        reply_markup=telegram.inline_keyboard(
                            [[("Dismiss", f"cp:dismiss:{ping_id}")]]
                        ),
                    )
                    return True
                except Exception:
                    return False

            send_fn = _send

    tally = run_governor(db_path, send_fn=send_fn)
    print(f"run_coach_pings: {tally}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
