"""Next-earnings date — multi-source preference stack.

Returns the *next* upcoming earnings date (or None). Recent past earnings
dates aren't included here — those live in `transcripts` + `financial_facts`
already.

Priority order:

  1. FMP `data/historical/fmp/<TICKER>_earnings_calendar.json` on-disk cache
     — pulled by execution/fetch_fmp_earnings_calendar.py. Source of truth
     for confirmed dates while subscribed.
  2. yfinance.Ticker(t).calendar — free fallback; returns the next earnings
     date when reportable, otherwise None. Less reliable for foreign issuers
     and OTC tickers but covers the long tail of US names well.

Every attempt writes one row to `source_calls`.

Usage:

    from sources.earnings_calendar import next_earnings_date
    nx = next_earnings_date(repo_root, "GOOG")
    if nx:
        print(nx.expected_date, nx.source_name)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from sources.registry import CallStatus, log_call


@dataclass(frozen=True)
class NextEarnings:
    """One upcoming earnings observation."""

    expected_date: date
    source_name: str  # "fmp_cache" | "yfinance"
    confirmed: bool   # True if the source treats it as confirmed (vs estimated)


def next_earnings_date(repo_root: Path, ticker: str) -> NextEarnings | None:
    """Try sources in priority order; first hit wins."""
    ticker = ticker.upper()
    fmp = _try_fmp_cache(repo_root, ticker)
    if fmp is not None:
        return fmp
    return _try_yfinance(ticker)


def _try_fmp_cache(repo_root: Path, ticker: str) -> NextEarnings | None:
    """Pick the first event with date >= today from the FMP cache file."""
    started = time.monotonic()
    path = repo_root / "data" / "historical" / "fmp" / f"{ticker}_earnings_calendar.json"
    if not path.exists():
        log_call(
            source_name="fmp_cache",
            kind="earnings_calendar",
            ticker=ticker,
            status=CallStatus.NOT_FOUND,
            notes="earnings_calendar.json missing",
        )
        return None

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log_call(
            source_name="fmp_cache",
            kind="earnings_calendar",
            ticker=ticker,
            status=CallStatus.ERROR,
            notes=f"{type(e).__name__}: {e}"[:256],
        )
        return None

    if not isinstance(payload, list):
        log_call(
            source_name="fmp_cache",
            kind="earnings_calendar",
            ticker=ticker,
            status=CallStatus.NOT_FOUND,
            notes="payload not a list",
        )
        return None

    today = date.today()
    upcoming: list[date] = []
    for rec in payload:
        if not isinstance(rec, dict):
            continue
        ds = rec.get("date") or rec.get("fiscalDateEnding")
        if not isinstance(ds, str):
            continue
        try:
            d = date.fromisoformat(ds[:10])
        except ValueError:
            continue
        if d >= today:
            upcoming.append(d)

    if not upcoming:
        log_call(
            source_name="fmp_cache",
            kind="earnings_calendar",
            ticker=ticker,
            status=CallStatus.NOT_FOUND,
            latency_ms=int((time.monotonic() - started) * 1000),
            notes="no future dates in cache",
        )
        return None

    nxt = min(upcoming)
    log_call(
        source_name="fmp_cache",
        kind="earnings_calendar",
        ticker=ticker,
        status=CallStatus.OK,
        latency_ms=int((time.monotonic() - started) * 1000),
        record_count=1,
        notes=f"date={nxt.isoformat()}",
    )
    return NextEarnings(expected_date=nxt, source_name="fmp_cache", confirmed=True)


def _try_yfinance(ticker: str) -> NextEarnings | None:
    """Pull from yfinance.Ticker(t).calendar. Returns None on miss or error."""
    started = time.monotonic()
    try:
        import yfinance as yf  # type: ignore[import-untyped]
    except ImportError:
        log_call(
            source_name="yfinance",
            kind="earnings_calendar",
            ticker=ticker,
            status=CallStatus.ERROR,
            notes="yfinance not installed",
        )
        return None

    try:
        tkr = yf.Ticker(ticker)
        cal = getattr(tkr, "calendar", None)
        # yfinance returns a dict {"Earnings Date": [datetime,...], ...} OR
        # a DataFrame depending on version. Normalize both.
        candidates: list[date] = []
        if isinstance(cal, dict):
            raw = cal.get("Earnings Date") or cal.get("earningsDate") or []
            if isinstance(raw, list):
                for v in raw:
                    if isinstance(v, datetime):
                        candidates.append(v.date())
                    elif isinstance(v, date):
                        candidates.append(v)
        else:
            # DataFrame path (older yfinance versions)
            try:
                values = list(getattr(cal, "values", lambda: [])())  # type: ignore[arg-type]
                for row in values:
                    for v in row:
                        if isinstance(v, datetime):
                            candidates.append(v.date())
            except Exception:  # noqa: BLE001
                pass

        today = date.today()
        upcoming = [d for d in candidates if d >= today]
        if not upcoming:
            log_call(
                source_name="yfinance",
                kind="earnings_calendar",
                ticker=ticker,
                status=CallStatus.NOT_FOUND,
                latency_ms=int((time.monotonic() - started) * 1000),
                notes="no future earnings date",
            )
            return None

        nxt = min(upcoming)
        log_call(
            source_name="yfinance",
            kind="earnings_calendar",
            ticker=ticker,
            status=CallStatus.OK,
            latency_ms=int((time.monotonic() - started) * 1000),
            record_count=1,
            notes=f"date={nxt.isoformat()}",
        )
        # yfinance dates can be estimates pre-confirmation; flag as unconfirmed.
        return NextEarnings(expected_date=nxt, source_name="yfinance", confirmed=False)
    except Exception as e:  # noqa: BLE001
        log_call(
            source_name="yfinance",
            kind="earnings_calendar",
            ticker=ticker,
            status=CallStatus.ERROR,
            latency_ms=int((time.monotonic() - started) * 1000),
            notes=f"{type(e).__name__}: {e}"[:256],
        )
        return None
