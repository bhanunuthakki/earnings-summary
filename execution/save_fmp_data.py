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
from typing import cast

import requests
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
import db as portfolio_db  # noqa: E402
from log_redact import redact as _redact  # noqa: E402
from compute.split_normalization import (  # noqa: E402
    NormalizationEvent,
    normalize_estimates,
)
from models.fmp_payloads import (  # noqa: E402
    FmpAnalystEstimateRecord,
    FmpBalanceSheetRecord,
    FmpCashFlowRecord,
    FmpIncomeStatementRecord,
)
from pipeline.deferred_fmp import (  # noqa: E402
    DeferredFmpTask,
    default_store_path,
    log_deferred,
)
from sources import registry as source_calls_log  # noqa: E402

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

# Log every FMP fetch outcome (and every cache-skip) to the shared source_calls
# provenance table so FMP cache-hit-rate is measurable from real data — the
# dominant external path was previously invisible there (only the live-price
# adapters logged). FMP is a flat-rate subscription, so the budget unit is
# request VOLUME, not per-call dollars: an `ok` row is a request spent, a
# `skipped` row is a request saved by the cache. Point the logger at the same DB
# this fetcher writes to (db.DB_PATH), so prod, tests, and worktree runs agree.
source_calls_log.set_db_path(Path(portfolio_db.DB_PATH))
_FMP_SOURCE = "fmp"

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

# Under --skip-existing we normally skip any endpoint already 'ok'. But the
# TIME_SENSITIVE_ENDPOINTS above carry an as-of-today value (current price,
# market cap, DCF, forward consensus, ratings) that goes stale — skipping them on
# a name whose statements are already cached leaves the brief showing a weeks-old
# price (the "stale profile" trap: a name promoted from index_member keeps its
# last monthly profile snapshot, so §1 + the evaluation "Numbers at a glance"
# panel mis-state price/market-cap/valuation). So those endpoints are re-fetched
# once their cached row ages past this TTL, which matches the daily snapshot
# cadence for active names. Statements (NOT in the set) keep skipping when 'ok' —
# they only change at earnings and re-fetching them burns request volume.
_SKIP_EXISTING_TTL_DAYS = 1


def _snapshot_is_stale(
    last_pulled: str | None,
    *,
    ttl_days: int = _SKIP_EXISTING_TTL_DAYS,
    now: datetime | None = None,
) -> bool:
    """True if a time-sensitive endpoint's cached row is older than ``ttl_days``
    (or has a missing / unparseable ``last_pulled``). Only meaningful for
    endpoints in TIME_SENSITIVE_ENDPOINTS — callers gate on membership first."""
    if not last_pulled:
        return True
    try:
        pulled = datetime.fromisoformat(last_pulled)
    except ValueError:
        return True
    return ((now or datetime.now()) - pulled) >= timedelta(days=ttl_days)


# Snapshot cadence by list_type. Anything not present here defaults to monthly.
# Active-universe names (portfolio/watchlist/evaluation) get daily history;
# index members are sampled monthly to keep snapshot drift bounded across
# thousands of tickers.
_SNAPSHOT_CADENCE_DAYS: dict[str, int] = {
    "portfolio": 1,
    "watchlist": 1,
    "evaluation": 1,
    "none": 1,
    "index_member": 30,
    "etf": 30,
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
            wait = 5 * (2**attempt)
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


# Tier gate. On free FMP every /api/v3 and /api/v4 call 403s — the v3
# deprecation is global as of 2025-08-31, not just this account — so emitting
# those fallback rungs only burns one of the 250 daily requests apiece. When
# FMP_TIER=free (or the --stable-only CLI flag), the try-ladder drops to the
# stable rung + stable PATH_ALIASES only. Any other tier keeps the v3/v4
# fallbacks (legacy/paid accounts where v3 still resolves). The cutover is a
# deliberate flip of FMP_TIER, never auto-detected.
_stable_only = os.environ.get("FMP_TIER", "").strip().lower() == "free"


def _enable_stable_only() -> None:
    """Force the stable-only ladder (drop the v3/v4 rungs) for this process,
    regardless of FMP_TIER. Used by the --stable-only and --probe-stable CLI
    flags so a manual run can exercise the free-tier ladder on demand."""
    global _stable_only
    _stable_only = True


# Known alternate spellings — when one variant 403s, try these next. These are
# stable-to-stable aliases (alternate stable spellings), NOT v3, so they survive
# the _stable_only gate.
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


def _candidates(
    endpoint_path: str, symbol: str | None, extra: dict[str, object]
) -> list[tuple[str, str, dict[str, object]]]:
    paths = [endpoint_path] + PATH_ALIASES.get(endpoint_path, [])
    out: list[tuple[str, str, dict[str, object]]] = []
    for p in paths:
        if symbol is not None:
            out.append(
                (
                    f"stable:{p}",
                    f"https://financialmodelingprep.com/stable/{p}",
                    {"symbol": symbol, **extra},
                )
            )
            if not _stable_only:
                out.append(
                    (
                        f"v3-path:{p}",
                        f"https://financialmodelingprep.com/api/v3/{p}/{symbol}",
                        {**extra},
                    )
                )
                out.append(
                    (
                        f"v4-query:{p}",
                        f"https://financialmodelingprep.com/api/v4/{p}",
                        {"symbol": symbol, **extra},
                    )
                )
        else:
            out.append((f"stable:{p}", f"https://financialmodelingprep.com/stable/{p}", {**extra}))
            if not _stable_only:
                out.append(
                    (f"v3-path:{p}", f"https://financialmodelingprep.com/api/v3/{p}", {**extra})
                )
                out.append(
                    (f"v4-query:{p}", f"https://financialmodelingprep.com/api/v4/{p}", {**extra})
                )
    return out


def fmp_call(
    endpoint_path: str,
    symbol: str | None = None,
    extra: dict | None = None,
) -> tuple[int, object | None, str | None, str | None]:
    """Try all URL variants. First 200 with non-empty body wins.
    A 200 with an empty array stops the ladder and returns (200, None,
    "empty-list", kind) — the endpoint is accessible, FMP just has no rows
    yet. On only-403 results, returns 403 (true tier restriction).
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
                # Accessible endpoint, but FMP has no rows yet — the common
                # case for freshly-IPO'd tickers (e.g. /stable/income-statement
                # returns 200 []). This is NOT a tier restriction, so stop the
                # ladder and report it as empty. Falling through to the v3/v4
                # fallbacks would 403 and mis-record the endpoint as forbidden
                # (and burn 2 extra calls per empty endpoint).
                return (200, None, "empty-list", kind)
            return (code, body, None, kind)
        if code == 403:
            saw_403 = True
        if code in (401, 402):
            return (code, None, err or f"http-{code}", kind)
    if saw_403:
        return (403, None, last_err or "tier-restricted", None)
    return (last_code, None, last_err, None)


# ---------------------------------------------------------------------------
# Pre-write validation gate (stable rungs only)
# ---------------------------------------------------------------------------
#
# Ported from a now-retired v3-only statements fetcher: Pydantic-validate
# the first record of a statement response *before* persisting it, so a drifted
# stable envelope can't silently overwrite good cached JSON and break the
# downstream compute/* extractors. On drift we dump the raw body to .tmp/ and
# skip the write (recording the endpoint as an error) rather than caching a
# malformed envelope.

_VALIDATION_DUMP_DIR = PROJECT_ROOT / ".tmp" / "fmp_validation_failures"

# Catalog endpoint path -> stable-shaped Pydantic model, keyed by the catalog
# `path` (job["path"]). The cash-flow catalog path is the stable spelling
# "cashflow-statement" (PATH_ALIASES maps it to the v3-ish "cash-flow-statement").
# Endpoints absent from this map are not gated.
_STABLE_VALIDATORS: dict[str, type[BaseModel]] = {
    "income-statement": FmpIncomeStatementRecord,
    "balance-sheet-statement": FmpBalanceSheetRecord,
    "cashflow-statement": FmpCashFlowRecord,
    "analyst-estimates": FmpAnalystEstimateRecord,
}


def _validate_stable_record(
    endpoint: str, kind: str | None, body: object
) -> ValidationError | None:
    """Validate body[0] against the stable-shaped model for `endpoint`.

    Returns the ValidationError on schema drift, else None. Validation applies
    ONLY when the winning rung is a `stable:` rung — the v3/v4 fallback rungs
    return v3-shaped bodies (calendarYear, dividendsPaid, ...) that the
    stable-shaped model would (correctly) reject; that is legacy shape, NOT
    drift, so non-stable rungs are passed through unchecked. An empty body is a
    200-OK no-data signal (freshly-IPO'd ticker) and passes; endpoints with no
    model pass.
    """
    if kind is None or not kind.startswith("stable:"):
        return None
    model = _STABLE_VALIDATORS.get(endpoint)
    if model is None:
        return None
    if not isinstance(body, list):
        return None
    records = cast("list[object]", body)
    if not records:
        return None
    try:
        model.model_validate(records[0])
    except ValidationError as exc:
        return exc
    return None


def _dump_validation_failure(
    ticker: str,
    endpoint: str,
    period: str,
    suffix: str,
    body: object,
    err: ValidationError,
) -> Path:
    """Write the malformed stable response + error breakdown to .tmp/ for
    inspection. Returns the dump path so the status row can cite it — the dump
    makes a schema-drift fix a one-liner.
    """
    _VALIDATION_DUMP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    out = _VALIDATION_DUMP_DIR / f"{ticker}_{suffix}_{stamp}.json"
    payload = {
        "ticker": ticker,
        "endpoint": endpoint,
        "period": period,
        "validation_errors": [
            {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in err.errors()
        ],
        "raw_response": body,
    }
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return out


# The catalog `path` for the analyst-consensus endpoint. Only this endpoint
# carries the per-share (EPS) series that FMP splices across two split bases, so
# split-normalization applies to it alone.
_ANALYST_ESTIMATES_ENDPOINT = "analyst-estimates"


def _load_income_rows(ticker: str) -> list[dict[str, object]]:
    """Read the ticker's cached FMP income statement (annual). FMP delivers this
    fully split-adjusted onto the CURRENT share basis, so it is the authority the
    estimates normalizer reconciles the per-share series against. Returns [] when
    it isn't cached yet (the normalizer then quarantines any spliced series
    rather than guessing a basis)."""
    path = FMP_DIR / f"{ticker}_income_statement_annual.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    records = cast("list[object]", raw)
    return [cast("dict[str, object]", r) for r in records if isinstance(r, dict)]


def _normalize_estimates_body(
    ticker: str,
    body: object,
) -> tuple[object, str | None, list[NormalizationEvent]]:
    """Force the per-share series of an analyst-estimates payload onto ONE
    current split basis before it is cached (fixes the BKNG-class contamination
    where pre-split historical actuals are spliced with post-split forwards, and
    repairs the grossly-corrupted historical `netIncomeAvg`).

    Returns ``(body_to_write, quarantine_reason, events)``. When
    ``quarantine_reason`` is non-None the caller dumps raw + skips the write (a
    spliced series with no income-statement authority to reconcile). A non-list
    body (empty / freshly-IPO'd) passes through untouched."""
    if not isinstance(body, list):
        return (body, None, [])
    records = cast("list[object]", body)
    rows = [cast("dict[str, object]", r) for r in records if isinstance(r, dict)]
    result = normalize_estimates(rows, _load_income_rows(ticker))
    no_events: list[NormalizationEvent] = []
    if result.quarantined:
        # `records` (the typed view of `body`) is dumped raw + skipped by the caller.
        return (records, result.quarantine_reason, no_events)
    if not result.changed:
        return (records, None, no_events)
    return (result.rows, None, result.events)


def _dump_quarantine(
    ticker: str,
    endpoint: str,
    period: str,
    suffix: str,
    body: object,
    reason: str,
) -> Path:
    """Write a quarantined (un-reconcilable) estimates payload + the reason to
    .tmp/ for inspection, per the schema-drift rule. Mirrors
    `_dump_validation_failure` but for a normalization quarantine (no
    ValidationError object). Returns the dump path so the status row can cite it.
    """
    _VALIDATION_DUMP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    out = _VALIDATION_DUMP_DIR / f"{ticker}_{suffix}_quarantine_{stamp}.json"
    payload = {
        "ticker": ticker,
        "endpoint": endpoint,
        "period": period,
        "quarantine_reason": reason,
        "raw_response": body,
    }
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return out


def _log_deferred_split_quarantine(
    ticker: str,
    suffix: str,
    reason: str,
    dump_rel: Path,
) -> None:
    """Auto-populate the deferred-FMP backlog when a per-share series is
    quarantined: the fix (back-adjust with the authoritative splits feed, or
    re-pull clean consensus) is blocked on FMP access, so record it instead of
    relying on memory. Best-effort: a backlog write failure must not break the
    fetch, so it degrades to a stderr note."""
    try:
        log_deferred(
            DeferredFmpTask(
                area="split_normalization",
                task=f"Reconcile quarantined analyst-estimates ({suffix}) for {ticker}",
                blocked_on="fmp_splits_feed",
                ticker=ticker,
                context=(
                    f"{reason}. Raw dumped to {dump_rel}. Needs the authoritative "
                    "FMP splits feed (or a clean consensus re-pull) to back-adjust."
                ),
            ),
            default_store_path(PROJECT_ROOT),
        )
    except (OSError, ValueError) as exc:
        print(f"  warn: deferred-log write failed for {ticker}: {exc}", file=sys.stderr)


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

    def add(
        path: str,
        period: str,
        suffix: str,
        extra: dict | None = None,
        file_override: str | None = None,
    ):
        jobs.append(
            {
                "path": path,
                "symbol": s,
                "period": period,
                "suffix": suffix,
                "extra": extra or {},
                "file_override": file_override,
            }
        )

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

    # TTM. NOTE: the 2026-06-01 stable probe (run_stable_probe on GOOGL) found
    # these three TTM *statement* endpoints return 403 "Restricted Endpoint ...
    # not available under your current subscription" on /stable — including via
    # their stable aliases. This is a subscription-tier restriction, not a
    # v3-vs-stable gap (v3/v4 can't recover them either), so dropping the v3/v4
    # rungs on free loses nothing. They stay in the catalog (a higher tier may
    # serve them); the cacher records them `forbidden` and retries every 30d.
    # key-metrics-ttm and ratios-ttm (below) DO resolve on stable.
    add("income-statements-ttm", "ttm", "income_statement_ttm", {"limit": 100})
    add("balance-sheet-statements-ttm", "ttm", "balance_sheet_ttm", {"limit": 100})
    add("cashflow-statements-ttm", "ttm", "cash_flow_ttm", {"limit": 100})

    # Segmentation
    for path, label in [
        ("revenue-product-segmentation", "product_segments"),
        ("revenue-geographic-segments", "geo_segments"),
    ]:
        add(path, "annual", f"{label}_annual", {"period": "annual", "structure": "flat"})
        add(path, "quarter", f"{label}_quarterly", {"period": "quarter", "structure": "flat"})

    # Metrics & ratios
    add("key-metrics", "annual", "key_metrics_annual", {"period": "annual", "limit": 50})
    add("key-metrics", "quarter", "key_metrics_quarterly", {"period": "quarter", "limit": 100})
    add("key-metrics-ttm", "ttm", "key_metrics_ttm")
    add("ratios", "annual", "financial_ratios_annual", {"period": "annual", "limit": 50})
    add("ratios", "quarter", "financial_ratios_quarterly", {"period": "quarter", "limit": 100})
    add("ratios-ttm", "ttm", "financial_ratios_ttm")
    add(
        "enterprise-values", "annual", "enterprise_values_annual", {"period": "annual", "limit": 50}
    )
    add(
        "enterprise-values",
        "quarter",
        "enterprise_values_quarterly",
        {"period": "quarter", "limit": 100},
    )
    add("financial-scores", "", "financial_scores")
    add("owner-earnings", "annual", "owner_earnings_annual", {"limit": 50})

    # Reports
    add("financial-reports-dates", "", "financial_reports_dates")
    # 10-K JSON: 10 calls per ticker (one per year) — SKIPPED for index_member/etf
    if not skip_10k:
        for year in range(TODAY.year - 10, TODAY.year):
            add(
                "financial-reports-form-10-k-json",
                f"FY{year}",
                f"form_10k_{year}",
                {"year": year, "period": "FY"},
            )

    # Analyst
    add(
        "analyst-estimates", "annual", "analyst_estimates_annual", {"period": "annual", "limit": 50}
    )
    add(
        "analyst-estimates",
        "quarter",
        "analyst_estimates_quarterly",
        {"period": "quarter", "limit": 100},
    )
    add("historical-rating", "", "historical_ratings", {"limit": 1000})
    add("price-target-consensus", "", "price_target_consensus")
    add("price-target-summary", "", "price_target_summary")
    add("grades-consensus", "", "grades_summary")
    add("grades-historical", "", "historical_grades", {"limit": 1000})
    add("ratings-snapshot", "", "ratings_snapshot")

    # Company
    add("profile", "", "profile")
    add(
        "historical-market-capitalization",
        "",
        "historical_market_cap",
        {"from": TEN_YEARS_AGO, "to": TODAY_STR, "limit": 5000},
    )
    add("shares-float", "", "shares_float")
    add("stock-peers", "", "peers")
    add("key-executives", "", "company_executives")
    add("historical-employee-count", "", "historical_employee_count")

    # DCF
    add("discounted-cash-flow", "", "dcf_basic")
    add("levered-discounted-cash-flow", "", "dcf_levered")

    # Chart
    add(
        "historical-price-eod/dividend-adjusted",
        "10y",
        "price_chart_10y_div_adj",
        {"from": TEN_YEARS_AGO, "to": TODAY_STR},
    )

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


def _build_status_row(
    ticker: str,
    endpoint: str,
    period: str,
    *,
    status: str,
    http_code: int | None = None,
    record_count: int | None = None,
    earliest: str | None = None,
    latest: str | None = None,
    file_path: str | None = None,
    file_bytes: int | None = None,
    error_msg: str | None = None,
) -> tuple:
    """Build the 12-tuple for the fmp_endpoint_status upsert."""
    return (
        ticker,
        endpoint,
        period or "",
        status,
        http_code,
        record_count,
        earliest,
        latest,
        file_path,
        file_bytes,
        error_msg,
        datetime.now().isoformat(timespec="seconds"),
    )


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
            wait = 2**attempt  # 1, 2, 4, 8, 16 seconds
            print(f"  [db-locked, retry {attempt + 1}/5 after {wait}s: {e}]", flush=True)
            time.sleep(wait)
        finally:
            conn.close()
    assert last_err is not None
    raise last_err


def _record_status(
    ticker: str,
    endpoint: str,
    period: str,
    *,
    status: str,
    http_code: int | None = None,
    record_count: int | None = None,
    earliest: str | None = None,
    latest: str | None = None,
    file_path: str | None = None,
    file_bytes: int | None = None,
    error_msg: str | None = None,
) -> None:
    """Upsert one fmp_endpoint_status row immediately. Used by global/sector path
    where there's no per-ticker batch to accumulate into.
    """
    _flush_status_batch(
        [
            _build_status_row(
                ticker,
                endpoint,
                period,
                status=status,
                http_code=http_code,
                record_count=record_count,
                earliest=earliest,
                latest=latest,
                file_path=file_path,
                file_bytes=file_bytes,
                error_msg=error_msg,
            )
        ]
    )


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
) -> dict[str, int]:
    lt = _list_type_for(ticker)
    print(f"\n=== {ticker} ({lt}) ===", flush=True)
    jobs = per_ticker_jobs(ticker, list_type=lt)
    if manifest_filter is not None:
        jobs = [j for j in jobs if (j["path"], j["period"]) in manifest_filter]
    snapshot_index = snapshot_index if snapshot_index is not None else {}

    summary = {"ok": 0, "empty": 0, "forbidden": 0, "error": 0, "skipped": 0, "total": len(jobs)}

    if skip_existing:
        conn = portfolio_db.get_connection()
        cur = conn.cursor()
        cur.execute(
            """SELECT endpoint, period, last_pulled FROM fmp_endpoint_status
                       WHERE ticker = ? AND status = 'ok'""",
            (ticker,),
        )
        # Map (endpoint, period) -> last_pulled so time-sensitive endpoints can be
        # re-fetched once their cached snapshot is stale (see _snapshot_is_stale);
        # everything else still skips on a plain 'ok'.
        already_ok = {(r["endpoint"], r["period"]): r["last_pulled"] for r in cur.fetchall()}
        conn.close()
    else:
        already_ok = {}

    # Accumulate fmp_endpoint_status upserts and flush once at end-of-ticker.
    # 57 individual commits cost ~1s/ticker on Windows; one batch commit is ~50ms.
    pending: list[tuple] = []
    # Parallel accumulator of source_calls rows for FMP cache-hit observability;
    # batch-flushed alongside `pending` so per-row connections don't reintroduce
    # the per-call fsync cost the status batching avoids. kind = the FMP endpoint
    # path (matches fmp_endpoint_status granularity).
    src_calls: list[source_calls_log.PendingSourceCall] = []

    for job in jobs:
        endpoint = cast("str", job["path"])
        period = cast("str", job["period"])
        suffix = cast("str", job["suffix"])

        if (endpoint, period) in already_ok and not (
            endpoint in TIME_SENSITIVE_ENDPOINTS
            and _snapshot_is_stale(already_ok[(endpoint, period)])
        ):
            summary["skipped"] += 1
            src_calls.append(
                source_calls_log.PendingSourceCall(
                    source_name=_FMP_SOURCE,
                    kind=endpoint,
                    ticker=ticker,
                    status=source_calls_log.CallStatus.SKIPPED,
                    notes="cache_hit",
                )
            )
            continue

        if max_calls is not None and _CALL_COUNTER >= max_calls:
            summary["skipped"] += 1
            src_calls.append(
                source_calls_log.PendingSourceCall(
                    source_name=_FMP_SOURCE,
                    kind=endpoint,
                    ticker=ticker,
                    status=source_calls_log.CallStatus.SKIPPED,
                    notes="budget_exhausted",
                )
            )
            continue

        _call_started = time.monotonic()
        code, body, err, kind = fmp_call(endpoint, ticker, job["extra"])
        _call_ms = int((time.monotonic() - _call_started) * 1000)

        if code == 200 and body is not None:
            # Pre-write schema-drift gate: validate stable statement responses
            # before caching. A drifted stable envelope is dumped to .tmp/ and
            # the write is skipped (recorded as error) rather than overwriting
            # good cached JSON. v3/v4 fallback rungs are not gated (see
            # _validate_stable_record).
            drift = _validate_stable_record(endpoint, kind, body)
            if drift is not None:
                dump_path = _dump_validation_failure(ticker, endpoint, period, suffix, body, drift)
                rel_dump = dump_path.relative_to(PROJECT_ROOT)
                pending.append(
                    _build_status_row(
                        ticker,
                        endpoint,
                        period,
                        status="error",
                        http_code=code,
                        error_msg=(
                            f"schema_drift: {len(drift.errors())} validation "
                            f"errors; raw dumped to {rel_dump}"
                        ),
                    )
                )
                summary["error"] += 1
                src_calls.append(
                    source_calls_log.PendingSourceCall(
                        source_name=_FMP_SOURCE,
                        kind=endpoint,
                        ticker=ticker,
                        status=source_calls_log.CallStatus.ERROR,
                        latency_ms=_call_ms,
                        http_code=code,
                        notes="schema_drift",
                    )
                )
                print(
                    f"  drift {endpoint:48s} {period:8s} schema drift -> "
                    f"{dump_path.name} (write skipped)"
                )
                continue

            # Split-basis normalization gate (analyst-estimates only). FMP splices
            # a name's pre-split per-share actuals with its post-split forward
            # consensus into one series (BKNG-class); force every per-share row
            # onto the CURRENT basis and repair the corrupted historical
            # netIncomeAvg before caching. A series we can't reconcile (no cached
            # income statement to establish the basis) is quarantined: dump raw,
            # skip the write, and auto-log a deferred FMP task so the backlog
            # populates itself.
            if endpoint == _ANALYST_ESTIMATES_ENDPOINT:
                body, quarantine_reason, norm_events = _normalize_estimates_body(ticker, body)
                if quarantine_reason is not None:
                    dump_path = _dump_quarantine(
                        ticker, endpoint, period, suffix, body, quarantine_reason
                    )
                    rel_dump = dump_path.relative_to(PROJECT_ROOT)
                    _log_deferred_split_quarantine(ticker, suffix, quarantine_reason, rel_dump)
                    pending.append(
                        _build_status_row(
                            ticker,
                            endpoint,
                            period,
                            status="error",
                            http_code=code,
                            error_msg=(
                                f"split_quarantine: {quarantine_reason}; raw dumped to {rel_dump}"
                            ),
                        )
                    )
                    summary["error"] += 1
                    src_calls.append(
                        source_calls_log.PendingSourceCall(
                            source_name=_FMP_SOURCE,
                            kind=endpoint,
                            ticker=ticker,
                            status=source_calls_log.CallStatus.ERROR,
                            latency_ms=_call_ms,
                            http_code=code,
                            notes="split_quarantine",
                        )
                    )
                    print(
                        f"  quar  {endpoint:48s} {period:8s} split quarantine -> "
                        f"{dump_path.name} (write skipped, deferred-logged)"
                    )
                    continue
                if norm_events:
                    print(
                        f"  norm  {endpoint:48s} {period:8s} "
                        f"{len(norm_events)} per-share/NI cells normalized onto current basis"
                    )

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
                    json.dumps(body, indent=2), encoding="utf-8"
                )
                snapshot_index[f"{ticker}_{suffix}"] = TODAY_STR

            count = len(body) if isinstance(body, list) else 1
            earliest, latest = _date_bounds(body if isinstance(body, list) else [body])
            pending.append(
                _build_status_row(
                    ticker,
                    endpoint,
                    period,
                    status="ok",
                    http_code=code,
                    record_count=count,
                    earliest=earliest,
                    latest=latest,
                    file_path=str(file_path.relative_to(PROJECT_ROOT)),
                    file_bytes=file_path.stat().st_size,
                )
            )
            summary["ok"] += 1
            src_calls.append(
                source_calls_log.PendingSourceCall(
                    source_name=_FMP_SOURCE,
                    kind=endpoint,
                    ticker=ticker,
                    status=source_calls_log.CallStatus.OK,
                    latency_ms=_call_ms,
                    http_code=code,
                    record_count=count,
                )
            )
            print(
                f"  ok   {endpoint:48s} {period:8s} n={count:<5} "
                f"{earliest or '?':10s} -> {latest or '?':10s}  [{kind}]"
            )
        elif code in (401, 402, 403):
            pending.append(
                _build_status_row(
                    ticker,
                    endpoint,
                    period,
                    status="forbidden",
                    http_code=code,
                    error_msg=err,
                )
            )
            summary["forbidden"] += 1
            src_calls.append(
                source_calls_log.PendingSourceCall(
                    source_name=_FMP_SOURCE,
                    kind=endpoint,
                    ticker=ticker,
                    status=source_calls_log.CallStatus.TIER_RESTRICTED,
                    latency_ms=_call_ms,
                    http_code=code,
                    notes=err,
                )
            )
            print(f"  403  {endpoint:48s} {period:8s} (tier-restricted)")
        elif err == "empty-list":
            pending.append(
                _build_status_row(
                    ticker,
                    endpoint,
                    period,
                    status="empty",
                    http_code=200,
                    record_count=0,
                    error_msg="empty-list",
                )
            )
            summary["empty"] += 1
            src_calls.append(
                source_calls_log.PendingSourceCall(
                    source_name=_FMP_SOURCE,
                    kind=endpoint,
                    ticker=ticker,
                    status=source_calls_log.CallStatus.NOT_FOUND,
                    latency_ms=_call_ms,
                    http_code=200,
                    record_count=0,
                    notes="empty-list",
                )
            )
            print(f"  empty {endpoint:48s} {period:8s}")
        else:
            pending.append(
                _build_status_row(
                    ticker,
                    endpoint,
                    period,
                    status="error",
                    http_code=code,
                    error_msg=err,
                )
            )
            summary["error"] += 1
            src_calls.append(
                source_calls_log.PendingSourceCall(
                    source_name=_FMP_SOURCE,
                    kind=endpoint,
                    ticker=ticker,
                    status=source_calls_log.CallStatus.ERROR,
                    latency_ms=_call_ms,
                    http_code=code,
                    notes=err,
                )
            )
            print(f"  err  {endpoint:48s} {period:8s} code={code} {err}")

    _flush_status_batch(pending)
    source_calls_log.log_calls_batch(src_calls)

    print(
        f"  --- {ticker}: ok={summary['ok']} empty={summary['empty']} "
        f"forbidden={summary['forbidden']} error={summary['error']} "
        f"skipped={summary['skipped']} / {summary['total']}",
        flush=True,
    )
    return summary


# ---------------------------------------------------------------------------
# Sector/industry one-time
# ---------------------------------------------------------------------------

GICS_SECTORS = [
    "Communication Services",
    "Consumer Cyclical",
    "Consumer Defensive",
    "Energy",
    "Financial Services",
    "Healthcare",
    "Industrials",
    "Real Estate",
    "Technology",
    "Basic Materials",
    "Utilities",
]


def _save_global(name: str, body) -> Path:
    p = SECTOR_DIR / f"{name}.json"
    p.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return p


def _save_global_status(
    endpoint: str,
    key: str,
    status: str,
    *,
    http_code=None,
    record_count=None,
    file_path=None,
    error_msg=None,
):
    _record_status(
        "__GLOBAL__",
        endpoint,
        key,
        status=status,
        http_code=http_code,
        record_count=record_count,
        file_path=file_path,
        error_msg=error_msg,
    )


def run_sector_industry(profiles_dir: Path = FMP_DIR) -> None:
    print("\n=== sector + industry one-time ===", flush=True)

    # Snapshots
    for ep in [
        "sector-pe-snapshot",
        "industry-pe-snapshot",
        "sector-performance-snapshot",
        "industry-performance-snapshot",
    ]:
        code, body, err, kind = fmp_call(ep, None, {"date": TODAY_STR})
        if code == 200 and body is not None:
            p = _save_global(ep.replace("-", "_"), body)
            print(
                f"  ok   {ep:42s}                  rows={len(body) if isinstance(body, list) else 1}"
            )
            _save_global_status(
                ep,
                "snapshot",
                "ok",
                http_code=code,
                record_count=len(body) if isinstance(body, list) else 1,
                file_path=str(p.relative_to(PROJECT_ROOT)),
            )
        else:
            print(f"  err  {ep:42s} code={code} {err}")
            _save_global_status(ep, "snapshot", "error", http_code=code, error_msg=err)

    # Sector PE & performance histories
    for sector in GICS_SECTORS:
        for ep in ["historical-sector-pe", "historical-sector-performance"]:
            code, body, err, kind = fmp_call(
                ep, None, {"sector": sector, "from": TEN_YEARS_AGO, "to": TODAY_STR}
            )
            key = f"{sector}".replace(" ", "_")
            if code == 200 and body is not None:
                p = _save_global(f"{ep.replace('-', '_')}_{key}", body)
                n = len(body) if isinstance(body, list) else 1
                print(f"  ok   {ep:42s} {sector:24s} n={n}")
                _save_global_status(
                    ep,
                    sector,
                    "ok",
                    http_code=code,
                    record_count=n,
                    file_path=str(p.relative_to(PROJECT_ROOT)),
                )
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
            code, body, err, kind = fmp_call(
                ep, None, {"industry": industry, "from": TEN_YEARS_AGO, "to": TODAY_STR}
            )
            key = industry.replace(" ", "_").replace("/", "_")
            if code == 200 and body is not None:
                p = _save_global(f"{ep.replace('-', '_')}_{key}", body)
                n = len(body) if isinstance(body, list) else 1
                print(f"  ok   {ep:42s} {industry:32s} n={n}")
                _save_global_status(
                    ep,
                    industry,
                    "ok",
                    http_code=code,
                    record_count=n,
                    file_path=str(p.relative_to(PROJECT_ROOT)),
                )
            else:
                print(f"  err  {ep:42s} {industry:32s} code={code} {err}")
                _save_global_status(ep, industry, "error", http_code=code, error_msg=err)

    # Global ticker list
    code, body, err, kind = fmp_call("company-symbols-list", None)
    if code == 200 and body is not None:
        p = _save_global("company_symbols_list", body)
        n = len(body) if isinstance(body, list) else 1
        print(f"  ok   company-symbols-list                                       n={n}")
        _save_global_status(
            "company-symbols-list",
            "all",
            "ok",
            http_code=code,
            record_count=n,
            file_path=str(p.relative_to(PROJECT_ROOT)),
        )
    else:
        print(f"  err  company-symbols-list code={code} {err}")
        _save_global_status("company-symbols-list", "all", "error", http_code=code, error_msg=err)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _ticker_list(list_type: str) -> list[str]:
    conn = portfolio_db.get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT ticker FROM tracked_companies WHERE list_type = ? ORDER BY ticker", (list_type,)
    )
    rows = [r["ticker"] for r in cur.fetchall()]
    conn.close()
    return rows


def run_stable_probe(ticker: str) -> dict[str, list[str]]:
    """Probe every catalog endpoint on STABLE ONLY and report coverage.

    Forces stable-only mode and runs each per_ticker_jobs endpoint through the
    stable rung (+ stable PATH_ALIASES) only — exactly the ladder a free-tier
    run uses. Read-only: writes no files and no DB rows. Prints a JSON report
    and returns {outcome: [endpoint:period, ...]} so the migration can see which
    endpoints do NOT resolve on stable (a `forbidden`/`error` outcome means the
    endpoint needs a stable alias added to PATH_ALIASES, or is genuinely
    unavailable on free and degrades to the SEC source-of-truth path).
    """
    _enable_stable_only()
    jobs = cast("list[dict[str, object]]", per_ticker_jobs(ticker))
    report: dict[str, list[str]] = {"ok": [], "empty": [], "forbidden": [], "error": []}
    markers = {"ok": "ok   ", "empty": "empty", "forbidden": "403  ", "error": "err  "}
    print(f"\n=== STABLE PROBE: {ticker} ({len(jobs)} endpoints, stable-only) ===", flush=True)
    for job in jobs:
        endpoint = cast("str", job["path"])
        period = cast("str", job["period"])
        extra = cast("dict[str, object]", job["extra"])
        label = f"{endpoint}:{period}" if period else endpoint
        code, _body, err, kind = fmp_call(endpoint, ticker, extra)
        if code == 200 and _body is not None:
            bucket = "ok"
        elif err == "empty-list":
            bucket = "empty"
        elif code in (401, 402, 403):
            bucket = "forbidden"
        else:
            bucket = "error"
        report[bucket].append(label)
        detail = "" if bucket in ("ok", "empty") else f" {err or ''}"
        print(f"  {markers[bucket]} {endpoint:46s} {period:8s} [{kind or '-'}]{detail}")

    print("\n=== STABLE PROBE SUMMARY ===")
    print(json.dumps({k: len(v) for k, v in report.items()}, indent=2))
    not_on_stable = report["forbidden"] + report["error"]
    if not_on_stable:
        print(
            "Endpoints that do NOT resolve on stable "
            "(add a stable alias to PATH_ALIASES, or accept + document):"
        )
        print(json.dumps(sorted(not_on_stable), indent=2))
    else:
        print("All catalog endpoints resolve on stable (ok/empty).")
    print(f"  http attempts: {_CALL_COUNTER}")
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="Run full endpoint set on GOOGL only")
    ap.add_argument("--tickers", help="Comma-separated tickers")
    ap.add_argument("--portfolio", action="store_true")
    ap.add_argument("--watchlist", action="store_true")
    ap.add_argument(
        "--evaluation", action="store_true", help="Pull all tickers with list_type='evaluation'"
    )
    ap.add_argument(
        "--index-members",
        action="store_true",
        help="Pull all tickers with list_type='index_member' (skips 10-K JSON)",
    )
    ap.add_argument(
        "--etfs",
        action="store_true",
        help="Pull all tickers with list_type='etf' (skips 10-K JSON)",
    )
    ap.add_argument("--sector-industry", action="store_true")
    ap.add_argument(
        "--all",
        action="store_true",
        help="Active universe (portfolio + watchlist + evaluation) + sector/industry",
    )
    ap.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip endpoints already 'ok' in DB (time-sensitive price/DCF/consensus "
        "endpoints still re-fetch once their cached snapshot ages past the freshness TTL)",
    )
    ap.add_argument(
        "--force-snapshot",
        action="store_true",
        help="Snapshot every time-sensitive endpoint regardless of cadence",
    )
    ap.add_argument(
        "--manifest",
        type=str,
        default=None,
        help="Path to JSON manifest of [{ticker, endpoint, period}] tuples; "
        "filters per_ticker_jobs to only those entries (cacher-driven runs)",
    )
    ap.add_argument(
        "--max-calls",
        type=int,
        default=None,
        help="Soft cap on total HTTP attempts. Run halts mid-ticker when "
        "the counter exceeds this; remaining work is left for next run",
    )
    ap.add_argument(
        "--stable-only",
        action="store_true",
        help="Drop the /api/v3 + /api/v4 fallback rungs; hit /stable only "
        "(same as FMP_TIER=free). Use on the free tier where v3/v4 403.",
    )
    ap.add_argument(
        "--probe-stable",
        action="store_true",
        help="Read-only: probe every catalog endpoint on /stable for one "
        "ticker (default GOOGL, or first --tickers entry) and report which "
        "do not resolve on stable. Writes nothing.",
    )
    args = ap.parse_args()

    if args.stable_only:
        _enable_stable_only()

    if args.probe_stable:
        probe_ticker = args.tickers.split(",")[0].strip().upper() if args.tickers else "GOOGL"
        run_stable_probe(probe_ticker)
        return

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
            manifest_filter.setdefault(t, set()).add((e["endpoint"], e.get("period", "")))
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
    print(
        f"  ok={grand['ok']} empty={grand['empty']} forbidden={grand['forbidden']} "
        f"error={grand['error']} skipped={grand['skipped']} / total={grand['total']}"
    )
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
