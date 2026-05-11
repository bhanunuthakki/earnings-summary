"""Scan the FMP earnings-calendar cache and populate `expected_earnings`.

Daily cron entry point. Reads each tracked ticker's
`data/historical/fmp/<TICKER>_earnings_calendar.json` cache (refreshed by the
standard FMP fetch cron) and upserts the events in a configurable window
into the `expected_earnings` table — the queue the daily fetch+brief worker
drains.

Window defaults to [today − 30d, today + 14d]: forward enough to see what's
coming this week, back enough to catch any earnings whose facts haven't
shown up yet.

Usage:
    python execution/earnings_calendar_watcher.py             # portfolio + watchlist
    python execution/earnings_calendar_watcher.py --ticker META
    python execution/earnings_calendar_watcher.py --forward-days 21 --backward-days 60
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402

CALENDAR_FILE_SUFFIX = "_earnings_calendar.json"
DEFAULT_FORWARD_DAYS = 14
DEFAULT_BACKWARD_DAYS = 30
DETECTED_SOURCE = "fmp"


def main() -> int:
    args = _parse_args()
    repo_root = args.repo_root.resolve()
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        sys.stderr.write(f"FATAL: no DB at {db_path}\n")
        return 2

    tickers = _resolve_tickers(repo_root, args)
    if not tickers:
        print(json.dumps({"event": "no_tickers", "detail": "no tracked companies in scope"}))
        return 0

    window_start = date.today() - timedelta(days=args.backward_days)
    window_end = date.today() + timedelta(days=args.forward_days)
    fmp_dir = repo_root / "data" / "historical" / "fmp"

    inserted = 0
    refreshed = 0
    no_file = 0
    no_events_in_window = 0
    with sqlite3.connect(str(db_path)) as conn:
        for ticker in tickers:
            path = fmp_dir / f"{ticker}{CALENDAR_FILE_SUFFIX}"
            events = _load_calendar_events(path)
            if events is None:
                no_file += 1
                continue
            in_window = [e for e in events if _is_in_window(e, window_start, window_end)]
            if not in_window:
                no_events_in_window += 1
                continue
            for event in in_window:
                is_new = _upsert_event(conn, ticker, event)
                if is_new:
                    inserted += 1
                else:
                    refreshed += 1
        conn.commit()

    summary = {
        "tickers_scanned": len(tickers),
        "rows_inserted": inserted,
        "rows_refreshed": refreshed,
        "tickers_no_calendar_file": no_file,
        "tickers_no_events_in_window": no_events_in_window,
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
    }
    print(json.dumps(summary, indent=2))
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--ticker", help="Single ticker to scan (overrides default portfolio+watchlist scope)"
    )
    p.add_argument(
        "--forward-days",
        type=int,
        default=DEFAULT_FORWARD_DAYS,
        help=f"Days into the future to include. Default {DEFAULT_FORWARD_DAYS}.",
    )
    p.add_argument(
        "--backward-days",
        type=int,
        default=DEFAULT_BACKWARD_DAYS,
        help=(
            f"Days into the past to include — catches earnings whose facts "
            f"haven't been ingested yet. Default {DEFAULT_BACKWARD_DAYS}."
        ),
    )
    p.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/, micro_thesis/. Default: this repo.",
    )
    return p.parse_args()


def _resolve_tickers(repo_root: Path, args: argparse.Namespace) -> list[str]:
    if args.ticker:
        return [args.ticker.upper()]
    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.DB_PATH = str(repo_root / "data" / "portfolio.db")
    rows = db.get_tracked_companies()
    return [str(r["ticker"]).upper() for r in rows if r["list_type"] in ("portfolio", "watchlist")]


def _load_calendar_events(path: Path) -> list[dict[str, object]] | None:
    """Read the FMP earnings calendar JSON cache for a ticker.

    Returns None if the file is missing or malformed (caller skips). FMP's
    schema is a list of `{symbol, date, epsEstimated, ...}` records.
    """
    if not path.exists():
        return None
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, list):
        return None
    records: list[object] = cast("list[object]", payload)
    out: list[dict[str, object]] = []
    for raw in records:
        if isinstance(raw, dict):
            out.append(cast("dict[str, object]", raw))
    return out


def _is_in_window(event: dict[str, object], start: date, end: date) -> bool:
    raw = event.get("date")
    if not isinstance(raw, str) or len(raw) < 10:
        return False
    try:
        ev_date = date.fromisoformat(raw[:10])
    except ValueError:
        return False
    return start <= ev_date <= end


def _upsert_event(conn: sqlite3.Connection, ticker: str, event: dict[str, object]) -> bool:
    """Insert a new expected_earnings row or refresh last_seen_at on an existing one.

    Returns True if a new row was inserted, False if an existing row was refreshed.
    """
    expected_date = str(event.get("date"))[:10]
    label = _quarter_label(expected_date)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM expected_earnings WHERE ticker = ? AND expected_date = ?",
        (ticker, expected_date),
    )
    existing = cursor.fetchone()
    if existing is not None:
        cursor.execute(
            "UPDATE expected_earnings "
            "SET last_seen_at = CURRENT_TIMESTAMP, "
            "    fiscal_period_label = COALESCE(fiscal_period_label, ?) "
            "WHERE id = ?",
            (label, existing[0]),
        )
        return False
    cursor.execute(
        "INSERT INTO expected_earnings "
        "(ticker, expected_date, fiscal_period_label, detected_source) "
        "VALUES (?, ?, ?, ?)",
        (ticker, expected_date, label, DETECTED_SOURCE),
    )
    return True


def _quarter_label(iso_date: str) -> str:
    """Heuristic Q-label from the earnings date: maps the calendar quarter the
    report typically covers (e.g. an early-May earnings → 'Q1' of that year).
    """
    try:
        d = date.fromisoformat(iso_date[:10])
    except ValueError:
        return ""
    month = d.month
    # Earnings reports usually cover the prior fiscal quarter (e.g. May → Q1).
    # Use a shift heuristic: report in (Feb,May,Aug,Nov) → (Q4 prior, Q1, Q2, Q3).
    if month in (1, 2, 3):
        return f"Q4 {d.year - 1}"
    if month in (4, 5, 6):
        return f"Q1 {d.year}"
    if month in (7, 8, 9):
        return f"Q2 {d.year}"
    return f"Q3 {d.year}"


if __name__ == "__main__":
    raise SystemExit(main())
