"""Fetch quarterly financial-reports JSONs from FMP for the tracked book.

For each tracked ticker (excluding ETFs), year (default 2018-current), and
quarter (Q1/Q2/Q3 — Q4 lives in the 10-K annual file), call FMP's
stable/financial-reports-json endpoint and save to
`data/historical/fmp/{TICKER}_form_10q_{YEAR}_{QUARTER}.json`.

Skips fetches whose target file already exists, so re-running fills only
the gaps. Use --tickers to scope to a subset, or pass nothing to walk all
classified tickers.

After fetching, indexes every touched ticker's on-disk FMP files into the
``documents`` table via ``pipeline.fmp_doc_index.index_fmp_files_for_ticker``
(idempotent, INSERT OR IGNORE on sha256) — this is the fix for the
``fmp_10q_json`` documents-registration gap: files fetched here previously
never got a ``documents`` row, since only ``onboard_ticker.py`` /
``fmp_backpop.py`` called the indexer. Pass --no-index to skip (e.g. for a
dry data-only fetch).

Usage:
    python execution/fetch_fmp_10q_json.py
    python execution/fetch_fmp_10q_json.py --tickers GOOG,AMZN,META
    python execution/fetch_fmp_10q_json.py --start-year 2022
    python execution/fetch_fmp_10q_json.py --db-path C:/path/to/data/portfolio.db
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from net.client import (  # noqa: E402
    FMP_CLIENT,
    HttpCallError,
    JsonShape,
    JsonValue,
    RetryPolicy,
)
from pipeline.fmp_doc_index import index_fmp_files_for_ticker  # noqa: E402
from runtime.secrets import load_project_env, project_env_file  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

# .env is loaded lazily in main(), AFTER --db-path is known — when running
# from a worktree against the main checkout's DB (data/ is gitignored and a
# fresh worktree has no .env of its own), the secret lives next to the
# passed --db-path, not next to this script's own PROJECT_ROOT.
API_KEY: str | None = None

# Data files (cached FMP payloads, the DB) live alongside the DB, not
# alongside the code — running this from a worktree against the main
# checkout's DB is the normal case here (see docs/design/
# filing_longitudinal_language.md verification notes). --db-path overrides
# both DB_PATH and FMP_DIR/PROJECT_ROOT_DATA to the passed DB's siblings.
PROJECT_ROOT_DATA = PROJECT_ROOT
FMP_DIR = PROJECT_ROOT / "data" / "historical" / "fmp"
DB_PATH = PROJECT_ROOT / "data" / "portfolio.db"
DELAY_S = 0.10
TIMEOUT = (10, 60)
QUARTERS: tuple[str, ...] = ("Q1", "Q2", "Q3")
DEFAULT_START_YEAR = 2018

_FMP_RETRY = RetryPolicy(max_attempts=3, backoff_base_s=5.0)


def _fetch_once(
    symbol: str, year: int, quarter: str
) -> tuple[int, dict[str, JsonValue] | list[JsonValue] | None, str | None]:
    """One FMP call with 429 backoff. Returns (http_code, body, error_msg)."""
    try:
        response = FMP_CLIENT.get_json(
            "financial-reports-json",
            params={"symbol": symbol, "year": year, "period": quarter},
            api_key=API_KEY,
            expected=JsonShape.ANY,
            timeout=TIMEOUT,
            retry=_FMP_RETRY,
        )
        code = response.status_code
        body = response.payload
    except HttpCallError as exc:
        code = exc.status_code or 0
        body = exc.payload
        if isinstance(body, dict) and "Error Message" in body:
            return (code, None, f"fmp-err: {str(body['Error Message'])[:200]}")
        prefix = "network" if code == 0 else "http"
        return (code, None, f"{prefix}: {exc}")
    if isinstance(body, dict) and "Error Message" in body:
        return (code, None, f"fmp-err: {str(body['Error Message'])[:200]}")
    if not isinstance(body, (dict, list)):
        return (code, None, f"unexpected-json: {type(body).__name__}")
    return (code, body, None)


def _resolve_tickers(arg_tickers: str | None) -> list[str]:
    if arg_tickers:
        return [t.strip().upper() for t in arg_tickers.split(",") if t.strip()]
    conn = connect_sqlite(str(DB_PATH), role=SQLiteConnectionRole.READ_ONLY)
    cur = conn.cursor()
    cur.execute(
        "SELECT ticker FROM tracked_companies "
        "WHERE instrument_type IS NOT NULL AND instrument_type != 'etf' "
        "ORDER BY ticker"
    )
    tickers = [r[0] for r in cur.fetchall()]
    conn.close()
    return tickers


def _is_real_payload(body: dict[str, JsonValue] | list[JsonValue] | None) -> bool:
    """Distinguish real 10-Q content from FMP's empty / metadata-only sentinels."""
    if not isinstance(body, dict):
        return False
    if len(body) <= 3:
        return False
    return any(
        k.lower().startswith(("segment", "consolidated balance")) or "balance sheet" in k.lower()
        for k in body
    )


def main() -> int:
    global FMP_DIR, DB_PATH, PROJECT_ROOT_DATA, API_KEY

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", help="Comma-separated tickers (default: all classified)")
    parser.add_argument("--start-year", type=int, default=DEFAULT_START_YEAR)
    parser.add_argument(
        "--end-year",
        type=int,
        default=date.today().year,
        help="Inclusive upper bound (default: current year)",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help=(
            "Portfolio DB path override. Re-derives FMP_DIR from the DB's "
            "parent so this can run from a worktree against the main "
            "checkout's data/ (gitignored, not present in a fresh worktree)."
        ),
    )
    parser.add_argument(
        "--no-index",
        action="store_true",
        help="Skip the post-fetch documents-table indexing step",
    )
    args = parser.parse_args()

    if args.db_path:
        DB_PATH = Path(args.db_path)
        PROJECT_ROOT_DATA = DB_PATH.parent.parent
        FMP_DIR = DB_PATH.parent / "historical" / "fmp"

    load_project_env(PROJECT_ROOT_DATA)
    API_KEY = os.environ.get("FMP_API_KEY")
    if not API_KEY:
        sys.stderr.write(f"FATAL: FMP_API_KEY not set in {project_env_file(PROJECT_ROOT_DATA)}\n")
        return 1

    tickers = _resolve_tickers(args.tickers)
    if not tickers:
        sys.stderr.write("No tickers resolved\n")
        return 1

    FMP_DIR.mkdir(parents=True, exist_ok=True)
    fetched = 0
    skipped_existing = 0
    empty = 0
    failed = 0

    for ticker in tickers:
        for year in range(args.start_year, args.end_year + 1):
            for q in QUARTERS:
                fname = f"{ticker}_form_10q_{year}_{q}.json"
                path = FMP_DIR / fname
                if path.exists():
                    skipped_existing += 1
                    continue
                time.sleep(DELAY_S)
                code, body, err = _fetch_once(ticker, year, q)
                if code != 200 or body is None:
                    failed += 1
                    sys.stderr.write(f"  FAIL {ticker} {year} {q}: HTTP {code} {err or ''}\n")
                    continue
                if not _is_real_payload(body):
                    empty += 1
                    continue
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(body, f)
                fetched += 1
                if fetched % 25 == 0:
                    print(f"  fetched={fetched} (last: {ticker} {year} {q})", flush=True)

    indexed = 0
    if not args.no_index:
        conn = connect_sqlite(str(DB_PATH), role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        try:
            for ticker in tickers:
                indexed += index_fmp_files_for_ticker(conn, ticker, PROJECT_ROOT_DATA)
        finally:
            conn.close()

    print(
        json.dumps(
            {
                "tickers": len(tickers),
                "fetched": fetched,
                "skipped_existing": skipped_existing,
                "empty_or_unavailable": empty,
                "failed": failed,
                "documents_indexed": indexed,
            },
            indent=2,
        )
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
