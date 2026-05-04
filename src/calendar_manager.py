"""
calendar_manager
----------------
Reads earnings calendar JSON files produced by
`execution/fetch_fmp_earnings_calendar.py` and exposes the next upcoming
earnings event per ticker for the front-end calendar.

This module performs no network I/O. To refresh data, run the execution script.

Data layout: data/historical/fmp/<TICKER>_earnings_calendar.json
"""

import json
import os
from datetime import date
from typing import TypedDict

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FMP_DIR = os.path.join(PROJECT_ROOT, "data", "historical", "fmp")

# Tickers whose FMP earnings file may live under an alias prefix
# (e.g. user holds GOOG but FMP also publishes under GOOGL)
_FMP_ALIASES: dict[str, list[str]] = {
    "GOOG": ["GOOG", "GOOGL"],
    "GOOGL": ["GOOGL", "GOOG"],
}


class EarningsEvent(TypedDict):
    symbol: str
    date: str
    epsActual: float | None
    epsEstimated: float | None
    revenueActual: float | None
    revenueEstimated: float | None
    lastUpdated: str


def _candidate_paths(ticker: str) -> list[str]:
    prefixes = _FMP_ALIASES.get(ticker.upper(), [ticker.upper()])
    return [os.path.join(FMP_DIR, f"{p}_earnings_calendar.json") for p in prefixes]


def _load_events(ticker: str) -> list[EarningsEvent]:
    for path in _candidate_paths(ticker):
        if os.path.exists(path):
            with open(path, "r") as f:
                events: list[EarningsEvent] = json.load(f)
            # Dedupe on date (FMP occasionally returns duplicates)
            seen: set[str] = set()
            unique: list[EarningsEvent] = []
            for e in events:
                if e["date"] not in seen:
                    seen.add(e["date"])
                    unique.append(e)
            return unique
    return []


def get_next_earnings_event(ticker: str) -> EarningsEvent | None:
    """Returns the soonest upcoming earnings event (date >= today) or None."""
    events = _load_events(ticker)
    today = date.today().isoformat()
    upcoming = [e for e in events if e["date"] >= today]
    if not upcoming:
        return None
    return min(upcoming, key=lambda e: e["date"])


def get_earnings_date(ticker: str) -> str | None:
    """Returns the next upcoming earnings date as YYYY-MM-DD, or None."""
    event = get_next_earnings_event(ticker)
    return event["date"] if event else None
