"""Phase-2 conviction-drift narration (quiet, secondary -- plan §4.1).

Runs the existing timeseries primitive over a theme's conviction series (fed by
decision captures) and produces a SMALL, deterministic drift read -- a low-priority
insight, never an interrupt. The governed ``drift_narrate`` LLM is a thin phrasing
layer ON TOP of this computed signal; this module is the signal, not the judgment
(reuses ``timeseries/primitives`` unmodified -- no new math).

The LLM phrasing is OPT-IN: ``narrate_drift`` fires no LLM by default (a drift read
is cheap and cosmetic), so the deterministic summary always stands unless a caller
explicitly passes ``narrate_fn=llm_narrate`` (or its own). The injected narrator may
ONLY reword the human ``summary`` string -- it can never change the drift/slope/
inflecting classification, which is computed here and authoritative.
"""

from __future__ import annotations

from collections.abc import Callable

from timeseries.primitives import Observation, detect_trend

# A phrasing layer: given the computed signal dict, return one sentence, or None to
# keep the deterministic baseline. NEVER changes the classification -- wording only.
NarrateFn = Callable[["dict[str, object]"], "str | None"]


def narrate_drift(
    series: list[Observation], *, narrate_fn: NarrateFn | None = None
) -> dict[str, object]:
    """A quiet drift read over a conviction series. Deterministic classification.

    ``firming`` / ``softening`` follow the OLS slope sign only when the move is
    material (>=2%/period of the mean); otherwise ``steady``. The second-derivative
    ``direction`` from detect_trend flags a recent turn (``inflecting``).

    ``narrate_fn`` (default None -> no LLM) may reword ONLY the ``summary`` string;
    if it is absent, raises, or returns nothing usable, the deterministic summary
    stands. Pass ``narrate_fn=llm_narrate`` to opt into the governed phrasing.
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

    baseline = f"Conviction is {heading}"
    if inflecting:
        baseline += "; it recently turned"

    signal: dict[str, object] = {
        "drift": heading,
        "slope": slope,
        "slope_pct_of_mean": slope_pct,
        "significant": bool(trend.get("statistical_significance")),
        "second_derivative": second,
        "inflecting": inflecting,
        "baseline_summary": baseline,
    }
    summary = _phrase(signal, narrate_fn) or baseline

    return {
        "drift": heading,
        "slope": slope,
        "slope_pct_of_mean": slope_pct,
        "significant": bool(trend.get("statistical_significance")),
        "second_derivative": second,
        "inflecting": inflecting,
        "summary": summary,
    }


def _phrase(signal: dict[str, object], narrate_fn: NarrateFn | None) -> str | None:
    """Return a truthy one-line phrasing, or None to keep the deterministic baseline.

    No narrator wired -> None (no LLM by default). A narrator that raises or returns
    a non-truthy string degrades to None: a cosmetic reword must NEVER break the
    drift read, so ALL exceptions are swallowed here (this is the one purpose where
    even a budget/setup hard stop is non-fatal -- the deterministic summary stands).
    """
    if narrate_fn is None:
        return None
    try:
        out = narrate_fn(signal)
    except Exception:
        return None
    return out.strip() if isinstance(out, str) and out.strip() else None


def llm_narrate(signal: dict[str, object]) -> str | None:
    """The governed ``drift_narrate`` phrasing layer -- a caller opts in by passing
    this as ``narrate_fn``. Free-text (one sentence), lazily imported so importing
    this module never pulls the LLM CLI."""
    from llm.cli import call_llm

    prompt = (
        "You rephrase a PRE-COMPUTED conviction-drift signal into ONE short, plain "
        "sentence for an investor's private journal. The classification below is already "
        "decided and is AUTHORITATIVE: do NOT re-judge, reverse, hedge, quantify, or add "
        "analysis, numbers, tickers, or advice. Restate ONLY what is given, in at most 12 "
        "words. 'firming' stays positive-direction, 'softening' negative-direction, "
        "'steady' flat; if inflecting is true, note it recently turned. Output plain text "
        "only - no JSON, quotes, preamble, or markdown.\n\n"
        f"drift={signal.get('drift')} inflecting={signal.get('inflecting')}\n"
        f"Baseline to improve on: {signal.get('baseline_summary')}\n"
        "Sentence:"
    )
    stripped = call_llm(prompt, purpose="drift_narrate", scope="ledger_drift").strip()
    return stripped or None


def _num(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0
