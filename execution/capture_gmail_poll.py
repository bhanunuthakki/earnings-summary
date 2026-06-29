"""execution/capture_gmail_poll.py — poll the Capture/Inbox Gmail label (The Ledger).

The SECONDARY capture mouth, run as its own scheduled task (decoupled from the
Telegram poller). Ingests each labelled message through the shared LLM-free
pipeline and relabels to Capture/Done. Unconfigured (no token) → exit cleanly.

    python execution/capture_gmail_poll.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from capture import gmail  # noqa: E402
from capture.token_store import CaptureSetupError  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    repo_root = PROJECT_ROOT
    db_path = repo_root / "data" / "portfolio.db"
    try:
        counts = gmail.poll_gmail(repo_root, db_path)
    except CaptureSetupError as exc:
        print(f"capture_gmail_poll: not configured ({exc}); exiting cleanly", file=sys.stderr)
        return 0
    print(f"capture_gmail_poll: {counts}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
