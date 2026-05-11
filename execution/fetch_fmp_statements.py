"""
execution/fetch_fmp_statements.py
----------------------------------
Fetches FMP financial statements for a list of tickers and saves them to
data/historical/fmp/.

Fetches:
  income-statement   (annual + quarterly)
  balance-sheet-statement (annual + quarterly)
  cash-flow-statement (annual + quarterly)
  key-metrics         (annual + quarterly)

Usage:
  python execution/fetch_fmp_statements.py [--tickers CNQ WY ...] [--force]

Options:
  --tickers  Whitespace-separated ticker list (default: all 16 watchlist additions)
  --force    Re-fetch and overwrite even if file already exists

Outputs: data/historical/fmp/{TICKER}_{statement}_{period}.json
Logs:    structured JSON events to stderr
Exit codes: 0 = all succeeded, 1 = one or more fetch errors
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import NamedTuple

import requests
from dotenv import load_dotenv

_APIKEY_RE = re.compile(r"(apikey=)[^&\s]+", re.IGNORECASE)


def _redact(text: object) -> str:
    """Mask FMP apikey value in a string before logging (URLs in error messages)."""
    return _APIKEY_RE.sub(r"\1***", str(text))

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent
FMP_DIR = PROJECT_ROOT / "data" / "historical" / "fmp"
ENV_PATH = PROJECT_ROOT / ".env"

load_dotenv(ENV_PATH)
FMP_API_KEY = os.environ.get("FMP_API_KEY", "")

BASE_URL = "https://financialmodelingprep.com/api/v3"

DEFAULT_TICKERS = [
    "CNQ", "WY", "FNV", "RIO", "VALE", "FCX",
    "ASML", "AMAT", "TOL", "SOFI", "HDB", "ABNB",
    "BHP", "LLY", "LMND", "JPM",
]

# (fmp_endpoint, period_param, file_suffix, limit)
STATEMENT_CONFIGS = [
    ("income-statement",        "annual",  "income_statement_annual",        120),
    ("income-statement",        "quarter", "income_statement_quarterly",      80),
    ("balance-sheet-statement", "annual",  "balance_sheet_annual",           120),
    ("balance-sheet-statement", "quarter", "balance_sheet_quarterly",         80),
    ("cash-flow-statement",     "annual",  "cash_flow_annual",               120),
    ("cash-flow-statement",     "quarter", "cash_flow_quarterly",             80),
    ("key-metrics",             "annual",  "key_metrics_annual",             120),
    ("key-metrics",             "quarter", "key_metrics_quarterly",           80),
]

MAX_WORKERS = 16
RETRY_LIMIT = 3
RETRY_BACKOFF = 2.0  # seconds


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class FetchTask(NamedTuple):
    ticker: str
    endpoint: str
    period: str
    file_suffix: str
    limit: int
    out_path: Path


class FetchResult(NamedTuple):
    task: FetchTask
    ok: bool
    records: int
    error: str | None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(event: str, **kwargs) -> None:
    payload = {"event": event, **kwargs}
    print(json.dumps(payload), file=sys.stderr)


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

def _fetch_with_retry(task: FetchTask) -> FetchResult:
    url = f"{BASE_URL}/{task.endpoint}/{task.ticker}"
    params = {"period": task.period, "limit": task.limit, "apikey": FMP_API_KEY}

    last_err: str = ""
    for attempt in range(1, RETRY_LIMIT + 1):
        try:
            resp = requests.get(url, params=params, timeout=30)
            if resp.status_code in (401, 403):
                _log("auth_error", ticker=task.ticker, endpoint=task.endpoint,
                     status=resp.status_code)
                return FetchResult(task, False, 0, f"HTTP {resp.status_code} auth error")
            if resp.status_code == 429 or resp.status_code >= 500:
                last_err = f"HTTP {resp.status_code}"
                if attempt < RETRY_LIMIT:
                    _log("transient_retry", ticker=task.ticker, endpoint=task.endpoint,
                         status=resp.status_code, attempt=attempt)
                    time.sleep(RETRY_BACKOFF * attempt)
                    continue
                return FetchResult(task, False, 0, last_err)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                return FetchResult(task, False, 0, f"unexpected response type: {type(data)}")
            return FetchResult(task, True, len(data), None)
        except requests.RequestException as exc:
            last_err = _redact(exc)
            if attempt < RETRY_LIMIT:
                _log("network_retry", ticker=task.ticker, endpoint=task.endpoint,
                     attempt=attempt, error=last_err)
                time.sleep(RETRY_BACKOFF * attempt)
            else:
                return FetchResult(task, False, 0, last_err)

    return FetchResult(task, False, 0, last_err)


def _fetch_and_save(task: FetchTask) -> FetchResult:
    url = f"{BASE_URL}/{task.endpoint}/{task.ticker}"
    params = {"period": task.period, "limit": task.limit, "apikey": FMP_API_KEY}

    last_err: str = ""
    for attempt in range(1, RETRY_LIMIT + 1):
        try:
            resp = requests.get(url, params=params, timeout=30)
            if resp.status_code in (401, 403):
                _log("auth_error", ticker=task.ticker, endpoint=task.endpoint,
                     status=resp.status_code)
                return FetchResult(task, False, 0, f"HTTP {resp.status_code} auth error")
            if resp.status_code == 429 or resp.status_code >= 500:
                last_err = f"HTTP {resp.status_code}"
                if attempt < RETRY_LIMIT:
                    _log("transient_retry", ticker=task.ticker, endpoint=task.endpoint,
                         status=resp.status_code, attempt=attempt)
                    time.sleep(RETRY_BACKOFF * attempt)
                    continue
                return FetchResult(task, False, 0, last_err)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                return FetchResult(task, False, 0, f"unexpected response type: {type(data)}")
            task.out_path.parent.mkdir(parents=True, exist_ok=True)
            task.out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return FetchResult(task, True, len(data), None)
        except requests.RequestException as exc:
            last_err = _redact(exc)
            if attempt < RETRY_LIMIT:
                _log("network_retry", ticker=task.ticker, endpoint=task.endpoint,
                     attempt=attempt, error=last_err)
                time.sleep(RETRY_BACKOFF * attempt)
            else:
                return FetchResult(task, False, 0, last_err)

    return FetchResult(task, False, 0, last_err)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_tasks(tickers: list[str], force: bool) -> list[FetchTask]:
    tasks: list[FetchTask] = []
    for ticker in tickers:
        for endpoint, period, suffix, limit in STATEMENT_CONFIGS:
            out_path = FMP_DIR / f"{ticker}_{suffix}.json"
            if out_path.exists() and not force:
                _log("skip_existing", ticker=ticker, file=out_path.name)
                continue
            tasks.append(FetchTask(
                ticker=ticker,
                endpoint=endpoint,
                period=period,
                file_suffix=suffix,
                limit=limit,
                out_path=out_path,
            ))
    return tasks


def run(tickers: list[str], force: bool) -> int:
    if not FMP_API_KEY:
        _log("error", message="FMP_API_KEY not set in environment")
        return 1

    FMP_DIR.mkdir(parents=True, exist_ok=True)
    tasks = build_tasks(tickers, force)

    if not tasks:
        _log("nothing_to_do", message="all files already exist; use --force to re-fetch")
        return 0

    _log("start", total_tasks=len(tasks), tickers=tickers)
    errors: list[FetchResult] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_and_save, t): t for t in tasks}
        for future in as_completed(futures):
            result = future.result()
            if result.ok:
                _log("saved", ticker=result.task.ticker,
                     file=result.task.out_path.name, records=result.records)
            else:
                _log("error", ticker=result.task.ticker,
                     endpoint=result.task.endpoint,
                     period=result.task.period,
                     error=result.error)
                errors.append(result)

    _log("done", total=len(tasks), errors=len(errors),
         succeeded=len(tasks) - len(errors))

    if errors:
        _log("failed_tasks",
             tasks=[f"{r.task.ticker}/{r.task.file_suffix}" for r in errors])
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch FMP financial statements")
    parser.add_argument("--tickers", nargs="+", default=DEFAULT_TICKERS,
                        help="Ticker symbols to fetch")
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch even if file already exists")
    args = parser.parse_args()

    tickers = [t.upper() for t in args.tickers]
    sys.exit(run(tickers, args.force))


if __name__ == "__main__":
    main()
