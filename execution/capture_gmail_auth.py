"""execution/capture_gmail_auth.py — one-time Gmail OAuth consent for The Ledger.

Runs the browser consent flow once and caches a gmail.modify token at
``data/secrets/gmail_token.json`` (separate from the Drive token). Prereq: drop
your Google OAuth client-secrets at ``data/secrets/gmail_credentials.json``.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from capture import gmail  # noqa: E402


def main() -> int:
    gmail.build_service(PROJECT_ROOT, interactive=True)
    print("Gmail authorized; token saved to data/secrets/gmail_token.json")
    print(
        "Next: in Gmail, create a filter that applies a 'Capture/Inbox' label to the "
        "thoughts you forward to yourself (and create a 'Capture/Done' label). The poller "
        "ingests Capture/Inbox and relabels to Capture/Done."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
