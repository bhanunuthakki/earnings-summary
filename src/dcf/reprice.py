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

Run it daily (it is wired as stage 0e of the morning pipeline) or on demand:

    python execution/reprice_dcf.py
    python execution/reprice_dcf.py --ticker NU
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from dcf import persist
from sources.price import LivePrice, read_live_price

# A price source: (repo_root, ticker) -> LivePrice | None. Defaults to the
# multi-source stack `refresh_dcf` itself uses, so the re-priced value matches
# what a full refresh would compute. Injected in tests to avoid the network.
PriceReader = Callable[[Path, str], "LivePrice | None"]


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


def _latest_rows(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """The latest ``dcf_runs`` row per ticker (UNIQUE(ticker) means one in
    practice; MAX(id) makes the pick deterministic if a legacy duplicate exists).
    A missing table / column degrades to no rows (partial / pre-Phase-3 DBs)."""
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, ticker, npv_per_share, live_price, over_under_pct FROM dcf_runs"
        ).fetchall()
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
) -> list[RepriceResult]:
    """Re-price the price leg of every ticker's latest ``dcf_runs`` row.

    For each row, fetch a fresh live price and rewrite ``live_price`` /
    ``live_price_at`` / ``over_under_pct`` in one UPDATE (so the 0076 CHECK holds
    on the post-update row). The fair value (``npv_per_share``) and
    ``valuation_date`` are read-only here. ``tickers`` scopes the sweep when set
    (case-insensitive); ``None`` re-prices all rows. Commits once at the end.
    """
    latest = _latest_rows(conn)
    if tickers is not None:
        wanted = {t.upper() for t in tickers}
        latest = {t: r for t, r in latest.items() if t in wanted}

    results: list[RepriceResult] = []
    cur = conn.cursor()
    for ticker in sorted(latest):
        row = latest[ticker]
        fair = row["npv_per_share"]
        if fair is None or float(fair) <= 0.0:
            results.append(RepriceResult(ticker=ticker, status="no_fair_value"))
            continue
        live = price_reader(repo_root, ticker)
        if live is None:
            results.append(RepriceResult(ticker=ticker, status="no_price"))
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
    conn.commit()
    return results
