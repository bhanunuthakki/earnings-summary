"""Re-price the persisted DCF over/under against a fresh live price.

A DCF's fair value (``dcf_runs.npv_per_share``) is fixed between fundamentals
refreshes, but the price leg of ``over_under_pct = (live_price - fair) / fair``
goes stale the instant the market moves. ``refresh_dcf`` (which recomputes both
legs) is opt-in with no cron, so a price-only move silently corrupts every
consumer that reads the stored column — the trim/sell ladder, the cockpit
fv-gap, and the 50%-weight next-dollar "ret" factor all read the persisted row
— while the DCF coverage panel still tones the row "fresh" off ``valuation_date``.

This module re-divides the *persisted* fair value by a *fresh* live price and
rewrites ONLY the three price-leg columns (``live_price``, ``live_price_at``,
``over_under_pct``). It is pure arithmetic over the stored fair value — it never
re-runs the DCF. The re-derivation goes through ``persist.derive_over_under`` (the
single producer of the column), so:

  * the re-priced value is identical to what a full ``refresh_dcf`` would persist
    for the price leg (same source stack, same convention), and
  * the row stays consistent with the migration-0076 CHECK, because all three
    columns move together in one UPDATE.

``valuation_date``, ``npv_per_share`` and the assumption snapshot are never
touched, so the fair-value leg keeps its own (slower) cadence — and the coverage
panel can now mark each leg's freshness honestly.

Row selection (migration 0137): on a versioned schema, only ``is_latest = 1``
rows are re-priced. Every ``refresh_dcf`` keeps superseded history rather than
overwriting, so ``dcf_runs`` grows monotonically (149 rows / 96 tickers as of
2026-07 — already ~1.5x); reading every row instead of filtering to the current
one would make this stage's cost grow linearly forever as history piles up. On
a pre-0137 schema (no ``is_latest`` column) this falls back to a MAX(id)-per-
ticker pick, since the versioning column doesn't exist to filter on.

Live-price fetches run threaded (``PRICE_FETCH_WORKERS`` at a time), each under
its own wall-clock budget (``PER_TICKER_TIMEOUT_S``): yfinance exposes no
request-level timeout through this call shape, so a hung socket would
otherwise stall the whole sweep. A ticker that blows its budget is treated
like ``no_price`` — the stale row is left intact — and the sweep moves on
rather than hanging the stage; the DB write itself stays single-threaded (one
UPDATE per resolved fetch, back on the calling thread) since sqlite3
connections aren't shared across threads. Progress is flushed per ticker so a
future cron timeout shows exactly which ticker the stage died on instead of an
empty log section.

Run it daily (it is wired as stage 0e of the morning pipeline) or on demand:

    python execution/reprice_dcf.py
    python execution/reprice_dcf.py --ticker NU
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path

from dcf import persist
from sources.price import LivePrice, read_live_price

# A price source: (repo_root, ticker) -> LivePrice | None. Defaults to the
# multi-source stack `refresh_dcf` itself uses, so the re-priced value matches
# what a full refresh would compute. Injected in tests to avoid the network.
PriceReader = Callable[[Path, str], "LivePrice | None"]

# Threaded like the additive yfinance-grades news feed: yfinance is HTTP-bound,
# so running several tickers' fetches at once bounds the whole sweep's
# wall-clock to ceil(n/workers) calls instead of n.
PRICE_FETCH_WORKERS = 8
# Wall-clock cap per ticker's live-price fetch. yfinance's session exposes no
# request timeout through .fast_info/.info, so a stalled socket would otherwise
# hang indefinitely; a fresh quote normally resolves in well under a second
# (measured ~0.2-4s/ticker live against prod, 2026-07), so 15s is generous
# headroom while still bounding the whole stage: worst case (every ticker
# hangs its full budget) is ceil(len(tickers)/PRICE_FETCH_WORKERS) *
# PER_TICKER_TIMEOUT_S, which the stage timeout must exceed.
PER_TICKER_TIMEOUT_S = 15.0


def _log(event: str, **kwargs: object) -> None:
    import json

    print(json.dumps({"event": event, **kwargs}), file=sys.stderr, flush=True)


def _fetch_prices(
    price_reader: PriceReader,
    repo_root: Path,
    tickers: list[str],
    *,
    workers: int,
    timeout_s: float,
) -> dict[str, LivePrice | None]:
    """Fetch every ticker's live price under bounded concurrency.

    Submits all fetches to one shared pool (``workers`` at a time) rather than
    one executor per ticker, then reads each result under ``timeout_s``. A
    future that blows its budget is abandoned mid-flight (the worker thread
    leaks until interpreter exit — acceptable for a once-daily batch script)
    and its ticker maps to None, same as any other "no live price" miss.

    The executor is shut down with ``wait=False``: the default ``wait=True``
    would block this function until every submitted future finishes, including
    an abandoned one still hung on a stalled socket — silently reintroducing
    the same "one stuck ticker blocks the whole sweep" bug this function
    exists to bound."""
    out: dict[str, LivePrice | None] = {}
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="reprice-price")
    try:
        futures: dict[Future[LivePrice | None], str] = {
            executor.submit(price_reader, repo_root, ticker): ticker for ticker in tickers
        }
        for future, ticker in futures.items():
            try:
                out[ticker] = future.result(timeout=timeout_s)
            except FutureTimeoutError:
                _log("reprice_ticker_timeout", ticker=ticker, timeout_s=timeout_s)
                out[ticker] = None
            except Exception as exc:  # belt-and-suspenders; price_reader already degrades
                _log("reprice_ticker_error", ticker=ticker, error=str(exc)[:200])
                out[ticker] = None
    finally:
        executor.shutdown(wait=False)
    return out


@dataclass(frozen=True)
class RepriceResult:
    """Outcome of re-pricing one ticker's latest ``dcf_runs`` row.

    ``status`` is one of:
      * ``repriced``       — fresh price fetched, the price-leg columns rewritten;
      * ``no_price``       — no live price available, the stale row left untouched
                             (degrade to last-known rather than blanking it);
      * ``no_fair_value``  — non-positive / missing ``npv_per_share`` (the #291
                             case), over/under is undefined so nothing to re-price.
    """

    ticker: str
    status: str
    over_under_pct: float | None = None
    live_price: float | None = None
    source_name: str | None = None


def _has_is_latest_column(conn: sqlite3.Connection) -> bool:
    try:
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(dcf_runs)")}
    except sqlite3.OperationalError:
        return False
    return "is_latest" in cols


def _latest_rows(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """The current ``dcf_runs`` row per ticker.

    On a versioned schema (migration 0137+) this filters to ``is_latest = 1``
    in SQL — the indexed, authoritative flag — rather than scanning every
    superseded row and re-deriving "latest" in Python. ``dcf_runs`` keeps every
    superseded version (never overwrites), so without this filter the sweep's
    cost grows with total history instead of with the number of tickers.

    On a pre-0137 schema (no ``is_latest`` column — legacy INSERT-OR-REPLACE
    keyed on UNIQUE(ticker)) falls back to a MAX(id)-per-ticker pick, which is
    equivalent there since the legacy path never keeps more than one row per
    ticker. A missing table degrades to no rows (partial / pre-Phase-3 DBs)."""
    conn.row_factory = sqlite3.Row
    versioned = _has_is_latest_column(conn)
    query = (
        "SELECT id, ticker, npv_per_share, live_price, over_under_pct FROM dcf_runs "
        "WHERE is_latest = 1"
        if versioned
        else "SELECT id, ticker, npv_per_share, live_price, over_under_pct FROM dcf_runs"
    )
    try:
        rows = conn.execute(query).fetchall()
    except sqlite3.OperationalError:
        return {}
    latest: dict[str, sqlite3.Row] = {}
    for r in rows:
        t = str(r["ticker"]).upper()
        prev = latest.get(t)
        if prev is None or int(r["id"]) > int(prev["id"]):
            latest[t] = r
    return latest


def reprice_runs(
    conn: sqlite3.Connection,
    repo_root: Path,
    *,
    price_reader: PriceReader = read_live_price,
    tickers: Sequence[str] | None = None,
    price_fetch_workers: int = PRICE_FETCH_WORKERS,
    per_ticker_timeout_s: float = PER_TICKER_TIMEOUT_S,
) -> list[RepriceResult]:
    """Re-price the price leg of every ticker's latest ``dcf_runs`` row.

    Live prices are fetched for every priceable ticker under bounded
    concurrency (``price_fetch_workers`` at once, each capped at
    ``per_ticker_timeout_s`` — see the module docstring for why), then the
    price-leg UPDATEs are applied sequentially in ticker order on the one
    connection (sqlite3 connections aren't thread-safe to share). Each UPDATE
    rewrites ``live_price`` / ``live_price_at`` / ``over_under_pct`` together
    (so the 0076 CHECK holds on the post-update row); the fair value
    (``npv_per_share``) and ``valuation_date`` are read-only here. ``tickers``
    scopes the sweep when set (case-insensitive); ``None`` re-prices every
    ``is_latest`` row. Commits once at the end. Progress is flushed to stderr
    per ticker (JSON lines) so a killed stage's cron log shows exactly how far
    the sweep got.
    """
    latest = _latest_rows(conn)
    if tickers is not None:
        wanted = {t.upper() for t in tickers}
        latest = {t: r for t, r in latest.items() if t in wanted}

    ordered = sorted(latest)
    _log("reprice_start", tickers=len(ordered))

    priceable = [
        t
        for t in ordered
        if latest[t]["npv_per_share"] is not None and float(latest[t]["npv_per_share"]) > 0.0
    ]
    prices = _fetch_prices(
        price_reader,
        repo_root,
        priceable,
        workers=price_fetch_workers,
        timeout_s=per_ticker_timeout_s,
    )

    results: list[RepriceResult] = []
    cur = conn.cursor()
    for i, ticker in enumerate(ordered, start=1):
        row = latest[ticker]
        fair = row["npv_per_share"]
        if fair is None or float(fair) <= 0.0:
            results.append(RepriceResult(ticker=ticker, status="no_fair_value"))
            _log("reprice_ticker_done", ticker=ticker, i=i, n=len(ordered), status="no_fair_value")
            continue
        live = prices.get(ticker)
        if live is None:
            results.append(RepriceResult(ticker=ticker, status="no_price"))
            _log("reprice_ticker_done", ticker=ticker, i=i, n=len(ordered), status="no_price")
            continue
        # derive_over_under is the single producer of the column and applies the
        # same #291 guard the original write did; identical to a full refresh's
        # price leg, so the row stays consistent with the migration-0076 CHECK.
        over_under = persist.derive_over_under(live.price, float(fair))
        cur.execute(
            "UPDATE dcf_runs SET live_price = ?, live_price_at = ?, over_under_pct = ? "
            "WHERE id = ?",
            (live.price, live.fetched_at.isoformat(), over_under, int(row["id"])),
        )
        results.append(
            RepriceResult(
                ticker=ticker,
                status="repriced",
                over_under_pct=over_under,
                live_price=live.price,
                source_name=live.source_name,
            )
        )
        _log("reprice_ticker_done", ticker=ticker, i=i, n=len(ordered), status="repriced")
    conn.commit()
    _log("reprice_done", tickers=len(ordered))
    return results
