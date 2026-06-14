"""Measured confidence prior — replaces the hand-set tier constant once earned (L10 PR2).

``pipeline.confidence.TIER_BASE`` is a hand-set constant, uniform across every
ticker and line item. PR1 stood up the ground-truth ledger; this turns it into a
*measured* per-(source tier × ticker × line-item) prior that can replace the
constant — but only when the evidence is strong enough.

Two guards keep a sparse, biased denominator from overwriting a considered
constant:

* **Minimum-n gate** (the shared ``calibration_guard``): a cell must have at
  least ``min_n`` graded observations before its measured value is used at all.
  Below the floor the caller falls back to the constant — the directive's "ship
  the measurement now, swap the prior only once enough accrues."
* **Shrinkage toward the constant** (Beta-Binomial posterior mean): even past the
  gate the measured base is blended with the constant, weighted by sample size,
  so a just-confident cell (n≈10) moves the base only partway. A large, decisive
  sample moves it most of the way.

Backoff is hierarchical: the most *specific* confident cell wins —
``(tier, ticker, line_item)`` → ``(tier, line_item)`` → ``(tier)`` → the
constant. So a name with its own track record uses it; everyone else borrows the
broader tier rate; an untested tier keeps the constant.

Caveat honoured from PR1: the hold-rate is *conditional* (only facts a later
filing revisited or a disagreement contested), so it is evidence about a tier's
reliability, not its unconditional accuracy — which is exactly why the constant
is the shrinkage anchor rather than being discarded.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from calibration_guard import MIN_CONFIDENT_N, is_confident

# How many observations the hand-set constant is "worth" as a prior. At
# n == SHRINKAGE_PSEUDOCOUNT the measured base is a 50/50 blend of the observed
# hold-rate and the constant; as n grows it converges to the observed rate.
SHRINKAGE_PSEUDOCOUNT = float(MIN_CONFIDENT_N)


@dataclass(frozen=True, slots=True)
class _Cell:
    """Accumulated (count, held-count) for one aggregation key."""

    n: int = 0
    held: int = 0

    def plus(self, outcome: int) -> _Cell:
        return _Cell(n=self.n + 1, held=self.held + (1 if outcome else 0))

    @property
    def observed_rate(self) -> float:
        return self.held / self.n if self.n else 0.0


def _shrink(cell: _Cell, *, constant: float, pseudocount: float) -> float:
    """Beta-Binomial posterior mean: blend the observed hold-rate with the
    constant prior, weighted by sample size."""
    return (cell.held + pseudocount * constant) / (cell.n + pseudocount)


@dataclass(frozen=True, slots=True)
class MeasuredPriors:
    """A queryable measured prior over the confidence-observation ledger.

    ``tier_base`` is the only consumer surface: it returns the measured base for
    the most-specific cell that clears the min-n gate, shrunk toward the supplied
    constant, or the constant itself when nothing qualifies.
    """

    by_full: dict[tuple[str, str, str], _Cell] = field(
        default_factory=dict[tuple[str, str, str], _Cell]
    )
    by_tier_item: dict[tuple[str, str], _Cell] = field(default_factory=dict[tuple[str, str], _Cell])
    by_tier: dict[str, _Cell] = field(default_factory=dict[str, _Cell])
    min_n: int = MIN_CONFIDENT_N
    pseudocount: float = SHRINKAGE_PSEUDOCOUNT

    def _confident_cell(self, tier: str, ticker: str | None, line_item: str | None) -> _Cell | None:
        """The most specific cell that clears the min-n gate, or None."""
        if ticker is not None and line_item is not None:
            cell = self.by_full.get((tier, ticker.upper(), line_item))
            if cell is not None and is_confident(cell.n, min_n=self.min_n):
                return cell
        if line_item is not None:
            cell = self.by_tier_item.get((tier, line_item))
            if cell is not None and is_confident(cell.n, min_n=self.min_n):
                return cell
        cell = self.by_tier.get(tier)
        if cell is not None and is_confident(cell.n, min_n=self.min_n):
            return cell
        return None

    def tier_base(
        self,
        tier: str | None,
        *,
        fallback: float,
        ticker: str | None = None,
        line_item: str | None = None,
    ) -> float:
        """The measured base for ``(tier, ticker, line_item)`` shrunk toward
        ``fallback``, or ``fallback`` when no cell clears the min-n gate."""
        if tier is None:
            return fallback
        cell = self._confident_cell(tier, ticker, line_item)
        if cell is None:
            return fallback
        return round(_shrink(cell, constant=fallback, pseudocount=self.pseudocount), 4)

    def measured_cell(
        self, tier: str, ticker: str | None = None, line_item: str | None = None
    ) -> tuple[int, float] | None:
        """(n, observed_rate) for the most-specific confident cell, or None.
        Drives the panel's "measured vs constant" display."""
        cell = self._confident_cell(tier, ticker, line_item)
        return None if cell is None else (cell.n, round(cell.observed_rate, 4))


def build_measured_priors(
    conn: sqlite3.Connection, *, user_id: str = "bhanu", min_n: int = MIN_CONFIDENT_N
) -> MeasuredPriors:
    """Aggregate the ledger into the three backoff levels. Returns an empty
    (all-fallback) MeasuredPriors when the table is absent or empty."""
    priors = MeasuredPriors(min_n=min_n)
    try:
        rows = conn.execute(
            "SELECT source_tier, ticker, line_item, outcome "
            "FROM confidence_observations WHERE user_id = ? AND source_tier IS NOT NULL",
            (user_id,),
        ).fetchall()
    except sqlite3.Error:
        return priors
    for r in rows:
        tier = str(r[0])
        ticker = None if r[1] is None else str(r[1]).upper()
        line_item = None if r[2] is None else str(r[2])
        outcome = int(r[3])
        priors.by_tier[tier] = priors.by_tier.get(tier, _Cell()).plus(outcome)
        if line_item is not None:
            key2 = (tier, line_item)
            priors.by_tier_item[key2] = priors.by_tier_item.get(key2, _Cell()).plus(outcome)
            if ticker is not None:
                key3 = (tier, ticker, line_item)
                priors.by_full[key3] = priors.by_full.get(key3, _Cell()).plus(outcome)
    return priors
