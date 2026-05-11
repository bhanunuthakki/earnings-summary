"""Earnings-calendar surrogate webhook: schedule pre/post-earnings refreshes.

FMP doesn't push events when fundamentals change. Instead, we poll the FMP
earnings calendar daily (1 call/day, fits even Basic), find portfolio +
watchlist tickers reporting in the next 7 days, and forcibly mark their
time-sensitive endpoints as "stale" so the cacher's next run pulls them
ahead of the print, and again the day after.

Mechanism: writes "force-stale" hints to `.tmp/cacher/forced_stale.json`,
which the cacher merges into its audit. Hint TTL is 14 days (covers pre +
post window with margin).

Usage:
    python execution/schedule_pre_earnings_refresh.py            # poll + schedule
    python execution/schedule_pre_earnings_refresh.py --dry-run  # report only

This script is called from the cacher's daily startup hook OR run manually.
The hint file is read on every audit; if it doesn't exist, audit proceeds
normally with cadence-only staleness.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db as portfolio_db  # noqa: E402
from log_redact import redact as _redact  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")
API_KEY = os.environ.get("FMP_API_KEY")

CACHE_DIR = PROJECT_ROOT / ".tmp" / "cacher"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
HINTS_PATH = CACHE_DIR / "forced_stale.json"

# Days before earnings to schedule pre-print refresh
LOOKAHEAD_DAYS = 7
# Days after earnings to keep the post-print refresh hint live
HINT_TTL_DAYS = 14


def _watched_tickers() -> set[str]:
    """Active portfolio + watchlist tickers (not archived)."""
    conn = portfolio_db.get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT ticker FROM tracked_companies "
        "WHERE list_type IN ('portfolio', 'watchlist') AND archived_at IS NULL"
    )
    out = {row[0] for row in cur.fetchall()}
    conn.close()
    return out


def _fetch_earnings_calendar(start: date, end: date) -> list[dict[str, str]]:
    """Hit FMP /earnings-calendar from start..end. Returns [{symbol, date}, ...].

    1 API call. Free on Basic+ plans.
    """
    if not API_KEY:
        raise RuntimeError("FMP_API_KEY not set in .env")
    url = "https://financialmodelingprep.com/stable/earnings-calendar"
    params = {"from": start.isoformat(), "to": end.isoformat(), "apikey": API_KEY}
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"FMP earnings-calendar fetch failed: {_redact(e)}") from None
    body = r.json()
    if not isinstance(body, list):
        raise RuntimeError(f"unexpected calendar shape: {type(body).__name__}")
    return [{"symbol": e.get("symbol", "").upper(), "date": e.get("date", "")} for e in body]


def _load_hints() -> dict[str, str]:
    """Map ticker -> hint_expires_at (ISO timestamp)."""
    if not HINTS_PATH.exists():
        return {}
    try:
        return json.loads(HINTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_hints(hints: dict[str, str]) -> None:
    HINTS_PATH.write_text(json.dumps(hints, indent=2, sort_keys=True), encoding="utf-8")


def _prune_expired(hints: dict[str, str], now: datetime) -> dict[str, str]:
    return {
        t: exp for t, exp in hints.items()
        if datetime.fromisoformat(exp) > now
    }


def schedule(*, dry_run: bool = False) -> dict[str, object]:
    """Run one polling pass; return summary dict."""
    now = datetime.now()
    today = now.date()
    end = today + timedelta(days=LOOKAHEAD_DAYS)

    watched = _watched_tickers()
    if not watched:
        return {"watched": 0, "hints": 0, "matches": []}

    calendar = _fetch_earnings_calendar(today, end)
    matches = [e for e in calendar if e["symbol"] in watched]

    hints = _prune_expired(_load_hints(), now)
    new_hints: list[dict[str, str]] = []
    for m in matches:
        ticker = m["symbol"]
        # Hint expires HINT_TTL_DAYS after earnings date
        try:
            earnings_dt = datetime.fromisoformat(m["date"])
        except ValueError:
            continue
        expires = earnings_dt + timedelta(days=HINT_TTL_DAYS)
        # Only update if new expiry is later than existing
        existing = hints.get(ticker)
        if existing is None or datetime.fromisoformat(existing) < expires:
            hints[ticker] = expires.isoformat(timespec="seconds")
            new_hints.append({"ticker": ticker, "earnings_date": m["date"], "hint_expires": hints[ticker]})

    if not dry_run:
        _save_hints(hints)

    return {
        "watched": len(watched),
        "calendar_window": [today.isoformat(), end.isoformat()],
        "calendar_entries": len(calendar),
        "matches": [m for m in matches],
        "new_or_updated_hints": new_hints,
        "active_hints_total": len(hints),
        "dry_run": dry_run,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Hit calendar API but don't write hints")
    args = ap.parse_args()
    summary = schedule(dry_run=args.dry_run)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
