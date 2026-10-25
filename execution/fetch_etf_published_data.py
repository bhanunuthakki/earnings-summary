"""Refresh one ETF's published data: N-PORT holdings, issuer overlay, prices.

The evaluation lane's data fetch for ETF instruments (directives/etf_data.md).
Thin CLI over ``etf_sources.ingest.refresh_published_data``:

  1. SEC N-PORT (EDGAR)  — full holdings + per-constituent country. Idempotent
     on (ticker, rep_period_date): re-running against an already-ingested
     report is an explicit "already done". Schema drift halts loudly with the
     raw XML dumped to .tmp/etf_nport/.
  2. Issuer overlay      — fresher holdings / basket characteristics when the
     ticker's issuer adapter exists (etf_sources/issuer_registry.py). Soft.
  3. Price history       — yfinance dividend-adjusted closes into
     data/factor_proxies/<T>.json when the FMP price cache has nothing.
  4. FMP enrichment      — optional (--fmp): the legacy /stable/etf endpoints
     for expense ratio / AUM when the plan allows. Failure tolerated.

Usage:
    python execution/fetch_etf_published_data.py --ticker AVDV
    python execution/fetch_etf_published_data.py --ticker VWO --force
    python execution/fetch_etf_published_data.py --ticker AVUV --fmp
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from db_paths import require_db_path
from etf_sources.ingest import PublishedDataResult, refresh_published_data
from etf_sources.nport import NportParseError
from log_redact import redact
from operations.paths import configured_product_state_root
from runtime.secrets import load_project_env
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def print_result(result: PublishedDataResult) -> None:
    as_of = result.nport_as_of.isoformat() if result.nport_as_of else "-"
    issuer_as_of = result.issuer_as_of.isoformat() if result.issuer_as_of else "-"
    print(
        f"[etf] {result.ticker} nport={result.nport_status} as_of={as_of} rows={result.nport_rows}",
        flush=True,
    )
    print(
        f"[etf] {result.ticker} issuer={result.issuer_status} "
        f"as_of={issuer_as_of} rows={result.issuer_rows} "
        f"characteristics={'yes' if result.characteristics_applied else 'no'}",
        flush=True,
    )
    print(
        f"[etf] {result.ticker} prices={result.price_status} rows={result.price_rows}",
        flush=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ticker", required=True, help="ETF ticker (e.g. AVDV)")
    ap.add_argument("--db", type=Path, help="Existing database in the product state root")
    ap.add_argument(
        "--repo-root", type=Path, help="Product state root; otherwise use configured authority"
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Re-ingest even when the newest N-PORT report / price series is already on file",
    )
    ap.add_argument(
        "--skip-prices",
        action="store_true",
        help="Skip the yfinance price-history leg",
    )
    ap.add_argument(
        "--fmp",
        action="store_true",
        help="Also run the legacy FMP /stable/etf enrichment (requires FMP_API_KEY; "
        "402s on plan-gated symbols are tolerated)",
    )
    args = ap.parse_args()
    load_project_env(PROJECT_ROOT)
    ticker = args.ticker.upper()

    state_root = (
        args.repo_root.expanduser().resolve()
        if args.repo_root
        else configured_product_state_root(PROJECT_ROOT)
    )
    database = require_db_path(args.db or state_root / "data" / "portfolio.db")
    if database != (state_root / "data" / "portfolio.db").resolve():
        raise ValueError("ETF database must match the product state root/data/portfolio.db")
    conn = connect_sqlite(database, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    try:
        try:
            result = refresh_published_data(
                conn,
                ticker,
                state_root,
                force=args.force,
                skip_prices=args.skip_prices,
            )
        except NportParseError as exc:
            conn.rollback()
            # Schema drift: the directive needs updating, not a retry.
            print(f"[etf] {ticker} NPORT PARSE HALT: {exc}", flush=True)
            return 2
        conn.commit()
        print_result(result)

        if args.fmp:
            from fetch_etf_data import ingest_live  # same-directory script import

            try:
                profile, n = ingest_live(conn, ticker, state_root)
                conn.commit()
                print(
                    f"[etf] {ticker} fmp_enrichment ok issuer={profile.issuer or '?'} "
                    f"holdings_rows={n}",
                    flush=True,
                )
            except (RuntimeError, ValueError, OSError, sqlite3.Error) as exc:
                conn.rollback()
                print(f"[etf] {ticker} fmp_enrichment unavailable: {redact(exc)}", flush=True)
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

    if result.nport_status == "unavailable" and result.issuer_status == "unavailable":
        print(
            f"[etf] {ticker} WARNING: no holdings source succeeded — "
            f"look-through analytics will degrade until one does",
            flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
