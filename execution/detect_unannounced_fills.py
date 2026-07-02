"""execution/detect_unannounced_fills.py — the retro half of the entry net.

Scans the portfolio tracker's recent transactions for fills the owner never
announced (no direction-compatible owner decision within the window) and lands
annotation-pending owner stubs in the decisions ledger. The W2 coach follow-up
asks for the conviction + falsifier the feed can't supply.

    python execution/detect_unannounced_fills.py
    python execution/detect_unannounced_fills.py --lookback-days 14 --min-usd 500
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from research.decision_feed import detect_unannounced_fills  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--match-window-days", type=int, default=3)
    parser.add_argument("--min-usd", type=float, default=1000.0)
    args = parser.parse_args()
    db_path = args.repo_root.resolve() / "data" / "portfolio.db"
    tally = detect_unannounced_fills(
        db_path=db_path,
        lookback_days=args.lookback_days,
        match_window_days=args.match_window_days,
        min_usd=args.min_usd,
    )
    print(f"detect_unannounced_fills: {tally}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
