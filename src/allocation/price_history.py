"""Daily price history over the FMP price-chart cache.

The 10-year dividend-adjusted price charts that ``save_fmp_data`` writes to
``data/historical/fmp/<TICKER>_price_chart_10y_div_adj.json`` are the repo's
only on-disk daily price source (the portfolio tracker exposes positions and
benchmark math, not per-ticker price series). This module reads them into
date-sorted closes, converts them to daily log returns, and aligns a set of
tickers onto a common trading calendar as a numpy matrix — the substrate for
the next-dollar covariance estimate (directives/next_dollar_model.md).

Parsing intentionally mirrors ``execution/compute_macro_sensitivities.
_load_ticker_prices`` — the macro betas and this covariance matrix must be
estimated off the same notion of "price" (dividend-adjusted close, falling
back to close), or the two factors silently diverge.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from itertools import pairwise
from pathlib import Path
from typing import Any, cast

import numpy as np
import numpy.typing as npt


def _fmp_directory_state(fmp_dir: Path) -> tuple[int, int]:
    """Return metadata that changes when a legacy chart is added or removed."""
    metadata = fmp_dir.stat()
    return metadata.st_mtime_ns, metadata.st_ctime_ns


@lru_cache(maxsize=32)
def _legacy_chart_manifest(
    fmp_dir: Path, directory_state: tuple[int, int]
) -> dict[str, tuple[Path, ...]]:
    """Index legacy FMP names once for an observed directory state.

    A legacy filename has no stable endpoint contract, so it cannot be resolved
    directly. One bounded manifest amortizes a directory scan across a panel
    render, while the directory state in the cache key exposes newly written
    charts to the next request.
    """
    del directory_state  # Deliberately a cache-key input, not payload data.
    by_ticker: dict[str, list[Path]] = {}
    try:
        for path in fmp_dir.iterdir():
            name = path.name
            if not name.endswith(".json") or "_price_chart" not in name:
                continue
            ticker, _separator, _suffix = name.partition("_price_chart")
            if ticker:
                by_ticker.setdefault(ticker, []).append(path)
    except OSError:
        return {}
    return {
        ticker: tuple(sorted(paths, key=lambda path: path.name))
        for ticker, paths in by_ticker.items()
    }


def load_daily_closes(ticker: str, repo_root: Path) -> list[tuple[date, float]]:
    """Read the ticker's FMP price-chart JSON into ascending (date, close) pairs.

    Prefers ``adjClose`` (dividend-adjusted) over ``close``; tolerates both the
    bare-list payload the stable endpoint returns and the legacy
    ``{"historical": [...]}`` wrapper. Non-positive and unparseable rows are
    skipped. Returns [] when no usable file exists.

    The canonical ``save_fmp_data`` filename is tried directly before any
    glob — the cache directory holds ~100k files, and one directory scan
    costs ~0.4s on this box (the panel loads every holding per render).

    When the FMP cache has nothing for the ticker, the yfinance proxy store
    (``data/factor_proxies/<T>.json``) is the fallback — the price path for
    ETFs the FMP plan doesn't cover (directives/etf_data.md). FMP's
    dividend-adjusted chart always wins when both exist.
    """
    fmp_dir = repo_root / "data" / "historical" / "fmp"
    upper = ticker.upper()
    if not fmp_dir.exists():
        return _load_proxy_store_closes(repo_root, upper)
    candidates = [
        p
        for p in (
            fmp_dir / f"{upper}_price_chart_10y_div_adj.json",
            fmp_dir / f"{upper}L_price_chart_10y_div_adj.json",  # GOOG ↔ GOOGL
        )
        if p.exists()
    ]
    if not candidates:
        try:
            manifest = _legacy_chart_manifest(fmp_dir, _fmp_directory_state(fmp_dir))
        except OSError:
            return _load_proxy_store_closes(repo_root, upper)
        # Preserve established symbol precedence (GOOG before GOOGL) while
        # making each legacy symbol's filename choice deterministic.
        candidates = [*manifest.get(upper, ()), *manifest.get(f"{upper}L", ())]
    for path in candidates:
        try:
            data: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows: list[dict[str, object]] = []
        if isinstance(data, list):
            raw_list = cast("list[object]", data)
            rows = [cast("dict[str, object]", r) for r in raw_list if isinstance(r, dict)]
        elif isinstance(data, dict):
            inner = cast("dict[str, object]", data).get("historical")
            if isinstance(inner, list):
                inner_list = cast("list[object]", inner)
                rows = [cast("dict[str, object]", r) for r in inner_list if isinstance(r, dict)]
        out: list[tuple[date, float]] = []
        for r in rows:
            d_raw = r.get("date")
            v_raw = r.get("adjClose") if "adjClose" in r else r.get("close")
            if not isinstance(d_raw, str) or v_raw is None:
                continue
            try:
                d = date.fromisoformat(d_raw[:10])
                v = float(cast("Any", v_raw))
            except (ValueError, TypeError):
                continue
            if v <= 0:
                continue
            out.append((d, v))
        if out:
            out.sort(key=lambda t: t[0])
            return out
    return _load_proxy_store_closes(repo_root, upper)


def _load_proxy_store_closes(repo_root: Path, upper: str) -> list[tuple[date, float]]:
    """Parse ``data/factor_proxies/<T>.json`` (payload ``{"rows": [[iso_date,
    close], ...]}``) into ascending (date, close) pairs; [] when absent or
    malformed.

    Deliberately re-implements ``factor_proxies.load_proxy_closes`` locally:
    ``factor_proxies`` imports this module, so importing it back would be a
    circular import. The payload shape is trivial and owned by
    ``factor_proxies.store_proxy_series``.
    """
    path = repo_root / "data" / "factor_proxies" / f"{upper}.json"
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
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
    out.sort(key=lambda t: t[0])
    return out


def daily_log_returns(prices: Sequence[tuple[date, float]]) -> dict[date, float]:
    """Log returns between consecutive observations, keyed by the later date.

    Gaps (holidays, halts) simply produce a multi-day return on the next
    observation — the alignment step intersects calendars, so what matters is
    that every ticker's return for a shared date covers the same span back to
    the previous shared trading day.
    """
    out: dict[date, float] = {}
    for (_, prev_v), (d, v) in pairwise(prices):
        if prev_v <= 0 or v <= 0:
            continue
        out[d] = math.log(v / prev_v)
    return out


@dataclass(slots=True)
class AlignedReturns:
    """A T×N daily log-return matrix over the tickers' common trading dates."""

    tickers: list[str]  # column order, sorted
    dates: list[date]  # row order, ascending
    matrix: npt.NDArray[np.float64]  # shape (len(dates), len(tickers))
    dropped: dict[str, str] = field(default_factory=dict[str, str])  # ticker -> reason


def build_aligned_returns(
    returns_by_ticker: Mapping[str, Mapping[date, float]],
    *,
    lookback_obs: int = 252,
    min_overlap_obs: int = 120,
) -> AlignedReturns | None:
    """Intersect the tickers' return calendars into one matrix.

    Tickers with fewer than ``min_overlap_obs`` observations of their own are
    dropped up front; if the joint intersection is still too thin (a short
    history crushing everyone else's calendar — the recently-IPO'd case), the
    shortest-history ticker is dropped greedily until the overlap clears the
    bar. The matrix keeps at most the latest ``lookback_obs`` common dates.
    Returns None when fewer than two tickers have a usable overlap.
    """
    dropped: dict[str, str] = {}
    candidates: dict[str, Mapping[date, float]] = {}
    for t, rets in returns_by_ticker.items():
        if len(rets) >= min_overlap_obs:
            candidates[t] = rets
        else:
            dropped[t] = f"only {len(rets)} daily returns on file (need {min_overlap_obs})"
    while len(candidates) >= 2:
        calendars = iter(candidates.values())
        common: set[date] = set(next(calendars).keys())
        for rets in calendars:
            common.intersection_update(rets.keys())
        if len(common) >= min_overlap_obs:
            dates = sorted(common)[-lookback_obs:]
            tickers = sorted(candidates)
            matrix = np.array(
                [[candidates[t][d] for t in tickers] for d in dates], dtype=np.float64
            )
            return AlignedReturns(tickers=tickers, dates=dates, matrix=matrix, dropped=dropped)
        # Tie-break deterministically by ticker so reruns drop the same name.
        shortest = min(candidates, key=lambda t: (len(candidates[t]), t))
        dropped[shortest] = (
            f"calendar overlap with the rest of the book below {min_overlap_obs} days"
        )
        del candidates[shortest]
    return None
