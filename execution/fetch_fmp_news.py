"""execution/fetch_fmp_news.py — primary FMP stock-news feed for the `news` table.

Fetches recent per-ticker news from FMP's **stable** stock-news endpoint, maps
each article into a validated ``NewsRow``, and persists via ``upsert_news_rows``
so the material_news trigger has structured stories to classify.

Endpoint: ``GET https://financialmodelingprep.com/stable/news/stock?symbols={T}``
(the v3 ``stock_news`` endpoint was deprecated 2025-08-31 and now 403s for
non-legacy keys — see scratch/plans/news_table_plan.md Risk R6). The probe in
that plan (one keyless call) returns ``401 {"Error Message": ...}``, confirming
both the endpoint and the refusal shape the dispatcher's ``_fmp_refused`` keys on.

Two consumers:
  * standalone CLI (``run``) — fetch the active tracked book, persist, exit 0/1.
  * ``fetch_news_for_ticker`` — the per-ticker unit the dispatcher
    (execution/fetch_news.py, PR5) reuses; it returns the raw ``(status, body)``
    so ``auto`` mode can decide per ticker whether to fall back to WebSearch+Opus.

Timezone (Risk R1): FMP's ``publishedDate`` is ``'YYYY-MM-DD HH:MM:SS'`` in
**US/Eastern**; the trigger compares against UTC. ``to_utc`` converts (DST-aware
via zoneinfo) and the NewsRow validator is the backstop. A record whose date
can't be parsed is DROPPED (never timestamp-fabricated).

Usage:
    python execution/fetch_fmp_news.py
    python execution/fetch_fmp_news.py --tickers GOOG AMZN META
    python execution/fetch_fmp_news.py --db-path /tmp/x.db --days 2 --limit 50
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple, cast
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from db import ACTIVE_LIST_TYPES_SQL, DB_PATH  # noqa: E402
from log_redact import redact as _redact  # noqa: E402
from models.fmp_payloads import FmpStockNewsRecord  # noqa: E402
from news.store import SOURCE_FEED_FMP, NewsRow, upsert_news_rows  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")
FMP_API_KEY = os.environ.get("FMP_API_KEY", "")

FMP_NEWS_ENDPOINT = "https://financialmodelingprep.com/stable/news/stock"
_EASTERN = ZoneInfo("America/New_York")
_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

RETRY_LIMIT = 3
RETRY_BACKOFF = 2.0  # seconds, multiplied by attempt number
MAX_WORKERS = 16
DEFAULT_DAYS = 2  # FMP from/to window — margin over the trigger's 24h recency
DEFAULT_LIMIT = 50  # the trigger reads only the latest 15; 50 is generous headroom
REQUEST_TIMEOUT = 30

_VALIDATION_DUMP_DIR = PROJECT_ROOT / ".tmp" / "fmp_validation_failures"


def _log(event: str, **kwargs: object) -> None:
    """Structured JSON event to stderr (the repo's fetcher logging idiom)."""
    print(json.dumps({"event": event, **kwargs}), file=sys.stderr)


class FmpNewsResult(NamedTuple):
    """Per-ticker FMP fetch outcome.

    ``status`` / ``body`` are the raw HTTP result the dispatcher's
    ``_fmp_refused`` predicate reads (status 0 = network failure). ``rows`` are
    the validated + mapped NewsRows (empty unless a 200 carried real articles).
    ``error`` is a human-readable reason when something went wrong, else None.
    """

    ticker: str
    status: int
    body: object
    rows: list[NewsRow]
    error: str | None


# ---------------------------------------------------------------------------
# Mapping (FMP wire shape -> NewsRow)
# ---------------------------------------------------------------------------


def to_utc(published_date: str) -> str:
    """Convert FMP's US/Eastern ``'YYYY-MM-DD HH:MM:SS'`` to canonical UTC of the
    same shape (DST-aware). Raises ValueError if the input isn't that format."""
    naive = datetime.strptime(published_date, _DATETIME_FORMAT)
    return naive.replace(tzinfo=_EASTERN).astimezone(UTC).strftime(_DATETIME_FORMAT)


def _map_record(rec: FmpStockNewsRecord, ticker: str) -> NewsRow | None:
    """Map one validated FMP record to a NewsRow, associating it with the queried
    ticker. Returns None (drop, never fabricate) when the publish date can't be
    parsed/converted to UTC."""
    try:
        published_at = to_utc(rec.publishedDate)
    except ValueError:
        _log(
            "fmp_news_unparseable_date",
            ticker=ticker,
            published_date=rec.publishedDate[:40],
            url=rec.url[:120],
        )
        return None
    return NewsRow(
        ticker=ticker,
        headline=rec.title,
        url=rec.url,
        published_at=published_at,
        snippet=rec.text or None,
        source=rec.site,
        source_feed=SOURCE_FEED_FMP,
    )


def _validate_and_map(body: object, ticker: str) -> tuple[list[NewsRow], ValidationError | None]:
    """Validate + map a fetched body into NewsRows.

    Returns ``(rows, drift)``. A non-list body (refusal / error envelope) or an
    empty list (genuine no-news) yields ``([], None)`` — the dispatcher's
    ``_fmp_refused`` decides whether to fall back. If the FIRST record fails the
    contract it is loud schema drift: returns ``([], ValidationError)`` so the
    caller dumps + halts the ticker. A LATER record that drifts is skipped
    individually (degraded, not a halt)."""
    if not isinstance(body, list):
        return [], None
    records = cast("list[object]", body)
    if not records:
        return [], None
    try:
        _ = FmpStockNewsRecord.model_validate(records[0])
    except ValidationError as exc:
        return [], exc
    rows: list[NewsRow] = []
    for raw in records:
        try:
            rec = FmpStockNewsRecord.model_validate(raw)
        except ValidationError:
            continue
        mapped = _map_record(rec, ticker)
        if mapped is not None:
            rows.append(mapped)
    return rows, None


def _dump_validation_failure(ticker: str, body: object, err: ValidationError) -> Path:
    """Write the drifted response + error breakdown to .tmp/ for inspection,
    mirroring save_fmp_data's stable-validation gate. Returns the dump path."""
    _VALIDATION_DUMP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    out = _VALIDATION_DUMP_DIR / f"{ticker}_stock_news_{stamp}.json"
    payload = {
        "ticker": ticker,
        "endpoint": FMP_NEWS_ENDPOINT,
        "validation_errors": [
            {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in err.errors()
        ],
        "raw_response": body,
    }
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def _safe_json(resp: requests.Response) -> object:
    """resp.json() or None on a non-JSON body (kept as the raw refusal signal)."""
    try:
        return resp.json()
    except ValueError:
        return None


def fetch_news_for_ticker(
    ticker: str,
    *,
    days: int = DEFAULT_DAYS,
    limit: int = DEFAULT_LIMIT,
    api_key: str = "",
) -> FmpNewsResult:
    """Fetch + validate + map recent news for one ticker (does NOT persist).

    Fail-fast on auth/plan refusal (401/402/403); retry 429/5xx with backoff.
    Returns the raw (status, body) alongside the mapped rows so the dispatcher
    can run ``_fmp_refused`` and decide per-ticker fallback."""
    ticker = ticker.upper()
    today = datetime.now(UTC).date()
    params: dict[str, str] = {
        "symbols": ticker,
        "from": (today - timedelta(days=days)).isoformat(),
        "to": today.isoformat(),
        "limit": str(limit),
        "page": "0",
        "apikey": api_key,
    }

    last_err = ""
    for attempt in range(1, RETRY_LIMIT + 1):
        try:
            resp = requests.get(FMP_NEWS_ENDPOINT, params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            last_err = f"network: {_redact(exc)}"
            if attempt < RETRY_LIMIT:
                time.sleep(RETRY_BACKOFF * attempt)
                continue
            return FmpNewsResult(ticker, 0, None, [], last_err)

        status = resp.status_code
        if status in (401, 402, 403):
            _log("fmp_news_auth_error", ticker=ticker, status=status)
            return FmpNewsResult(ticker, status, _safe_json(resp), [], f"auth: HTTP {status}")
        if status == 429 or status >= 500:
            last_err = f"HTTP {status}"
            if attempt < RETRY_LIMIT:
                _log("fmp_news_transient_retry", ticker=ticker, status=status, attempt=attempt)
                time.sleep(RETRY_BACKOFF * attempt)
                continue
            return FmpNewsResult(ticker, status, _safe_json(resp), [], last_err)

        body = _safe_json(resp)
        rows, drift = _validate_and_map(body, ticker)
        if drift is not None:
            dump = _dump_validation_failure(ticker, body, drift)
            try:
                shown = str(dump.relative_to(PROJECT_ROOT))
            except ValueError:
                shown = str(dump)  # dump dir outside the repo (e.g. under test)
            _log("fmp_news_schema_drift_halt", ticker=ticker, dump=shown)
            return FmpNewsResult(
                ticker,
                status,
                body,
                [],
                f"schema_drift: {len(drift.errors())} errors; dumped {shown}",
            )
        return FmpNewsResult(ticker, status, body, rows, None)

    return FmpNewsResult(ticker, 0, None, [], last_err or "retries exhausted")


# ---------------------------------------------------------------------------
# Ticker selection + standalone run
# ---------------------------------------------------------------------------


def _default_tickers(db_path: str) -> list[str]:
    """Active tracked book (portfolio/watchlist/evaluation, not archived) — the
    exact set the trigger driver scans. Empty list if the table is absent."""
    conn = sqlite3.connect(db_path)
    try:
        try:
            rows = conn.execute(
                f"SELECT ticker FROM tracked_companies WHERE list_type IN {ACTIVE_LIST_TYPES_SQL} "
                "AND (archived_at IS NULL) ORDER BY ticker"
            ).fetchall()
        except sqlite3.Error:
            return []
        return [str(r[0]).upper() for r in rows]
    finally:
        conn.close()


def run(tickers: list[str], *, db_path: str, days: int, limit: int) -> int:
    """Fetch every ticker (concurrently), persist all rows through one
    connection, and return an exit code (1 if any ticker errored, else 0)."""
    if not FMP_API_KEY:
        _log("fmp_news_error", message="FMP_API_KEY not set in environment")
        return 1
    if not tickers:
        _log("fmp_news_no_tickers")
        return 0

    results: list[FmpNewsResult] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures: dict[Future[FmpNewsResult], str] = {
            executor.submit(
                fetch_news_for_ticker, ticker, days=days, limit=limit, api_key=FMP_API_KEY
            ): ticker
            for ticker in tickers
        }
        for future in as_completed(futures):
            results.append(future.result())

    all_rows: list[NewsRow] = []
    errors = 0
    for res in results:
        if res.error is not None:
            errors += 1
        all_rows.extend(res.rows)

    conn = sqlite3.connect(db_path)
    try:
        inserted, deduped = upsert_news_rows(conn, all_rows)
    except sqlite3.OperationalError as exc:
        # The `news` table is created by migration 0065_news; if it is absent the
        # executor surfaces it loudly rather than dropping news.
        _log("fmp_news_table_missing", error=str(exc))
        return 1
    finally:
        conn.close()

    _log(
        "fmp_news_done",
        tickers=len(tickers),
        fetched_rows=len(all_rows),
        inserted=inserted,
        deduped=deduped,
        errors=errors,
    )
    return 1 if errors else 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--tickers",
        nargs="*",
        help="Whitespace-separated tickers (default: the active tracked book).",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Override the portfolio DB path (default: data/portfolio.db).",
    )
    parser.add_argument(
        "--days", type=int, default=DEFAULT_DAYS, help="FMP from/to window in days."
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Max articles per ticker.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path = cast("str", args.db_path) if args.db_path else DB_PATH
    tickers = (
        [t.upper() for t in cast("list[str]", args.tickers)]
        if args.tickers
        else _default_tickers(db_path)
    )
    return run(tickers, db_path=db_path, days=args.days, limit=args.limit)


if __name__ == "__main__":
    sys.exit(main())
