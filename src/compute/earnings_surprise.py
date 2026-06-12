"""Per-holding beat-rate aggregator on top of `earnings_surprises`.

Pure read-only — consumes the rows that `execution/ingest_earnings_surprises.py`
upserts and returns aggregated beat-rate / average-surprise stats per ticker.
Companion to `compute/holding_scorecard.py:_commitment_scorecard()` (Say-Do
hit rate); both compose into `HoldingScorecard`.

EPS and Revenue are tracked separately because revenue coverage degrades to
"no data" after an FMP plan lapse (yfinance, the fallback source, doesn't
publish historical revenue actual-vs-estimate). The scorecard exposes this
explicitly via `revenue.no_data` so consumers can detect coverage loss
without inferring it from a zero beat-rate.

Beat semantics: surprise_pct >= 0 = beat, < 0 = miss. Meeting consensus
exactly is counted as a beat (industry convention — "in line" reads as a
non-miss). NULL surprise_pct (e.g. yfinance EPS-only on the revenue side)
is bucketed into `no_data` and excluded from the beat-rate denominator.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class SurpriseMetric:
    """Beat-rate stats for ONE side of the surprise table (EPS or Revenue)
    over a fixed lookback window.

    Fields:
      beats         records with surprise_pct >= 0
      misses        records with surprise_pct < 0
      no_data       records where surprise_pct is NULL (source didn't publish it)
      beat_rate_pct beats / (beats + misses) × 100, rounded 1dp.
                    None when (beats + misses) == 0 (no usable history).
      avg_surprise_pct  mean of the non-null surprise_pct values, rounded 2dp.
      latest_surprise_pct  surprise_pct of the most recent record in the window
                           (None if that record's value is itself NULL).
    """

    beats: int
    misses: int
    no_data: int
    beat_rate_pct: Decimal | None
    avg_surprise_pct: Decimal | None
    latest_surprise_pct: Decimal | None


@dataclass(frozen=True)
class SurpriseScorecard:
    """Two-sided beat-rate rollup for one ticker over the lookback window.

    `total_quarters` counts how many records are in the window. `eps` and
    `revenue` carry the actual stats. If the table has no rows for the ticker,
    `total_quarters == 0` and both sides are empty (all counts = 0, all rates
    = None) — callers can short-circuit on `total_quarters == 0` to skip
    rendering.
    """

    total_quarters: int
    eps: SurpriseMetric
    revenue: SurpriseMetric


_EMPTY_METRIC = SurpriseMetric(
    beats=0,
    misses=0,
    no_data=0,
    beat_rate_pct=None,
    avg_surprise_pct=None,
    latest_surprise_pct=None,
)
EMPTY_SCORECARD = SurpriseScorecard(
    total_quarters=0,
    eps=_EMPTY_METRIC,
    revenue=_EMPTY_METRIC,
)


def _to_decimal(v: object) -> Decimal | None:
    """SQLite NUMERIC round-trips as str | int | float | None depending on the
    stored representation. Normalize to Decimal | None."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        if v != v or v in (float("inf"), float("-inf")):
            return None
        return Decimal(str(v))
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return Decimal(s)
        except Exception:
            return None
    return None


def _aggregate_one_side(
    surprise_pcts: Sequence[Decimal | None], latest_value: Decimal | None
) -> SurpriseMetric:
    """Compute the metric for one side (EPS or Revenue) from an ordered list
    of surprise % values. `latest_value` is the first element's value (kept
    separate so callers can pass it without re-indexing); the caller is
    responsible for ordering most-recent-first.

    Beat/miss bucketing:
      - None             → no_data
      - >= 0             → beat
      - < 0              → miss
    """
    beats = 0
    misses = 0
    no_data = 0
    total = Decimal(0)
    matched = 0
    for v in surprise_pcts:
        if v is None:
            no_data += 1
            continue
        total += v
        matched += 1
        if v >= 0:
            beats += 1
        else:
            misses += 1
    decided = beats + misses
    beat_rate = (
        (Decimal(beats) / Decimal(decided) * Decimal(100)).quantize(Decimal("0.1"))
        if decided > 0
        else None
    )
    avg = (total / Decimal(matched)).quantize(Decimal("0.01")) if matched > 0 else None
    return SurpriseMetric(
        beats=beats,
        misses=misses,
        no_data=no_data,
        beat_rate_pct=beat_rate,
        avg_surprise_pct=avg,
        latest_surprise_pct=latest_value,
    )


def surprise_scorecard_for(
    conn: sqlite3.Connection, ticker: str, *, lookback_quarters: int = 8
) -> SurpriseScorecard:
    """Build the EPS+Revenue scorecard for one ticker over the last N quarters.

    Reads `earnings_surprises` for the ticker, orders by release_date DESC
    (most recent first), takes the top `lookback_quarters` rows, and aggregates.

    Returns `EMPTY_SCORECARD` if there are no rows for the ticker — i.e. the
    ingest hasn't landed for this name yet. Callers should treat that as "skip
    rendering" rather than "0% beat rate".
    """
    if lookback_quarters <= 0:
        return EMPTY_SCORECARD
    cur = conn.execute(
        "SELECT release_date, eps_surprise_pct, revenue_surprise_pct "
        "FROM earnings_surprises WHERE ticker = ? "
        "ORDER BY release_date DESC LIMIT ?",
        (ticker.upper(), lookback_quarters),
    )
    rows = cur.fetchall()
    if not rows:
        return EMPTY_SCORECARD
    eps_values = [_to_decimal(r["eps_surprise_pct"]) for r in rows]
    rev_values = [_to_decimal(r["revenue_surprise_pct"]) for r in rows]
    # Most recent is rows[0] (ORDER BY DESC); index 0 is latest.
    return SurpriseScorecard(
        total_quarters=len(rows),
        eps=_aggregate_one_side(eps_values, latest_value=eps_values[0]),
        revenue=_aggregate_one_side(rev_values, latest_value=rev_values[0]),
    )
