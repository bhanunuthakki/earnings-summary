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
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import execution.fetch_edgar_news as edgarnews  # noqa: E402
import execution.fetch_fmp_news as fmpnews  # noqa: E402
import execution.fetch_yf_grades as yfgrades  # noqa: E402
from competitive.sec_watch import check_s1_watch, load_watches  # noqa: E402
from db import DB_PATH  # noqa: E402
from execution.fetch_news_websearch import fetch_websearch_news_for_ticker  # noqa: E402
from llm.cli import is_hard_stop  # noqa: E402
from news.store import NewsRow, drop_duplicate_stories, upsert_news_rows  # noqa: E402
from signals.quality import score_unscored_signals  # noqa: E402

SOURCES = ("fmp", "websearch", "auto")
DEFAULT_SOURCE = "auto"

_GRADES_WORKERS = 8  # yfinance is HTTP-bound; EDGAR stays sequential (SEC throttle)

# The primary per-ticker collection (FMP, or its WebSearch+Opus fallback on
# refusal) used to run fully sequentially. That was fine while FMP served
# news, but FMP's stock-news endpoint now 402s for every ticker (verified live
# 2026-07-03 -- see directives/news_sources_plan.md Risk R6), so `auto` falls
# back to a ~55s Opus web-search call for EVERY ticker in the book. Serially
# across a ~100-ticker book that is ~90 minutes of work crammed into the old
# 900s stage budget -- the exact cause of the stage_0_news timeout. Threaded
# like the additive grades feed bounds the wall-clock to ceil(n/workers) calls
# instead of n. 8 workers keeps the concurrent Claude-CLI subprocess burst
# modest (each websearch call spawns its own `claude` process) while still
# cutting a ~98-ticker, all-fallback book from ~90min to ceil(98/8) * 60s = 780s
# worst case (every ticker hangs its full per-ticker budget) / ~715s typical
# (every ticker takes the ~55s websearch fallback) -- both now fit inside
# _NEWS_TIMEOUT_S alongside the additive EDGAR/grades feeds that run after.
_PRIMARY_WORKERS = 8
# Hard per-ticker wall-clock budget for the WHOLE _collect_for_ticker call
# (FMP attempt + retries + the optional websearch fallback). Well over the
# fallback's typical ~55s (measured live 2026-07-03) so a normal call always
# completes, but bounded well under call_llm_with_web's own 1800s timeout floor
# (CLAUDE_WEB_TIMEOUT_SECONDS) so one stuck ticker degrades to "no rows this
# run" and releases its worker slot rather than the run inheriting that stall.
_TICKER_TIMEOUT_S = 60.0


def _log(event: str, **kwargs: object) -> None:
    print(json.dumps({"event": event, **kwargs}), file=sys.stderr, flush=True)


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


def _safe_s1_watch() -> list[NewsRow]:
    """Competitor IPO S-1 watch (src/competitive/sec_watch.py): book-agnostic —
    reads its own watch config, queries EDGAR full-text search, and attributes any
    hit to the affected holding's ticker. A no-op until a watched competitor files."""
    try:
        return check_s1_watch(load_watches(PROJECT_ROOT))
    except Exception as exc:  # additive: must never block the primary feeds
        _log("news_s1_watch_failed", error=str(exc)[:200])
        return []


def _collect_additive(
    tickers: list[str],
    *,
    days: int,
    skip_edgar: bool,
    skip_grades: bool,
    skip_s1_watch: bool,
) -> list[NewsRow]:
    """The additive non-FMP feeds for the whole book: EDGAR sequentially (its
    module throttles to honor SEC's 10 req/s policy), grades threaded (plain
    HTTP), plus the book-agnostic competitor S-1 watch. Per-ticker failures are
    already degraded inside the _safe_* wrappers, so this always returns whatever
    could be fetched."""
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
    if not skip_s1_watch:
        rows.extend(_safe_s1_watch())
    return rows


def collect_primary(
    tickers: list[str],
    *,
    source: str,
    db_path: str,
    days: int,
    limit: int,
    workers: int = _PRIMARY_WORKERS,
    per_ticker_timeout_s: float = _TICKER_TIMEOUT_S,
) -> list[NewsRow]:
    """Collect every ticker's primary-policy rows (FMP, or its WebSearch+Opus
    fallback on refusal) under bounded concurrency and a hard per-ticker time
    budget.

    Threaded across ``workers`` tickers at once so the whole book's wall-clock
    is roughly ``ceil(n/workers) * per-ticker-time`` instead of ``n *
    per-ticker-time`` — the fix for the stage timing out when every ticker
    falls back to the ~55s Opus/web call (FMP news 402ing, verified live
    2026-07-03). Each ticker also gets its own ``per_ticker_timeout_s`` wall-clock
    cap so one stuck ticker (a hung web/LLM call) degrades to "no rows" and
    frees its worker slot rather than blocking the whole run. Progress is
    flushed to stderr per ticker so a killed stage's cron log shows exactly how
    far the sweep got instead of an empty section.

    The executor is shut down with ``wait=False``: a ticker that blows its
    budget leaves an abandoned thread still running in the background (it will
    finish or the interpreter will reap it at exit), but this function returns
    as soon as every future has either resolved or been given up on — it must
    NOT block on the executor's default ``wait=True`` shutdown, which would
    wait for even an abandoned, still-sleeping worker thread to finish and
    silently reintroduce the same "one hung ticker blocks everything" bug this
    whole function exists to fix.
    """
    _log("news_primary_start", tickers=len(tickers), source=source, workers=workers)
    t_start = time.monotonic()
    rows: list[NewsRow] = []
    done = 0
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="news-primary")
    try:
        futures: dict[Future[list[NewsRow]], str] = {
            executor.submit(
                _collect_for_ticker,
                ticker.upper(),
                source=source,
                db_path=db_path,
                days=days,
                limit=limit,
            ): ticker.upper()
            for ticker in tickers
        }
        for future, ticker in futures.items():
            try:
                ticker_rows = future.result(timeout=per_ticker_timeout_s)
            except FutureTimeoutError:
                _log(
                    "news_primary_ticker_timeout",
                    ticker=ticker,
                    timeout_s=per_ticker_timeout_s,
                )
                ticker_rows = []
            except Exception as exc:  # belt-and-suspenders; _collect_for_ticker degrades itself
                _log("news_primary_ticker_error", ticker=ticker, error=str(exc)[:200])
                ticker_rows = []
            done += 1
            rows.extend(ticker_rows)
            _log(
                "news_primary_ticker_done",
                ticker=ticker,
                i=done,
                n=len(tickers),
                rows=len(ticker_rows),
                elapsed_s=round(time.monotonic() - t_start, 1),
            )
    finally:
        executor.shutdown(wait=False)
    _log("news_primary_done", tickers=len(tickers), rows=len(rows))
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
    skip_s1_watch: bool = False,
) -> int:
    """Collect every ticker under the source policy, add the additive feeds,
    and persist through one connection. Returns 0 normally; 1 only on a
    structural failure (the `news` table absent) — per-ticker feed hiccups are
    logged and degraded, so the morning pipeline's trigger stage still runs."""
    if not tickers:
        _log("news_no_tickers")
        return 0

    all_rows = collect_primary(tickers, source=source, db_path=db_path, days=days, limit=limit)

    additive = _collect_additive(
        tickers,
        days=days,
        skip_edgar=skip_edgar,
        skip_grades=skip_grades,
        skip_s1_watch=skip_s1_watch,
    )

    # 30s busy timeout: this single persist lands AFTER the whole (potentially
    # Opus-billed) collection — a brief concurrent writer must stall it, not
    # throw the entire haul away with "database is locked" (sqlite's default
    # busy wait is 5s; lock contention observed live 2026-07-03).
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        # Additive feeds supplement, never duplicate: drop any story the policy
        # rows (or the table) already carry under another url.
        additive = drop_duplicate_stories(conn, additive, against=all_rows)
        inserted, deduped = upsert_news_rows(conn, all_rows + additive)
    except sqlite3.OperationalError as exc:
        _log("news_persist_failed", error=str(exc))
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

    # Follow-on: LLM information-quality scoring of the freshly-mirrored diet
    # signals (upsert_news_rows ran sync_news_to_signals). Batched Haiku calls
    # over rows WHERE quality_score IS NULL, capped per run; the pass degrades
    # per-batch internally (transient failures leave rows NULL — retried on
    # the next fetch) and this wrapper only lets HARD stops (budget cap /
    # missing CLI — llm.cli.is_hard_stop) fail the run loudly. The news rows
    # themselves are already persisted either way.
    try:
        quality_tally = score_unscored_signals(db_path)
        _log("news_diet_quality_scored", **quality_tally)
    except Exception as exc:
        if is_hard_stop(exc):
            raise
        _log("news_diet_quality_deferred", error=f"{type(exc).__name__}: {str(exc)[:200]}")
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
    parser.add_argument(
        "--skip-s1-watch",
        action="store_true",
        help="Skip the additive competitor IPO S-1 watch (EDGAR full-text search).",
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
        skip_s1_watch=cast("bool", args.skip_s1_watch),
    )


if __name__ == "__main__":
    sys.exit(main())
