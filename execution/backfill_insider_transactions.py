"""Backfill insider transactions across the portfolio + watchlist.

Fetches Form 4 / Form 5 / Form 144 filings from SEC EDGAR for every active
ticker (portfolio + watchlist), starting from --since. SEC EDGAR is rate-
limited to 10 req/sec; we serialize per-ticker but parallelize across
tickers conservatively (4-wide default) to stay under the limit.

For ADR / foreign issuers, US Form 4 coverage is sparse but real (US-person
insiders still file). Coverage gaps that surface in the dashboard later will
inform whether the Canadian SEDI / UK PDMR / Indian SEBI adapters are worth
shipping. Phase 5a ships SEC primary; foreign-regime adapters are documented
follow-ups.

Usage:
    python execution/backfill_insider_transactions.py --since 2020-01-01
    python execution/backfill_insider_transactions.py --since 2024-01-01 --tickers AMZN,GOOG,META
    python execution/backfill_insider_transactions.py --since 2024-01-01 --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from insider_transactions import (  # noqa: E402
    SEC_USER_AGENT_DEFAULT,
    fetch_form4_from_edgar,
    upsert,
)

log = logging.getLogger("backfill_insider")


def _load_tracked_tickers(repo_root: Path) -> list[tuple[str, str]]:
    """Return [(ticker, list_type)] for every active tracked company.
    Excludes index_member / etf / none."""
    import sqlite3

    db = repo_root / "data" / "portfolio.db"
    if not db.exists():
        log.error({"event": "db_missing", "path": str(db)})
        return []
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            """
            SELECT ticker, list_type FROM tracked_companies
            WHERE archived_at IS NULL
              AND list_type IN ('portfolio', 'watchlist', 'evaluation')
            ORDER BY list_type, ticker
            """
        ).fetchall()
        return [(r[0], r[1]) for r in rows]
    finally:
        conn.close()


def _backfill_one(
    ticker: str,
    *,
    since: datetime,
    until: datetime,
    user_agent: str,
    db_path: Path,
    dry_run: bool,
) -> tuple[str, int, int, str | None]:
    """Backfill a single ticker. Returns (ticker, fetched_count, inserted_count, error)."""
    try:
        txs = fetch_form4_from_edgar(
            ticker=ticker, since=since, until=until, user_agent=user_agent
        )
        if dry_run:
            return (ticker, len(txs), 0, None)
        inserted = upsert(txs, db_path=db_path)
        return (ticker, len(txs), inserted, None)
    except Exception as exc:  # noqa: BLE001 — log + continue across tickers
        return (ticker, 0, 0, f"{type(exc).__name__}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        type=lambda s: datetime.fromisoformat(s).replace(tzinfo=UTC),
        default=datetime(2020, 1, 1, tzinfo=UTC),
        help="Lookback start date (YYYY-MM-DD). Default 2020-01-01.",
    )
    parser.add_argument(
        "--until",
        type=lambda s: datetime.fromisoformat(s).replace(tzinfo=UTC),
        default=None,
        help="End date (YYYY-MM-DD). Default: now.",
    )
    parser.add_argument(
        "--tickers",
        default=None,
        help="Comma-separated tickers (overrides DB lookup; useful for one-off backfills).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/portfolio.db.",
    )
    parser.add_argument(
        "--user-agent",
        default=os.environ.get("EDGAR_USER_AGENT", SEC_USER_AGENT_DEFAULT),
        help="User-Agent for SEC EDGAR. Set EDGAR_USER_AGENT in env (recommended).",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=4,
        help="Parallel tickers (default 4). SEC rate-limits at 10 req/sec; "
        "each ticker uses ~2-5 req/min so 4 wide is conservative.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch + parse only; don't write to DB.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    db_path = args.repo_root / "data" / "portfolio.db"

    if args.tickers:
        targets = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        cohorts = [("override", targets)]
    else:
        tracked = _load_tracked_tickers(args.repo_root)
        if not tracked:
            log.error({"event": "no_tracked_companies"})
            return 1
        # Split by list_type so the operator can see progress per cohort
        cohorts_d: dict[str, list[str]] = {}
        for ticker, list_type in tracked:
            cohorts_d.setdefault(list_type, []).append(ticker)
        cohorts = list(cohorts_d.items())

    until = args.until or datetime.now(UTC)
    total_fetched = 0
    total_inserted = 0
    errors: list[tuple[str, str]] = []

    for cohort_name, tickers in cohorts:
        log.info(
            {"event": "backfill_cohort_start", "cohort": cohort_name, "n_tickers": len(tickers)}
        )
        with ThreadPoolExecutor(max_workers=args.max_workers) as ex:
            futures = {
                ex.submit(
                    _backfill_one,
                    t,
                    since=args.since,
                    until=until,
                    user_agent=args.user_agent,
                    db_path=db_path,
                    dry_run=args.dry_run,
                ): t
                for t in tickers
            }
            for fut in as_completed(futures):
                t = futures[fut]
                ticker, fetched, inserted, err = fut.result()
                if err:
                    errors.append((ticker, err))
                    log.warning({"event": "ticker_error", "ticker": ticker, "error": err})
                else:
                    total_fetched += fetched
                    total_inserted += inserted
                    log.info(
                        {
                            "event": "ticker_done",
                            "ticker": ticker,
                            "fetched": fetched,
                            "inserted": inserted,
                        }
                    )

    log.info(
        {
            "event": "backfill_complete",
            "total_fetched": total_fetched,
            "total_inserted": total_inserted,
            "errors": len(errors),
            "dry_run": args.dry_run,
        }
    )
    if errors:
        log.warning({"event": "errors_summary", "errors": errors[:20]})
    return 0 if not errors else 0  # don't fail the run on per-ticker errors


if __name__ == "__main__":
    sys.exit(main())
