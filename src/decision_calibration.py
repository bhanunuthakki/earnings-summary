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

from calibration_guard import (
    MIN_CONFIDENT_N,
    confidence_note,
    is_confident,
    rate_phrase,
    wilson_interval,
)
from integrations.portfolio_tracker_client import ExitQuality, PositionAlpha

CONVICTION_ORDER: tuple[str, ...] = ("high", "medium", "low", "unstated")
GRADED_LABELS: frozenset[str] = frozenset({"correct", "wrong", "mixed"})

# Predicted probability each stated conviction level IMPLIES — "when I call a
# name high-conviction, I expect to be right ~this often". The Brier score
# (L-seam 2) scores those implied probabilities against the binary graded
# outcome, so the owner learns whether his conviction language is calibrated or
# just loud. 'unstated' has no implied probability and is left out of scoring.
CONVICTION_PROBABILITY: dict[str, float] = {"high": 0.75, "medium": 0.55, "low": 0.40}
ACTION_KINDS: tuple[str, ...] = ("followed", "ignored", "partial", "reversed")

# The omission side (L11): a graded 'avoid' that RAN AWAY is an error of omission.
# The grader inverts the verdict for passes (a passed name that rallied grades
# 'wrong'), so 'wrong' avoid rows are the misses and 'correct' the dodges.
_MAX_OMISSION_MISSES = 5

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
class Expectancy:
    """Batting-vs-slugging for one cohort (L-seam 1): the size of the wins, not
    just how often they land. ``batting`` is owned by the hit-rate buckets; this
    is the dollar view over the names with a tracker-supplied realized magnitude
    (window $-alpha vs SPY: ``/position-alpha`` for open/traded names,
    ``/exit-quality`` for fully-closed ones). Win/loss is classified by the
    magnitude SIGN (the trading-desk convention), so it complements — does not
    restate — the grade-based hit rate.

    ``expectancy`` is the mean magnitude per graded call: positive = the cohort
    makes money in expectation. ``slugging`` = avg_win / avg_loss (None when the
    cohort has no win or no loss to size the ratio). Magnitude is the name's
    whole-window alpha, so multiple calls on one ticker share it — a thin-book
    diagnostic, gated by the same min-n guard, never a settled verdict."""

    n: int  # graded calls with a usable magnitude
    wins: int  # magnitude > 0
    losses: int  # magnitude < 0
    avg_win: float | None  # mean positive magnitude
    avg_loss: float | None  # mean |negative magnitude|
    slugging: float | None  # avg_win / avg_loss
    expectancy: float  # mean magnitude per graded call (total / n)
    total: float  # summed magnitude


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
    # Wilson 95% CI on the hit-rate (L-seam 3) — so the rate is shown as a band,
    # not a misleading point estimate on a thin denominator. None when nothing
    # graded. Defaulted so positional hand-construction (tests) still builds.
    wilson_low: float | None = None
    wilson_high: float | None = None
    # The dollar view (L-seam 1) — None when no tracker magnitude was supplied
    # (the panels build calibration offline; the advisor supplies it).
    expectancy: Expectancy | None = None


@dataclass(frozen=True, slots=True)
class ConvictionReliabilityRow:
    """One conviction level's predicted-vs-observed point on the reliability
    curve (L-seam 2): the probability the level IMPLIES vs the rate it actually
    graded. ``gap`` > 0 = overconfident at this level."""

    conviction: str  # high | medium | low
    predicted: float  # the implied probability (CONVICTION_PROBABILITY)
    observed: float  # correct / (correct + wrong) at this level
    n: int  # correct + wrong (mixed excluded from the binary score)

    @property
    def gap(self) -> float:
        """Predicted − observed. Positive = the conviction overstated the rate."""
        return self.predicted - self.observed


@dataclass(frozen=True, slots=True)
class ConvictionCalibration:
    """Proper-scoring Brier + reliability curve on the owner's OWN conviction
    (L-seam 2). ``brier`` is the mean squared error between each call's implied
    probability (from its stated conviction) and its binary outcome (correct=1,
    wrong=0; mixed/unfalsifiable excluded). ``baseline_brier`` is the score of
    always predicting the base rate — the no-discrimination reference; a brier
    below it means the conviction LEVELS carry real signal, not just volume.
    Lower brier is better (0 = perfect, 0.25 = a coin)."""

    n: int  # graded correct/wrong calls with a stated conviction
    brier: float | None
    baseline_brier: float | None
    base_rate: float | None  # overall correct rate over the scored subset
    rows: list[ConvictionReliabilityRow] = field(default_factory=list[ConvictionReliabilityRow])

    @property
    def beats_baseline(self) -> bool | None:
        """True when conviction discriminates better than the flat base rate.
        None when either score is missing."""
        if self.brier is None or self.baseline_brier is None:
            return None
        return self.brier < self.baseline_brier


PROCESS_QUALITY_ORDER: tuple[str, ...] = ("sound", "flawed", "lucky")


@dataclass(frozen=True, slots=True)
class ProcessOutcomeMatrix:
    """Process quality × outcome (Track B seam 8) — the two-axis read the flat
    hit-rate can't give. ``cells`` is keyed (process_quality, outcome) → count
    over graded, process-scored decisions. The two headline diagonals name the
    cases the owner most needs to see: ``right_for_wrong_reasons`` (graded
    correct but the process was flawed/lucky — don't bank the edge) and
    ``wrong_for_right_reasons`` (graded wrong but the process was sound — don't
    punish the discipline)."""

    total_scored: int
    cells: dict[tuple[str, str], int]
    right_for_wrong_reasons: int
    wrong_for_right_reasons: int
    sound_and_correct: int


@dataclass(frozen=True, slots=True)
class OmissionMiss:
    """One pass that ran away — a graded 'avoid' the name rallied past, the
    omission side's worst-case evidence (the named 'you passed on a winner')."""

    ticker: str
    made_at: str  # ISO date
    outcome_pct: float | None  # the move since the pass (positive = it ran)
    note: str | None  # the grader's outcome_notes


@dataclass(frozen=True, slots=True)
class OmissionStats:
    """The errors-of-omission ledger (L11): how the names the owner PASSED on
    turned out. ``missed`` = graded 'wrong' (the pass ran away — opportunity
    cost); ``dodged`` = graded 'correct' (the pass was a non-mover/decliner).
    ``miss_rate`` is missed/graded — surface it min-n gated, never bare."""

    graded: int  # graded avoid decisions (correct + wrong + mixed)
    dodged: int  # correct — avoiding cost nothing
    missed: int  # wrong — the name ran away (the omission)
    mixed: int
    miss_rate: float | None  # missed / graded; None when nothing graded
    worst_misses: list[OmissionMiss]  # the passes that ran the most, biggest first


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
    # The errors-of-omission ledger (L11): how the passed names graded. Defaulted
    # so the pre-L11 hand-constructed call shapes still build; build_calibration
    # always supplies it (None only when there are zero avoid rows).
    omissions: OmissionStats | None = None
    # Book-wide batting-vs-slugging (L-seam 1) — None when no tracker magnitudes
    # were supplied (offline panels); the per-conviction view rides on each
    # ConvictionBucket.expectancy.
    expectancy: Expectancy | None = None
    # Proper-scoring Brier + reliability on the owner's own conviction (L-seam
    # 2). None when no graded correct/wrong call carries a stated conviction.
    conviction_calibration: ConvictionCalibration | None = None
    # Process quality x outcome (Track B seam 8). None when no graded decision
    # has been process-scored yet (pre-0114 schema, or nothing scored).
    process_outcome: ProcessOutcomeMatrix | None = None


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


def realized_magnitudes(
    position_alpha: PositionAlpha | None,
    exit_quality: ExitQuality | None = None,
) -> dict[str, float]:
    """``ticker → realized window magnitude`` for the batting-vs-slugging view.

    One consistent unit throughout — dollar alpha vs the SPY counterfactual:
    ``/position-alpha``'s per-name ``alpha`` for open/traded names (takes
    precedence), with ``/exit-quality``'s ``exit_alpha_vs_spy`` filling in
    fully-closed names that have left the position book. Built by the caller
    (advisor / panel) from already-fetched tracker payloads so
    ``build_calibration`` stays decoupled from the wire shape."""
    out: dict[str, float] = {}
    if position_alpha is not None:
        for row in position_alpha.rows:
            if row.ticker and row.alpha is not None:
                out[row.ticker.upper()] = float(row.alpha)
    if exit_quality is not None:
        for row in exit_quality.rows:
            t = (row.ticker or "").upper()
            if t and t not in out and row.exit_alpha_vs_spy is not None:
                out[t] = float(row.exit_alpha_vs_spy)
    return out


@dataclass(slots=True)
class _MagAcc:
    """Mutable win/loss magnitude tally — the expectancy working state."""

    n: int = 0
    wins: int = 0
    losses: int = 0
    sum_win: float = 0.0
    sum_loss: float = 0.0  # accumulated as POSITIVE magnitudes
    total: float = 0.0

    def add(self, magnitude: float) -> None:
        self.n += 1
        self.total += magnitude
        if magnitude > 0:
            self.wins += 1
            self.sum_win += magnitude
        elif magnitude < 0:
            self.losses += 1
            self.sum_loss += -magnitude


def _to_expectancy(acc: _MagAcc | None) -> Expectancy | None:
    if acc is None or acc.n == 0:
        return None
    avg_win = (acc.sum_win / acc.wins) if acc.wins else None
    avg_loss = (acc.sum_loss / acc.losses) if acc.losses else None
    slugging = (
        (avg_win / avg_loss) if (avg_win is not None and avg_loss not in (None, 0.0)) else None
    )
    return Expectancy(
        n=acc.n,
        wins=acc.wins,
        losses=acc.losses,
        avg_win=avg_win,
        avg_loss=avg_loss,
        slugging=slugging,
        expectancy=acc.total / acc.n,
        total=acc.total,
    )


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
    *,
    db_path: Path | str,
    cohort_granularity: str = DEFAULT_COHORT_GRANULARITY,
    magnitudes_by_ticker: dict[str, float] | None = None,
) -> CalibrationStats | None:
    """One pass over the decisions table → the three flat lenses PLUS the
    period-over-period cohort curve. None when the substrate is absent; a
    present-but-empty ledger returns zeroed stats (the panel renders the
    how-to-populate hint instead of hiding).

    ``cohort_granularity`` ('quarter' | 'year') buckets the curve; an
    unrecognised value falls back to the quarter default rather than raising —
    a thin coaching surface must never crash on a bad arg.

    ``magnitudes_by_ticker`` (L-seam 1) optionally supplies each name's realized
    window magnitude (see ``realized_magnitudes``); when present the overall +
    per-conviction batting-vs-slugging (``Expectancy``) is computed alongside
    the hit-rate. Omitted by the offline panels (expectancy stays None)."""
    if cohort_granularity not in COHORT_GRANULARITIES:
        cohort_granularity = DEFAULT_COHORT_GRANULARITY
    mags = magnitudes_by_ticker or {}
    conn = _open(db_path)
    if conn is None:
        return None
    try:
        # outcome_pct/outcome_notes feed the L11 omission ledger; both exist on
        # every real DB (0046) but may be absent on a hand-rolled/old schema —
        # degrade to NULL rather than error (the best-effort posture).
        cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)").fetchall()}
        opct = "outcome_pct" if "outcome_pct" in cols else "NULL AS outcome_pct"
        onotes = "outcome_notes" if "outcome_notes" in cols else "NULL AS outcome_notes"
        pq = "process_quality" if "process_quality" in cols else "NULL AS process_quality"
        rows = conn.execute(
            "SELECT id, ticker, recommendation_kind, conviction, made_at, "
            f"user_action_kind, outcome_at, outcome_label, {opct}, {onotes}, {pq} FROM decisions"
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
    # Magnitude accumulators (L-seam 1): overall + per conviction bucket. Stay
    # empty (→ expectancy None) when no magnitudes were supplied.
    overall_mag = _MagAcc()
    bucket_mag: dict[str, _MagAcc] = {c: _MagAcc() for c in CONVICTION_ORDER}
    # Brier accumulators (L-seam 2): squared error of each stated conviction's
    # implied probability vs the binary outcome, over correct/wrong calls.
    brier_sum = 0.0
    brier_n = 0
    brier_correct = 0
    # Process-quality x outcome matrix (Track B seam 8): counts keyed
    # (process_quality, outcome) over graded, process-scored rows.
    proc_cells: dict[tuple[str, str], int] = {}
    # Omission accumulators (L11): the graded 'avoid' rows, inverted so a pass
    # that ran away ('wrong') is the miss.
    om_graded = om_dodged = om_missed = om_mixed = 0
    om_misses: list[OmissionMiss] = []

    for row in rows:
        label = str(row["outcome_label"] or "pending")
        is_graded = label in GRADED_LABELS
        is_correct = label == "correct"

        if str(row["recommendation_kind"] or "") == "avoid" and is_graded:
            om_graded += 1
            if label == "correct":
                om_dodged += 1
            elif label == "wrong":
                om_missed += 1
                opct = row["outcome_pct"]
                om_misses.append(
                    OmissionMiss(
                        ticker=str(row["ticker"]).upper(),
                        made_at=str(row["made_at"] or "")[:10],
                        outcome_pct=float(opct) if opct is not None else None,
                        note=str(row["outcome_notes"])
                        if row["outcome_notes"] is not None
                        else None,
                    )
                )
            else:
                om_mixed += 1

        conviction = str(row["conviction"] or "").lower()
        if conviction not in CONVICTION_ORDER:
            conviction = "unstated"
        bucket = counts[conviction]
        if is_graded:
            bucket[label] += 1
            graded_total += 1
            if is_correct:
                correct_total += 1
            if mags:
                m = mags.get(str(row["ticker"] or "").upper())
                if m is not None:
                    overall_mag.add(m)
                    bucket_mag[conviction].add(m)
            if conviction in CONVICTION_PROBABILITY and label in ("correct", "wrong"):
                outcome = 1.0 if is_correct else 0.0
                brier_sum += (CONVICTION_PROBABILITY[conviction] - outcome) ** 2
                brier_n += 1
                brier_correct += 1 if is_correct else 0
            pq = str(row["process_quality"] or "").lower()
            if pq in PROCESS_QUALITY_ORDER:
                key = (pq, label)
                proc_cells[key] = proc_cells.get(key, 0) + 1
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

    by_conviction: list[ConvictionBucket] = []
    for c, b in counts.items():
        if (b["correct"] + b["wrong"] + b["mixed"] + b["ungraded"]) == 0:
            continue
        g = b["correct"] + b["wrong"] + b["mixed"]
        ci = wilson_interval(b["correct"], g) if g else None
        by_conviction.append(
            ConvictionBucket(
                conviction=c,
                graded=g,
                correct=b["correct"],
                wrong=b["wrong"],
                mixed=b["mixed"],
                ungraded=b["ungraded"],
                hit_rate=(b["correct"] / g) if g else None,
                wilson_low=ci[0] if ci else None,
                wilson_high=ci[1] if ci else None,
                expectancy=_to_expectancy(bucket_mag.get(c)),
            )
        )
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
    conviction_cal: ConvictionCalibration | None = None
    if brier_n:
        base_rate = brier_correct / brier_n
        rel_rows: list[ConvictionReliabilityRow] = []
        for c in ("high", "medium", "low"):
            b = counts[c]
            cw = b["correct"] + b["wrong"]
            if cw == 0:
                continue
            rel_rows.append(
                ConvictionReliabilityRow(
                    conviction=c,
                    predicted=CONVICTION_PROBABILITY[c],
                    observed=b["correct"] / cw,
                    n=cw,
                )
            )
        conviction_cal = ConvictionCalibration(
            n=brier_n,
            brier=brier_sum / brier_n,
            baseline_brier=base_rate * (1.0 - base_rate),
            base_rate=base_rate,
            rows=rel_rows,
        )
    process_outcome: ProcessOutcomeMatrix | None = None
    total_scored = sum(proc_cells.values())
    if total_scored:
        process_outcome = ProcessOutcomeMatrix(
            total_scored=total_scored,
            cells=proc_cells,
            right_for_wrong_reasons=proc_cells.get(("flawed", "correct"), 0)
            + proc_cells.get(("lucky", "correct"), 0),
            wrong_for_right_reasons=proc_cells.get(("sound", "wrong"), 0),
            sound_and_correct=proc_cells.get(("sound", "correct"), 0),
        )
    omissions: OmissionStats | None = None
    if om_graded:
        om_misses.sort(
            key=lambda m: m.outcome_pct if m.outcome_pct is not None else 0.0, reverse=True
        )
        omissions = OmissionStats(
            graded=om_graded,
            dodged=om_dodged,
            missed=om_missed,
            mixed=om_mixed,
            miss_rate=om_missed / om_graded,
            worst_misses=om_misses[:_MAX_OMISSION_MISSES],
        )
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
        omissions=omissions,
        expectancy=_to_expectancy(overall_mag),
        conviction_calibration=conviction_cal,
        process_outcome=process_outcome,
    )


def omission_clause(om: OmissionStats | None) -> str | None:
    """One honest, min-n-framed clause summarising the errors-of-omission ledger
    (L11) — shared by the L1 calibration pack and the L8 coach grounding so the
    two never diverge on phrasing. The caller prefixes its own label
    ('omissions — ' / 'OMISSIONS — '). None when nothing graded yet to surface."""
    if om is None or not om.graded:
        return None
    misses = "; ".join(
        f"{m.ticker} {f'+{m.outcome_pct * 100:.0f}%' if m.outcome_pct is not None else 'ran'}"
        for m in om.worst_misses
    )
    clause = (
        f"of {om.graded} passed names graded, {om.missed} ran away from you (missed), "
        f"{om.dodged} you correctly dodged ({confidence_note(om.graded)})"
    )
    if misses:
        clause += f"; biggest passes that ran: {misses}"
    return clause


# =========================================================================== #
# Advice-influence read (tenet-2 Phase 5, §5.2, docs/design/
# tenet2_advisory_program.md) — graded decision outcomes crossed by "advice
# delivered before the decision vs not" x "followed vs overridden". Rides
# ``v_decision_journal`` (alembic 0179) rather than re-deriving the
# advice-before window join here — this module doesn't need to know the
# lookback window or the memo-kind/agent-source exclusions; the view already
# resolved them. Purely descriptive, per the shared min-n floor
# (calibration_guard): below it, counts still print but no verdict/comparison
# prose is ever synthesized here or by any caller.
# =========================================================================== #

# user_action_kind values (decisions.user_action_kind) that count as the owner
# having overridden rather than followed the advice/recommendation.
_OVERRIDDEN_ACTION_KINDS: frozenset[str] = frozenset({"ignored", "partial", "reversed"})

# The four (advice_before, action) cells, in a fixed, stable display order.
_ADVICE_INFLUENCE_LABELS: tuple[tuple[bool, str, str], ...] = (
    (True, "followed", "advice-before, followed"),
    (True, "overridden", "advice-before, overridden"),
    (False, "followed", "no advice, followed"),
    (False, "overridden", "no advice, overridden"),
)


@dataclass(frozen=True, slots=True)
class AdviceInfluenceBucket:
    """One cell of the advice-before x followed/overridden partition. ``phrase``
    is the shared ``calibration_guard.rate_phrase`` rendering — descriptive
    only, hedged below the min-n floor, never a bare assertion."""

    label: str
    n: int
    correct: int
    hit_rate: float | None
    phrase: str


@dataclass(frozen=True, slots=True)
class AdviceInfluenceStats:
    """The quarterly advice-influence read: graded ``decisions`` rows (correct/
    wrong/mixed) partitioned by whether advice existed before the decision
    (``v_decision_journal.advice_before_memo_id`` or ``linked_memo_id``) and
    whether the owner's ``user_action_kind`` reads as followed or overridden.
    Deliberately thin at n≈dozens — the point is a habit of asking, not
    statistical significance (§5.2)."""

    total_graded: int
    advice_before_n: int
    no_advice_n: int
    # Graded rows where advice-status is known but user_action_kind isn't
    # 'followed' or one of the overridden kinds (NULL, or an unrecognized
    # value) — excluded from the 4 crossed cells, reported so the total
    # accounts for every graded row honestly.
    unknown_action_n: int
    buckets: list[AdviceInfluenceBucket]
    notes: list[str]


def compute_advice_influence(
    db_path: Path | str, *, min_n: int = MIN_CONFIDENT_N
) -> AdviceInfluenceStats | None:
    """Read-only partition over ``v_decision_journal``. Returns None when the
    view doesn't exist (a DB stamped before 0179, or a hand-DDL test fixture)
    — the caller renders/persists nothing rather than fabricating an all-zero
    read for a substrate that was never built."""
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT outcome_label, user_action_kind, advice_before_memo_id, "
                "linked_memo_id FROM v_decision_journal"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return None

    cell_counts: dict[tuple[bool, str], list[int]] = {}
    unknown_action_n = 0
    advice_before_n = 0
    no_advice_n = 0
    total_graded = 0
    for r in rows:
        label = str(r["outcome_label"] or "")
        if label not in GRADED_LABELS:
            continue
        total_graded += 1
        has_advice = r["advice_before_memo_id"] is not None or r["linked_memo_id"] is not None
        if has_advice:
            advice_before_n += 1
        else:
            no_advice_n += 1
        action = r["user_action_kind"]
        if action == "followed":
            bucket_action = "followed"
        elif action in _OVERRIDDEN_ACTION_KINDS:
            bucket_action = "overridden"
        else:
            unknown_action_n += 1
            continue
        counts = cell_counts.setdefault((has_advice, bucket_action), [0, 0])
        counts[0] += 1
        if label == "correct":
            counts[1] += 1

    buckets: list[AdviceInfluenceBucket] = []
    for has_advice, bucket_action, display_label in _ADVICE_INFLUENCE_LABELS:
        n, correct = cell_counts.get((has_advice, bucket_action), [0, 0])
        hit_rate = (correct / n) if n else None
        buckets.append(
            AdviceInfluenceBucket(
                label=display_label,
                n=n,
                correct=correct,
                hit_rate=hit_rate,
                phrase=rate_phrase(display_label, hit_rate, n, min_n=min_n),
            )
        )

    notes: list[str] = []
    if not is_confident(total_graded, min_n=min_n):
        notes.append(
            f"{confidence_note(total_graded, min_n=min_n)} — too thin to conclude anything "
            "about advice influence; the counts above are the honest whole of it."
        )
    return AdviceInfluenceStats(
        total_graded=total_graded,
        advice_before_n=advice_before_n,
        no_advice_n=no_advice_n,
        unknown_action_n=unknown_action_n,
        buckets=buckets,
        notes=notes,
    )
