"""Phase-2 conviction-drift narration (quiet, secondary -- plan §4.1).

Runs the existing timeseries primitive over a theme's conviction series (fed by
decision captures) and produces a SMALL, deterministic drift read -- a low-priority
insight, never an interrupt. The governed ``drift_narrate`` LLM is a thin phrasing
layer ON TOP of this computed signal; this module is the signal, not the judgment
(reuses ``timeseries/primitives`` unmodified -- no new math).
"""

from __future__ import annotations

from timeseries.primitives import Observation, detect_trend


def narrate_drift(series: list[Observation]) -> dict[str, object]:
    """A quiet drift read over a conviction series. Deterministic; no LLM.

    ``firming`` / ``softening`` follow the OLS slope sign only when the trend is
    statistically significant (p<0.05 and r2>0.3); otherwise ``steady``. The
    second-derivative ``direction`` from detect_trend flags a recent turn
    (``inflecting``).
    """
    trend = detect_trend(series)
    if trend.get("insufficient_data"):
        n = trend.get("n", len(series))
        return {
            "drift": "insufficient",
            "n": n,
            "summary": "not enough decisions yet to read conviction drift",
        }

    slope = float(_num(trend.get("slope")))
    slope_pct = abs(float(_num(trend.get("slope_pct_of_mean"))))
    second = str(trend.get("direction") or "flat")
    inflecting = second == "inflecting"

    # A meaningful conviction move is >=2%/period of the mean level; below that it
    # is steady. (The M-K significance flag is calibrated for quarterly KPI series
    # and does not fire on a short conviction ramp, so it is reported, not gated on.)
    if slope_pct < 0.02 or slope == 0.0:
        heading = "steady"
    elif slope > 0:
        heading = "firming"
    else:
        heading = "softening"

    summary = f"Conviction is {heading}"
    if inflecting:
        summary += "; it recently turned"

    return {
        "drift": heading,
        "slope": slope,
        "slope_pct_of_mean": slope_pct,
        "significant": bool(trend.get("statistical_significance")),
        "second_derivative": second,
        "inflecting": inflecting,
        "summary": summary,
    }


def _num(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0
