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

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
from expected_earnings import record_next_earnings  # noqa: E402
from log_redact import redact as _redact  # noqa: E402
from sources import registry as sources_registry  # noqa: E402
from sources.earnings_calendar import next_earnings_date  # noqa: E402

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


def _select_tickers(ticker: str | None) -> list[str]:
    if ticker:
        return [ticker.upper()]
    companies = db.get_tracked_companies()
    return sorted(str(c["ticker"]) for c in companies if c["list_type"] in db.ACTIVE_LIST_TYPES)


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh the expected_earnings table")
    parser.add_argument("--ticker", help="Single ticker (default: active universe)")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo whose data/ + portfolio.db to use (default: this checkout)",
    )
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    if repo_root != PROJECT_ROOT:
        db.set_db_path(str(repo_root / "data" / "portfolio.db"))
        sources_registry.set_db_path(repo_root / "data" / "portfolio.db")

    tickers = _select_tickers(args.ticker)
    if not tickers:
        print(json.dumps({"status": "ok", "tickers": 0}))
        return 0

    try:
        conn = db.get_connection()
    except sqlite3.Error as e:
        print(f"DB unavailable: {_redact(e)}", file=sys.stderr)
        return 1
    try:
        summary = refresh_tickers(repo_root, tickers, conn)
    finally:
        conn.close()
    print(json.dumps({"status": "ok", **summary}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
