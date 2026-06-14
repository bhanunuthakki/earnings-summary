"""Reliability + Brier scoring over the confidence-observation ledger (L10).

This is the MEASUREMENT half of the credibility engine: it reads
``confidence_observations`` (the graded ground truth) and answers "is the stored
confidence number calibrated?" two ways:

* a **reliability table** — observations bucketed by their predicted confidence,
  with the *observed* hold-rate per bucket. A well-calibrated 0.9 bucket holds
  ~90% of the time; a gap between predicted and observed is miscalibration.
* a **Brier score** — mean squared error between the predicted confidence and
  the binary outcome (0 = the fact was wrong, 1 = it held). Lower is better;
  0.25 is the no-skill baseline of always guessing 0.5.

Both are also broken out per **source tier**, because that is the lever phase-2
turns: if FMP facts hold far less often than their 0.92 prior implies, the tier
constant is too high.

The denominator is honest and narrow by construction. We can only observe an
outcome for a fact a later filing revisited (a restatement chain) or that a
logged cross-source disagreement contested — so this is *conditional* reliability
("of the facts we could check, how many held?"), not unconditional accuracy.
``calibration_guard`` decides when a cell's denominator is large enough to
assert rather than report-with-a-hedge.

Pure read: returns an empty table (``total_n == 0``) when the ledger is empty,
and ``None`` only when the table itself is absent (pre-0106 DB). Never raises.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from calibration_guard import MIN_CONFIDENT_N, is_confident

# Bucket edges over the [0, 1] confidence range. The stored confidences cluster
# at the tier/method priors (0.65 / 0.72 / 0.79 / 0.92 / 0.98 / 1.0), so a flat
# decile split would leave most buckets empty; these five bands track the real
# distribution (everything below 0.6 is a heavily-penalized fact).
_BUCKET_EDGES: tuple[float, ...] = (0.0, 0.6, 0.7, 0.8, 0.9, 1.0001)


@dataclass(frozen=True, slots=True)
class ReliabilityBucket:
    """One predicted-confidence band and what actually happened in it."""

    lo: float
    hi: float
    n: int
    mean_predicted: float
    observed_rate: float  # fraction of graded facts that held (mean outcome)

    @property
    def confident(self) -> bool:
        return is_confident(self.n)

    @property
    def gap(self) -> float:
        """Predicted − observed. Positive = overconfident in this band."""
        return self.mean_predicted - self.observed_rate

    @property
    def label(self) -> str:
        hi = min(self.hi, 1.0)
        if self.lo == 0.0:
            return f"<{hi:.0%}"
        return f"{self.lo:.0%}-{hi:.0%}"


@dataclass(frozen=True, slots=True)
class TierReliability:
    """Per-source-tier reliability + Brier."""

    tier: str
    n: int
    mean_predicted: float
    observed_rate: float
    brier: float

    @property
    def confident(self) -> bool:
        return is_confident(self.n)

    @property
    def gap(self) -> float:
        return self.mean_predicted - self.observed_rate


@dataclass(frozen=True, slots=True)
class ReliabilityTable:
    """The full measurement: buckets + per-tier rows + the headline Brier."""

    total_n: int
    brier: float | None
    observed_rate: float | None
    mean_predicted: float | None
    restatement_n: int
    disagreement_n: int
    buckets: list[ReliabilityBucket] = field(default_factory=list[ReliabilityBucket])
    tiers: list[TierReliability] = field(default_factory=list[TierReliability])
    min_n: int = MIN_CONFIDENT_N

    @property
    def has_confident_cell(self) -> bool:
        """True when at least one bucket or tier has crossed the min-n floor —
        the gate phase-2 reads before letting a measured prior replace a
        constant."""
        return any(b.confident for b in self.buckets) or any(t.confident for t in self.tiers)


@dataclass(frozen=True, slots=True)
class _Obs:
    predicted: float
    outcome: int
    tier: str | None
    kind: str


def _brier(rows: list[_Obs]) -> float | None:
    if not rows:
        return None
    return round(sum((o.predicted - o.outcome) ** 2 for o in rows) / len(rows), 4)


def _mean_predicted(rows: list[_Obs]) -> float:
    return round(sum(o.predicted for o in rows) / len(rows), 4)


def _observed_rate(rows: list[_Obs]) -> float:
    return round(sum(o.outcome for o in rows) / len(rows), 4)


def _bucket_of(predicted: float) -> int:
    """Index of the bucket ``predicted`` falls in (last bucket is inclusive)."""
    for i in range(len(_BUCKET_EDGES) - 1):
        if _BUCKET_EDGES[i] <= predicted < _BUCKET_EDGES[i + 1]:
            return i
    return len(_BUCKET_EDGES) - 2


def _load_observations(conn: sqlite3.Connection, *, user_id: str) -> list[_Obs] | None:
    """All ledger rows as ``_Obs``, or None when the table is absent."""
    try:
        rows = conn.execute(
            "SELECT predicted_confidence, outcome, source_tier, kind "
            "FROM confidence_observations WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    except sqlite3.Error:
        return None
    out: list[_Obs] = []
    for r in rows:
        out.append(
            _Obs(
                predicted=float(r[0]),
                outcome=int(r[1]),
                tier=None if r[2] is None else str(r[2]),
                kind=str(r[3]),
            )
        )
    return out


def build_reliability_table(
    conn: sqlite3.Connection, *, user_id: str = "bhanu", min_n: int = MIN_CONFIDENT_N
) -> ReliabilityTable | None:
    """Aggregate the ledger into the reliability table + Brier. ``None`` when the
    ``confidence_observations`` table is absent (pre-0106 DB)."""
    observations = _load_observations(conn, user_id=user_id)
    if observations is None:
        return None
    if not observations:
        return ReliabilityTable(
            total_n=0,
            brier=None,
            observed_rate=None,
            mean_predicted=None,
            restatement_n=0,
            disagreement_n=0,
            min_n=min_n,
        )

    buckets: list[ReliabilityBucket] = []
    by_bucket: dict[int, list[_Obs]] = {}
    for o in observations:
        by_bucket.setdefault(_bucket_of(o.predicted), []).append(o)
    for i in range(len(_BUCKET_EDGES) - 1):
        rows = by_bucket.get(i, [])
        if not rows:
            continue
        buckets.append(
            ReliabilityBucket(
                lo=_BUCKET_EDGES[i],
                hi=_BUCKET_EDGES[i + 1],
                n=len(rows),
                mean_predicted=_mean_predicted(rows),
                observed_rate=_observed_rate(rows),
            )
        )

    tiers: list[TierReliability] = []
    by_tier: dict[str, list[_Obs]] = {}
    for o in observations:
        if o.tier is not None:
            by_tier.setdefault(o.tier, []).append(o)
    for tier, rows in sorted(by_tier.items()):
        brier = _brier(rows)
        tiers.append(
            TierReliability(
                tier=tier,
                n=len(rows),
                mean_predicted=_mean_predicted(rows),
                observed_rate=_observed_rate(rows),
                brier=brier if brier is not None else 0.0,
            )
        )

    return ReliabilityTable(
        total_n=len(observations),
        brier=_brier(observations),
        observed_rate=_observed_rate(observations),
        mean_predicted=_mean_predicted(observations),
        restatement_n=sum(1 for o in observations if o.kind == "restatement"),
        disagreement_n=sum(1 for o in observations if o.kind == "disagreement"),
        buckets=buckets,
        tiers=tiers,
        min_n=min_n,
    )
