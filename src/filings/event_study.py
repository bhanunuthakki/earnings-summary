"""P5 — our own event study on detected disclosure events
(``docs/design/disclosure_change_build_stack.md`` §P5).

**Why this exists.** KPI discontinuation has NO published effect size
(``docs/design/disclosure_change_signals.md`` §1.5). The nearest analogs —
guidance cessation (-4.8% 3-day CAR, Chen/Matsumoto/Rajgopal, *JAE* 2011) and
expected-but-missing disclosure (-41bps around the NEXT earnings announcement,
Zhou & Zhou, *JAR* 2020) — are for a DIFFERENT disclosure act (guidance, not
an operating KPI silently dropped from a filing). Measuring forward returns on
OUR OWN detected ``disclosure_events`` closes a genuine gap rather than
re-deriving a number that already exists in the literature.

**Pre-registration (written before any number in this module was computed —
see the honesty-bar rule in the P5 build-stack task: no window/bucket added or
dropped after seeing results).**

Windows, in trading days relative to day 0 (the first trading day on/after
the event's information-arrival date — see ``resolve_arrival_dates`` for what
"arrival" means per event type):

* ``announce_3d``  = trading-day offsets [-1, +1] (3 daily log-returns,
  roughly centered on arrival) — the Chen/Matsumoto/Rajgopal short-window
  shape.
* ``drift_1m``      = offsets [+2, +22]  (21 daily returns, starts the day
  after the announcement window closes)
* ``drift_3m``      = offsets [+2, +64]  (63 daily returns)
* ``drift_6m``      = offsets [+2, +127] (126 daily returns)

Both a short window AND multi-month drift are measured and reported for
EVERY bucket, because Cohen/Malloy/Nguyen (*JF* 2020) found NO announcement
effect and all the signal in drift — a short-window-only study would falsely
conclude there is nothing here.

Buckets, all reported, none cherry-picked:

1. ``ALL`` — every collapsed observation pooled (the top-line "is there
   anything here" cut).
2. By ``event_type`` alone, pooled across verdicts.
3. By ``verdict`` alone, pooled across event types.
4. By the full (``event_type``, ``verdict``) cross — the finest cut, and the
   one most likely to be underpowered.

``MIN_EVENTS_FOR_HEADLINE`` (5): a bucket below this still reports n and the
raw mean, but is flagged ``underpowered=True`` and must never be read as a
tradeable number.

**Unit of observation — the pseudo-replication fix.** Multiple
``disclosure_events`` rows routinely share ONE underlying filing (e.g. 23
``item_added`` rows from a single MELI 10-K). They all draw the identical
forward return, because they happened on the same information-arrival date.
Treating each row as an independent observation would silently multiply the
sample size and understate variance. The statistical unit here is therefore
one row per (``ticker``, ``arrival_date``, ``event_type``, ``verdict``) —
disclosure_events rows sharing that key are collapsed into one
``EventObservation`` before any return or CI is computed; ``n_underlying_events``
records how many rows collapsed into it, purely as metadata.

**Information-arrival date, never period_end:**

* Item-level events (``item_added``/``item_removed``/``item_reworded``, P0):
  ``disclosure_events.source_ref`` is the accession number of the filing that
  CONTAINS the changed language — joined straight to
  ``filing_sections.filing_date`` (source=``edgar_text``). Exact, always
  resolves for these three types in this repo's current coverage (P0's
  default concepts, risk_factors/mdna, come only from 10-K/20-F ``edgar_text``
  sections, which always carry a ``filing_date``).
* Metric-lifecycle events (``metric_discontinued``/``metric_relabeled``/
  ``metric_standard_transition``, P1): ``disclosure_events.fiscal_year`` /
  ``fiscal_period`` record the LAST period the tag WAS reported — NOT the
  period its absence became observable. Using that date would be
  ``period_end``-flavored look-ahead bias by a different name (the market
  cannot react to an absence before the filing that reveals it exists). The
  correct arrival date is the NEXT filing/announcement after that period:
    - Annual axis (``fiscal_period == 'FY'``): the next 10-K/20-F's
      ``filing_date`` from ``filing_sections`` (source=``edgar_text``).
    - Quarterly axis (``fiscal_period`` in Q1-Q3): this repo's cached
      ``filing_sections`` has NO 10-Q rows at all (edgar_text ingestion only
      covers annual filings so far — measured empirically, not assumed), and
      ``earnings_surprises`` release-date coverage does not reach back far
      enough for most of these events' periods either. Rather than
      approximate with a calendar-quarter-end guess (which would silently
      reintroduce the exact look-ahead-bias risk this rule exists to avoid),
      quarterly-axis metric events are EXCLUDED from the event study with an
      explicit, counted reason (``arrival_unresolved:quarterly_axis_no_data``)
      — see ``resolve_arrival_dates``.

**Earnings-surprise control.** Tone/disclosure signals correlate with the
earnings surprise almost mechanically (docs/design/disclosure_change_signals.md
§1.7); every rigorous paper in this literature controls for it before
claiming incremental content. Each observation is matched to the most recent
``earnings_surprises.eps_surprise_pct`` with ``release_date <= arrival_date``
(within ``_SURPRISE_MATCH_MAX_DAYS`` days — a looser match is not "the same
quarter" and would just add noise). A single pooled OLS
(``raw_car = a + b * eps_surprise_pct``) is fit ONCE per window across every
matched observation (not per-bucket — there is nowhere near enough data for a
per-bucket regression); each observation's residual is its surprise-controlled
return. Both the raw and residualized bucket means are reported side by side
— never only the more flattering one.

**Benchmark.** Market-adjusted (SPY) — chosen over a sector adjustment because
every tracked ticker has SPY price history on file, so no observation is
dropped for lack of a sector-ETF match. Sector-adjustment is a documented
follow-up, not attempted here (see the module's public
``DEFAULT_BENCHMARK_TICKER``).

**The honesty bar.** The cross-section is 44 tickers over a few years — a
TINY sample for return prediction. Every bucket reports its exact n and a
bootstrap 95% CI (percentile method, ``_BOOTSTRAP_DRAWS`` resamples, fixed
seed for reproducibility — chosen over a normal/t approximation because daily
equity returns are fat-tailed and n is too small to trust asymptotics).

**``n`` overstates independence for every bucket coarser than the full
(event_type, verdict) cross — read ``n_unique_ticker_dates`` instead.** One
filing routinely produces several disclosure_events rows of DIFFERENT types
or verdicts (an added risk factor and a removed one on the same 10-K), and
those rows all share the identical underlying price return. The
pseudo-replication fix (``collapse_events``) only merges rows that share
``event_type`` AND ``verdict``; the ``ALL`` / ``event_type=...`` /
``verdict=...`` bucket cuts pool ACROSS that boundary and so still contain
non-independent rows. ``BucketStat.n_unique_ticker_dates`` — the count of
distinct (ticker, arrival_date) pairs behind the bucket — is the true upper
bound on independent return draws, and ``underpowered`` is gated on IT, not
on the raw ``n``. Even that number overstates independence further: the same
44-ticker book means non-overlapping dates for the same firm are still not
fully independent (firm-specific persistence), and different firms in the
same window share undiversified market/sector shocks the SPY adjustment only
partly removes. Treat every n as an upper bound on power, never a floor.

A bucket below ``MIN_EVENTS_FOR_HEADLINE`` independent ticker-dates is marked
``underpowered`` and must not be read as a number to trade on. "Underpowered,
inconclusive" is a legitimate, useful finding.
"""

from __future__ import annotations

import sqlite3
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, Field

from allocation.price_history import daily_log_returns, load_daily_closes

# ---------------------------------------------------------------------------
# Pre-registered windows and buckets (see module docstring — do not add,
# remove, or reorder after computing results without re-running the whole
# study; that is exactly the p-hacking this design forbids).
# ---------------------------------------------------------------------------

#: name -> (start_offset, end_offset), inclusive, trading days relative to
#: day 0 (first trading day on/after the arrival date).
EVENT_STUDY_WINDOWS: dict[str, tuple[int, int]] = {
    "announce_3d": (-1, 1),
    "drift_1m": (2, 22),
    "drift_3m": (2, 64),
    "drift_6m": (2, 127),
}

MIN_EVENTS_FOR_HEADLINE = 5
DEFAULT_BENCHMARK_TICKER = "SPY"
_BOOTSTRAP_DRAWS = 5000
_BOOTSTRAP_SEED = 20260725  # fixed: idempotent output across re-runs.
_SURPRISE_MATCH_MAX_DAYS = 200
_MIN_REGRESSION_OBS = 10

_ITEM_EVENT_TYPES = ("item_added", "item_removed", "item_reworded")
_METRIC_EVENT_TYPES = (
    "metric_discontinued",
    "metric_relabeled",
    "metric_standard_transition",
)
ALL_EVENT_TYPES = _ITEM_EVENT_TYPES + _METRIC_EVENT_TYPES


# ---------------------------------------------------------------------------
# Typed contracts
# ---------------------------------------------------------------------------


class RawDisclosureEvent(BaseModel):
    """One ``disclosure_events`` row, exactly as needed by this module —
    never a raw ``sqlite3.Row``/``dict`` past the query boundary."""

    id: int
    ticker: str
    event_type: str
    verdict: str
    fiscal_year: int | None
    fiscal_period: str | None
    source_ref: str | None
    subject: str


class SkippedEvent(BaseModel):
    """A ``disclosure_events`` row that could not be placed in the study,
    with a reason code — never silently dropped."""

    id: int
    ticker: str
    event_type: str
    reason: str


class EventObservation(BaseModel):
    """One event-study unit: (ticker, arrival_date, event_type, verdict)
    after collapsing pseudo-replicated rows from the same filing/period.
    """

    ticker: str
    arrival_date: date
    event_type: str
    verdict: str
    n_underlying_events: int = Field(ge=1)
    subjects: list[str] = Field(default_factory=list[str])


@dataclass(slots=True)
class WindowReturn:
    """Raw and benchmark-adjusted cumulative log return for one observation
    over one pre-registered window; ``None`` fields mean the window did not
    fully fit in the available price/benchmark calendar."""

    observation_index: int
    window: str
    raw_car: float | None
    adjusted_car: float | None
    eps_surprise_pct: float | None


class BucketStat(BaseModel):
    """One (bucket, window) cell of the final report.

    ``n`` counts (ticker, arrival_date, event_type, verdict) observations —
    already collapsed once for the same-filing pseudo-replication (see module
    docstring). But the ALL / event_type-only / verdict-only bucket cuts
    still pool ACROSS event types and verdicts, and one filing routinely
    produces several of those (an added risk factor and a removed one on the
    same 10-K, say) — rows that are DIFFERENT observations by this module's
    unit-of-analysis but still share one identical underlying price draw.
    ``n_unique_ticker_dates`` is the count of distinct (ticker, arrival_date)
    pairs behind ``n`` — the true upper bound on independent return draws in
    the bucket — and ``underpowered`` is gated on THIS number, not ``n``,
    because ``n`` alone overstates statistical power for any bucket coarser
    than the full (event_type, verdict) cross.
    """

    bucket: str
    window: str
    n: int
    n_unique_ticker_dates: int
    mean_raw_car_pct: float | None
    mean_adjusted_car_pct: float | None
    mean_surprise_controlled_car_pct: float | None
    ci95_adjusted_low_pct: float | None
    ci95_adjusted_high_pct: float | None
    ci95_surprise_controlled_low_pct: float | None
    ci95_surprise_controlled_high_pct: float | None
    underpowered: bool
    n_surprise_matched: int


class SurpriseRegression(BaseModel):
    """One pooled OLS fit (``raw_car = a + b * eps_surprise_pct``) per window."""

    window: str
    n_obs: int
    intercept: float | None
    slope: float | None
    fit_skipped_reason: str | None = None


class EventStudyReport(BaseModel):
    """Full P5 output — every pre-registered window x bucket, plus coverage
    accounting so a reader can see what was excluded and why."""

    benchmark_ticker: str
    windows: dict[str, tuple[int, int]]
    n_events_total: int
    n_observations_collapsed: int
    n_events_skipped: int
    skip_reasons: dict[str, int]
    skipped_events: list[SkippedEvent]
    regressions: list[SurpriseRegression]
    buckets: list[BucketStat]


# ---------------------------------------------------------------------------
# Step 1 — load disclosure_events
# ---------------------------------------------------------------------------


def load_disclosure_events(
    conn: sqlite3.Connection, *, tickers: list[str] | None = None
) -> list[RawDisclosureEvent]:
    """Every disclosure_events row in scope, typed. ``tickers=None`` loads
    the whole table."""
    where = ""
    params: list[str] = []
    if tickers:
        placeholders = ",".join("?" for _ in tickers)
        where = f"WHERE ticker IN ({placeholders})"
        params = [t.upper() for t in tickers]
    rows = conn.execute(
        f"""
        SELECT id, ticker, event_type, verdict, fiscal_year, fiscal_period,
               source_ref, subject
        FROM disclosure_events
        {where}
        ORDER BY ticker, fiscal_year, fiscal_period, id
        """,  # `where` is one of two fixed literals, never caller-supplied; params are bound
        params,
    ).fetchall()
    return [
        RawDisclosureEvent(
            id=r["id"],
            ticker=r["ticker"],
            event_type=r["event_type"],
            verdict=r["verdict"],
            fiscal_year=r["fiscal_year"],
            fiscal_period=r["fiscal_period"],
            source_ref=r["source_ref"],
            subject=r["subject"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Step 2 — resolve information-arrival dates (see module docstring)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _AnnualTimeline:
    """ticker -> sorted [(fiscal_year, filing_date), ...] for FY periods,
    edgar_text only (the only source with a real filing_date)."""

    by_ticker: dict[str, list[tuple[int, str]]] = field(
        default_factory=dict[str, list[tuple[int, str]]]
    )

    def next_filing_date(self, ticker: str, after_fiscal_year: int) -> str | None:
        years = self.by_ticker.get(ticker.upper())
        if not years:
            return None
        for fy, fdate in years:
            if fy > after_fiscal_year:
                return fdate
        return None


def _build_annual_timeline(conn: sqlite3.Connection) -> _AnnualTimeline:
    rows = conn.execute(
        """
        SELECT ticker, fiscal_year, MIN(filing_date) AS filing_date
        FROM filing_sections
        WHERE source = 'edgar_text' AND fiscal_period = 'FY' AND filing_date IS NOT NULL
        GROUP BY ticker, fiscal_year
        ORDER BY ticker, fiscal_year
        """
    ).fetchall()
    timeline = _AnnualTimeline()
    for r in rows:
        timeline.by_ticker.setdefault(r["ticker"].upper(), []).append(
            (int(r["fiscal_year"]), str(r["filing_date"]))
        )
    return timeline


def _item_event_filing_dates(conn: sqlite3.Connection) -> dict[str, str]:
    """accession number (source_ref) -> filing_date, edgar_text only."""
    rows = conn.execute(
        """
        SELECT DISTINCT source_ref, filing_date
        FROM filing_sections
        WHERE source = 'edgar_text' AND filing_date IS NOT NULL
        """
    ).fetchall()
    return {str(r["source_ref"]): str(r["filing_date"]) for r in rows}


def resolve_arrival_dates(
    conn: sqlite3.Connection, events: list[RawDisclosureEvent]
) -> tuple[dict[int, date], list[SkippedEvent]]:
    """event.id -> resolved arrival ``date``, plus every event that could not
    be resolved with an explicit reason (never a silent drop).
    """
    item_dates = _item_event_filing_dates(conn)
    annual_timeline = _build_annual_timeline(conn)

    resolved: dict[int, date] = {}
    skipped: list[SkippedEvent] = []

    for e in events:
        if e.event_type in _ITEM_EVENT_TYPES:
            raw = item_dates.get(e.source_ref or "")
            if raw is None:
                skipped.append(
                    SkippedEvent(
                        id=e.id,
                        ticker=e.ticker,
                        event_type=e.event_type,
                        reason="arrival_unresolved:no_filing_sections_match",
                    )
                )
                continue
            try:
                resolved[e.id] = date.fromisoformat(raw)
            except ValueError:
                skipped.append(
                    SkippedEvent(
                        id=e.id,
                        ticker=e.ticker,
                        event_type=e.event_type,
                        reason="arrival_unresolved:unparseable_filing_date",
                    )
                )
            continue

        if e.event_type in _METRIC_EVENT_TYPES:
            if e.fiscal_period != "FY" or e.fiscal_year is None:
                skipped.append(
                    SkippedEvent(
                        id=e.id,
                        ticker=e.ticker,
                        event_type=e.event_type,
                        reason="arrival_unresolved:quarterly_axis_no_data",
                    )
                )
                continue
            raw = annual_timeline.next_filing_date(e.ticker, e.fiscal_year)
            if raw is None:
                skipped.append(
                    SkippedEvent(
                        id=e.id,
                        ticker=e.ticker,
                        event_type=e.event_type,
                        reason="arrival_unresolved:no_subsequent_10k_on_file",
                    )
                )
                continue
            try:
                resolved[e.id] = date.fromisoformat(raw)
            except ValueError:
                skipped.append(
                    SkippedEvent(
                        id=e.id,
                        ticker=e.ticker,
                        event_type=e.event_type,
                        reason="arrival_unresolved:unparseable_filing_date",
                    )
                )
            continue

        skipped.append(
            SkippedEvent(
                id=e.id,
                ticker=e.ticker,
                event_type=e.event_type,
                reason=f"arrival_unresolved:unknown_event_type:{e.event_type}",
            )
        )

    return resolved, skipped


# ---------------------------------------------------------------------------
# Step 3 — collapse pseudo-replication
# ---------------------------------------------------------------------------


def collapse_events(
    events: list[RawDisclosureEvent], arrival_dates: dict[int, date]
) -> list[EventObservation]:
    """Group events sharing (ticker, arrival_date, event_type, verdict) into
    one ``EventObservation`` — see module docstring for why this is required
    before any statistic is computed (they share one identical forward return
    and would otherwise be counted as independent draws they are not).
    """
    grouped: dict[tuple[str, date, str, str], list[RawDisclosureEvent]] = {}
    for e in events:
        arrival = arrival_dates.get(e.id)
        if arrival is None:
            continue
        key = (e.ticker, arrival, e.event_type, e.verdict)
        grouped.setdefault(key, []).append(e)

    out: list[EventObservation] = []
    for (ticker, arrival, event_type, verdict), members in grouped.items():
        out.append(
            EventObservation(
                ticker=ticker,
                arrival_date=arrival,
                event_type=event_type,
                verdict=verdict,
                n_underlying_events=len(members),
                subjects=[m.subject for m in members][:10],
            )
        )
    out.sort(key=lambda o: (o.ticker, o.arrival_date, o.event_type, o.verdict))
    return out


# ---------------------------------------------------------------------------
# Step 4 — returns: price loading, benchmark adjustment, window CARs
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Calendar:
    """A ticker's trading-day return calendar intersected with the
    benchmark's, as parallel ascending arrays."""

    dates: list[date]
    ticker_returns: npt.NDArray[np.float64]
    benchmark_returns: npt.NDArray[np.float64]


def _build_calendar(ticker: str, benchmark_ticker: str, repo_root: Path) -> _Calendar | None:
    ticker_closes = load_daily_closes(ticker, repo_root)
    bench_closes = load_daily_closes(benchmark_ticker, repo_root)
    if len(ticker_closes) < 2 or len(bench_closes) < 2:
        return None
    ticker_rets = daily_log_returns(ticker_closes)
    bench_rets = daily_log_returns(bench_closes)
    common = sorted(set(ticker_rets) & set(bench_rets))
    if not common:
        return None
    return _Calendar(
        dates=common,
        ticker_returns=np.array([ticker_rets[d] for d in common], dtype=np.float64),
        benchmark_returns=np.array([bench_rets[d] for d in common], dtype=np.float64),
    )


def _window_return(
    calendar: _Calendar, arrival: date, offsets: tuple[int, int]
) -> tuple[float | None, float | None]:
    """(raw_car, adjusted_car) for one window, or (None, None) when the
    window does not fully fit inside the cached calendar (event too recent
    for a drift window, or too far outside the price cache's coverage)."""
    i0 = bisect_left(calendar.dates, arrival)
    if i0 >= len(calendar.dates):
        return None, None
    lo, hi = offsets
    start_idx = i0 + lo
    end_idx = i0 + hi
    if start_idx < 0 or end_idx >= len(calendar.dates):
        return None, None
    raw = float(np.sum(calendar.ticker_returns[start_idx : end_idx + 1]))
    bench = float(np.sum(calendar.benchmark_returns[start_idx : end_idx + 1]))
    return raw, raw - bench


def _nearest_prior_surprise(conn: sqlite3.Connection, ticker: str, arrival: date) -> float | None:
    """Most recent ``earnings_surprises.eps_surprise_pct`` with
    ``release_date <= arrival``, within ``_SURPRISE_MATCH_MAX_DAYS`` — see
    module docstring on why a looser match is not "the same quarter"."""
    row = conn.execute(
        """
        SELECT release_date, eps_surprise_pct
        FROM earnings_surprises
        WHERE ticker = ? AND release_date <= ? AND eps_surprise_pct IS NOT NULL
        ORDER BY release_date DESC
        LIMIT 1
        """,
        (ticker.upper(), arrival.isoformat()),
    ).fetchone()
    if row is None:
        return None
    try:
        release = date.fromisoformat(str(row["release_date"]))
    except ValueError:
        return None
    if (arrival - release).days > _SURPRISE_MATCH_MAX_DAYS:
        return None
    return float(row["eps_surprise_pct"])


def compute_window_returns(
    conn: sqlite3.Connection,
    observations: list[EventObservation],
    repo_root: Path,
    *,
    benchmark_ticker: str = DEFAULT_BENCHMARK_TICKER,
) -> list[list[WindowReturn]]:
    """Per observation, per pre-registered window: raw/adjusted CAR and the
    matched earnings surprise. Outer list is index-aligned with
    ``observations``; inner list is one ``WindowReturn`` per window in
    ``EVENT_STUDY_WINDOWS`` (in that fixed order).
    """
    calendars: dict[str, _Calendar | None] = {}
    out: list[list[WindowReturn]] = []
    for idx, obs in enumerate(observations):
        if obs.ticker not in calendars:
            calendars[obs.ticker] = _build_calendar(obs.ticker, benchmark_ticker, repo_root)
        calendar = calendars[obs.ticker]
        surprise = _nearest_prior_surprise(conn, obs.ticker, obs.arrival_date)
        row: list[WindowReturn] = []
        for window_name, offsets in EVENT_STUDY_WINDOWS.items():
            if calendar is None:
                raw, adjusted = None, None
            else:
                raw, adjusted = _window_return(calendar, obs.arrival_date, offsets)
            row.append(
                WindowReturn(
                    observation_index=idx,
                    window=window_name,
                    raw_car=raw,
                    adjusted_car=adjusted,
                    eps_surprise_pct=surprise,
                )
            )
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Step 5 — surprise-control regression (pooled, one fit per window)
# ---------------------------------------------------------------------------


def fit_surprise_regressions(
    returns_by_obs: list[list[WindowReturn]],
) -> tuple[list[SurpriseRegression], dict[tuple[int, str], float]]:
    """One OLS per window across every observation with a matched surprise
    (never per-bucket — nowhere near enough data for that). Returns the
    fitted (window -> a, b) plus a residual lookup keyed by
    (observation_index, window).
    """
    regressions: list[SurpriseRegression] = []
    residuals: dict[tuple[int, str], float] = {}

    for window_name in EVENT_STUDY_WINDOWS:
        xs: list[float] = []
        ys: list[float] = []
        idx_for_row: list[int] = []
        for row in returns_by_obs:
            for wr in row:
                if wr.window != window_name:
                    continue
                if wr.raw_car is None or wr.eps_surprise_pct is None:
                    continue
                xs.append(wr.eps_surprise_pct)
                ys.append(wr.raw_car)
                idx_for_row.append(wr.observation_index)

        if len(xs) < _MIN_REGRESSION_OBS:
            regressions.append(
                SurpriseRegression(
                    window=window_name,
                    n_obs=len(xs),
                    intercept=None,
                    slope=None,
                    fit_skipped_reason=(
                        f"fewer than {_MIN_REGRESSION_OBS} observations with a "
                        "matched earnings surprise"
                    ),
                )
            )
            continue

        x_arr = np.array(xs, dtype=np.float64)
        y_arr = np.array(ys, dtype=np.float64)
        design = np.column_stack([np.ones_like(x_arr), x_arr])
        coeffs, *_ = np.linalg.lstsq(design, y_arr, rcond=None)
        intercept, slope = float(coeffs[0]), float(coeffs[1])
        regressions.append(
            SurpriseRegression(window=window_name, n_obs=len(xs), intercept=intercept, slope=slope)
        )
        predicted = intercept + slope * x_arr
        resid = y_arr - predicted
        for obs_idx, r in zip(idx_for_row, resid, strict=True):
            residuals[(obs_idx, window_name)] = float(r)

    return regressions, residuals


# ---------------------------------------------------------------------------
# Step 6 — bucketing + bootstrap CIs
# ---------------------------------------------------------------------------


def _bootstrap_ci(values: npt.NDArray[np.float64], *, seed: int) -> tuple[float, float] | None:
    """Percentile-method bootstrap 95% CI on the mean. ``None`` when there
    are fewer than 2 values (nothing to resample)."""
    if len(values) < 2:
        return None
    rng = np.random.default_rng(seed)
    n = len(values)
    means = np.empty(_BOOTSTRAP_DRAWS, dtype=np.float64)
    for i in range(_BOOTSTRAP_DRAWS):
        sample = rng.choice(values, size=n, replace=True)
        means[i] = float(np.mean(sample))
    low, high = np.percentile(means, [2.5, 97.5])
    return float(low), float(high)


def _bucket_key_all(_obs: EventObservation) -> str:
    return "ALL"


def _bucket_key_event_type(obs: EventObservation) -> str:
    return f"event_type={obs.event_type}"


def _bucket_key_verdict(obs: EventObservation) -> str:
    return f"verdict={obs.verdict}"


def _bucket_key_crossed(obs: EventObservation) -> str:
    return f"event_type={obs.event_type}|verdict={obs.verdict}"


#: Every bucketing function, applied to every observation — pre-registered,
#: see module docstring point "Buckets, all reported, none cherry-picked".
_BUCKET_FUNCS = (
    _bucket_key_all,
    _bucket_key_event_type,
    _bucket_key_verdict,
    _bucket_key_crossed,
)


def build_bucket_stats(
    observations: list[EventObservation],
    returns_by_obs: list[list[WindowReturn]],
    residuals: dict[tuple[int, str], float],
) -> list[BucketStat]:
    out: list[BucketStat] = []
    for window_name in EVENT_STUDY_WINDOWS:
        for bucket_fn in _BUCKET_FUNCS:
            buckets: dict[str, list[int]] = {}
            for idx, obs in enumerate(observations):
                buckets.setdefault(bucket_fn(obs), []).append(idx)

            for bucket_name, idxs in buckets.items():
                raw_vals: list[float] = []
                adj_vals: list[float] = []
                adj_idxs: list[int] = []
                resid_vals: list[float] = []
                for idx in idxs:
                    wr = next(w for w in returns_by_obs[idx] if w.window == window_name)
                    if wr.raw_car is not None:
                        raw_vals.append(wr.raw_car)
                    if wr.adjusted_car is not None:
                        adj_vals.append(wr.adjusted_car)
                        adj_idxs.append(idx)
                    r = residuals.get((idx, window_name))
                    if r is not None:
                        resid_vals.append(r)

                raw_arr = np.array(raw_vals, dtype=np.float64)
                adj_arr = np.array(adj_vals, dtype=np.float64)
                resid_arr = np.array(resid_vals, dtype=np.float64)

                adj_ci = _bootstrap_ci(adj_arr, seed=_BOOTSTRAP_SEED)
                resid_ci = _bootstrap_ci(resid_arr, seed=_BOOTSTRAP_SEED + 1)

                # The true upper bound on INDEPENDENT return draws behind this
                # cell — see BucketStat's docstring on why `n` alone overstates
                # power for any bucket coarser than the full (event_type,
                # verdict) cross (one filing can contribute several rows there).
                unique_ticker_dates = {
                    (observations[i].ticker, observations[i].arrival_date) for i in adj_idxs
                }

                out.append(
                    BucketStat(
                        bucket=bucket_name,
                        window=window_name,
                        n=len(adj_vals),
                        n_unique_ticker_dates=len(unique_ticker_dates),
                        mean_raw_car_pct=(
                            float(np.mean(raw_arr)) * 100.0 if len(raw_arr) else None
                        ),
                        mean_adjusted_car_pct=(
                            float(np.mean(adj_arr)) * 100.0 if len(adj_arr) else None
                        ),
                        mean_surprise_controlled_car_pct=(
                            float(np.mean(resid_arr)) * 100.0 if len(resid_arr) else None
                        ),
                        ci95_adjusted_low_pct=adj_ci[0] * 100.0 if adj_ci else None,
                        ci95_adjusted_high_pct=adj_ci[1] * 100.0 if adj_ci else None,
                        ci95_surprise_controlled_low_pct=(
                            resid_ci[0] * 100.0 if resid_ci else None
                        ),
                        ci95_surprise_controlled_high_pct=(
                            resid_ci[1] * 100.0 if resid_ci else None
                        ),
                        underpowered=len(unique_ticker_dates) < MIN_EVENTS_FOR_HEADLINE,
                        n_surprise_matched=len(resid_vals),
                    )
                )
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_event_study(
    conn: sqlite3.Connection,
    repo_root: Path,
    *,
    tickers: list[str] | None = None,
    benchmark_ticker: str = DEFAULT_BENCHMARK_TICKER,
) -> EventStudyReport:
    events = load_disclosure_events(conn, tickers=tickers)
    arrival_dates, skipped = resolve_arrival_dates(conn, events)
    observations = collapse_events(events, arrival_dates)
    returns_by_obs = compute_window_returns(
        conn, observations, repo_root, benchmark_ticker=benchmark_ticker
    )
    regressions, residuals = fit_surprise_regressions(returns_by_obs)
    buckets = build_bucket_stats(observations, returns_by_obs, residuals)

    skip_reasons: dict[str, int] = {}
    for s in skipped:
        skip_reasons[s.reason] = skip_reasons.get(s.reason, 0) + 1

    return EventStudyReport(
        benchmark_ticker=benchmark_ticker,
        windows=EVENT_STUDY_WINDOWS,
        n_events_total=len(events),
        n_observations_collapsed=len(observations),
        n_events_skipped=len(skipped),
        skip_reasons=skip_reasons,
        skipped_events=skipped,
        regressions=regressions,
        buckets=buckets,
    )
