"""What-if at a chosen weight — the book BEFORE vs AFTER adding one name.

Answers the owner's actual question ("what does a 3% AVUV position do to my
book?") with magnitudes, not just the marginal-Sharpe pass/fail the fit chip
carries: vol, Sharpe (Δ in basis points), growth tilt, sector mix, and the
candidate's top correlations to individual holdings.

Modeling choices (stated in the UI footnote, pinned by tests):

* **Pro-rata funding** — the after-book is ``(1-w)·book + w·candidate``:
  every existing position shrinks proportionally to fund the new one.
  "Fund from cash" is different math this deliberately does not model.
* **Realized-series consistency** — before AND after stats come from the same
  aligned daily log-return window (``price_history.build_aligned_returns``,
  252 obs / 120 min overlap), so the delta is internally consistent. This is
  the MODELED book (holdings priced from the local cache), not the tracker's
  reported Sharpe — the two must never be mixed in one delta.
* **Log-return blend** — the after series blends log returns linearly, the
  same approximation ``candidate_fit`` and ``book_risk`` already make.
* **funding_mode is FRAMING, not different math** (PRD §7.2, P0.2) — the
  book-level return blend ``(1-w)·book + w·candidate`` is mathematically
  IDENTICAL under both ``"new_cash"`` and ``"pro_rata_reallocation"``: the
  old book's post-trade weight becomes ``1-w`` either way, so
  :func:`compute_what_if` runs the same computation regardless. The two
  modes differ only in how the result is EXPLAINED to the owner:
  ``"new_cash"`` implies depositing ``T*w/(1-w)`` new dollars and no implied
  sales; ``"pro_rata_reallocation"`` implies selling ``w`` of every existing
  holding to fund the add. Pick the mode that matches how the owner actually
  intends to fund the position — it changes the narrative, never the vol/
  Sharpe/tilt numbers.

Costs and caching: one call is a covariance-grade price read (every holding +
the candidate). The peek route is user-initiated, so a cold ~100-300 ms
compute is fine; module-level LRU caches (aligned bundle per candidate+book,
result per (ticker, weight, funding_mode, prices-through)) make repeat
weight-clicks instant. Weights must be in ``(0, 0.25]`` (validated by
:func:`validate_weight`, rounded to 4 decimals for cache-key stability);
:data:`ALLOWED_WEIGHTS` is the UI's preset chip menu, not an input
restriction. Never call this on the GET / render path for a whole table —
the materializer persists the default-weight ΔSR per candidate instead.
"""

from __future__ import annotations

import math
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TypeVar

import numpy as np

from allocation.candidate_fit import ANNUALIZE, TRADING_DAYS, mean_var, ols_beta, series_cov
from allocation.concentration import classify_zone
from allocation.price_history import (
    AlignedReturns,
    build_aligned_returns,
    daily_log_returns,
    load_daily_closes,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

#: The UI's preset weight-chip menu (PRD §7.2, P0.2 extends this through the
#: concentration-zone boundaries: 10/12/15/20/25%). This is a UI convenience,
#: NOT an input restriction — :func:`validate_weight` accepts any weight in
#: ``(0, 0.25]``, not just these presets.
ALLOWED_WEIGHTS: tuple[float, ...] = (
    0.01,
    0.02,
    0.03,
    0.05,
    0.08,
    0.10,
    0.12,
    0.15,
    0.20,
    0.25,
)
DEFAULT_WEIGHT = 0.03

#: Cache-key rounding precision for a validated weight (see validate_weight).
_WEIGHT_KEY_DECIMALS = 4

#: The two ways the UI can frame funding an add — see the module docstring
#: ("funding_mode is FRAMING, not different math").
FUNDING_MODES: tuple[str, ...] = ("new_cash", "pro_rata_reallocation")
DEFAULT_FUNDING_MODE = "new_cash"

_SPY, _QQQ = "SPY", "QQQ"

_BUNDLE_CACHE_MAX = 16
_RESULT_CACHE_MAX = 256


@dataclass(frozen=True, slots=True)
class WhatIfResult:
    """Book stats before → after adding ``ticker`` at ``weight`` (pro-rata)."""

    ticker: str
    weight: float
    funding_mode: str = DEFAULT_FUNDING_MODE
    # Post-trade single-name weight (PERCENT, 0-100) — the book's existing
    # weight for ``ticker`` (if any) blended with ``weight`` the same way the
    # book-level return series is: w0*(1-w) + w. Zero when ``ticker`` is not
    # currently in ``book_weights``.
    resulting_weight_pct: float = 0.0
    # allocation.concentration.Zone for resulting_weight_pct, or None (should
    # not happen — resulting_weight_pct is always a float here, never None).
    zone: str | None = None
    vol_before_ann: float | None = None
    vol_after_ann: float | None = None
    sharpe_before: float | None = None  # modeled book, realized window
    sharpe_after: float | None = None
    sharpe_delta_bps: float | None = None  # (after - before) · 1e4
    growth_tilt_before: float | None = None
    growth_tilt_after: float | None = None
    sector_mix_after: dict[str, float] | None = None
    top_correlations: tuple[tuple[str, float], ...] = ()  # candidate vs holding, |corr| desc
    # C7 — book-level business-factor exposure (src.risk_factors, C3) before
    # and after pro-rata folding in the candidate at ``weight``, the SAME
    # ``(1-w)*before + w*candidate`` blend ``sector_mix_after`` already uses.
    # Both None when ``db_path`` wasn't passed, the book has no persisted
    # loadings yet, or the substrate predates the C3 migration (absent table
    # -> absent section, never a fabricated zero vector).
    factor_vector_before: dict[str, float] | None = None
    factor_vector_after: dict[str, float] | None = None
    obs: int | None = None
    prices_through: str | None = None  # last aligned date, ISO
    degraded: tuple[str, ...] = ()  # human-readable reasons; empty when clean


def validate_weight(raw: float) -> float:
    """Validate an owner-chosen add weight (fraction of book, e.g. 0.03 = 3%).

    Accepts any weight in ``(0, 0.25]`` — NOT restricted to
    :data:`ALLOWED_WEIGHTS` (that tuple is the UI's preset chip menu, not an
    input bound; PRD §7.2, P0.2 explicitly ends the old silent-clamp
    behavior). Returns ``raw`` rounded to :data:`_WEIGHT_KEY_DECIMALS`
    decimals — the precision the module's result cache key uses, so a
    validated weight is already cache-stable. Raises ``ValueError`` (naming
    the preset percentages) for anything outside the allowed range.
    """
    if not (0.0 < raw <= 0.25):
        presets = ", ".join(f"{w * 100:g}%" for w in ALLOWED_WEIGHTS)
        raise ValueError(
            f"weight must be > 0% and <= 25% (got {raw * 100:g}%); preset menu: {presets}"
        )
    return round(raw, _WEIGHT_KEY_DECIMALS)


# --------------------------------------------------------------------------- #
# Module caches (the Flask process serves repeat peek clicks from memory)
# --------------------------------------------------------------------------- #

_lock = threading.Lock()
_bundle_cache: OrderedDict[tuple[str, int], AlignedReturns] = OrderedDict()
# Result key carries the book identity (weights hash + rf) alongside the
# prices-through date and funding_mode — a weights refresh mid-session must
# miss, not serve yesterday's book, and the two funding framings (identical
# math, different narrative) must never collide in the same cache slot. The
# trailing ``str`` is the (possibly empty) ``db_path`` string — C7's
# factor-vector legs are keyed off it too, so a cache hit from a call WITHOUT
# ``db_path`` (peeks.py today) never masquerades as one WITH it.
_result_cache: OrderedDict[tuple[str, float, str, str, int, float | None, str], WhatIfResult] = (
    OrderedDict()
)


def _weights_key(book_weights: Mapping[str, float]) -> int:
    return hash(tuple(sorted((t.upper(), round(w, 6)) for t, w in book_weights.items())))


_K = TypeVar("_K")
_V = TypeVar("_V")


def _cache_get(cache: OrderedDict[_K, _V], key: _K) -> _V | None:
    with _lock:
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
    return None


def _cache_put(cache: OrderedDict[_K, _V], key: _K, value: _V, cap: int) -> None:
    with _lock:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > cap:
            cache.popitem(last=False)


def clear_caches() -> None:
    """Test seam / post-materialize hygiene."""
    with _lock:
        _bundle_cache.clear()
        _result_cache.clear()


# --------------------------------------------------------------------------- #
# Core
# --------------------------------------------------------------------------- #


def _aligned_bundle(
    repo_root: Path,
    ticker: str,
    book_weights: Mapping[str, float],
    *,
    lookback_obs: int,
    min_overlap_obs: int,
) -> AlignedReturns | None:
    key = (ticker.upper(), _weights_key(book_weights) ^ hash(lookback_obs))
    cached = _cache_get(_bundle_cache, key)
    if cached is not None:
        return cached
    returns_by_ticker: dict[str, dict[date, float]] = {}
    for t in {*(t.upper() for t in book_weights), ticker.upper()}:
        rets = daily_log_returns(load_daily_closes(t, repo_root))
        if rets:
            returns_by_ticker[t] = rets
    aligned = build_aligned_returns(
        returns_by_ticker, lookback_obs=lookback_obs, min_overlap_obs=min_overlap_obs
    )
    if aligned is not None:
        _cache_put(_bundle_cache, key, aligned, _BUNDLE_CACHE_MAX)
    return aligned


def _series_stats(series: np.ndarray, risk_free_annual: float | None) -> tuple[float, float | None]:
    """(annualized vol, Sharpe|None) of a daily log-return series."""
    mean, var = mean_var([float(x) for x in series])
    sd = math.sqrt(var)
    vol_ann = sd * ANNUALIZE
    if sd <= 0.0 or risk_free_annual is None:
        return vol_ann, None
    return vol_ann, (mean * TRADING_DAYS - risk_free_annual) / vol_ann


def compute_what_if(
    repo_root: Path,
    ticker: str,
    weight: float,
    *,
    book_weights: Mapping[str, float],
    risk_free_annual: float | None,
    book_growth_tilt: float | None,
    sector: str | None = None,
    book_sector_weights: Mapping[str, float] | None = None,
    lookback_obs: int = 252,
    min_overlap_obs: int = 120,
    funding_mode: str = DEFAULT_FUNDING_MODE,
    db_path: Path | str | None = None,
) -> WhatIfResult:
    """One candidate at one weight against the passed book. Never raises on
    data absence — missing legs populate ``degraded`` and stay None.

    ``funding_mode`` (one of :data:`FUNDING_MODES`) is FRAMING ONLY — see the
    module docstring: the book-level blend is identical math under either
    mode, so it changes the result's narrative fields (and the cache key)
    but never the vol/Sharpe/tilt numbers. Raises ``ValueError`` for an
    unknown mode (the same fail-loud contract ``validate_weight`` uses for an
    out-of-range weight — a typo'd query param must never silently degrade
    to a mode the caller didn't ask for).

    ``db_path`` (C7 — optional, defaults to ``None``) unlocks the
    ``factor_vector_before``/``factor_vector_after`` legs (src.risk_factors,
    C3): the book's persisted business-factor exposure vector before and
    after pro-rata folding in the candidate at ``weight``, the SAME
    ``(1-w)*before + w*candidate`` blend already used for
    ``sector_mix_after``. Omitted, or a substrate predating the C3
    migration, or an empty ``business_factor_exposures`` table -> both
    fields stay ``None``, never raising. ``pipeline.peeks.render_what_if_peek``
    passes a live ``db_path`` through from its Flask app closure.
    """
    if funding_mode not in FUNDING_MODES:
        raise ValueError(f"funding_mode must be one of {FUNDING_MODES} (got {funding_mode!r})")
    upper = ticker.upper()
    w = validate_weight(weight)
    degraded: list[str] = []

    w0 = next((v for t, v in book_weights.items() if t.upper() == upper), 0.0)
    resulting_weight_pct = (w0 * (1.0 - w) + w) * 100.0
    zone_assessment = classify_zone(resulting_weight_pct)
    zone = zone_assessment.zone if zone_assessment is not None else None

    aligned = _aligned_bundle(
        repo_root, upper, book_weights, lookback_obs=lookback_obs, min_overlap_obs=min_overlap_obs
    )
    prices_through = aligned.dates[-1].isoformat() if aligned is not None else None
    result_key = (
        upper,
        w,
        funding_mode,
        prices_through or "",
        _weights_key(book_weights),
        risk_free_annual,
        str(db_path) if db_path is not None else "",
    )
    if prices_through is not None:
        cached = _cache_get(_result_cache, result_key)
        if cached is not None:
            return cached

    vol_before = vol_after = sharpe_before = sharpe_after = delta_bps = None
    top_corr: tuple[tuple[str, float], ...] = ()
    obs: int | None = None

    if aligned is None or upper not in aligned.tickers:
        degraded.append(
            f"no usable common price history for {upper} against the book "
            f"(need {min_overlap_obs} overlapping days)"
        )
    elif len(aligned.tickers) < 2:
        degraded.append("book has fewer than two priced holdings — no book series")
    else:
        cand_idx = aligned.tickers.index(upper)
        book_cols = [i for i, t in enumerate(aligned.tickers) if t != upper]
        raw = np.array(
            [max(book_weights.get(aligned.tickers[i], 0.0), 0.0) for i in book_cols],
            dtype=np.float64,
        )
        w_sum = float(raw.sum())
        if w_sum <= 0.0:
            degraded.append("no priced holding carries weight — book series undefined")
        else:
            bw = raw / w_sum
            r_book = aligned.matrix[:, book_cols] @ bw
            r_cand = aligned.matrix[:, cand_idx]
            r_after = (1.0 - w) * r_book + w * r_cand
            obs = len(aligned.dates)
            vol_before, sharpe_before = _series_stats(r_book, risk_free_annual)
            vol_after, sharpe_after = _series_stats(r_after, risk_free_annual)
            if sharpe_before is not None and sharpe_after is not None:
                delta_bps = (sharpe_after - sharpe_before) * 1e4
            elif risk_free_annual is None:
                degraded.append("risk-free rate unknown (tracker-only) — Sharpe legs unscored")
            corrs: list[tuple[str, float]] = []
            cand_list = [float(x) for x in r_cand]
            _, cand_var = mean_var(cand_list)
            cand_sd = math.sqrt(cand_var)
            if cand_sd > 0.0:
                for i in book_cols:
                    col = [float(x) for x in aligned.matrix[:, i]]
                    _, col_var = mean_var(col)
                    col_sd = math.sqrt(col_var)
                    if col_sd > 0.0:
                        corrs.append(
                            (aligned.tickers[i], series_cov(cand_list, col) / (cand_sd * col_sd))
                        )
                corrs.sort(key=lambda kv: abs(kv[1]), reverse=True)
                top_corr = tuple(corrs[:5])

    # Growth tilt after: linear blend of OLS betas — exact for a portfolio.
    tilt_after: float | None = None
    cand_tilt = _candidate_growth_tilt(repo_root, upper, min_overlap_obs=min_overlap_obs)
    if book_growth_tilt is not None and cand_tilt is not None:
        tilt_after = (1.0 - w) * book_growth_tilt + w * cand_tilt
    elif book_growth_tilt is None:
        degraded.append("book growth tilt unknown — tilt leg unscored")
    elif cand_tilt is None:
        degraded.append(f"{upper} growth tilt uncomputable (no SPY/QQQ overlap)")

    sector_mix: dict[str, float] | None = None
    if book_sector_weights:
        sector_mix = {s: (1.0 - w) * sw for s, sw in book_sector_weights.items()}
        label = sector or "(unclassified)"
        sector_mix[label] = sector_mix.get(label, 0.0) + w

    # C7 factor-vector before/after: pure arithmetic over C3's persisted
    # is_latest loadings, the same pro-rata blend sector_mix_after uses.
    # book_factor_vector already never raises (a missing/pre-migration table
    # degrades to an empty vector), so this leg only needs to guard the
    # candidate's OWN loadings read and never let either failure escape.
    factor_vector_before: dict[str, float] | None = None
    factor_vector_after: dict[str, float] | None = None
    if db_path is not None:
        try:
            from risk_factors import book_factor_vector

            book_vector = book_factor_vector(db_path, repo_root).vector
            if book_vector:
                cand_loadings = _ticker_factor_loadings(db_path, upper)
                factor_vector_before = dict(book_vector)
                after: dict[str, float] = {f: (1.0 - w) * v for f, v in book_vector.items()}
                for factor, loading in cand_loadings.items():
                    after[factor] = after.get(factor, 0.0) + w * loading
                factor_vector_after = after
        except Exception as exc:
            degraded.append(f"business-factor deltas unavailable: {type(exc).__name__}")

    result = WhatIfResult(
        ticker=upper,
        weight=w,
        funding_mode=funding_mode,
        resulting_weight_pct=resulting_weight_pct,
        zone=zone,
        vol_before_ann=vol_before,
        vol_after_ann=vol_after,
        sharpe_before=sharpe_before,
        sharpe_after=sharpe_after,
        sharpe_delta_bps=delta_bps,
        growth_tilt_before=book_growth_tilt,
        growth_tilt_after=tilt_after,
        sector_mix_after=sector_mix,
        top_correlations=top_corr,
        factor_vector_before=factor_vector_before,
        factor_vector_after=factor_vector_after,
        obs=obs,
        prices_through=prices_through,
        degraded=tuple(degraded),
    )
    if prices_through is not None:
        _cache_put(_result_cache, result_key, result, _RESULT_CACHE_MAX)
    return result


def _ticker_factor_loadings(db_path: Path | str | None, ticker: str) -> dict[str, float]:
    """This ticker's OWN latest business-factor loadings (C3
    ``business_factor_exposures``), ``factor -> loading``. ``{}`` on a
    missing ``db_path``, a missing DB file, a pre-migration substrate
    (``sqlite3.OperationalError`` — the table doesn't exist), or any other
    read error — never raises, matching ``risk_factors._open``'s degrade
    contract (this module can't import that private helper directly, so the
    connection-open logic is duplicated here at the same defensiveness)."""
    if db_path is None:
        return {}
    try:
        from db_paths import resolve_db_path

        resolved = resolve_db_path(db_path)
        if resolved is None or not Path(resolved).exists():
            return {}
        conn = connect_sqlite(resolved, role=SQLiteConnectionRole.READ_ONLY)
    except (sqlite3.Error, OSError):
        return {}
    try:
        rows = conn.execute(
            "SELECT factor, loading FROM business_factor_exposures "
            "WHERE ticker = ? AND is_latest = 1",
            (ticker.upper(),),
        ).fetchall()
        return {str(factor): float(loading) for factor, loading in rows}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _candidate_growth_tilt(repo_root: Path, ticker: str, *, min_overlap_obs: int) -> float | None:
    """QQQ-beta minus SPY-beta from the local caches (same legs candidate_fit
    computes); None when benchmark overlap is thin."""
    cand = daily_log_returns(load_daily_closes(ticker, repo_root))
    if not cand:
        return None
    spy = daily_log_returns(load_daily_closes(_SPY, repo_root))
    qqq = daily_log_returns(load_daily_closes(_QQQ, repo_root))
    spy_dates = sorted(set(cand) & set(spy))
    qqq_dates = sorted(set(cand) & set(qqq))
    if len(spy_dates) < min_overlap_obs or len(qqq_dates) < min_overlap_obs:
        return None
    beta_spy = ols_beta([cand[d] for d in spy_dates], [spy[d] for d in spy_dates])
    beta_qqq = ols_beta([cand[d] for d in qqq_dates], [qqq[d] for d in qqq_dates])
    if beta_spy is None or beta_qqq is None:
        return None
    return beta_qqq - beta_spy
