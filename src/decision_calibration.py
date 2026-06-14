"""Decisions-history calibration — deterministic SQL, no LLM (S15 PR2).

The decisions ledger (0046/0086) accumulates graded outcomes, but the panel
only ever showed the raw timeline. This module computes the analysis lenses
the directive names, all from plain aggregation:

  hit rate by conviction   — of the decisions stated at each conviction
      level, how many graded correct? The denominator is GRADED decisions
      (correct + wrong + mixed); pending and unfalsifiable rows are shown
      but never scored — a hit rate over unresolved calls would flatter.
  reversal patterns        — every ``user_action_kind='reversed'`` row,
      crossed with how the recommendation eventually graded: reversing a
      call that graded *wrong* vindicates the override; reversing one that
      graded *correct* cost money. The action mix (followed / ignored /
      partial / reversed / unacted) frames how often the owner overrides
      at all.
  time-to-outcome          — days from ``made_at`` to ``outcome_at`` per
      recommendation kind (mean + median): how long a call stays open
      before it can be judged.
  cohort curve (L8)        — the same hit rate GROUPED BY ``made_at`` quarter
      (or year), so "am I getting better?" is answered as a trend, not a
      single all-time number. Each period also carries its conviction-
      calibration gap (did high-conviction calls grade above the rest that
      period?) and reversal-cost count. Periods bucket on the CALL date, not
      the grade date, so the curve tracks when judgement improved.

``build_calibration`` returns None when the DB or decisions table is absent
(the panel hides the section) — the decision_extractor best-effort posture.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import median

CONVICTION_ORDER: tuple[str, ...] = ("high", "medium", "low", "unstated")
GRADED_LABELS: frozenset[str] = frozenset({"correct", "wrong", "mixed"})
ACTION_KINDS: tuple[str, ...] = ("followed", "ignored", "partial", "reversed")

# Cohort granularity for the period-over-period "am I getting better?" curve.
COHORT_GRANULARITIES: frozenset[str] = frozenset({"quarter", "year"})
DEFAULT_COHORT_GRANULARITY = "quarter"


def bucket_for_conviction(value: object) -> str:
    """Map a stated conviction to a calibration cohort bucket — the one place
    the two conviction vocabularies are reconciled. ``decisions.conviction`` is
    already a high|medium|low string; ``position_sizing_intent`` states it
    numerically as n/5. Both land in CONVICTION_ORDER so the focus-cohort
    lookup (L1) and the advisor's per-name calibration (L1/L8) compare like
    with like. Anything unrecognised → 'unstated'."""
    if value is None:
        return "unstated"
    if isinstance(value, str):
        v = value.strip().lower()
        return v if v in CONVICTION_ORDER else "unstated"
    try:
        n = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "unstated"
    if n >= 4.0:
        return "high"
    if n >= 3.0:
        return "medium"
    if n >= 1.0:
        return "low"
    return "unstated"


def period_key(made_at: object, granularity: str) -> tuple[str, str] | None:
    """``(sort_key, label)`` for a decision's ``made_at`` under ``granularity``
    ('quarter' → ('2026Q1', '2026-Q1'); 'year' → ('2026', '2026')). None when
    the stamp is unparseable — that row is left out of the cohort curve but
    still counted in the flat stats. Reads the year/month off the ISO prefix
    (the ledger's made_at is 'YYYY-MM-DD...' in every era), so it never depends
    on the naive/aware mix the duration math has to reconcile."""
    s = str(made_at or "")
    if len(s) < 7 or s[4] != "-":
        return None
    try:
        year = int(s[0:4])
        month = int(s[5:7])
    except ValueError:
        return None
    if not (1 <= month <= 12):
        return None
    if granularity == "year":
        return f"{year:04d}", f"{year:04d}"
    quarter = (month - 1) // 3 + 1
    return f"{year:04d}Q{quarter}", f"{year:04d}-Q{quarter}"


@dataclass(frozen=True, slots=True)
class CohortPeriod:
    """One period's slice of the ledger — the unit of the "am I getting better?"
    curve. Periods GROUP BY ``made_at`` (the call date, not the grade date), so
    the trend tracks when the analyst's JUDGEMENT improved, not when grades
    happened to land. ``hit_rate`` is None for a period with nothing graded yet
    (recent calls still open) — a gap in the curve, never a fabricated zero."""

    period: str  # display label, e.g. "2026-Q1"
    sort_key: str  # chronological sort key, e.g. "2026Q1"
    total: int  # all decisions MADE in the period
    graded: int  # the graded subset
    correct: int
    hit_rate: float | None  # correct / graded; None when nothing graded
    # Conviction-calibration gap: the high-conviction hit-rate minus the rest's
    # within the SAME period. Positive = high-conviction calls earned their
    # label (graded above the rest); negative = overconfident on the highs. None
    # when either side has no graded call in the period (no gap to measure).
    high_graded: int
    high_hit_rate: float | None
    rest_hit_rate: float | None
    conviction_gap: float | None
    reversals: int  # calls reversed that were MADE in the period
    reversals_cost: int  # of those, how many the reversal cost (call graded correct)


@dataclass(frozen=True, slots=True)
class ConvictionBucket:
    """Outcome distribution of one stated-conviction level."""

    conviction: str  # high | medium | low | unstated
    graded: int  # correct + wrong + mixed
    correct: int
    wrong: int
    mixed: int
    ungraded: int  # pending / unfalsifiable / not yet judged
    hit_rate: float | None  # correct / graded; None when nothing graded


@dataclass(frozen=True, slots=True)
class ReversalRecord:
    """One decision the owner reversed, with the eventual verdict."""

    decision_id: int
    ticker: str
    kind: str
    made_at: str  # ISO date
    outcome_label: str | None
    # True — the call graded wrong, the override was right. False — the call
    # graded correct, the override cost. None — still unresolved/mixed.
    vindicated: bool | None


@dataclass(frozen=True, slots=True)
class KindTiming:
    """Days from made_at to outcome_at for one recommendation kind."""

    kind: str
    n: int
    avg_days: float
    median_days: float


@dataclass(frozen=True, slots=True)
class CalibrationStats:
    """Everything the decisions panel's calibration section renders."""

    total: int
    graded: int
    overall_hit_rate: float | None
    by_conviction: list[ConvictionBucket]
    action_mix: dict[str, int]  # ACTION_KINDS + 'unacted'
    reversals: list[ReversalRecord]  # newest-first
    reversals_vindicated: int
    reversals_cost: int
    time_to_outcome: list[KindTiming]  # by kind, descending n
    # Period-over-period cohorts (L8): the trend the flat stats can't show.
    # Defaulted so the pre-L8 hand-constructed call shape (advisor/socratic
    # fixtures) still builds — build_calibration always supplies them.
    cohorts: list[CohortPeriod] = field(default_factory=list[CohortPeriod])
    cohort_granularity: str = DEFAULT_COHORT_GRANULARITY  # 'quarter' | 'year'
    # Latest graded period's hit-rate minus the prior graded period's — the
    # curve's direction. None when fewer than two periods have a graded call.
    hit_rate_delta: float | None = None
    improving: bool | None = None  # hit_rate_delta > 0 ("am I getting better?")


def _open(db_path: Path | str) -> sqlite3.Connection | None:
    try:
        path = Path(db_path)
        if not path.exists():
            return None
        conn = sqlite3.connect(str(path), timeout=5.0)
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.row_factory = sqlite3.Row
        if (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='decisions'"
            ).fetchone()
            is None
        ):
            conn.close()
            return None
        return conn
    except (sqlite3.Error, OSError):
        return None


def _days_between(made_at: object, outcome_at: object) -> float | None:
    try:
        made = datetime.fromisoformat(str(made_at))
        graded = datetime.fromisoformat(str(outcome_at))
    except (TypeError, ValueError):
        return None
    # The ledger mixes naive and aware stamps across eras; compare naive.
    return (graded.replace(tzinfo=None) - made.replace(tzinfo=None)).total_seconds() / 86_400.0


@dataclass(slots=True)
class _PeriodAcc:
    """Mutable per-period tally — the cohort curve's working state. Typed (not
    a dict[str, object]) so the rate derivations stay strict-clean."""

    label: str
    total: int = 0
    graded: int = 0
    correct: int = 0
    high_graded: int = 0
    high_correct: int = 0
    rest_graded: int = 0
    rest_correct: int = 0
    reversals: int = 0
    reversals_cost: int = 0


def _accumulate_period(
    periods: dict[str, _PeriodAcc],
    keyed: tuple[str, str],
    *,
    is_graded: bool,
    is_correct: bool,
    is_high: bool,
    reversed_cost: bool,
    reversed_any: bool,
) -> None:
    """Fold one decision into its period bucket. The bucket carries the raw
    tallies the cohort curve needs; rates are derived once at build time."""
    sort_key, label = keyed
    acc = periods.get(sort_key)
    if acc is None:
        acc = _PeriodAcc(label=label)
        periods[sort_key] = acc
    acc.total += 1
    if is_graded:
        acc.graded += 1
        if is_high:
            acc.high_graded += 1
            acc.high_correct += 1 if is_correct else 0
        else:
            acc.rest_graded += 1
            acc.rest_correct += 1 if is_correct else 0
        acc.correct += 1 if is_correct else 0
    if reversed_any:
        acc.reversals += 1
        if reversed_cost:
            acc.reversals_cost += 1


def _rate(correct: int, graded: int) -> float | None:
    return (correct / graded) if graded else None


def _build_cohorts(
    periods: dict[str, _PeriodAcc],
) -> tuple[list[CohortPeriod], float | None, bool | None]:
    """Sorted cohort list + the latest-vs-prior graded hit-rate delta. The
    delta walks the periods that actually have a graded call (a fully-pending
    recent quarter is a gap in the curve, not a break in the trend)."""
    cohorts: list[CohortPeriod] = []
    for sort_key in sorted(periods):
        acc = periods[sort_key]
        high_hr = _rate(acc.high_correct, acc.high_graded)
        rest_hr = _rate(acc.rest_correct, acc.rest_graded)
        gap = (high_hr - rest_hr) if (high_hr is not None and rest_hr is not None) else None
        cohorts.append(
            CohortPeriod(
                period=acc.label,
                sort_key=sort_key,
                total=acc.total,
                graded=acc.graded,
                correct=acc.correct,
                hit_rate=_rate(acc.correct, acc.graded),
                high_graded=acc.high_graded,
                high_hit_rate=high_hr,
                rest_hit_rate=rest_hr,
                conviction_gap=gap,
                reversals=acc.reversals,
                reversals_cost=acc.reversals_cost,
            )
        )
    graded_rates = [c.hit_rate for c in cohorts if c.hit_rate is not None]
    delta: float | None = None
    improving: bool | None = None
    if len(graded_rates) >= 2:
        delta = graded_rates[-1] - graded_rates[-2]
        improving = delta > 0
    return cohorts, delta, improving


def build_calibration(
    *, db_path: Path | str, cohort_granularity: str = DEFAULT_COHORT_GRANULARITY
) -> CalibrationStats | None:
    """One pass over the decisions table → the three flat lenses PLUS the
    period-over-period cohort curve. None when the substrate is absent; a
    present-but-empty ledger returns zeroed stats (the panel renders the
    how-to-populate hint instead of hiding).

    ``cohort_granularity`` ('quarter' | 'year') buckets the curve; an
    unrecognised value falls back to the quarter default rather than raising —
    a thin coaching surface must never crash on a bad arg."""
    if cohort_granularity not in COHORT_GRANULARITIES:
        cohort_granularity = DEFAULT_COHORT_GRANULARITY
    conn = _open(db_path)
    if conn is None:
        return None
    try:
        rows = conn.execute(
            "SELECT id, ticker, recommendation_kind, conviction, made_at, "
            "user_action_kind, outcome_at, outcome_label FROM decisions"
        ).fetchall()
    finally:
        conn.close()

    counts: dict[str, dict[str, int]] = {
        c: {"correct": 0, "wrong": 0, "mixed": 0, "ungraded": 0} for c in CONVICTION_ORDER
    }
    action_mix: dict[str, int] = {k: 0 for k in (*ACTION_KINDS, "unacted")}
    reversals: list[ReversalRecord] = []
    durations: dict[str, list[float]] = {}
    periods: dict[str, _PeriodAcc] = {}
    graded_total = correct_total = 0

    for row in rows:
        label = str(row["outcome_label"] or "pending")
        is_graded = label in GRADED_LABELS
        is_correct = label == "correct"

        conviction = str(row["conviction"] or "").lower()
        if conviction not in CONVICTION_ORDER:
            conviction = "unstated"
        bucket = counts[conviction]
        if is_graded:
            bucket[label] += 1
            graded_total += 1
            if is_correct:
                correct_total += 1
        else:
            bucket["ungraded"] += 1

        action = str(row["user_action_kind"] or "").lower()
        is_reversed = action == "reversed"
        action_mix[action if action in ACTION_KINDS else "unacted"] += 1
        if is_reversed:
            vindicated: bool | None = None
            if label == "wrong":
                vindicated = True
            elif label == "correct":
                vindicated = False
            reversals.append(
                ReversalRecord(
                    decision_id=int(row["id"]),
                    ticker=str(row["ticker"]).upper(),
                    kind=str(row["recommendation_kind"]),
                    made_at=str(row["made_at"] or "")[:10],
                    outcome_label=None if label == "pending" else label,
                    vindicated=vindicated,
                )
            )

        if is_graded and row["outcome_at"] is not None:
            days = _days_between(row["made_at"], row["outcome_at"])
            if days is not None and days >= 0:
                durations.setdefault(str(row["recommendation_kind"]), []).append(days)

        keyed = period_key(row["made_at"], cohort_granularity)
        if keyed is not None:
            _accumulate_period(
                periods,
                keyed,
                is_graded=is_graded,
                is_correct=is_correct,
                is_high=conviction == "high",
                reversed_cost=is_reversed and label == "correct",
                reversed_any=is_reversed,
            )

    by_conviction = [
        ConvictionBucket(
            conviction=c,
            graded=(g := b["correct"] + b["wrong"] + b["mixed"]),
            correct=b["correct"],
            wrong=b["wrong"],
            mixed=b["mixed"],
            ungraded=b["ungraded"],
            hit_rate=(b["correct"] / g) if g else None,
        )
        for c, b in counts.items()
        if (b["correct"] + b["wrong"] + b["mixed"] + b["ungraded"]) > 0
    ]
    reversals.sort(key=lambda r: r.made_at, reverse=True)
    timing = sorted(
        (
            KindTiming(
                kind=kind,
                n=len(days),
                avg_days=sum(days) / len(days),
                median_days=median(days),
            )
            for kind, days in durations.items()
        ),
        key=lambda t: -t.n,
    )
    cohorts, hit_rate_delta, improving = _build_cohorts(periods)
    return CalibrationStats(
        total=len(rows),
        graded=graded_total,
        overall_hit_rate=(correct_total / graded_total) if graded_total else None,
        by_conviction=by_conviction,
        action_mix=action_mix,
        reversals=reversals,
        reversals_vindicated=sum(1 for r in reversals if r.vindicated is True),
        reversals_cost=sum(1 for r in reversals if r.vindicated is False),
        time_to_outcome=timing,
        cohorts=cohorts,
        cohort_granularity=cohort_granularity,
        hit_rate_delta=hit_rate_delta,
        improving=improving,
    )
