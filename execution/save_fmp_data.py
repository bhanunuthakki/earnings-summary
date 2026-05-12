"""
execution/save_fmp_data.py
--------------------------
Downloads FMP financial data for tracked tickers, writing JSON files to
data/historical/fmp/ and recording per-endpoint outcomes in fmp_endpoint_status.

Try-ladder for each logical endpoint: FMP /stable, then /api/v3 path-style,
then /api/v4 query-style. First success wins; failures recorded.

Usage:
    python execution/save_fmp_data.py --probe                    # GOOGL only, full endpoint set
    python execution/save_fmp_data.py --tickers GOOGL,META       # specific tickers
    python execution/save_fmp_data.py --portfolio                # all tracked_companies portfolio
    python execution/save_fmp_data.py --watchlist                # all tracked_companies watchlist
    python execution/save_fmp_data.py --sector-industry          # one-time sector/industry pulls
    python execution/save_fmp_data.py --all                      # portfolio + watchlist + sector
    python execution/save_fmp_data.py --skip-existing            # skip endpoints already 'ok' in DB
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, date
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
import db as portfolio_db  # noqa: E402
from log_redact import redact as _redact  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")
API_KEY = os.environ.get("FMP_API_KEY")
if not API_KEY:
    print("FATAL: FMP_API_KEY not set in .env", file=sys.stderr)
    sys.exit(1)

FMP_DIR = PROJECT_ROOT / "data" / "historical" / "fmp"
SNAP_DIR = PROJECT_ROOT / "data" / "historical" / "fmp_snapshots"
SECTOR_DIR = PROJECT_ROOT / "data" / "historical" / "sector_industry"
FMP_DIR.mkdir(parents=True, exist_ok=True)
SNAP_DIR.mkdir(parents=True, exist_ok=True)
SECTOR_DIR.mkdir(parents=True, exist_ok=True)

# Endpoints whose values change over time (forward consensus, current ratings,
# DCF, TTM aggregates, market-quote-driven). Each pull is snapshotted to
# fmp_snapshots/<YYYY-MM-DD>/<TICKER>_<suffix>.json so history is preserved.
TIME_SENSITIVE_ENDPOINTS: set[str] = {
    "analyst-estimates",
    "price-target-consensus",
    "price-target-summary",
    "grades-consensus",
    "grades-historical",
    "ratings-snapshot",
    "historical-rating",
    "discounted-cash-flow",
    "levered-discounted-cash-flow",
    "key-metrics-ttm",
    "ratios-ttm",
    "financial-scores",
    "profile",
    "shares-float",
    "historical-market-capitalization",
}

# Snapshot cadence by list_type. Anything not present here defaults to monthly.
# Active-universe names (portfolio/watchlist/evaluation) get daily history;
# index members are sampled monthly to keep snapshot drift bounded across
# thousands of tickers.
_SNAPSHOT_CADENCE_DAYS: dict[str, int] = {
    "portfolio":    1,
    "watchlist":    1,
    "evaluation":   1,
    "none":         1,
    "index_member": 30,
    "etf":          30,
}


def _load_snapshot_index() -> dict[str, str]:
    """Walk SNAP_DIR once; return {<TICKER>_<suffix>: latest_YYYY-MM-DD}."""
    index: dict[str, str] = {}
    if not SNAP_DIR.exists():
        return index
    for date_dir in SNAP_DIR.iterdir():
        if not date_dir.is_dir():
            continue
        date_str = date_dir.name
        if len(date_str) != 10 or date_str[4] != "-":
            continue
        for f in date_dir.iterdir():
            if not f.name.endswith(".json"):
                continue
            key = f.name[:-5]
            prev = index.get(key)
            if prev is None or date_str > prev:
                index[key] = date_str
    return index


def _should_snapshot(
    ticker: str,
    suffix: str,
    list_type: str,
    today: date,
    snapshot_index: dict[str, str],
    *,
    force: bool,
) -> bool:
    """True if (ticker, suffix) is overdue for a snapshot under list_type's cadence."""
    if force:
        return True
    cadence_days = _SNAPSHOT_CADENCE_DAYS.get(list_type, 30)
    last_str = snapshot_index.get(f"{ticker}_{suffix}")
    if last_str is None:
        return True
    try:
        last_date = date.fromisoformat(last_str)
    except ValueError:
        return True
    return (today - last_date).days >= cadence_days

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "earnings-summary/1.0"

# Live HTTP-attempt counter. The cacher reads this after a run via the budget
# file (`.tmp/cacher/budget_<YYYY-MM-DD>.json`) so the next invocation knows
# how much daily quota is left. Counts every attempt (including 4xx) — that
# matches FMP's billing model.
_CALL_COUNTER = 0
_BUDGET_DIR = PROJECT_ROOT / ".tmp" / "cacher"

# FMP starter-tier limit is 750 requests/minute. We size the token bucket at
# 12 tokens/sec (=720/min steady state) with a 12-token burst. That leaves
# headroom for retry bursts after a 429 backoff without ever brushing the cap.
# Override at runtime via FMP_RATE_LIMIT_PER_SEC env var.
RATE_LIMIT_PER_SEC = float(os.environ.get("FMP_RATE_LIMIT_PER_SEC", "12"))
RATE_LIMIT_BURST = max(1, int(RATE_LIMIT_PER_SEC))
TIMEOUT = (10, 60)


class TokenBucket:
    """Process-local token bucket; thread-safe enough for our single-thread fetcher."""

    def __init__(self, rate_per_sec: float, burst: int) -> None:
        self.rate = rate_per_sec
        self.capacity = float(burst)
        self.tokens = float(burst)
        self.last = time.monotonic()

    def acquire(self) -> None:
        """Block until one token is available, then consume it."""
        while True:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return
            time.sleep(max(0.005, (1.0 - self.tokens) / self.rate))


_BUCKET = TokenBucket(RATE_LIMIT_PER_SEC, RATE_LIMIT_BURST)

TODAY = date.today()
TEN_YEARS_AGO = (TODAY - timedelta(days=365 * 10 + 3)).isoformat()
TODAY_STR = TODAY.isoformat()


# ---------------------------------------------------------------------------
# HTTP try-ladder
# ---------------------------------------------------------------------------

def _http_get(url: str, params: dict) -> tuple[int, object | None, str | None]:
    full = {**params, "apikey": API_KEY}
    # 429 backoff: up to 3 retries, exponential
    global _CALL_COUNTER
    for attempt in range(3):
        _BUCKET.acquire()
        _CALL_COUNTER += 1
        try:
            r = SESSION.get(url, params=full, timeout=TIMEOUT)
        except requests.RequestException as e:
            return (0, None, f"network: {_redact(e)}")
        if r.status_code == 429:
            wait = 5 * (2 ** attempt)
            print(f"  [429 rate-limit, sleeping {wait}s]", flush=True)
            time.sleep(wait)
            continue
        break
    code = r.status_code
    try:
        body = r.json()
    except ValueError:
        return (code, None, f"non-json: {r.text[:200]}")
    if isinstance(body, dict) and "Error Message" in body:
        return (code, None, f"fmp-err: {body['Error Message'][:200]}")
    return (code, body, None)


# Known alternate spellings — when one variant 403s, try these next
PATH_ALIASES: dict[str, list[str]] = {
    "cashflow-statement": ["cash-flow-statement"],
    "cashflow-statement-growth": ["cash-flow-statement-growth"],
    "financial-statement-growth": ["financial-growth"],
    "income-statements-ttm": ["income-statement-ttm"],
    "balance-sheet-statements-ttm": ["balance-sheet-statement-ttm"],
    "cashflow-statements-ttm": ["cash-flow-statement-ttm", "cashflow-statement-ttm"],
    "revenue-geographic-segments": ["revenue-geographic-segmentation"],
    "as-reported-income-statements": ["income-statement-as-reported"],
    "as-reported-balance-statements": ["balance-sheet-statement-as-reported"],
    "as-reported-cashflow-statements": ["cash-flow-statement-as-reported"],
    "as-reported-financial-statements": ["financial-statement-full-as-reported"],
    "financial-reports-form-10-k-json": ["financial-reports-json"],
    "historical-rating": ["ratings-historical", "historical-ratings"],
    "grades-consensus": ["grades-summary"],
    "grades-historical": ["historical-grades", "grades"],
    "stock-peers": ["peers"],
    "key-executives": ["company-executives", "executives"],
    "ratios": ["financial-ratios"],
    "ratios-ttm": ["financial-ratios-ttm"],
    "discounted-cash-flow": ["discounted-cashflow", "dcf-advanced"],
    "levered-discounted-cash-flow": ["levered-dcf", "dcf-levered"],
    "historical-price-eod/dividend-adjusted": [
        "historical-price-eod/full",
        "historical-price-full",
    ],
    "company-symbols-list": ["financial-statement-symbol-list", "stock/list"],
    "sector-pe-snapshot": ["sector-pe"],
    "industry-pe-snapshot": ["industry-pe"],
    "sector-performance-snapshot": ["sectors-performance"],
    "industry-performance-snapshot": ["industries-performance"],
}


def _candidates(endpoint_path: str, symbol: str | None, extra: dict) -> list[tuple[str, str, dict]]:
    paths = [endpoint_path] + PATH_ALIASES.get(endpoint_path, [])
    out: list[tuple[str, str, dict]] = []
    for p in paths:
        if symbol is not None:
            out.append((f"stable:{p}",
                        f"https://financialmodelingprep.com/stable/{p}",
                        {"symbol": symbol, **extra}))
            out.append((f"v3-path:{p}",
                        f"https://financialmodelingprep.com/api/v3/{p}/{symbol}",
                        {**extra}))
            out.append((f"v4-query:{p}",
                        f"https://financialmodelingprep.com/api/v4/{p}",
                        {"symbol": symbol, **extra}))
        else:
            out.append((f"stable:{p}",
                        f"https://financialmodelingprep.com/stable/{p}",
                        {**extra}))
            out.append((f"v3-path:{p}",
                        f"https://financialmodelingprep.com/api/v3/{p}",
                        {**extra}))
            out.append((f"v4-query:{p}",
                        f"https://financialmodelingprep.com/api/v4/{p}",
                        {**extra}))
    return out


def fmp_call(
    endpoint_path: str,
    symbol: str | None = None,
    extra: dict | None = None,
) -> tuple[int, object | None, str | None, str | None]:
    """Try all URL variants. First 200 with non-empty body wins.
    On only-403 results, returns 403 (true tier restriction).
    Returns (http_code, body_or_None, error_or_None, url_kind_used).
    """
    extra = extra or {}
    last_code = 0
    last_err = "no-attempt"
    saw_403 = False
    for kind, url, params in _candidates(endpoint_path, symbol, extra):
        code, body, err = _http_get(url, params)
        last_code, last_err = code, err
        if code == 200 and body is not None:
            if isinstance(body, list) and len(body) == 0:
                last_err = "empty-list"
                continue
            return (code, body, None, kind)
        if code == 403:
            saw_403 = True
        if code in (401, 402):
            return (code, None, err or f"http-{code}", kind)
    if saw_403:
        return (403, None, last_err or "tier-restricted", None)
    return (last_code, None, last_err, None)


# ---------------------------------------------------------------------------
# Endpoint catalog
# ---------------------------------------------------------------------------

def per_ticker_jobs(symbol: str, *, list_type: str = "portfolio") -> list[dict]:
    """Build the endpoint job list for a ticker.

    list_type controls scope:
      - portfolio / watchlist / none / TEMPLATE -> full 67-endpoint set
      - index_member / etf -> skips the 10 financial-reports-form-10-k-json calls
        (saves ~1,700 calls across ~170 index members)
    """
    s = symbol.upper()
    skip_10k = list_type in ("index_member", "etf")
    jobs: list[dict] = []

    def add(path: str, period: str, suffix: str, extra: dict | None = None,
            file_override: str | None = None):
        jobs.append({
            "path": path,
            "symbol": s,
            "period": period,
            "suffix": suffix,
            "extra": extra or {},
            "file_override": file_override,
        })

    # Standard statements
    for base, label in [
        ("income-statement", "income_statement"),
        ("balance-sheet-statement", "balance_sheet"),
        ("cashflow-statement", "cash_flow"),
    ]:
        add(base, "annual", f"{label}_annual", {"period": "annual", "limit": 50})
        add(base, "quarter", f"{label}_quarterly", {"period": "quarter", "limit": 100})

    # Growth
    for base, label in [
        ("income-statement-growth", "income_growth"),
        ("balance-sheet-statement-growth", "balance_growth"),
        ("cashflow-statement-growth", "cashflow_growth"),
        ("financial-statement-growth", "financial_growth"),
    ]:
        add(base, "annual", f"{label}_annual", {"period": "annual", "limit": 50})
        add(base, "quarter", f"{label}_quarterly", {"period": "quarter", "limit": 100})

    # As Reported
    for base, label in [
        ("as-reported-income-statements", "as_reported_income"),
        ("as-reported-balance-statements", "as_reported_balance"),
        ("as-reported-cashflow-statements", "as_reported_cashflow"),
        ("as-reported-financial-statements", "as_reported_financial"),
    ]:
        add(base, "annual", f"{label}_annual", {"period": "annual", "limit": 50})
        add(base, "quarter", f"{label}_quarterly", {"period": "quarter", "limit": 100})

    # TTM
    add("income-statements-ttm", "ttm", "income_statement_ttm", {"limit": 100})
    add("balance-sheet-statements-ttm", "ttm", "balance_sheet_ttm", {"limit": 100})
    add("cashflow-statements-ttm", "ttm", "cash_flow_ttm", {"limit": 100})

    # Segmentation
    for path, label in [
        ("revenue-product-segmentation", "product_segments"),
        ("revenue-geographic-segments", "geo_segments"),
    ]:
        add(path, "annual", f"{label}_annual",
            {"period": "annual", "structure": "flat"})
        add(path, "quarter", f"{label}_quarterly",
            {"period": "quarter", "structure": "flat"})

    # Metrics & ratios
    add("key-metrics", "annual", "key_metrics_annual", {"period": "annual", "limit": 50})
    add("key-metrics", "quarter", "key_metrics_quarterly", {"period": "quarter", "limit": 100})
    add("key-metrics-ttm", "ttm", "key_metrics_ttm")
    add("ratios", "annual", "financial_ratios_annual", {"period": "annual", "limit": 50})
    add("ratios", "quarter", "financial_ratios_quarterly", {"period": "quarter", "limit": 100})
    add("ratios-ttm", "ttm", "financial_ratios_ttm")
    add("enterprise-values", "annual", "enterprise_values_annual", {"period": "annual", "limit": 50})
    add("enterprise-values", "quarter", "enterprise_values_quarterly", {"period": "quarter", "limit": 100})
    add("financial-scores", "", "financial_scores")
    add("owner-earnings", "annual", "owner_earnings_annual", {"limit": 50})

    # Reports
    add("financial-reports-dates", "", "financial_reports_dates")
    # 10-K JSON: 10 calls per ticker (one per year) — SKIPPED for index_member/etf
    if not skip_10k:
        for year in range(TODAY.year - 10, TODAY.year):
            add("financial-reports-form-10-k-json", f"FY{year}", f"form_10k_{year}",
                {"year": year, "period": "FY"})

    # Analyst
    add("analyst-estimates", "annual", "analyst_estimates_annual",
        {"period": "annual", "limit": 50})
    add("analyst-estimates", "quarter", "analyst_estimates_quarterly",
        {"period": "quarter", "limit": 100})
    add("historical-rating", "", "historical_ratings", {"limit": 1000})
    add("price-target-consensus", "", "price_target_consensus")
    add("price-target-summary", "", "price_target_summary")
    add("grades-consensus", "", "grades_summary")
    add("grades-historical", "", "historical_grades", {"limit": 1000})
    add("ratings-snapshot", "", "ratings_snapshot")

    # Company
    add("profile", "", "profile")
    add("historical-market-capitalization", "", "historical_market_cap",
        {"from": TEN_YEARS_AGO, "to": TODAY_STR, "limit": 5000})
    add("shares-float", "", "shares_float")
    add("stock-peers", "", "peers")
    add("key-executives", "", "company_executives")
    add("historical-employee-count", "", "historical_employee_count")

    # DCF
    add("discounted-cash-flow", "", "dcf_basic")
    add("levered-discounted-cash-flow", "", "dcf_levered")

    # Chart
    add("historical-price-eod/dividend-adjusted", "10y", "price_chart_10y_div_adj",
        {"from": TEN_YEARS_AGO, "to": TODAY_STR})

    return jobs


# ---------------------------------------------------------------------------
# Per-ticker runner
# ---------------------------------------------------------------------------

_STATUS_UPSERT_SQL = """
    INSERT INTO fmp_endpoint_status
        (ticker, endpoint, period, status, http_code, record_count,
         earliest_date, latest_date, file_path, file_bytes, error_msg,
         last_pulled)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(ticker, endpoint, period) DO UPDATE SET
        status=excluded.status, http_code=excluded.http_code,
        record_count=excluded.record_count,
        earliest_date=excluded.earliest_date, latest_date=excluded.latest_date,
        file_path=excluded.file_path, file_bytes=excluded.file_bytes,
        error_msg=excluded.error_msg, last_pulled=excluded.last_pulled
"""


def _build_status_row(ticker: str, endpoint: str, period: str, *, status: str,
                      http_code: int | None = None, record_count: int | None = None,
                      earliest: str | None = None, latest: str | None = None,
                      file_path: str | None = None, file_bytes: int | None = None,
                      error_msg: str | None = None) -> tuple:
    """Build the 12-tuple for the fmp_endpoint_status upsert."""
    return (ticker, endpoint, period or "", status, http_code, record_count,
            earliest, latest, file_path, file_bytes, error_msg,
            datetime.now().isoformat(timespec="seconds"))


def _flush_status_batch(rows: list[tuple]) -> None:
    """Upsert many fmp_endpoint_status rows in one transaction with retry-on-lock.

    Batched commits cut per-ticker DB time from ~1.1s (57 separate fsyncs) to
    ~50ms (one fsync). The DB is shared with sibling-branch pipelines;
    busy_timeout (set in portfolio_db.get_connection) handles short waits, but
    we additionally retry up to 5 times on persistent lock errors so a
    multi-hour pull doesn't die on a one-off contention spike.
    """
    if not rows:
        return
    last_err: sqlite3.OperationalError | None = None
    for attempt in range(5):
        conn = portfolio_db.get_connection()
        try:
            conn.executemany(_STATUS_UPSERT_SQL, rows)
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            last_err = e
            wait = 2 ** attempt  # 1, 2, 4, 8, 16 seconds
            print(f"  [db-locked, retry {attempt + 1}/5 after {wait}s: {e}]", flush=True)
            time.sleep(wait)
        finally:
            conn.close()
    assert last_err is not None
    raise last_err


def _record_status(ticker: str, endpoint: str, period: str, *, status: str,
                   http_code: int | None = None, record_count: int | None = None,
                   earliest: str | None = None, latest: str | None = None,
                   file_path: str | None = None, file_bytes: int | None = None,
                   error_msg: str | None = None) -> None:
    """Upsert one fmp_endpoint_status row immediately. Used by global/sector path
    where there's no per-ticker batch to accumulate into.
    """
    _flush_status_batch([_build_status_row(
        ticker, endpoint, period, status=status, http_code=http_code,
        record_count=record_count, earliest=earliest, latest=latest,
        file_path=file_path, file_bytes=file_bytes, error_msg=error_msg,
    )])


def _date_bounds(records) -> tuple[str | None, str | None]:
    if not isinstance(records, list) or not records:
        return (None, None)
    dates = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        for k in ("date", "period", "fillingDate", "calendarYear"):
            v = rec.get(k)
            if isinstance(v, (str, int)):
                s = str(v)[:10]
                if s and s[0].isdigit():
                    dates.append(s)
                    break
    if not dates:
        return (None, None)
    return (min(dates), max(dates))


def _list_type_for(ticker: str) -> str:
    conn = portfolio_db.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT list_type FROM tracked_companies WHERE ticker = ?", (ticker,))
    row = cur.fetchone()
    conn.close()
    return row["list_type"] if row else "none"


def run_ticker(
    ticker: str,
    *,
    skip_existing: bool = False,
    snapshot_index: dict[str, str] | None = None,
    force_snapshot: bool = False,
    manifest_filter: set[tuple[str, str]] | None = None,
    max_calls: int | None = None,
) -> dict:
    lt = _list_type_for(ticker)
    print(f"\n=== {ticker} ({lt}) ===", flush=True)
    jobs = per_ticker_jobs(ticker, list_type=lt)
    if manifest_filter is not None:
        jobs = [j for j in jobs if (j["path"], j["period"]) in manifest_filter]
    snapshot_index = snapshot_index if snapshot_index is not None else {}

    summary = {"ok": 0, "empty": 0, "forbidden": 0, "error": 0, "skipped": 0,
               "total": len(jobs)}

    if skip_existing:
        conn = portfolio_db.get_connection()
        cur = conn.cursor()
        cur.execute("""SELECT endpoint, period FROM fmp_endpoint_status
                       WHERE ticker = ? AND status = 'ok'""", (ticker,))
        already_ok = {(r["endpoint"], r["period"]) for r in cur.fetchall()}
        conn.close()
    else:
        already_ok = set()

    # Accumulate fmp_endpoint_status upserts and flush once at end-of-ticker.
    # 57 individual commits cost ~1s/ticker on Windows; one batch commit is ~50ms.
    pending: list[tuple] = []

    for job in jobs:
        endpoint = job["path"]
        period = job["period"]
        suffix = job["suffix"]

        if (endpoint, period) in already_ok:
            summary["skipped"] += 1
            continue

        if max_calls is not None and _CALL_COUNTER >= max_calls:
            summary["skipped"] += 1
            continue

        code, body, err, kind = fmp_call(endpoint, ticker, job["extra"])

        if code == 200 and body is not None:
            file_path = FMP_DIR / f"{ticker}_{suffix}.json"
            file_path.write_text(json.dumps(body, indent=2), encoding="utf-8")

            # For time-sensitive endpoints (forward consensus, ratings, DCF, TTM,
            # market-data-driven), also write a dated snapshot so historical
            # values aren't overwritten on next pull. Cadence is per list_type:
            # daily for portfolio/watchlist, monthly for index_member/etf.
            if endpoint in TIME_SENSITIVE_ENDPOINTS and _should_snapshot(
                ticker, suffix, lt, TODAY, snapshot_index, force=force_snapshot
            ):
                snap_dir = SNAP_DIR / TODAY_STR
                snap_dir.mkdir(parents=True, exist_ok=True)
                (snap_dir / f"{ticker}_{suffix}.json").write_text(
                    json.dumps(body, indent=2), encoding="utf-8")
                snapshot_index[f"{ticker}_{suffix}"] = TODAY_STR

            count = len(body) if isinstance(body, list) else 1
            earliest, latest = _date_bounds(body if isinstance(body, list) else [body])
            pending.append(_build_status_row(
                ticker, endpoint, period, status="ok",
                http_code=code, record_count=count,
                earliest=earliest, latest=latest,
                file_path=str(file_path.relative_to(PROJECT_ROOT)),
                file_bytes=file_path.stat().st_size,
            ))
            summary["ok"] += 1
            print(f"  ok   {endpoint:48s} {period:8s} n={count:<5} "
                  f"{earliest or '?':10s} -> {latest or '?':10s}  [{kind}]")
        elif code in (401, 402, 403):
            pending.append(_build_status_row(
                ticker, endpoint, period, status="forbidden",
                http_code=code, error_msg=err,
            ))
            summary["forbidden"] += 1
            print(f"  403  {endpoint:48s} {period:8s} (tier-restricted)")
        elif err == "empty-list":
            pending.append(_build_status_row(
                ticker, endpoint, period, status="empty",
                http_code=200, record_count=0, error_msg="empty-list",
            ))
            summary["empty"] += 1
            print(f"  empty {endpoint:48s} {period:8s}")
        else:
            pending.append(_build_status_row(
                ticker, endpoint, period, status="error",
                http_code=code, error_msg=err,
            ))
            summary["error"] += 1
            print(f"  err  {endpoint:48s} {period:8s} code={code} {err}")

    _flush_status_batch(pending)

    print(f"  --- {ticker}: ok={summary['ok']} empty={summary['empty']} "
          f"forbidden={summary['forbidden']} error={summary['error']} "
          f"skipped={summary['skipped']} / {summary['total']}", flush=True)
    return summary


# ---------------------------------------------------------------------------
# Sector/industry one-time
# ---------------------------------------------------------------------------

GICS_SECTORS = [
    "Communication Services", "Consumer Cyclical", "Consumer Defensive",
    "Energy", "Financial Services", "Healthcare", "Industrials",
    "Real Estate", "Technology", "Basic Materials", "Utilities",
]


def _save_global(name: str, body) -> Path:
    p = SECTOR_DIR / f"{name}.json"
    p.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return p


def _save_global_status(endpoint: str, key: str, status: str, *, http_code=None,
                        record_count=None, file_path=None, error_msg=None):
    _record_status("__GLOBAL__", endpoint, key, status=status, http_code=http_code,
                   record_count=record_count, file_path=file_path, error_msg=error_msg)


def run_sector_industry(profiles_dir: Path = FMP_DIR) -> None:
    print("\n=== sector + industry one-time ===", flush=True)

    # Snapshots
    for ep in ["sector-pe-snapshot", "industry-pe-snapshot",
               "sector-performance-snapshot", "industry-performance-snapshot"]:
        code, body, err, kind = fmp_call(ep, None,
                                         {"date": TODAY_STR})
        if code == 200 and body is not None:
            p = _save_global(ep.replace("-", "_"), body)
            print(f"  ok   {ep:42s}                  rows={len(body) if isinstance(body, list) else 1}")
            _save_global_status(ep, "snapshot", "ok", http_code=code,
                                record_count=len(body) if isinstance(body, list) else 1,
                                file_path=str(p.relative_to(PROJECT_ROOT)))
        else:
            print(f"  err  {ep:42s} code={code} {err}")
            _save_global_status(ep, "snapshot", "error", http_code=code, error_msg=err)

    # Sector PE & performance histories
    for sector in GICS_SECTORS:
        for ep in ["historical-sector-pe", "historical-sector-performance"]:
            code, body, err, kind = fmp_call(ep, None,
                                             {"sector": sector,
                                              "from": TEN_YEARS_AGO,
                                              "to": TODAY_STR})
            key = f"{sector}".replace(" ", "_")
            if code == 200 and body is not None:
                p = _save_global(f"{ep.replace('-', '_')}_{key}", body)
                n = len(body) if isinstance(body, list) else 1
                print(f"  ok   {ep:42s} {sector:24s} n={n}")
                _save_global_status(ep, sector, "ok", http_code=code,
                                    record_count=n,
                                    file_path=str(p.relative_to(PROJECT_ROOT)))
            else:
                print(f"  err  {ep:42s} {sector:24s} code={code} {err}")
                _save_global_status(ep, sector, "error", http_code=code, error_msg=err)

    # Industry — collect from existing profile.json files
    industries: set[str] = set()
    for f in profiles_dir.glob("*_profile.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            recs = data if isinstance(data, list) else [data]
            for r in recs:
                if isinstance(r, dict) and r.get("industry"):
                    industries.add(r["industry"])
        except Exception:
            continue

    print(f"  -- industries discovered: {sorted(industries)}")

    for industry in sorted(industries):
        for ep in ["historical-industry-pe", "historical-industry-performance"]:
            code, body, err, kind = fmp_call(ep, None,
                                             {"industry": industry,
                                              "from": TEN_YEARS_AGO,
                                              "to": TODAY_STR})
            key = industry.replace(" ", "_").replace("/", "_")
            if code == 200 and body is not None:
                p = _save_global(f"{ep.replace('-', '_')}_{key}", body)
                n = len(body) if isinstance(body, list) else 1
                print(f"  ok   {ep:42s} {industry:32s} n={n}")
                _save_global_status(ep, industry, "ok", http_code=code,
                                    record_count=n,
                                    file_path=str(p.relative_to(PROJECT_ROOT)))
            else:
                print(f"  err  {ep:42s} {industry:32s} code={code} {err}")
                _save_global_status(ep, industry, "error", http_code=code, error_msg=err)

    # Global ticker list
    code, body, err, kind = fmp_call("company-symbols-list", None)
    if code == 200 and body is not None:
        p = _save_global("company_symbols_list", body)
        n = len(body) if isinstance(body, list) else 1
        print(f"  ok   company-symbols-list                                       n={n}")
        _save_global_status("company-symbols-list", "all", "ok", http_code=code,
                            record_count=n, file_path=str(p.relative_to(PROJECT_ROOT)))
    else:
        print(f"  err  company-symbols-list code={code} {err}")
        _save_global_status("company-symbols-list", "all", "error",
                            http_code=code, error_msg=err)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _ticker_list(list_type: str) -> list[str]:
    conn = portfolio_db.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT ticker FROM tracked_companies WHERE list_type = ? ORDER BY ticker",
                (list_type,))
    rows = [r["ticker"] for r in cur.fetchall()]
    conn.close()
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true",
                    help="Run full endpoint set on GOOGL only")
    ap.add_argument("--tickers", help="Comma-separated tickers")
    ap.add_argument("--portfolio", action="store_true")
    ap.add_argument("--watchlist", action="store_true")
    ap.add_argument("--evaluation", action="store_true",
                    help="Pull all tickers with list_type='evaluation'")
    ap.add_argument("--index-members", action="store_true",
                    help="Pull all tickers with list_type='index_member' (skips 10-K JSON)")
    ap.add_argument("--etfs", action="store_true",
                    help="Pull all tickers with list_type='etf' (skips 10-K JSON)")
    ap.add_argument("--sector-industry", action="store_true")
    ap.add_argument("--all", action="store_true",
                    help="Active universe (portfolio + watchlist + evaluation) + sector/industry")
    ap.add_argument("--skip-existing", action="store_true",
                    help="Skip endpoints already 'ok' in DB")
    ap.add_argument("--force-snapshot", action="store_true",
                    help="Snapshot every time-sensitive endpoint regardless of cadence")
    ap.add_argument("--manifest", type=str, default=None,
                    help="Path to JSON manifest of [{ticker, endpoint, period}] tuples; "
                    "filters per_ticker_jobs to only those entries (cacher-driven runs)")
    ap.add_argument("--max-calls", type=int, default=None,
                    help="Soft cap on total HTTP attempts. Run halts mid-ticker when "
                    "the counter exceeds this; remaining work is left for next run")
    args = ap.parse_args()

    targets: list[str] = []
    do_sector = False

    # Manifest mode: cacher-driven precise (ticker, endpoint, period) work.
    # Accepts either a bare list of entries or a wrapped {"items": [...], ...}
    # object (the cacher writes the wrapped form for diagnostics).
    manifest_filter: dict[str, set[tuple[str, str]]] | None = None
    if args.manifest:
        with open(args.manifest, encoding="utf-8") as mf:
            payload = json.load(mf)
        entries = payload["items"] if isinstance(payload, dict) else payload
        manifest_filter = {}
        for e in entries:
            t = e["ticker"].upper()
            manifest_filter.setdefault(t, set()).add(
                (e["endpoint"], e.get("period", ""))
            )
        targets = sorted(manifest_filter.keys())

    if args.probe:
        targets = ["GOOGL"]
    if args.tickers:
        targets += [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    if args.portfolio:
        targets += _ticker_list("portfolio")
    if args.watchlist:
        targets += _ticker_list("watchlist")
    if args.evaluation:
        targets += _ticker_list("evaluation")
    if args.index_members:
        targets += _ticker_list("index_member")
    if args.etfs:
        targets += _ticker_list("etf")
    if args.sector_industry:
        do_sector = True
    if args.all:
        for lt in portfolio_db.ACTIVE_LIST_TYPES:
            targets += _ticker_list(lt)
        targets += _ticker_list("index_member") + _ticker_list("etf")
        do_sector = True

    seen = set()
    targets = [t for t in targets if not (t in seen or seen.add(t))]

    if not targets and not do_sector:
        ap.print_help()
        sys.exit(2)

    snapshot_index = _load_snapshot_index()
    print(f"snapshot index: {len(snapshot_index)} entries", flush=True)

    grand = {"ok": 0, "empty": 0, "forbidden": 0, "error": 0, "skipped": 0, "total": 0}
    for t in targets:
        if args.max_calls is not None and _CALL_COUNTER >= args.max_calls:
            print(f"  [max-calls {args.max_calls} reached; halting before {t}]")
            break
        s = run_ticker(
            t,
            skip_existing=args.skip_existing,
            snapshot_index=snapshot_index,
            force_snapshot=args.force_snapshot,
            manifest_filter=manifest_filter.get(t) if manifest_filter else None,
            max_calls=args.max_calls,
        )
        for k in grand:
            grand[k] += s[k]

    if do_sector:
        run_sector_industry()

    print("\n=== GRAND TOTAL ===")
    print(f"  ok={grand['ok']} empty={grand['empty']} forbidden={grand['forbidden']} "
          f"error={grand['error']} skipped={grand['skipped']} / total={grand['total']}")
    print(f"  tickers processed: {len(targets)}")
    print(f"  http attempts:     {_CALL_COUNTER}")

    # Append today's HTTP attempts to the daily budget ledger so the cacher
    # can read it on the next invocation. Atomic append-or-create with read+
    # rewrite — concurrent writers serialize through portfolio_db's busy_timeout
    # at the consumer layer, but we don't double-write here.
    try:
        _BUDGET_DIR.mkdir(parents=True, exist_ok=True)
        budget_path = _BUDGET_DIR / f"budget_{TODAY_STR}.json"
        existing = {"calls_made": 0, "runs": 0}
        if budget_path.exists():
            existing = json.loads(budget_path.read_text(encoding="utf-8"))
        existing["calls_made"] = existing.get("calls_made", 0) + _CALL_COUNTER
        existing["runs"] = existing.get("runs", 0) + 1
        existing["last_run_at"] = datetime.now().isoformat(timespec="seconds")
        budget_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"  [warn] could not write budget file: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
