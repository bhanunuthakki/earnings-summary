"""Produce the bounded Portfolio Tracker daily-refresh receipt.

This command is read-only: it probes the configured v1 API and records typed
evidence. It never starts a listener, changes Scheduler state, or mutates the
tracker database.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from operations.paths import portfolio_tracker_receipt_path  # noqa: E402
from runtime.portfolio_tracker import produce_daily_refresh_receipt  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--api-url", default=os.environ.get("PORTFOLIO_TRACKER_API_URL"))
    args = parser.parse_args()
    if not args.api_url:
        parser.error("PORTFOLIO_TRACKER_API_URL or --api-url is required")
    now = datetime.now(UTC)
    receipt = produce_daily_refresh_receipt(
        api_url=args.api_url,
        receipt_path=portfolio_tracker_receipt_path(args.repo_root),
        now=now,
    )
    print(receipt.model_dump_json())
    return 0 if receipt.lifecycle_state == "already_running" else 1


if __name__ == "__main__":
    raise SystemExit(main())
