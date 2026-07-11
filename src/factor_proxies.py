"""Free ETF-proxy daily close series — the style-factor return substrate.

The portfolio tracker owns all benchmark math but exposes no factor-return
series, and the FMP price-chart cache only covers TRACKED tickers — the style
proxies (VTV / VUG / IWM / MTUM, plus SPY as the market leg of the spreads)
are not tracked names, so their daily closes need their own small local store.
yfinance is the repo's established free fallback (``sources/price.py``,
``fetch_yf_grades.py``), so the refresh path pulls dividend-adjusted daily
closes from it OFFLINE (``execution/fetch_factor_proxies.py``, morning-pipeline
stage 0g) and persists them under ``data/factor_proxies/<ETF>.json``. The
render path only ever READS the files — never the network — the same
materialized-cache discipline as ``portfolio_weights``.

The same store also carries HELD_ETF_TICKERS — real portfolio holdings (FLKR)
that the FMP price-chart cache doesn't cover but that need a daily close
series for the correlation/style/Monte-Carlo sections just the same as a
style-factor proxy does; ``allocation.price_history.load_daily_closes``
already falls back to this store for ANY ticker missing from the FMP cache.

Last-good semantics match ``materialize_weights``: a failed fetch leaves the
existing file untouched, so a transient yfinance outage degrades to a stale
(dated) series instead of a blank section.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import tempfile
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

from allocation.price_history import daily_log_returns

__all__ = [
    "HELD_ETF_TICKERS",
    "PROXY_TICKERS",
    "fetch_proxy_series",
    "load_proxy_closes",
    "load_proxy_returns",
    "proxy_path",
    "refresh_factor_proxies",
    "store_proxy_series",
]

# Every ETF any style-factor spread references (portfolio_style_factors owns
# WHICH spreads exist; this store just keeps the series they need fresh).
PROXY_TICKERS: tuple[str, ...] = ("SPY", "VTV", "VUG", "IWM", "MTUM")

# Held ETFs the FMP price-chart cache doesn't cover (no SEC filer / not a
# tracked-company price-chart pull — directives/monthly_red_team.md PR4) but
# that portfolio_correlation / portfolio_style_factors / portfolio_montecarlo
# still need a daily close series for: same store, different reason for being
# here (a real HOLDING needing daily prices, not a style-factor spread leg).
# Kept separate from PROXY_TICKERS so load_proxy_returns()'s style-factor
# default is unaffected — only the CLI's default --tickers list (and so
# morning-pipeline stage 0g) picks these up, keeping the cache fresh
# alongside the style proxies without a second fetch stage.
HELD_ETF_TICKERS: tuple[str, ...] = ("FLKR",)

# ~2 calendar years of daily closes comfortably covers the 252-observation
# regression window plus the calendar-intersection losses against holdings.
DEFAULT_PERIOD = "2y"

_DIR_REL: tuple[str, ...] = ("data", "factor_proxies")


def proxy_path(repo_root: Path, ticker: str) -> Path:
    """``data/factor_proxies/<TICKER>.json`` for the ETF."""
    return repo_root.joinpath(*_DIR_REL, f"{ticker.upper()}.json")


def store_proxy_series(
    repo_root: Path, ticker: str, rows: Sequence[tuple[date, float]]
) -> Path | None:
    """Persist the ETF's close series atomically (temp file + ``os.replace``).

    No-op returning ``None`` on an empty series — a failed fetch must never
    clobber the last-good file. The payload is
    ``{"ticker", "fetched_at" (naive-UTC ISO), "rows": [[iso_date, close]]}``.
    """
    if not rows:
        return None
    path = proxy_path(repo_root, ticker)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ticker": ticker.upper(),
        "fetched_at": datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds"),
        "rows": [[d.isoformat(), float(v)] for d, v in sorted(rows, key=lambda r: r[0])],
    }
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=f"{ticker.upper()}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def load_proxy_closes(repo_root: Path, ticker: str) -> list[tuple[date, float]]:
    """The stored (date, close) series, ascending; ``[]`` when the file is
    absent or unreadable (the caller's leg then hides itself)."""
    try:
        raw = proxy_path(repo_root, ticker).read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        payload: object = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    rows = cast("dict[str, object]", payload).get("rows")
    if not isinstance(rows, list):
        return []
    out: list[tuple[date, float]] = []
    for entry in cast("list[object]", rows):
        if not isinstance(entry, list) or len(cast("list[object]", entry)) != 2:
            continue
        d_raw, v_raw = cast("list[object]", entry)
        if not isinstance(d_raw, str) or isinstance(v_raw, bool):
            continue
        if not isinstance(v_raw, (int, float)):
            continue
        try:
            d = date.fromisoformat(d_raw[:10])
        except ValueError:
            continue
        v = float(v_raw)
        if math.isfinite(v) and v > 0:
            out.append((d, v))
    out.sort(key=lambda r: r[0])
    return out


def load_proxy_returns(
    repo_root: Path, tickers: Sequence[str] = PROXY_TICKERS
) -> dict[str, dict[date, float]]:
    """Daily log returns per stored proxy, keyed upper-cased; proxies with no
    usable file are simply absent from the result."""
    out: dict[str, dict[date, float]] = {}
    for t in tickers:
        closes = load_proxy_closes(repo_root, t)
        rets = daily_log_returns(closes)
        if rets:
            out[t.upper()] = rets
    return out


def fetch_proxy_series(ticker: str, *, period: str = DEFAULT_PERIOD) -> list[tuple[date, float]]:
    """Dividend-adjusted daily closes from yfinance, ascending; ``[]`` on any
    failure (missing package, network error, empty frame) — callers treat an
    empty series as "keep the last-good file"."""
    try:
        import yfinance as yf  # type: ignore[import-untyped]
    except ImportError:
        return []
    try:
        hist = (
            cast("Any", yf).Ticker(ticker).history(period=period, interval="1d", auto_adjust=True)
        )
        stamps: list[Any] = hist.index.tolist()
        closes: list[Any] = hist["Close"].tolist()
    except Exception:
        return []
    out: list[tuple[date, float]] = []
    for ts, close in zip(stamps, closes, strict=False):
        try:
            d = ts.date() if hasattr(ts, "date") else date.fromisoformat(str(ts)[:10])
            v = float(close)
        except (ValueError, TypeError):
            continue
        if isinstance(d, datetime):
            d = d.date()
        if math.isfinite(v) and v > 0:
            out.append((d, v))
    out.sort(key=lambda r: r[0])
    return out


def refresh_factor_proxies(
    repo_root: Path,
    tickers: Sequence[str] = PROXY_TICKERS,
    *,
    period: str = DEFAULT_PERIOD,
) -> dict[str, int]:
    """Fetch + store every proxy; ``ticker -> rows persisted`` (0 = fetch
    failed, last-good file left untouched)."""
    out: dict[str, int] = {}
    for t in tickers:
        rows = fetch_proxy_series(t, period=period)
        stored = store_proxy_series(repo_root, t, rows)
        out[t.upper()] = len(rows) if stored is not None else 0
    return out
