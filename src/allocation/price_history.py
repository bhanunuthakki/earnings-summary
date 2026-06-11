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
from itertools import pairwise
from pathlib import Path
from typing import Any, cast

import numpy as np
import numpy.typing as npt


def load_daily_closes(ticker: str, repo_root: Path) -> list[tuple[date, float]]:
    """Read the ticker's FMP price-chart JSON into ascending (date, close) pairs.

    Prefers ``adjClose`` (dividend-adjusted) over ``close``; tolerates both the
    bare-list payload the stable endpoint returns and the legacy
    ``{"historical": [...]}`` wrapper. Non-positive and unparseable rows are
    skipped. Returns [] when no usable file exists.

    The canonical ``save_fmp_data`` filename is tried directly before any
    glob — the cache directory holds ~100k files, and one directory scan
    costs ~0.4s on this box (the panel loads every holding per render).
    """
    fmp_dir = repo_root / "data" / "historical" / "fmp"
    if not fmp_dir.exists():
        return []
    upper = ticker.upper()
    candidates = [
        p
        for p in (
            fmp_dir / f"{upper}_price_chart_10y_div_adj.json",
            fmp_dir / f"{upper}L_price_chart_10y_div_adj.json",  # GOOG ↔ GOOGL
        )
        if p.exists()
    ]
    if not candidates:
        candidates = list(fmp_dir.glob(f"{upper}_*price_chart*.json"))
        candidates.extend(fmp_dir.glob(f"{upper}L_*price_chart*.json"))
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
    return []


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
