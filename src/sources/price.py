"""Live equity price — multi-source preference stack.

Priority order (first hit wins):

  1. yfinance.Ticker(t).fast_info / .info — real-time-ish quote, free,
     unauthenticated, 60/min soft limit per host. Returns most recent trade
     price.
  2. FMP `data/historical/fmp/<TICKER>_profile.json` cache — what
     `src/dcf/live_price.py` used pre-stack. Price is as-of last FMP cycle
     (24h for portfolio/eval/watchlist).

Every attempt writes one row to `source_calls` for future routing decisions.

Usage:

    from sources.price import read_live_price
    lp = read_live_price(repo_root, "GOOG")
    if lp:
        print(lp.price, lp.fetched_at, lp.source_name)
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from sources.registry import CallStatus, log_call


@dataclass(frozen=True)
class LivePrice:
    """A single price observation. `source_name` identifies which adapter won."""

    price: float
    fetched_at: datetime  # UTC
    source_name: str  # "yfinance" | "fmp_cache"
    currency: str | None = None  # explicit exchange/adapter declaration; never assumed


def read_live_price(repo_root: Path, ticker: str) -> LivePrice | None:
    """Try sources in priority order; return the first success."""
    ticker = ticker.upper()
    yf_result = _try_yfinance(ticker)
    if yf_result is not None:
        return yf_result
    fmp_result = _try_fmp_cache(repo_root, ticker)
    if fmp_result is not None:
        return fmp_result
    return None


def _try_yfinance(ticker: str) -> LivePrice | None:
    """Pull the latest trade price from yfinance. Returns None on miss or error."""
    started = time.monotonic()
    try:
        import yfinance as yf  # type: ignore[import-untyped]
    except ImportError:
        log_call(
            source_name="yfinance",
            kind="live_price",
            ticker=ticker,
            status=CallStatus.ERROR,
            notes="yfinance not installed",
        )
        return None

    try:
        tkr = yf.Ticker(ticker)
        # `fast_info` is the modern lightweight call — last_price + currency, no DataFrame.
        # `.info` is the legacy heavy call (full snapshot); use as a fallback.
        price: float | None = None
        currency: str | None = None
        fast = getattr(tkr, "fast_info", None)
        if fast is not None:
            raw = (
                getattr(fast, "last_price", None) or fast.get("last_price")
                if hasattr(fast, "get")
                else None
            )
            if (
                isinstance(raw, (int, float))
                and not isinstance(raw, bool)
                and math.isfinite(float(raw))
                and raw > 0
            ):
                price = float(raw)
            raw_currency = getattr(fast, "currency", None)
            if raw_currency is None and hasattr(fast, "get"):
                raw_currency = fast.get("currency")
            if isinstance(raw_currency, str) and raw_currency.strip():
                currency = raw_currency.strip().upper()

        if price is None:
            raw_info = getattr(tkr, "info", None)
            info = cast("dict[str, object]", raw_info) if isinstance(raw_info, dict) else {}
            raw_currency = info.get("currency")
            if isinstance(raw_currency, str) and raw_currency.strip():
                currency = raw_currency.strip().upper()
            for key in ("regularMarketPrice", "currentPrice", "previousClose"):
                v = info.get(key)
                if (
                    isinstance(v, (int, float))
                    and not isinstance(v, bool)
                    and math.isfinite(float(v))
                    and v > 0
                ):
                    price = float(v)
                    break

        if price is None:
            log_call(
                source_name="yfinance",
                kind="live_price",
                ticker=ticker,
                status=CallStatus.NOT_FOUND,
                latency_ms=int((time.monotonic() - started) * 1000),
                notes="no price in fast_info or info",
            )
            return None

        log_call(
            source_name="yfinance",
            kind="live_price",
            ticker=ticker,
            status=CallStatus.OK,
            latency_ms=int((time.monotonic() - started) * 1000),
            record_count=1,
        )
        return LivePrice(
            price=price,
            fetched_at=datetime.now(UTC),
            source_name="yfinance",
            currency=currency,
        )
    except Exception as e:
        log_call(
            source_name="yfinance",
            kind="live_price",
            ticker=ticker,
            status=CallStatus.ERROR,
            latency_ms=int((time.monotonic() - started) * 1000),
            notes=f"{type(e).__name__}: {e}"[:256],
        )
        return None


def _try_fmp_cache(repo_root: Path, ticker: str) -> LivePrice | None:
    """Read price from data/historical/fmp/<T>_profile.json on disk. No network call.

    This is the pre-stack path: identical logic to the old src/dcf/live_price.py.
    """
    started = time.monotonic()
    path = repo_root / "data" / "historical" / "fmp" / f"{ticker}_profile.json"
    if not path.exists():
        log_call(
            source_name="fmp_cache",
            kind="live_price",
            ticker=ticker,
            status=CallStatus.NOT_FOUND,
            notes="profile.json missing",
        )
        return None

    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log_call(
            source_name="fmp_cache",
            kind="live_price",
            ticker=ticker,
            status=CallStatus.ERROR,
            notes=f"{type(e).__name__}: {e}"[:256],
        )
        return None

    rec: dict[str, object] | None = None
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        rec = cast("dict[str, object]", payload[0])
    elif isinstance(payload, dict):
        rec = cast("dict[str, object]", payload)
    if rec is None:
        log_call(
            source_name="fmp_cache",
            kind="live_price",
            ticker=ticker,
            status=CallStatus.NOT_FOUND,
            notes="profile shape unexpected",
        )
        return None

    raw_price = rec.get("price")
    raw_currency = rec.get("currency")
    if (
        not isinstance(raw_price, (int, float))
        or isinstance(raw_price, bool)
        or not math.isfinite(float(raw_price))
        or raw_price <= 0
    ):
        log_call(
            source_name="fmp_cache",
            kind="live_price",
            ticker=ticker,
            status=CallStatus.NOT_FOUND,
            notes="no usable price in profile",
        )
        return None

    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    log_call(
        source_name="fmp_cache",
        kind="live_price",
        ticker=ticker,
        status=CallStatus.OK,
        latency_ms=int((time.monotonic() - started) * 1000),
        record_count=1,
        notes=f"file mtime={mtime.isoformat(timespec='seconds')}",
    )
    return LivePrice(
        price=float(raw_price),
        fetched_at=mtime,
        source_name="fmp_cache",
        currency=raw_currency.strip().upper()
        if isinstance(raw_currency, str) and raw_currency.strip()
        else None,
    )
