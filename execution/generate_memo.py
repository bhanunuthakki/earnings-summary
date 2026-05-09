"""
execution/generate_memo.py
--------------------------
CLI wrapper for src/memo_generator.generate_memo().

Examples:
  python execution/generate_memo.py --ticker AMZN
  python execution/generate_memo.py --portfolio
  python execution/generate_memo.py --ticker GOOG --news-days 30

Output: transcripts/memos/<TICKER>_memo_<YYYY-MM-DD>.html
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.append(str(SRC_DIR))

from memo_generator import generate_memo  # noqa: E402
from portfolio import get_portfolio  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate HTML investor memo per portfolio name.")
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--ticker", help="Single ticker to memo")
    grp.add_argument("--portfolio", action="store_true", help="All portfolio names")
    grp.add_argument("--all", action="store_true", help="Portfolio + watchlist (heavy run)")
    parser.add_argument("--news-days", type=int, default=14,
                        help="FMP news window in days (default: 14)")
    args = parser.parse_args()

    if args.ticker:
        targets = [args.ticker.upper()]
    elif args.portfolio:
        targets = [e.ticker for e in get_portfolio(include_watchlist=False)]
    else:
        targets = [e.ticker for e in get_portfolio(include_watchlist=True)]

    print(f"[scope] {len(targets)} ticker(s): {', '.join(targets)}")
    ok = 0
    fail = 0
    for t in targets:
        try:
            out = generate_memo(t, news_days=args.news_days)
            print(f"  [ok]   {t} -> {out.relative_to(PROJECT_ROOT)}")
            ok += 1
        except FileNotFoundError as e:
            print(f"  [skip] {t} -> {e}")
            fail += 1
        except Exception as e:
            print(f"  [err]  {t} -> {type(e).__name__}: {e}")
            traceback.print_exc(file=sys.stderr)
            fail += 1
    print(f"[summary] ok={ok} skip_or_err={fail}")


if __name__ == "__main__":
    main()
