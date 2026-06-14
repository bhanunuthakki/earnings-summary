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

# Default denominator floor below which a rate is reported but never asserted
# bare. Ten graded observations is the rough point where a binomial proportion
# stops being dominated by sampling noise for a solo analyst's decision ledger.
MIN_CONFIDENT_N = 10


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


__all__ = ["MIN_CONFIDENT_N", "confidence_note", "is_confident", "rate_phrase"]
