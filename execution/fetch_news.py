"""execution/fetch_news.py — news ingestion dispatcher (the pipeline's entry point).

Drives the journalism feeds into the `news` table under one policy:

  * ``--source fmp``       — primary FMP stock-news only.
  * ``--source websearch`` — FMP-independent WebSearch+Opus only (the setting
                             once FMP's news is fully cut off).
  * ``--source auto`` (default) — FMP first; for any ticker where FMP **refused**,
                             run WebSearch+Opus for that ticker. Degrades as FMP
                             tightens: while FMP works the Opus path almost never
                             runs; as FMP starts refusing, the fallback transparently
                             picks up the slack — no code change.

On top of that ladder, two ADDITIVE non-FMP feeds run for EVERY source policy
(directives/news_sources_plan.md): EDGAR filings (8-K item-coded events +
13D/G stake disclosures; ``--skip-edgar`` opts out) and yfinance sell-side
rating changes (``--skip-grades``). Additive means they supplement — never
replace — the policy rows: their failures log and degrade to nothing, and
their rows pass through ``drop_duplicate_stories`` so a story the journalism
feeds already carry (same ticker + normalized headline + date) is not
double-posted.

All feeds map into one validated NewsRow and write through a single
``upsert_news_rows`` call, so ``(ticker, url)`` dedup means a story seen twice
by one feed is stored once.

The refusal predicate (``fmp_refused``) is source-policy-agnostic: every way FMP
withholds news — 401/402/403/429/5xx, OR the HTTP-200-with-a-non-array-body
gotcha — routes to the fallback, while a genuine empty array ``[]`` ("no news in
window") does NOT (so quiet tickers never burn an Opus call). See
scratch/plans/news_table_plan.md §4.4.

Usage:
    python execution/fetch_news.py                       # auto + additive, active book
    python execution/fetch_news.py --source websearch --skip-grades
    python execution/fetch_news.py --source fmp --tickers GOOG AMZN --db-path /tmp/x.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import execution.fetch_edgar_news as edgarnews  # noqa: E402
import execution.fetch_fmp_news as fmpnews  # noqa: E402
import execution.fetch_yf_grades as yfgrades  # noqa: E402
from db import DB_PATH  # noqa: E402
from execution.fetch_news_websearch import fetch_websearch_news_for_ticker  # noqa: E402
from news.store import NewsRow, drop_duplicate_stories, upsert_news_rows  # noqa: E402

SOURCES = ("fmp", "websearch", "auto")
DEFAULT_SOURCE = "auto"

_GRADES_WORKERS = 8  # yfinance is HTTP-bound; EDGAR stays sequential (SEC throttle)


def _log(event: str, **kwargs: object) -> None:
    print(json.dumps({"event": event, **kwargs}), file=sys.stderr)


def fmp_refused(status: int, body: object) -> bool:
    """True when FMP withheld news for a ticker (=> fall back). Source-policy-
    agnostic, so the design needn't know FMP's exact free-tier news policy:

      * 401/402/403/429/5xx — bad key / payment / plan-gate / quota / server.
      * HTTP 200 but a NON-array body — the silent gotcha: FMP delivers quota/
        plan messages as a 200 ``{"Error Message": ...}`` dict (or an empty/None
        body). status 0 (network failure, None body) also lands here.

    A genuine empty array ``[]`` is NOT refusal — it's "no news in the window"
    and must not trigger the costly Opus fallback."""
    return status in (401, 402, 403, 429) or status >= 500 or not isinstance(body, list)


def _safe_websearch(ticker: str, *, days: int, db_path: str) -> list[NewsRow]:
    try:
        return fetch_websearch_news_for_ticker(ticker, news_days=days, db_path=db_path)
    except Exception as exc:  # the fallback already degrades; this is belt-and-suspenders
        _log("news_websearch_failed", ticker=ticker, error=str(exc))
        return []


def _collect_for_ticker(
    ticker: str, *, source: str, db_path: str, days: int, limit: int
) -> list[NewsRow]:
    """Collect (do not persist) one ticker's NewsRows under the source policy."""
    rows: list[NewsRow] = []

    if source in ("fmp", "auto"):
        res = fmpnews.fetch_news_for_ticker(
            ticker, days=days, limit=limit, api_key=fmpnews.FMP_API_KEY
        )
        rows.extend(res.rows)
        if source == "fmp":
            return rows
        # auto: fall back to WebSearch+Opus only when FMP actually refused.
        if fmp_refused(res.status, res.body):
            _log("news_fmp_refused_falling_back", ticker=ticker, status=res.status)
            rows.extend(_safe_websearch(ticker, days=days, db_path=db_path))
        return rows

    # source == "websearch"
    rows.extend(_safe_websearch(ticker, days=days, db_path=db_path))
    return rows


def _safe_edgar(ticker: str, *, days: int) -> list[NewsRow]:
    try:
        return edgarnews.fetch_edgar_news_for_ticker(ticker, days=days)
    except Exception as exc:  # additive: must never block the primary feeds
        _log("news_edgar_failed", ticker=ticker, error=str(exc)[:200])
        return []


def _safe_grades(ticker: str, *, days: int) -> list[NewsRow]:
    try:
        return yfgrades.fetch_grades_for_ticker(ticker, days=days)
    except Exception as exc:  # additive: must never block the primary feeds
        _log("news_grades_failed", ticker=ticker, error=str(exc)[:200])
        return []


def _collect_additive(
    tickers: list[str], *, days: int, skip_edgar: bool, skip_grades: bool
) -> list[NewsRow]:
    """The additive non-FMP feeds for the whole book: EDGAR sequentially (its
    module throttles to honor SEC's 10 req/s policy), grades threaded (plain
    HTTP). Per-ticker failures are already degraded inside the _safe_* wrappers,
    so this always returns whatever could be fetched."""
    rows: list[NewsRow] = []
    if not skip_edgar:
        for ticker in tickers:
            rows.extend(_safe_edgar(ticker.upper(), days=days))
    if not skip_grades:
        with ThreadPoolExecutor(max_workers=_GRADES_WORKERS) as executor:
            futures = [
                executor.submit(_safe_grades, ticker.upper(), days=days) for ticker in tickers
            ]
            for future in futures:
                rows.extend(future.result())
    return rows


def run(
    tickers: list[str],
    *,
    source: str,
    db_path: str,
    days: int,
    limit: int,
    skip_edgar: bool = False,
    skip_grades: bool = False,
) -> int:
    """Collect every ticker under the source policy, add the additive feeds,
    and persist through one connection. Returns 0 normally; 1 only on a
    structural failure (the `news` table absent) — per-ticker feed hiccups are
    logged and degraded, so the morning pipeline's trigger stage still runs."""
    if not tickers:
        _log("news_no_tickers")
        return 0

    all_rows: list[NewsRow] = []
    for ticker in tickers:
        all_rows.extend(
            _collect_for_ticker(
                ticker.upper(), source=source, db_path=db_path, days=days, limit=limit
            )
        )

    additive = _collect_additive(tickers, days=days, skip_edgar=skip_edgar, skip_grades=skip_grades)

    conn = sqlite3.connect(db_path)
    try:
        # Additive feeds supplement, never duplicate: drop any story the policy
        # rows (or the table) already carry under another url.
        additive = drop_duplicate_stories(conn, additive, against=all_rows)
        inserted, deduped = upsert_news_rows(conn, all_rows + additive)
    except sqlite3.OperationalError as exc:
        _log("news_table_missing", error=str(exc))
        return 1
    finally:
        conn.close()

    _log(
        "news_dispatch_done",
        source=source,
        tickers=len(tickers),
        fetched_rows=len(all_rows),
        additive_rows=len(additive),
        inserted=inserted,
        deduped=deduped,
    )
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source",
        choices=SOURCES,
        default=DEFAULT_SOURCE,
        help="Which feed(s) to run (default: auto — FMP, falling back to "
        "WebSearch+Opus per ticker on refusal).",
    )
    parser.add_argument(
        "--tickers", nargs="*", help="Whitespace-separated tickers (default: active tracked book)."
    )
    parser.add_argument("--db-path", default=None, help="Override the portfolio DB path.")
    parser.add_argument(
        "--days", type=int, default=fmpnews.DEFAULT_DAYS, help="Recency window in days."
    )
    parser.add_argument(
        "--limit", type=int, default=fmpnews.DEFAULT_LIMIT, help="Max FMP articles per ticker."
    )
    parser.add_argument(
        "--skip-edgar",
        action="store_true",
        help="Skip the additive EDGAR filings feed (8-K / 13D / 13G).",
    )
    parser.add_argument(
        "--skip-grades",
        action="store_true",
        help="Skip the additive yfinance rating-changes feed.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path = cast("str", args.db_path) if args.db_path else DB_PATH
    if args.db_path:
        # Sync the global so the WebSearch+Opus path's LLM call ledger writes to
        # the SAME DB as the news rows (it resolves from db.DB_PATH, which the
        # explicit --db-path would otherwise bypass).
        import db

        db.set_db_path(db_path)
    tickers = (
        [t.upper() for t in cast("list[str]", args.tickers)]
        if args.tickers
        else fmpnews.default_tickers(db_path)
    )
    return run(
        tickers,
        source=cast("str", args.source),
        db_path=db_path,
        days=cast("int", args.days),
        limit=cast("int", args.limit),
        skip_edgar=cast("bool", args.skip_edgar),
        skip_grades=cast("bool", args.skip_grades),
    )


if __name__ == "__main__":
    sys.exit(main())
