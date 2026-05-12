"""
execution/fetch_fmp_earnings_calendar.py
----------------------------------------
Layer 3 execution script: fetch earnings calendar data from FMP for tracked
companies and persist as raw JSON for the front-end calendar to consume.

Output: data/historical/fmp/<TICKER>_earnings_calendar.json
Schema (list of events, FMP's native shape):
    [
      {
        "symbol": "NVO",
        "date": "2026-05-06",
        "epsActual": null,
        "epsEstimated": 0.87,
        "revenueActual": null,
        "revenueEstimated": 11115540000,
        "lastUpdated": "2026-05-03"
      },
      ...
    ]

Usage:
  python execution/fetch_fmp_earnings_calendar.py --ticker NVO
  python execution/fetch_fmp_earnings_calendar.py --all          # active universe
  python execution/fetch_fmp_earnings_calendar.py --portfolio
  python execution/fetch_fmp_earnings_calendar.py --watchlist
  python execution/fetch_fmp_earnings_calendar.py --evaluation
"""

import argparse
import json
import os
import sys

import requests
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
DATA_DIR = os.path.join(PROJECT_ROOT, "data", "historical", "fmp")
sys.path.append(SRC_DIR)

import db  # noqa: E402
from log_redact import redact as _redact  # noqa: E402

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
FMP_API_KEY = os.environ.get("FMP_API_KEY")

FMP_BASE = "https://financialmodelingprep.com/stable"
DATA_TYPE = "earnings_calendar"
DEFAULT_LIMIT = 12


def fetch_earnings(symbol: str, limit: int) -> list[dict] | None:
    url = f"{FMP_BASE}/earnings"
    params = {"symbol": symbol, "limit": limit, "apikey": FMP_API_KEY}
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            print(f"  [Schema] {symbol}: expected list, got {type(data).__name__}", file=sys.stderr)
            return None
        return data
    except requests.HTTPError as e:
        code = resp.status_code if "resp" in dir() else "?"
        print(f"  [HTTP {code}] {symbol}: {_redact(e)}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  [Error] {symbol}: {_redact(e)}", file=sys.stderr)
        return None


def save(symbol: str, events: list[dict]) -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{symbol.upper()}_{DATA_TYPE}.json")
    with open(path, "w") as f:
        json.dump(events, f, indent=2)
    return path


def process_ticker(ticker: str, limit: int) -> bool:
    ticker = ticker.upper()
    events = fetch_earnings(ticker, limit)
    if events is None:
        print(f"  [FAIL] {ticker}")
        return False
    path = save(ticker, events)
    print(f"  [OK] {ticker}: {len(events)} events -> {path}")
    return True


def select_tickers(args: argparse.Namespace) -> list[str]:
    if args.ticker:
        return [args.ticker.upper()]
    companies = db.get_tracked_companies()
    if args.portfolio:
        return [c["ticker"] for c in companies if c["list_type"] == "portfolio"]
    if args.watchlist:
        return [c["ticker"] for c in companies if c["list_type"] == "watchlist"]
    if args.evaluation:
        return [c["ticker"] for c in companies if c["list_type"] == "evaluation"]
    # --all: active universe (db.ACTIVE_LIST_TYPES)
    return [c["ticker"] for c in companies if c["list_type"] in db.ACTIVE_LIST_TYPES]


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch earnings calendar from FMP")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", help="Specific ticker symbol")
    group.add_argument("--all", action="store_true", help="Active universe (db.ACTIVE_LIST_TYPES)")
    group.add_argument("--portfolio", action="store_true", help="Portfolio only")
    group.add_argument("--watchlist", action="store_true", help="Watchlist only")
    group.add_argument("--evaluation", action="store_true", help="Evaluation only")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"Max events per ticker (default {DEFAULT_LIMIT})")
    args = parser.parse_args()

    if not FMP_API_KEY:
        print("Error: FMP_API_KEY not set in .env", file=sys.stderr)
        sys.exit(1)

    tickers = select_tickers(args)
    if not tickers:
        print("No tickers selected.")
        sys.exit(0)

    print(f"Fetching FMP earnings calendar for {len(tickers)} ticker(s)")
    failures = [t for t in tickers if not process_ticker(t, args.limit)]
    print(f"\nDone. {len(tickers) - len(failures)} ok, {len(failures)} failed.")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
