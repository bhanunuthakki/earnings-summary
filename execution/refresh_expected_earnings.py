"""Materialize the canonical next-earnings calendar into ``expected_earnings``.

For every active tracked ticker (portfolio + watchlist + evaluation), resolve
the next earnings date through ``sources.earnings_calendar.next_earnings_date``
— FMP on-disk cache first, yfinance fallback; every attempt is logged to
``source_calls`` — and upsert it into the ``expected_earnings`` table (0082).

Readers of the table: the morning digest's "Upcoming this week", the Home
cockpit's Next ER column, and the portfolio-tracker's read-only bridge.

Scheduling: ``cron/run_fetch_fmp_earnings_calendar.bat`` (daily 05:45) runs
this immediately after ``fetch_fmp_earnings_calendar.py`` refreshes the FMP
cache. While the FMP subscription is lapsed (the stable ``/earnings`` endpoint
402s on the free tier since 2026-06-10) the cache fetch fails and this script
serves cached-then-yfinance dates — per-ticker, the stack self-heals.

A ticker no source can resolve simply keeps its existing rows (no churn) and
ends up estimated downstream; that is not a failure. Exit 1 only on systemic
errors (unusable DB).

Usage:
  python execution/refresh_expected_earnings.py            # active universe
  python execution/refresh_expected_earnings.py --ticker NU
  python execution/refresh_expected_earnings.py --repo-root /abs/path
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

try:
    from _lib import PROJECT_ROOT
except ImportError:
    from execution._lib import PROJECT_ROOT

import db
from db_paths import default_product_state_root
from expected_earnings import record_next_earnings
from log_redact import redact as _redact
from pipeline.ticker_scope import load_active_capabilities, partition_by_capability
from sources import registry as sources_registry
from sources.earnings_calendar import next_earnings_date

# sources.earnings_calendar source_name -> expected_earnings.detected_source
# (0025's vocabulary: 'fmp' / 'yfinance' / 'manual').
_DETECTED_SOURCE = {"fmp_cache": "fmp", "yfinance": "yfinance"}


def refresh_tickers(
    repo_root: Path,
    tickers: list[str],
    conn: sqlite3.Connection,
    *,
    today: date | None = None,
) -> dict[str, int]:
    """Resolve + upsert each ticker; per-ticker misses/errors don't abort."""
    ref = today or date.today()
    written = 0
    missed = 0
    errors = 0
    for ticker in tickers:
        try:
            nx = next_earnings_date(repo_root, ticker)
        except Exception as e:  # a flaky source must not kill the run
            print(f"  [Error] {ticker}: {_redact(e)}", file=sys.stderr)
            errors += 1
            continue
        if nx is None:
            missed += 1
            continue
        record_next_earnings(
            conn,
            ticker,
            nx.expected_date,
            _DETECTED_SOURCE.get(nx.source_name, nx.source_name),
            today=ref,
        )
        conn.commit()
        written += 1
    return {"tickers": len(tickers), "written": written, "no_source": missed, "errors": errors}


def _select_tickers_with_skips(
    conn: sqlite3.Connection, ticker: str | None
) -> tuple[list[str], list[dict[str, str]]]:
    if ticker:
        return [ticker.upper()], []
    capabilities = load_active_capabilities(conn)
    eligible, skipped = partition_by_capability(
        capabilities,
        predicate=lambda cap: cap.supports_earnings_calendar,
        skip_reason="ineligible_instrument_type",
    )
    return sorted(cap.ticker for cap in eligible), skipped


def _select_tickers(ticker: str | None) -> list[str]:
    if ticker:
        return [ticker.upper()]
    conn = db.get_connection()
    try:
        selected, _ = _select_tickers_with_skips(conn, None)
        return selected
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh the expected_earnings table")
    parser.add_argument("--ticker", help="Single ticker (default: active universe)")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="State root containing data/portfolio.db (default: resolved product state root)",
    )
    args = parser.parse_args()

    repo_root = (
        args.repo_root.resolve()
        if args.repo_root is not None
        else default_product_state_root(PROJECT_ROOT)
    )
    if args.repo_root is not None and repo_root != PROJECT_ROOT:
        db.set_db_path(str(repo_root / "data" / "portfolio.db"))
        sources_registry.set_db_path(repo_root / "data" / "portfolio.db")

    try:
        conn = db.get_connection()
    except sqlite3.Error as e:
        print(f"DB unavailable: {_redact(e)}", file=sys.stderr)
        return 1
    try:
        tickers, skipped = _select_tickers_with_skips(conn, args.ticker)
        if skipped:
            print(
                json.dumps(
                    {
                        "event": "expected_earnings_skipped_ineligible",
                        "count": len(skipped),
                        "skipped": skipped,
                    }
                ),
                file=sys.stderr,
            )
        if not tickers:
            print(json.dumps({"status": "ok", "tickers": 0, "skipped_ineligible": len(skipped)}))
            return 0
        summary = refresh_tickers(repo_root, tickers, conn)
    finally:
        conn.close()
    print(json.dumps({"status": "ok", **summary, "skipped_ineligible": len(skipped)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
