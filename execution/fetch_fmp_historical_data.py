"""
execution/fetch_fmp_historical_data.py
--------------------------
Layer 3 execution script to fetch historical financial data (including segments) from FMP.

Responsibilities:
  - Fetch income statements, balance sheets, cash flow, and segment revenue from FMP.
  - Save raw JSON data to data/historical/fmp/<TICKER>_<type>.json.
  - Update the 'fmp_data_upto' column in the tracking database.

Usage:
  python execution/fetch_fmp_historical_data.py --ticker GOOG --limit 8
  python execution/fetch_fmp_historical_data.py --all --limit 20
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

# Paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
DATA_DIR = os.path.join(PROJECT_ROOT, "data", "historical", "fmp")
sys.path.append(SRC_DIR)

import db  # noqa: E402
from log_redact import redact as _redact  # noqa: E402
from runtime.secrets import load_project_env  # noqa: E402

# Load API Key
load_project_env(Path(PROJECT_ROOT))
FMP_API_KEY = os.environ.get("FMP_API_KEY")

FMP_BASE = "https://financialmodelingprep.com/stable"

# --all previously walked EVERY tracked company — including the ~2,350
# index_member peers — at full --limit depth, one of the writers behind the
# 9.1 GB fmp cache (2026-07-30 DB-size audit). Peers are covered by the
# shallow contract in save_fmp_data.PEER_ENDPOINT_ALLOWLIST instead, so
# --all now defaults to the active universe; override with --list-types.
ACTIVE_LIST_TYPES = ("portfolio", "watchlist", "evaluation")

# Retry transient FMP responses (rate-limit / upstream 5xx) with exponential
# backoff instead of silently returning None and skipping the statement. Mirrors
# the backoff in execution/save_fmp_data.py; the sibling fetchers already do this.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_S = 5


def fetch_from_fmp(path: str, params: dict) -> list | dict | None:
    params["apikey"] = FMP_API_KEY
    url = f"{FMP_BASE}/{path}"
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = requests.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            print(f"  [Error] {path}: {_redact(e)}", file=sys.stderr)
            return None
        if resp.status_code in _RETRYABLE_STATUS and attempt < _MAX_ATTEMPTS - 1:
            wait = _BACKOFF_BASE_S * (2**attempt)
            print(
                f"  [HTTP {resp.status_code} retry {attempt + 1}/{_MAX_ATTEMPTS - 1} "
                f"in {wait}s] {path}",
                file=sys.stderr,
            )
            time.sleep(wait)
            continue
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            print(f"  [HTTP {resp.status_code}] {path}: {_redact(e)}", file=sys.stderr)
            return None
        return resp.json()
    return None


def save_data(ticker: str, data_type: str, data: list | dict) -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    filename = f"{ticker.upper()}_{data_type}.json"
    filepath = os.path.join(DATA_DIR, filename)
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)
    return filepath


def process_ticker(ticker: str, limit: int = 20) -> None:
    ticker = ticker.upper()
    print(f"\n--- {ticker} ---")

    # 1. Income Statement (Quarterly)
    income = fetch_from_fmp(
        "income-statement",
        {"symbol": ticker, "period": "quarter", "limit": limit},
    )
    if income:
        p = save_data(ticker, "income_statement", income)
        print(f"  [OK] income_statement  -> {len(income)} quarters -> {p}")

    # 2. Balance Sheet (Quarterly)
    balance = fetch_from_fmp(
        "balance-sheet-statement",
        {"symbol": ticker, "period": "quarter", "limit": limit},
    )
    if balance:
        p = save_data(ticker, "balance_sheet", balance)
        print(f"  [OK] balance_sheet     -> {len(balance)} quarters -> {p}")

    # 3. Cash Flow (Quarterly)
    cash = fetch_from_fmp(
        "cash-flow-statement",
        {"symbol": ticker, "period": "quarter", "limit": limit},
    )
    if cash:
        p = save_data(ticker, "cash_flow", cash)
        print(f"  [OK] cash_flow         -> {len(cash)} quarters -> {p}")

    # 4. Product Revenue Segmentation (v4 still works for segmentation)
    product_seg = fetch_from_fmp(
        "revenue-product-segmentation",
        {"symbol": ticker, "period": "quarter", "limit": limit},
    )
    if product_seg:
        p = save_data(ticker, "product_segments", product_seg)
        print(f"  [OK] product_segments  -> {len(product_seg)} entries -> {p}")
    else:
        print("  [--] product_segments  -> not available (normal for some tickers)")

    # 5. Geographic Revenue Segmentation
    geo_seg = fetch_from_fmp(
        "revenue-geographic-segmentation",
        {"symbol": ticker, "period": "quarter", "limit": limit},
    )
    if geo_seg:
        p = save_data(ticker, "geo_segments", geo_seg)
        print(f"  [OK] geo_segments      -> {len(geo_seg)} entries -> {p}")
    else:
        print("  [--] geo_segments      -> not available (normal for some tickers)")

    # 6. Update Database
    if income and len(income) > 0:
        earliest_period = income[-1].get("date")
        conn = db.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE tracked_companies SET fmp_data_upto = ? WHERE ticker = ?",
            (earliest_period, ticker),
        )
        conn.commit()
        conn.close()
        print(f"  [DB] fmp_data_upto set to {earliest_period}")
    else:
        print("  [--] DB not updated (no income data)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch Historical Financial Data from FMP (stable endpoints)"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", help="Specific ticker (e.g. GOOG)")
    group.add_argument("--all", action="store_true", help="Fetch for all tracked companies")
    parser.add_argument(
        "--limit", type=int, default=20, help="Max quarters per data type (default: 20)"
    )
    parser.add_argument(
        "--list-types",
        default=",".join(ACTIVE_LIST_TYPES),
        help=(
            "Comma-separated tracked_companies.list_type values --all includes "
            f"(default: {','.join(ACTIVE_LIST_TYPES)}). index_member peers are "
            "excluded by default: their depth contract is "
            "save_fmp_data.PEER_ENDPOINT_ALLOWLIST"
        ),
    )
    args = parser.parse_args()

    if not FMP_API_KEY:
        print("Error: FMP_API_KEY not set in .env", file=sys.stderr)
        sys.exit(1)

    tickers: list[str] = []
    if args.all:
        wanted = {t.strip().lower() for t in args.list_types.split(",") if t.strip()}
        companies = db.get_tracked_companies()
        tickers = [
            str(c["ticker"]) for c in companies if str(c.get("list_type") or "").lower() in wanted
        ]
        if not tickers:
            print(f"No tracked companies found for list_types={sorted(wanted)}.")
            sys.exit(0)
    else:
        tickers = [args.ticker.upper()]

    print(f"FMP Historical Backfill - {len(tickers)} ticker(s), up to {args.limit} quarters each")
    for t in tickers:
        process_ticker(t, args.limit)

    print("\nDone.")


if __name__ == "__main__":
    main()
