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

import os
import sys
import argparse
import requests
import json
from dotenv import load_dotenv

# Paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
DATA_DIR = os.path.join(PROJECT_ROOT, "data", "historical", "fmp")
sys.path.append(SRC_DIR)

import db

# Load API Key
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
FMP_API_KEY = os.environ.get("FMP_API_KEY")

FMP_BASE = "https://financialmodelingprep.com/stable"


def fetch_from_fmp(path: str, params: dict) -> list | dict | None:
    params["apikey"] = FMP_API_KEY
    url = f"{FMP_BASE}/{path}"
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.HTTPError as e:
        print(f"  [HTTP {resp.status_code}] {path}: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  [Error] {path}: {e}", file=sys.stderr)
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
        print(f"  [--] product_segments  -> not available (normal for some tickers)")

    # 5. Geographic Revenue Segmentation
    geo_seg = fetch_from_fmp(
        "revenue-geographic-segmentation",
        {"symbol": ticker, "period": "quarter", "limit": limit},
    )
    if geo_seg:
        p = save_data(ticker, "geo_segments", geo_seg)
        print(f"  [OK] geo_segments      -> {len(geo_seg)} entries -> {p}")
    else:
        print(f"  [--] geo_segments      -> not available (normal for some tickers)")

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
        print(f"  [--] DB not updated (no income data)")


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
    args = parser.parse_args()

    if not FMP_API_KEY:
        print("Error: FMP_API_KEY not set in .env", file=sys.stderr)
        sys.exit(1)

    tickers: list[str] = []
    if args.all:
        companies = db.get_tracked_companies()
        tickers = [c["ticker"] for c in companies]
        if not tickers:
            print("No tracked companies found in database.")
            sys.exit(0)
    else:
        tickers = [args.ticker.upper()]

    print(f"FMP Historical Backfill - {len(tickers)} ticker(s), up to {args.limit} quarters each")
    for t in tickers:
        process_ticker(t, args.limit)

    print("\nDone.")


if __name__ == "__main__":
    main()
