"""Shared minimum-n guard for calibration-derived claims (Close-the-Loops L1).

Three surfaces draw the same conclusion from the same hazard: the calibration
pack/advisor (L1), the calibration coach (L8), and the self-calibrating
credibility engine (L10) all turn graded outcomes into a rate, then either
confront the owner with it ("your high-conviction calls grade 40%") or use it
to replace a hand-set constant. A rate built on n=3 is noise wearing a
percentage — confronting the owner with it, or letting it overwrite a
considered constant, is worse than staying quiet.

This is the ONE place that decides when a denominator is large enough to
assert, and the ONE place that frames a sparse one honestly, so the three
sessions never reinvent (and diverge on) the phrasing. Keep it tiny and
dependency-free — it is imported by packs, the advisor, and the provenance
engine alike.

Design notes:
- ``MIN_CONFIDENT_N`` is the default floor (10): a binomial rate over fewer
  than ten graded calls has a confidence interval wide enough to mislead. The
  directive's worked example treats n=12 as confident and n=3 as "low
  confidence", which this bracket honours. Callers that need a different bar
  (L10's tier-constant replacement may want a stricter one) pass ``min_n``.
- Nothing here hides data. Below the floor the number is still reported,
  explicitly tagged low-confidence — labelling uncertainty is more honest than
  suppressing the figure.
"""

from __future__ import annotations

import math

# Default denominator floor below which a rate is reported but never asserted
# bare. Ten graded observations is the rough point where a binomial proportion
# stops being dominated by sampling noise for a solo analyst's decision ledger.
MIN_CONFIDENT_N = 10

# z for a two-sided 95% interval (the standard reporting band). Pulled out so
# the Wilson helper and any caller agree on the same confidence level.
Z_95 = 1.959963984540054


def is_confident(n: int, *, min_n: int = MIN_CONFIDENT_N) -> bool:
    """True when ``n`` graded observations is enough to assert a rate without a
    hedge. The single predicate L1/L8/L10 gate on before confronting the owner
    or replacing a constant."""
    return n >= min_n


def confidence_note(n: int, *, min_n: int = MIN_CONFIDENT_N) -> str:
    """A parenthetical-ready clause naming the denominator and its trust:
    ``"n=12"`` when confident, ``"n=3, low confidence"`` when sparse,
    ``"n=0, no graded calls"`` when empty."""
    if n <= 0:
        return "n=0, no graded calls"
    if is_confident(n, min_n=min_n):
        return f"n={n}"
    return f"n={n}, low confidence"


def rate_phrase(label: str, rate: float | None, n: int, *, min_n: int = MIN_CONFIDENT_N) -> str:
    """One honest calibration line. ``rate`` is correct/graded in [0, 1] (or
    None when nothing graded), ``n`` the graded denominator.

    Confident:   ``"high-conviction calls 40% correct (n=12)"``
    Sparse:      ``"high-conviction calls 40% correct, but only n=3 — low confidence"``
    Empty:       ``"high-conviction calls: no graded calls yet (n=0)"``

    The percentage is always shown when there is one — the hedge qualifies it,
    it does not replace it (suppressing the figure would read as "no data" when
    the truth is "thin data").
    """
    if n <= 0 or rate is None:
        return f"{label}: no graded calls yet (n=0)"
    pct = round(100.0 * rate)
    if is_confident(n, min_n=min_n):
        return f"{label} {pct}% correct (n={n})"
    return f"{label} {pct}% correct, but only n={n} — low confidence"


def wilson_interval(correct: int, n: int, *, z: float = Z_95) -> tuple[float, float] | None:
    """Wilson score interval (default 95%) for a binomial proportion
    ``correct / n``. Returns ``(low, high)`` clamped to [0, 1], or None when
    ``n <= 0``.

    Wilson is the right interval for the small, skewed denominators a solo
    analyst's ledger produces: it stays inside [0, 1], and — unlike the normal
    approximation — it does NOT collapse to a zero-width point when the rate is
    0% or 100% (3/3 correct is "47-100%", honestly wide, not a false "100%
    ± 0"). The min-n guard still decides whether the point estimate may be
    asserted bare; this widens the *number* into a range when it is shown.
    """
    if n <= 0:
        return None
    p = correct / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    return max(0.0, center - margin), min(1.0, center + margin)


def rate_phrase_ci(
    label: str,
    rate: float | None,
    correct: int,
    n: int,
    *,
    min_n: int = MIN_CONFIDENT_N,
) -> str:
    """``rate_phrase`` widened with the Wilson 95% CI — so the advisor says
    "high-conviction calls 40% correct (95% CI 25-57%, n=12)" instead of a bare
    point estimate. Falls back to the plain phrase when there's nothing graded.
    """
    if n <= 0 or rate is None:
        return rate_phrase(label, rate, n, min_n=min_n)
    pct = round(100.0 * rate)
    ci = wilson_interval(correct, n)
    if ci is None:
        return rate_phrase(label, rate, n, min_n=min_n)
    lo, hi = ci
    tail = "" if is_confident(n, min_n=min_n) else ", low confidence"
    return f"{label} {pct}% correct (95% CI {round(100 * lo)}-{round(100 * hi)}%, n={n}{tail})"


__all__ = [
    "MIN_CONFIDENT_N",
    "Z_95",
    "confidence_note",
    "is_confident",
    "rate_phrase",
    "rate_phrase_ci",
    "wilson_interval",
]
