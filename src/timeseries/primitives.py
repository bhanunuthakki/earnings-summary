"""Statistical primitives over a quarterly Series.

Six functions, each over a list[Observation] of (period_end, value) pairs:

  detect_trend(series)        — linear regression + Mann-Kendall;
                                returns direction + slope + r2 + significance.
  detect_inflection(series)   — PELT changepoint when ruptures is available;
                                rolling-window regression fallback otherwise.
  rolling_zscore_anomalies(series, window, threshold)
                              — quarters whose z-score against the trailing
                                window exceeds threshold.
  seasonal_decompose(series, period)
                              — STL (statsmodels) when long enough; classical
                                additive decomposition fallback. Returns
                                trend / seasonal / residual aligned to inputs.
  correlation_matrix(ticker, kpi_names, lookback_quarters, ...)
                              — Pearson cross-correlations across KPIs after
                                inner-joining on period_end.
  yoy_acceleration(series)    — second derivative of YoY growth. Catches
                                deceleration-of-deceleration before the
                                level alone surfaces it.

Tolerance contract: a series too short for the requested test returns a
structured ``{"insufficient_data": True}`` payload rather than raising. All
JSON-serializable values are plain Python (float/int/str/list/dict/None).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

import numpy as np

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Observation:
    """One quarterly observation: a period_end + a numeric value.

    period_end is the calendar end of the fiscal quarter (e.g. 2025-03-31).
    value is the level — not a growth rate, not a margin (those are derived
    by the caller and then passed in as their own series).
    """

    period_end: datetime
    value: float


# ---------------------------------------------------------------------------
# Helpers — kept small and private to the module
# ---------------------------------------------------------------------------


def _values(series: list[Observation]) -> np.ndarray:
    """Float-typed numpy array of values, preserving order."""
    return np.asarray([float(o.value) for o in series], dtype=float)


def _is_finite(arr: np.ndarray) -> bool:
    """True when arr has no NaN/Inf — guards against degenerate inputs."""
    if arr.size == 0:
        return False
    return bool(np.all(np.isfinite(arr)))


def _linreg(y: np.ndarray) -> tuple[float, float, float]:
    """Plain OLS y = a + b * t where t = 0..n-1. Returns (slope, intercept, r2).

    Pure numpy (no scipy dependency at the boundary) so this primitive works
    in the partial-install case where statsmodels/scipy fail to import.
    """
    n = y.size
    t = np.arange(n, dtype=float)
    t_mean = t.mean()
    y_mean = y.mean()
    denom = float(np.sum((t - t_mean) ** 2))
    if denom == 0.0:
        return 0.0, float(y_mean), 0.0
    slope = float(np.sum((t - t_mean) * (y - y_mean)) / denom)
    intercept = float(y_mean - slope * t_mean)
    y_hat = intercept + slope * t
    ss_res = float(np.sum((y - y_hat) ** 2))
    ss_tot = float(np.sum((y - y_mean) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return slope, intercept, r2


def _mann_kendall_p(y: np.ndarray) -> float:
    """Two-sided Mann-Kendall p-value for monotonic trend.

    Non-parametric so it survives non-normal residuals (which financial
    series almost always have). Uses the normal approximation; valid for
    n >= 8. Returns 1.0 for shorter series (interpreted as "no signal").
    """
    n = y.size
    if n < 8:
        return 1.0
    s = 0
    for i in range(n - 1):
        s += int(np.sum(np.sign(y[i + 1 :] - y[i])))
    # Variance under the null; tie correction omitted (negligible on continuous data)
    var = (n * (n - 1) * (2 * n + 5)) / 18.0
    if var <= 0:
        return 1.0
    if s > 0:
        z = (s - 1) / math.sqrt(var)
    elif s < 0:
        z = (s + 1) / math.sqrt(var)
    else:
        z = 0.0
    # Two-sided p-value from the normal CDF
    p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(z) / math.sqrt(2))))
    return float(min(max(p, 0.0), 1.0))


def _split_slopes(y: np.ndarray) -> tuple[float, float]:
    """Slopes of the first half and second half — used to classify
    accelerating / decelerating / inflecting. Both halves are sized
    >= 2; caller must guarantee n >= 4."""
    mid = y.size // 2
    s1, _, _ = _linreg(y[:mid])
    s2, _, _ = _linreg(y[mid:])
    return s1, s2


# ---------------------------------------------------------------------------
# 1. detect_trend
# ---------------------------------------------------------------------------


def detect_trend(series: list[Observation]) -> dict[str, object]:
    """Linear regression + Mann-Kendall non-parametric trend test.

    Returns:
        direction: one of 'accelerating' | 'decelerating' | 'flat' | 'inflecting'
        slope: OLS slope (units per quarter)
        slope_pct_of_mean: slope normalized by mean(|y|) — comparability across KPIs
        r2: coefficient of determination of the linear fit
        mann_kendall_p: p-value of the M-K trend test (lower = more significant)
        statistical_significance: True when p < 0.05 AND r2 > 0.3
        n: number of observations

    Insufficient data (< 4) → {"insufficient_data": True, "n": n}.
    """
    n = len(series)
    if n < 4:
        return cast("dict[str, object]", {"insufficient_data": True, "n": n})

    y = _values(series)
    if not _is_finite(y):
        return cast(
            "dict[str, object]", {"insufficient_data": True, "n": n, "reason": "non_finite_values"}
        )

    slope, _, r2 = _linreg(y)
    p_value = _mann_kendall_p(y)
    mean_abs = float(np.mean(np.abs(y)))
    slope_pct_of_mean = (slope / mean_abs) if mean_abs > 0 else 0.0

    s1, s2 = _split_slopes(y)
    # Direction classifies the SECOND derivative (how the slope is changing),
    # not the slope itself — slope is reported separately. So a perfectly
    # linear series is "flat" (slope steady), regardless of whether that
    # slope is positive or negative.
    direction: str
    if s1 * s2 < 0 and abs(s1) > 1e-9 and abs(s2) > 1e-9:
        direction = "inflecting"
    elif abs(slope_pct_of_mean) < 0.005:
        direction = "flat"  # level barely moving — no meaningful trend
    elif abs(s2) > abs(s1) * 1.10:
        direction = "accelerating"
    elif abs(s2) < abs(s1) * 0.90:
        direction = "decelerating"
    else:
        direction = "flat"  # linear trend — slope changing by < 10% across the cut

    return cast(
        "dict[str, object]",
        {
            "n": n,
            "direction": direction,
            "slope": slope,
            "slope_pct_of_mean": slope_pct_of_mean,
            "r2": r2,
            "mann_kendall_p": p_value,
            "statistical_significance": bool(p_value < 0.05 and r2 > 0.3),
            "first_half_slope": s1,
            "second_half_slope": s2,
        },
    )


# ---------------------------------------------------------------------------
# 2. detect_inflection
# ---------------------------------------------------------------------------


def _ruptures_inflection(y: np.ndarray) -> int | None:
    """Try ruptures PELT — fast, robust changepoint detection. None if unavailable."""
    try:
        import ruptures as rpt  # local import so missing dep doesn't sink the module
    except ImportError:
        return None
    try:
        # rbf cost handles level + variance shifts; pen=3.0 is a calibrated default
        algo = rpt.Pelt(model="rbf", min_size=3, jump=1).fit(y.reshape(-1, 1))
        # ruptures is untyped; predict returns list[int] but pyright sees Unknown
        cps = cast("list[int]", algo.predict(pen=3.0))
        # ruptures appends n as the final endpoint; strip it
        interior: list[int] = [int(c) for c in cps if 0 < int(c) < y.size]
        if not interior:
            return None
        # Pick the strongest changepoint = the one with the largest pre/post mean delta
        best_cp = max(interior, key=lambda c: abs(float(y[:c].mean()) - float(y[c:].mean())))
        return int(best_cp)
    except (
        Exception
    ) as exc:  # broad except: degrade to fallback when ruptures throws on edge inputs
        log.debug({"event": "ruptures_failed", "error": str(exc)})
        return None


def _rolling_window_inflection(y: np.ndarray) -> int | None:
    """Fallback: scan candidate breakpoints, pick the one that minimizes
    the total residual sum-of-squares when fitting two separate lines."""
    n = y.size
    best_idx: int | None = None
    best_rss = float("inf")
    for cp in range(3, n - 3):
        left = y[:cp]
        right = y[cp:]
        _, _, r2_l = _linreg(left)
        _, _, r2_r = _linreg(right)
        # Use SSR instead of R2 directly so comparable across cp positions
        sl, il, _ = _linreg(left)
        sr, ir, _ = _linreg(right)
        ssr_l = float(np.sum((left - (il + sl * np.arange(left.size))) ** 2))
        ssr_r = float(np.sum((right - (ir + sr * np.arange(right.size))) ** 2))
        if (ssr_l + ssr_r) < best_rss:
            best_rss = ssr_l + ssr_r
            best_idx = cp
        # r2 vars consumed to keep linter happy; kept the call for cost parity
        _ = r2_l
        _ = r2_r
    return best_idx


def detect_inflection(series: list[Observation]) -> dict[str, object]:
    """Locate the most prominent changepoint in the series.

    Tries ruptures PELT first; falls back to a pure-numpy rolling-window
    least-squares search when ruptures is unavailable or errors. Returns
    None when the series is too short OR no clear inflection exists
    (prior/post slopes within 25% of one another and < 1 std apart in mean).

    Returns:
        inflection_period: ISO date of the period at the changepoint
                          (the FIRST period of the post-regime)
        inflection_index: integer index into the input series
        prior_slope: slope of the pre-changepoint segment
        post_slope: slope of the post-changepoint segment
        magnitude: absolute difference in mean across the cut, normalized
                   by the standard deviation of the entire series
        method: 'ruptures' | 'rolling_window'
    """
    n = len(series)
    if n < 8:
        return cast("dict[str, object]", {"insufficient_data": True, "n": n})

    y = _values(series)
    if not _is_finite(y):
        return cast(
            "dict[str, object]", {"insufficient_data": True, "n": n, "reason": "non_finite_values"}
        )

    method = "ruptures"
    cp = _ruptures_inflection(y)
    if cp is None:
        method = "rolling_window"
        cp = _rolling_window_inflection(y)
    if cp is None or cp <= 1 or cp >= n - 1:
        return cast(
            "dict[str, object]",
            {"n": n, "inflection_period": None, "magnitude": 0.0, "method": method},
        )

    prior = y[:cp]
    post = y[cp:]
    s_prior, _, _ = _linreg(prior)
    s_post, _, _ = _linreg(post)
    std_total = float(np.std(y))
    delta_mean = float(abs(prior.mean() - post.mean()))
    magnitude = (delta_mean / std_total) if std_total > 0 else 0.0

    # Suppress trivial inflections — both slopes nearly equal AND small delta
    if magnitude < 0.5 and abs(s_prior - s_post) < (abs(s_prior) * 0.25 + 1e-9):
        return cast(
            "dict[str, object]",
            {"n": n, "inflection_period": None, "magnitude": magnitude, "method": method},
        )

    return cast(
        "dict[str, object]",
        {
            "n": n,
            "inflection_index": int(cp),
            "inflection_period": series[cp].period_end.date().isoformat(),
            "prior_slope": s_prior,
            "post_slope": s_post,
            "prior_mean": float(prior.mean()),
            "post_mean": float(post.mean()),
            "magnitude": magnitude,
            "method": method,
        },
    )


# ---------------------------------------------------------------------------
# 3. rolling_zscore_anomalies
# ---------------------------------------------------------------------------


def rolling_zscore_anomalies(
    series: list[Observation],
    *,
    window: int = 8,
    threshold: float = 2.5,
) -> list[dict[str, object]]:
    """Quarters whose value is > threshold standard deviations from the
    trailing-window mean.

    Window is the number of preceding observations used to compute the
    baseline; the observation itself is NOT included so the z-score
    measures "how far is THIS quarter from the recent regime". Returns
    [] when the series is shorter than window+1.
    """
    if window < 3 or len(series) < window + 1:
        return []
    y = _values(series)
    if not _is_finite(y):
        return []
    anomalies: list[dict[str, object]] = []
    for i in range(window, y.size):
        baseline = y[i - window : i]
        b_mean = float(baseline.mean())
        b_std = float(baseline.std(ddof=1)) if baseline.size > 1 else 0.0
        if b_std == 0.0:
            continue
        z = (float(y[i]) - b_mean) / b_std
        if abs(z) >= threshold:
            anomalies.append(
                cast(
                    "dict[str, object]",
                    {
                        "period_end": series[i].period_end.date().isoformat(),
                        "value": float(y[i]),
                        "zscore": z,
                        "baseline_mean": b_mean,
                        "baseline_std": b_std,
                        "window": window,
                    },
                )
            )
    return anomalies


# ---------------------------------------------------------------------------
# 4. seasonal_decompose
# ---------------------------------------------------------------------------


def _classical_decompose(y: np.ndarray, period: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Pure-numpy additive classical decomposition.

    1. Trend: centered moving average of width `period`. For odd period a
       simple MA; for even period a 2x-MA (average of two adjacent MAs)
       so the trend lines up with integer indices.
    2. Detrended = y - trend (NaN at endpoints).
    3. Seasonal = average of detrended at each phase (mean over all
       periods at that within-cycle index), centered to sum to zero.
    4. Residual = y - trend - seasonal.
    """
    n = y.size
    trend = np.full(n, np.nan)
    # Centered moving average
    half = period // 2
    if period % 2 == 0:
        # 2x centered MA
        for i in range(half, n - half):
            window = y[i - half : i + half + 1]
            # Standard 2x even-MA: weight endpoints by 0.5
            w = np.ones(window.size)
            w[0] = 0.5
            w[-1] = 0.5
            trend[i] = float(np.sum(w * window) / np.sum(w))
    else:
        for i in range(half, n - half):
            trend[i] = float(np.mean(y[i - half : i + half + 1]))

    detrended = y - trend
    # Seasonal: mean of detrended at each phase position
    seasonal_phase = np.zeros(period)
    counts = np.zeros(period)
    for i in range(n):
        if not np.isnan(detrended[i]):
            seasonal_phase[i % period] += detrended[i]
            counts[i % period] += 1
    seasonal_phase = np.where(counts > 0, seasonal_phase / np.maximum(counts, 1), 0.0)
    # Center to mean zero
    seasonal_phase -= seasonal_phase.mean()

    seasonal = np.array([seasonal_phase[i % period] for i in range(n)])
    residual = y - trend - seasonal
    return trend, seasonal, residual


def seasonal_decompose(series: list[Observation], period: int = 4) -> dict[str, object]:
    """Trend / seasonal / residual decomposition.

    Uses statsmodels STL when available and the series is long enough
    (>= 2*period). Falls back to classical additive decomposition
    (pure-numpy) for shorter series or when statsmodels is missing.

    Period default is 4 (quarterly). Returns aligned lists matching the
    input series; NaN slots at the endpoints are stringified as None for
    JSON portability.
    """
    n = len(series)
    if n < max(2 * period, 4) or period < 2:
        return cast("dict[str, object]", {"insufficient_data": True, "n": n, "period": period})

    y = _values(series)
    if not _is_finite(y):
        return cast(
            "dict[str, object]", {"insufficient_data": True, "n": n, "reason": "non_finite_values"}
        )

    method = "classical"
    trend = seasonal = residual = None
    if n >= 2 * period + 2:
        try:
            from statsmodels.tsa.seasonal import STL  # type: ignore[import-not-found]

            # statsmodels is untyped; STL().fit() returns DecomposeResult with
            # trend/seasonal/resid attributes — cast to object so pyright
            # accepts the attribute access without unknown-type errors
            stl = cast("object", STL(y, period=period, robust=True).fit())
            trend = np.asarray(getattr(stl, "trend"), dtype=float)  # noqa: B009
            seasonal = np.asarray(getattr(stl, "seasonal"), dtype=float)  # noqa: B009
            residual = np.asarray(getattr(stl, "resid"), dtype=float)  # noqa: B009
            method = "stl"
        except Exception as exc:  # broad except: fall through to classical decomposition
            log.debug({"event": "stl_failed", "error": str(exc)})

    if trend is None or seasonal is None or residual is None:
        trend, seasonal, residual = _classical_decompose(y, period)

    def _to_json(arr: np.ndarray) -> list[float | None]:
        return [None if (math.isnan(v) or math.isinf(v)) else float(v) for v in arr.tolist()]

    return cast(
        "dict[str, object]",
        {
            "n": n,
            "period": period,
            "method": method,
            "period_ends": [o.period_end.date().isoformat() for o in series],
            "trend": _to_json(trend),
            "seasonal": _to_json(seasonal),
            "residual": _to_json(residual),
            "seasonal_strength": _seasonal_strength(seasonal, residual),
        },
    )


def _seasonal_strength(seasonal: np.ndarray, residual: np.ndarray) -> float:
    """Hyndman's strength-of-seasonality: max(0, 1 - var(R) / var(S + R)).
    1.0 = pure seasonal; 0.0 = no seasonality."""
    sr = seasonal + residual
    valid = ~np.isnan(sr)
    if valid.sum() < 4:
        return 0.0
    var_sr = float(np.var(sr[valid]))
    var_r = float(np.var(residual[~np.isnan(residual)]))
    if var_sr <= 0:
        return 0.0
    return float(max(0.0, 1.0 - var_r / var_sr))


# ---------------------------------------------------------------------------
# 5. correlation_matrix — cross-KPI Pearson correlations
# ---------------------------------------------------------------------------


def correlation_matrix(
    ticker: str,
    kpi_names: list[str],
    *,
    lookback_quarters: int = 16,
    repo_root: Path | None = None,
    db_path: Path | None = None,
) -> dict[str, object]:
    """Pearson cross-correlations across the listed KPIs.

    For each pair (kpi_i, kpi_j), aligns observations by period_end (inner
    join across all KPIs in the matrix), trims to the most recent
    `lookback_quarters`, then computes Pearson r. Periods missing from
    any KPI in the matrix are dropped from the JOIN — strict alignment.

    Returns the matrix as nested dicts so JSON round-trips don't need a
    custom encoder. Diagonal is 1.0. Cells with insufficient overlap
    (< 4 aligned obs) are None.
    """
    if not kpi_names:
        return cast("dict[str, object]", {"insufficient_data": True, "kpis": []})

    # Local import to avoid circular dependency on the loaders module at
    # primitives-import time. correlation_matrix is the only primitive that
    # itself reads the DB; everything else operates on a pre-loaded series.
    from timeseries.loaders import load_kpi_series

    series_by_kpi: dict[str, list[Observation]] = {}
    for k in kpi_names:
        s = load_kpi_series(ticker=ticker, kpi_name=k, repo_root=repo_root, db_path=db_path)
        if s:
            series_by_kpi[k] = s

    if len(series_by_kpi) < 2:
        return cast(
            "dict[str, object]",
            {
                "insufficient_data": True,
                "reason": "fewer_than_two_kpis_loaded",
                "kpis_loaded": list(series_by_kpi.keys()),
            },
        )

    # Inner join on period_end across all loaded series. The reduce is split
    # out so pyright infers the element type of the intersection (set.intersection
    # over *args defeats its inference).
    period_sets: list[set[datetime]] = [{o.period_end for o in s} for s in series_by_kpi.values()]
    intersection: set[datetime] = period_sets[0].copy()
    for ps in period_sets[1:]:
        intersection &= ps
    common_periods: list[datetime] = sorted(intersection)
    if len(common_periods) < 4:
        return cast(
            "dict[str, object]",
            {
                "insufficient_data": True,
                "reason": "insufficient_overlap",
                "n_overlap": len(common_periods),
            },
        )
    # Trim to the most recent lookback_quarters
    common_periods = common_periods[-lookback_quarters:]
    aligned: dict[str, list[float]] = {}
    for k, s in series_by_kpi.items():
        index: dict[datetime, float] = {o.period_end: float(o.value) for o in s}
        aligned[k] = [index[p] for p in common_periods]

    kpis = list(aligned.keys())
    matrix: dict[str, dict[str, float | None]] = {}
    for k1 in kpis:
        matrix[k1] = {}
        for k2 in kpis:
            if k1 == k2:
                matrix[k1][k2] = 1.0
                continue
            a = np.asarray(aligned[k1], dtype=float)
            b = np.asarray(aligned[k2], dtype=float)
            if a.size < 4 or float(a.std()) == 0.0 or float(b.std()) == 0.0:
                matrix[k1][k2] = None
                continue
            r = float(np.corrcoef(a, b)[0, 1])
            matrix[k1][k2] = r if math.isfinite(r) else None

    return cast(
        "dict[str, object]",
        {
            "ticker": ticker.upper(),
            "kpis": kpis,
            "n_overlap": len(common_periods),
            "period_start": common_periods[0].date().isoformat(),
            "period_end": common_periods[-1].date().isoformat(),
            "matrix": matrix,
        },
    )


# ---------------------------------------------------------------------------
# 6. yoy_acceleration — second derivative of YoY growth
# ---------------------------------------------------------------------------


def yoy_acceleration(series: list[Observation]) -> dict[str, object]:
    """Second derivative of YoY growth.

    YoY growth at quarter t is (value[t] - value[t-4]) / value[t-4]. The
    first derivative of THAT series (the change in YoY between consecutive
    quarters) catches acceleration / deceleration. This function returns
    both the YoY series and its first-difference, so a "decelerating
    deceleration" — where YoY is still negative but less so — surfaces
    in the most_recent_delta.

    Insufficient data: < 8 observations (need >= 4 for first YoY +
    >= 4 more for the difference series). Returns
    {"insufficient_data": True} in that case.

    Returns:
        yoy_series: list of {period_end, yoy} aligned to input[4:]
        yoy_acceleration: list of {period_end, delta} aligned to input[5:]
        most_recent_yoy: most recent YoY growth rate
        most_recent_delta: most recent quarter-over-quarter change in YoY
        trend: 'accelerating' | 'decelerating' | 'flat' based on last
               4 delta observations
    """
    n = len(series)
    if n < 8:
        return cast("dict[str, object]", {"insufficient_data": True, "n": n})

    y = _values(series)
    if not _is_finite(y):
        return cast(
            "dict[str, object]", {"insufficient_data": True, "n": n, "reason": "non_finite_values"}
        )

    # YoY: value[t] vs value[t-4]
    yoy: list[dict[str, object]] = []
    for t in range(4, n):
        prior = float(y[t - 4])
        if prior == 0.0:
            continue  # avoid divide-by-zero; skip the quarter
        g = (float(y[t]) - prior) / abs(prior)
        yoy.append({"period_end": series[t].period_end.date().isoformat(), "yoy": g})

    if len(yoy) < 2:
        return cast(
            "dict[str, object]",
            {"insufficient_data": True, "n": n, "reason": "insufficient_yoy_overlap"},
        )

    # First difference of YoY = the acceleration term
    accel: list[dict[str, object]] = []
    for i in range(1, len(yoy)):
        delta = float(cast("float", yoy[i]["yoy"])) - float(cast("float", yoy[i - 1]["yoy"]))
        accel.append({"period_end": yoy[i]["period_end"], "delta": delta})

    most_recent_yoy = float(cast("float", yoy[-1]["yoy"]))
    most_recent_delta = float(cast("float", accel[-1]["delta"])) if accel else 0.0

    # Trend from last <= 4 delta observations
    last_deltas = [float(cast("float", a["delta"])) for a in accel[-4:]]
    mean_delta = sum(last_deltas) / len(last_deltas) if last_deltas else 0.0
    if abs(mean_delta) < 0.005:
        trend = "flat"
    elif mean_delta > 0:
        trend = "accelerating"
    else:
        trend = "decelerating"

    return cast(
        "dict[str, object]",
        {
            "n": n,
            "yoy_series": yoy,
            "yoy_acceleration": accel,
            "most_recent_yoy": most_recent_yoy,
            "most_recent_delta": most_recent_delta,
            "trend": trend,
        },
    )
