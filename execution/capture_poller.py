"""execution/capture_poller.py — long-poll Telegram and ingest musings (The Ledger).

Runs as a Windows scheduled task (cron/capture_poller.task.xml), NOT a Flask
thread: capture liveness is decoupled from the dashboard, and the task supplies
RestartOnFailure + per-run logs. The bot token is read from
``data/secrets/telegram_bot_token``; absent → exit cleanly (capture is simply
unconfigured). ``--once`` drains a single batch (manual drain / smoke test).

    python execution/capture_poller.py --once
    python execution/capture_poller.py --repo-root /path/to/repo
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from capture import poller, token_store  # noqa: E402
from capture.matcher import load_roster  # noqa: E402


def _version_stamp(repo_root: Path) -> str:
    """The running git short SHA, or ``"unknown"`` on any failure — a stale
    long-running poller process (cron restarts it, but a hung/zombie instance
    survives on an old checkout) is otherwise invisible in cron logs until its
    behavior diverges from what's on disk. Best-effort: never blocks startup."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        return result.stdout.strip() or "unknown"
    except (subprocess.SubprocessError, OSError):
        return "unknown"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--once", action="store_true", help="drain one batch then exit")
    parser.add_argument("--poll-timeout", type=int, default=50)
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    print(
        f"capture_poller: version={_version_stamp(repo_root)} "
        f"started={datetime.now(UTC).isoformat()}",
        file=sys.stderr,
    )
    db_path = repo_root / "data" / "portfolio.db"
    offset_path = repo_root / "data" / "capture" / "telegram_offset.json"
    audio_dir = repo_root / "data" / "capture" / "audio"
    token_path = repo_root / "data" / "secrets" / "telegram_bot_token"

    try:
        token = token_store.load_token(token_path)
    except token_store.CaptureSetupError as exc:
        print(f"capture_poller: not configured ({exc}); exiting cleanly", file=sys.stderr)
        return 0

    roster = load_roster(db_path)
    print(
        f"capture_poller: repo_root={repo_root} once={args.once} timeout={args.poll_timeout}",
        file=sys.stderr,
    )

    if args.once:
        counts = poller.poll_once(
            token,
            db_path=db_path,
            offset_path=offset_path,
            audio_dir=audio_dir,
            roster=roster,
            poll_timeout=args.poll_timeout,
        )
        print(f"capture_poller: {counts}", file=sys.stderr)
        return 0

    poller.run_forever(
        token,
        db_path=db_path,
        offset_path=offset_path,
        audio_dir=audio_dir,
        roster=roster,
        poll_timeout=args.poll_timeout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
