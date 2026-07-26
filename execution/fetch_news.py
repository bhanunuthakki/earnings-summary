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
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import execution.fetch_edgar_news as edgarnews  # noqa: E402
import execution.fetch_fmp_news as fmpnews  # noqa: E402
import execution.fetch_yf_grades as yfgrades  # noqa: E402
import execution.fetch_yf_news as yfnews  # noqa: E402
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
    ticker: str, *, source: str, db_path: str, days: int, limit: int, websearch_ok: bool = True
) -> list[NewsRow]:
    """Collect (do not persist) one ticker's NewsRows under the source policy.

    ``websearch_ok`` gates the costly WebSearch+LLM fallback per ticker: with
    FMP news fully 402ing, an ungated `auto` sweep burned an LLM web call for
    EVERY active-book name (~90/day, >$400/30d — 2026-07-19 review) mostly on
    evaluation/watchlist names whose news nobody reads daily. The portfolio
    names keep the fallback; everyone else still gets FMP (if it ever returns)
    plus the additive EDGAR/grades feeds."""
    rows: list[NewsRow] = []

    if source in ("fmp", "auto"):
        res = fmpnews.fetch_news_for_ticker(
            ticker, days=days, limit=limit, api_key=fmpnews.FMP_API_KEY
        )
        rows.extend(res.rows)
        if source == "fmp":
            return rows
        # auto: fall back to WebSearch+LLM only when FMP actually refused AND
        # this ticker is fallback-eligible under the scope policy.
        if fmp_refused(res.status, res.body):
            if websearch_ok:
                _log("news_fmp_refused_falling_back", ticker=ticker, status=res.status)
                rows.extend(_safe_websearch(ticker, days=days, db_path=db_path))
            else:
                _log("news_fmp_refused_no_fallback", ticker=ticker, status=res.status)
        return rows

    # source == "websearch" (explicit manual policy — the scope gate does not apply)
    rows.extend(_safe_websearch(ticker, days=days, db_path=db_path))
    return rows


def portfolio_tickers(db_path: str) -> frozenset[str]:
    """Held names (list_type='portfolio', not archived) — the websearch-fallback
    eligibility set under the default 'portfolio' scope. Empty set if the table
    is absent (then NO ticker gets the costly fallback, the safe direction)."""
    conn = sqlite3.connect(db_path)
    try:
        try:
            rows = conn.execute(
                "SELECT ticker FROM tracked_companies "
                "WHERE list_type = 'portfolio' AND archived_at IS NULL"
            ).fetchall()
        except sqlite3.Error:
            return frozenset()
        return frozenset(str(r[0]).upper() for r in rows)
    finally:
        conn.close()


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


def _safe_yf_news(ticker: str, *, days: int) -> list[NewsRow]:
    """Free per-ticker journalism (yfinance). Degrades to [] like every other
    additive feed — this is now the DEFAULT journalism source, replacing the
    ~$0.40/ticker WebSearch+LLM call (see execution/fetch_yf_news.py for the
    measured economics)."""
    try:
        return yfnews.fetch_news_for_ticker(ticker, days=days)
    except Exception as exc:
        _log("yf_news_failed", ticker=ticker, error=f"{type(exc).__name__}: {exc}"[:200])
        return []


def _collect_additive(
    tickers: list[str],
    *,
    days: int,
    skip_edgar: bool,
    skip_grades: bool,
    skip_s1_watch: bool,
    skip_yf_news: bool = False,
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
    if not skip_yf_news:
        with ThreadPoolExecutor(max_workers=_GRADES_WORKERS) as executor:
            futures = [
                executor.submit(_safe_yf_news, ticker.upper(), days=days) for ticker in tickers
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
    websearch_eligible: frozenset[str] | None = None,
    on_rows: Callable[[str, list[NewsRow]], None] | None = None,
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
                websearch_ok=(websearch_eligible is None or ticker.upper() in websearch_eligible),
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
            # Persist AS WE GO (2026-07-19 review): the old collect-everything-
            # then-one-upsert shape meant the daily stage kill discarded the
            # whole (LLM-billed) haul — the news table froze for weeks while the
            # calls kept billing. A stage kill now loses only in-flight tickers.
            if on_rows is not None and ticker_rows:
                on_rows(ticker, ticker_rows)
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


def _score_diet_quality(db_path: str) -> None:
    """LLM information-quality scoring of the mirrored diet signals. Batched
    Haiku calls over rows WHERE quality_score IS NULL, capped per run; the pass
    degrades per-batch internally (transient failures leave rows NULL — retried
    on the next fetch) and this wrapper only lets HARD stops (budget cap /
    missing CLI — llm.cli.is_hard_stop) propagate."""
    try:
        quality_tally = score_unscored_signals(db_path)
        _log("news_diet_quality_scored", **quality_tally)
    except Exception as exc:
        if is_hard_stop(exc):
            raise
        _log("news_diet_quality_deferred", error=f"{type(exc).__name__}: {str(exc)[:200]}")


# A book sweep whose freshest persisted story is older than this is a dead
# feed, not a quiet week — fire the dead-man alert (0183).
_NEWS_STALE_DAYS = 3


def _fire_deadman_if_stale(db_path: str, *, tickers_n: int, inserted_total: int) -> None:
    """The 'data_feed_stale' dead-man (2026-07-19 review: the news table sat
    frozen at 2026-07-03 for weeks while the stage was killed daily and nothing
    told the owner). After a full-book sweep, if the table's freshest
    published_at is older than _NEWS_STALE_DAYS the outage fires ONE book-level
    alert row per day into the feed the owner actually reads. Targeted
    --tickers runs don't judge feed health. Never raises — the dead-man must
    not break the run it is watching."""
    if tickers_n < 5:
        return
    try:
        conn = sqlite3.connect(db_path, timeout=30.0)
        try:
            row = conn.execute("SELECT MAX(published_at) FROM news").fetchone()
        finally:
            conn.close()
        max_pub = str(row[0]) if row and row[0] else None
        now = datetime.now(UTC).replace(tzinfo=None)
        stale = True
        if max_pub:
            newest = datetime.strptime(max_pub[:10], "%Y-%m-%d")
            stale = (now - newest).days > _NEWS_STALE_DAYS
        if not stale:
            return
        from alerts import store as alerts_store

        sig = alerts_store.compute_signature_sha(
            "data_feed_stale", "PORTFOLIO", {"feed": "news", "date": now.date().isoformat()}
        )
        if alerts_store.find_by_signature(signature_sha=sig, db_path=db_path) is not None:
            return
        alerts_store.fire_alert(
            ticker="PORTFOLIO",  # book-level sentinel, the 0171 capacity convention
            trigger_kind="data_feed_stale",
            fired_at=now,
            evidence_json=json.dumps(
                {
                    "feed": "news",
                    "max_published_at": max_pub,
                    "inserted_this_run": inserted_total,
                    "tickers_swept": tickers_n,
                    "stale_days_limit": _NEWS_STALE_DAYS,
                }
            ),
            signature_sha=sig,
            db_path=db_path,
        )
        _log("news_deadman_fired", max_published_at=max_pub)
    except Exception as exc:
        _log("news_deadman_failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")


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
    websearch_scope: str = "portfolio",
) -> int:
    """Collect every ticker under the source policy, add the additive feeds,
    persisting INCREMENTALLY throughout. Returns 0 normally; 1 only on a
    structural failure (the `news` table absent) — per-ticker feed hiccups are
    logged and degraded, so the morning pipeline's trigger stage still runs.

    Shape (2026-07-19 review): the old collect-everything-then-one-upsert run
    was killed daily at the stage budget BEFORE its single persist — weeks of
    LLM-billed output discarded, the news table frozen, and the follow-on diet
    scoring never reached (0 calls ever despite correct wiring). Now every
    ticker's rows land as they arrive, diet scoring runs right after the
    primary sweep (the bulk of the value; additive rows score on the next
    run), and a dead-man alert fires if the table is still stale after a full
    sweep."""
    if not tickers:
        _log("news_no_tickers")
        return 0

    eligible: frozenset[str] | None = None
    if source == "auto" and websearch_scope == "none":
        # Nobody is eligible for the paid fallback: the free yfinance feed is
        # the journalism source now. An EMPTY frozenset (not None) is the
        # "gate everyone out" signal — None means "no gate".
        eligible = frozenset()
        _log("news_websearch_scope", scope="none", eligible=0)
    elif source == "auto" and websearch_scope == "portfolio":
        eligible = portfolio_tickers(db_path)
        _log("news_websearch_scope", scope="portfolio", eligible=len(eligible))

    # 30s busy timeout: persists interleave with (potentially LLM-billed)
    # collection — a brief concurrent writer must stall a write, not throw a
    # ticker's rows away with "database is locked" (default busy wait is 5s;
    # lock contention observed live 2026-07-03).
    conn = sqlite3.connect(db_path, timeout=30.0)
    inserted_total = 0
    deduped_total = 0
    persist_failures = 0

    def _persist(label: str, rows: list[NewsRow]) -> None:
        nonlocal inserted_total, deduped_total, persist_failures
        try:
            ins, dup = upsert_news_rows(conn, rows)
            inserted_total += ins
            deduped_total += dup
        except sqlite3.OperationalError as exc:
            persist_failures += 1
            _log("news_persist_failed", batch=label, error=str(exc))

    try:
        all_rows = collect_primary(
            tickers,
            source=source,
            db_path=db_path,
            days=days,
            limit=limit,
            websearch_eligible=eligible,
            on_rows=_persist,
        )

        # Diet scoring runs HERE — after the primary rows are safe but before
        # the additive sweep — so a stage kill during EDGAR/grades can no
        # longer starve it (it had 0 calls ever before this reordering).
        # It is resumable (WHERE quality_score IS NULL), so additive rows it
        # misses this run are scored next run.
        _score_diet_quality(db_path)

        additive = _collect_additive(
            tickers,
            days=days,
            skip_edgar=skip_edgar,
            skip_grades=skip_grades,
            skip_s1_watch=skip_s1_watch,
        )
        # Additive feeds supplement, never duplicate: drop any story the policy
        # rows (or the table) already carry under another url.
        additive = drop_duplicate_stories(conn, additive, against=all_rows)
        if additive:
            _persist("additive", additive)
    finally:
        conn.close()

    _log(
        "news_dispatch_done",
        source=source,
        tickers=len(tickers),
        fetched_rows=len(all_rows),
        additive_rows=len(additive),
        inserted=inserted_total,
        deduped=deduped_total,
        persist_failures=persist_failures,
    )
    _fire_deadman_if_stale(db_path, tickers_n=len(tickers), inserted_total=inserted_total)
    return 1 if persist_failures and not inserted_total else 0


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
    parser.add_argument(
        "--websearch-scope",
        choices=("none", "portfolio", "all"),
        default="none",
        help="Which tickers may use the WebSearch+LLM fallback under --source auto. "
        "DEFAULT 'none' (2026-07-25): the free yfinance journalism feed "
        "(execution/fetch_yf_news.py) now covers this, and the paid path measured "
        "$5.74 per STORED row — 93% of its calls stored nothing after (ticker,url) "
        "dedup, because FMP's stock-news endpoint 402s and the LLM fallback "
        "silently became the primary for the whole book. 'portfolio' = held names "
        "only (~$14/day); 'all' = the pre-2026-07 behavior. Use a non-none scope "
        "for a deliberate one-off sweep, not as the standing default.",
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
        websearch_scope=cast("str", args.websearch_scope),
    )


if __name__ == "__main__":
    sys.exit(main())
