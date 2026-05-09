"""Load S&P 500 and Russell 2000 constituents into tracked_companies.

Sources:
  - S&P 500: Wikipedia "List of S&P 500 companies" (table 0; Symbol + Security)
  - Russell 2000: ikoniaris/Russell2000 GitHub mirror (Ticker + Name)

Behavior:
  - Upserts each constituent with list_type='index_member'.
  - Refuses to downgrade tickers already classified as portfolio/watchlist —
    those rows are left untouched (the user analyzes them; index status is
    recorded only via membership tags inside instrument_type, not list_type).
  - Idempotent: re-running with --refresh re-upserts; ON CONFLICT updates name.

Usage:
    python execution/load_index_constituents.py --sp500
    python execution/load_index_constituents.py --russell2000
    python execution/load_index_constituents.py --all
    python execution/load_index_constituents.py --all --dry-run

Notes:
  Russell 2000 ticker symbols sometimes carry suffixes (e.g. 'BRK.B' on
  Wikipedia, 'BRK-B' on FMP). FMP uses dashes; we normalize '.' -> '-' on
  load. Other oddities (rights/warrants like 'XYZ.W') are dropped — those
  don't have FMP fundamentals coverage anyway.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models.companies import ListType  # noqa: E402

DB_PATH = PROJECT_ROOT / "data" / "portfolio.db"

SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
RUSSELL_URL = (
    "https://raw.githubusercontent.com/ikoniaris/Russell2000/master/"
    "russell_2000_components.csv"
)
WIKIPEDIA_UA = "earnings-summary/1.0 (constituent loader)"

# FMP-friendly ticker: capital alphanumerics with optional '-' suffix (BRK-B).
# Drop anything with '/', '.W', '.WS', '.U' (rights/warrants/units).
_TICKER_OK = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z])?$")


class LoaderError(Exception):
    """Raised when a constituent source returns unexpected schema or empty data."""


def _normalize(symbol: str) -> str | None:
    s = symbol.strip().upper().replace(".", "-")
    if not _TICKER_OK.match(s):
        return None
    return s


def fetch_sp500() -> list[tuple[str, str]]:
    """Return [(ticker, name), ...] from Wikipedia. Raises LoaderError on bad shape."""
    resp = requests.get(SP500_URL, headers={"User-Agent": WIKIPEDIA_UA}, timeout=20)
    resp.raise_for_status()
    tables = pd.read_html(StringIO(resp.text))
    if not tables:
        raise LoaderError("Wikipedia S&P 500 page returned no tables")
    df = tables[0]
    if "Symbol" not in df.columns or "Security" not in df.columns:
        raise LoaderError(
            f"Wikipedia S&P 500 table missing Symbol/Security columns; got {list(df.columns)}"
        )
    out: list[tuple[str, str]] = []
    for _, row in df.iterrows():
        sym = _normalize(str(row["Symbol"]))
        if sym is None:
            continue
        name = str(row["Security"]).strip()
        out.append((sym, name))
    if len(out) < 400:
        raise LoaderError(f"S&P 500 fetch returned only {len(out)} rows; refusing to load")
    return out


def fetch_russell2000() -> list[tuple[str, str]]:
    """Return [(ticker, name), ...] from the GitHub CSV mirror. Raises on bad shape."""
    resp = requests.get(RUSSELL_URL, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(StringIO(resp.text))
    if "Ticker" not in df.columns or "Name" not in df.columns:
        raise LoaderError(
            f"Russell 2000 CSV missing Ticker/Name columns; got {list(df.columns)}"
        )
    out: list[tuple[str, str]] = []
    for _, row in df.iterrows():
        sym = _normalize(str(row["Ticker"]))
        if sym is None:
            continue
        name = str(row["Name"]).strip()
        out.append((sym, name))
    if len(out) < 1500:
        raise LoaderError(f"Russell 2000 fetch returned only {len(out)} rows; refusing to load")
    return out


def _existing_tickers_by_list_type(conn: sqlite3.Connection) -> dict[str, str]:
    """Map ticker -> list_type for every row in tracked_companies."""
    cur = conn.cursor()
    cur.execute("SELECT ticker, list_type FROM tracked_companies")
    return {row[0]: row[1] for row in cur.fetchall()}


def upsert_index_members(
    conn: sqlite3.Connection,
    rows: list[tuple[str, str]],
    *,
    dry_run: bool,
) -> dict[str, int]:
    """Upsert (ticker, name) as list_type='index_member'; never downgrade analyzed lists."""
    existing = _existing_tickers_by_list_type(conn)
    protected = {ListType.PORTFOLIO.value, ListType.WATCHLIST.value}
    stats = {"inserted": 0, "updated": 0, "skipped_protected": 0}
    cur = conn.cursor()
    for ticker, name in rows:
        if existing.get(ticker) in protected:
            stats["skipped_protected"] += 1
            continue
        if ticker in existing:
            if not dry_run:
                cur.execute(
                    "UPDATE tracked_companies SET name = ?, list_type = ? "
                    "WHERE ticker = ? AND list_type NOT IN (?, ?)",
                    (
                        name,
                        ListType.INDEX_MEMBER.value,
                        ticker,
                        ListType.PORTFOLIO.value,
                        ListType.WATCHLIST.value,
                    ),
                )
            stats["updated"] += 1
        else:
            if not dry_run:
                cur.execute(
                    "INSERT INTO tracked_companies "
                    "(user_id, ticker, name, list_type, added_at) "
                    "VALUES (1, ?, ?, ?, ?)",
                    (
                        ticker,
                        name,
                        ListType.INDEX_MEMBER.value,
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
            stats["inserted"] += 1
    if not dry_run:
        conn.commit()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sp500", action="store_true", help="Load S&P 500 constituents")
    ap.add_argument("--russell2000", action="store_true", help="Load Russell 2000 constituents")
    ap.add_argument("--all", action="store_true", help="Load both indexes")
    ap.add_argument(
        "--db", default=str(DB_PATH), help="Path to portfolio.db (default: data/portfolio.db)"
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="Fetch and report counts without writing"
    )
    args = ap.parse_args()

    if not (args.sp500 or args.russell2000 or args.all):
        ap.print_help()
        return 2

    summary: dict[str, object] = {"dry_run": args.dry_run, "sources": {}}
    rows: list[tuple[str, str]] = []

    if args.sp500 or args.all:
        sp = fetch_sp500()
        summary["sources"]["sp500"] = {"fetched": len(sp)}
        rows.extend(sp)
    if args.russell2000 or args.all:
        ru = fetch_russell2000()
        summary["sources"]["russell2000"] = {"fetched": len(ru)}
        rows.extend(ru)

    # De-duplicate (S&P 500 and R2000 don't overlap by design, but be safe)
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for t, n in rows:
        if t in seen:
            continue
        seen.add(t)
        unique.append((t, n))
    summary["unique_tickers"] = len(unique)

    conn = sqlite3.connect(args.db)
    try:
        stats = upsert_index_members(conn, unique, dry_run=args.dry_run)
    finally:
        conn.close()
    summary["upsert"] = stats

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
