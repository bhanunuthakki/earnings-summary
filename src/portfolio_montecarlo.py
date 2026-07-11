"""Fat-tailed book Monte Carlo — the tail the covariance-only reads miss.

``portfolio_correlation`` / ``portfolio_style_factors`` / ``portfolio_tail_stress``
each answer a narrow question off the local price cache (co-movement, factor
tilt, "what if every bear fires at once"). None of them answers "how bad can
the WHOLE BOOK actually get" as a distribution — the naive-Gaussian answer
(covariance + a normal quantile) understates the left tail every real book
carries, because equity returns are leptokurtic and crashes correlate harder
than calm markets do (directives/monthly_red_team.md Phase 3: "Fat-tail book
MC").  This module simulates the book's ANNUAL return under two models from
the same local daily-return substrate the correlation module reads —
multivariate NORMAL (the naive-Gaussian baseline) and multivariate
STUDENT-T with df=4 (the fat-tail model) — plus a deterministic named
event-correlation stress ("what if the LatAm pair craters together").

**Why the shock is drawn at the ANNUAL horizon, not simulated day-by-day.**
The daily covariance IS the right substrate (it's the only thing on disk), but
naively drawing 252 daily t-distributed shocks and summing them defeats the
whole point: the Central Limit Theorem makes a sum of 252 i.i.d. draws look
normal again regardless of how fat each individual daily tail is — the crash
signal a fat-tailed marginal is supposed to carry gets averaged away before it
ever reaches the annual number. So this module estimates the DAILY covariance
(the trustworthy, data-rich estimate) then scales it x252 to an ANNUAL target
covariance and draws exactly ONE shock at the annual horizon per path from
that target — a t-distributed shock still looks like a t-distribution after
zero averaging.

**Crash-mixing.** A real multi-asset crash isn't "each name independently
draws a bad day" — it's "the whole book gets hit by the same macro/liquidity
shock, so the correlations that hold in calm markets go to ~1 exactly when it
matters." The standard multivariate-t construction captures this for free:
``X = mu + Z / sqrt(W / df)`` where ``Z ~ N(0, Sigma)`` (correlated across
names via a single Cholesky factor) and ``W`` is ONE chi-square(df) draw
SHARED across every name on that path. A path that draws a small ``W`` scales
every name's shock up together — the tails co-move by construction, not by
coincidence. (Because the scale matrix ``Sigma`` is used directly as the
t-distribution's scale — not variance-matched — the t-model's realized
variance is ``Sigma * df / (df - 2)``, i.e. ~2x the normal model's variance at
df=4; that inflation, combined with the heavy tails of chi-square(4), is what
actually produces the fat left tail, not just a wider normal.)

**Coverage honesty.** Names without enough aligned daily history are DROPPED
with a reason (mirrors ``portfolio_correlation``'s two drop modes — history
length vs degenerate/flat), never silently zero-weighted; the read reports
what fraction of the book actually entered the simulation. Cash-like
tickers get a fixed near-zero-vol return and are excluded from the covariance
panel entirely (nothing to correlate).

**Drift is a stated, low-confidence input, not a forecast.** The default
drift is each name's own sample-mean daily return, annualized — a short
lookback window is regime-biased (a 2-year window that happened to run
through a bull market will impute an optimistic drift). Callers can override
it (``drift_annual_override``); the output always names its provenance so a
reader never mistakes it for a return forecast. The TAILS are the signal this
module exists to produce, not the mean.

Pure disk reads (price cache) + numpy — never the network, never the DB (the
event-stress leg reads ``dcf_runs`` read-only for the bear fair values, same
substrate ``portfolio_tail_stress`` already reads) — so the Risk panel section
renders with the tracker down, matching every other local-substrate section.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np

from allocation.price_history import build_aligned_returns, daily_log_returns, load_daily_closes
from portfolio_tail_stress import build_tail_stress

__all__ = [
    "CASH_LIKE_ANNUAL_RETURN",
    "CASH_LIKE_TICKERS",
    "DEFAULT_N_PATHS",
    "DEFAULT_SEED",
    "DEFAULT_T_DF",
    "DRAWDOWN_LABELS",
    "DRAWDOWN_THRESHOLDS",
    "EVENT_SCENARIOS",
    "TRADING_DAYS_PER_YEAR",
    "WEALTHPLAN_CMA_ASSUMED_VOL_PCT",
    "DistributionRead",
    "EventLeg",
    "EventScenarioDef",
    "EventStressResult",
    "MonteCarloRead",
    "build_book_monte_carlo",
    "build_event_stress",
    "build_joint_latam_stress",
]

TRADING_DAYS_PER_YEAR = 252
DEFAULT_N_PATHS = 50_000
DEFAULT_SEED = 42
DEFAULT_T_DF = 4

# The book-drawdown thresholds the read reports P(book return < threshold) for
# (directive: "P(<-20/-30/-40/-50%)"). Order matches the display order.
DRAWDOWN_LABELS: tuple[str, ...] = ("-20%", "-30%", "-40%", "-50%")
DRAWDOWN_THRESHOLDS: tuple[float, ...] = (-0.20, -0.30, -0.40, -0.50)

# Money-market / cash tickers: ~4% annualized, ~0 vol, and — critically —
# excluded from the covariance panel (nothing to correlate; folding them into
# the price-history alignment would just burn overlap budget on a flat
# series, the exact "degenerate" case portfolio_correlation already guards
# against). Matches the tickers seen in data/portfolio_weights.json.
CASH_LIKE_TICKERS: frozenset[str] = frozenset({"SGOV", "FDRXX", "CUR:USD"})
CASH_LIKE_ANNUAL_RETURN = 0.04

# The wealthplan project's Capital Market Assumption for the book's volatility
# (directives/monthly_red_team.md Phase 3: "the plan's 16%-vol assumption
# materially understates the ~22-27% book"). A plain constant, not read from
# the wealthplan project (this repo never reads another project's files) —
# it is the documented number this module's Risk-panel section compares
# against; execution/export_book_cma.py is the actual data handoff.
WEALTHPLAN_CMA_ASSUMED_VOL_PCT = 16.0

_DEGENERATE_REASON = "degenerate (constant) over the aligned window"

# exp(X)-1 of a Student-t log-shock has NO finite population moments — the
# t-distribution's polynomial tail decays slower than exp() grows, so the
# integral defining E[exp(X)] diverges (this is real math, not a bug: a
# "log-t" random variable has literally infinite mean/variance). A 50k-path
# sample will occasionally draw a near-zero shared chi-square(df) scalar,
# which sends that one path's simple return to an astronomical (but finite,
# in float64) magnitude — enough to single-handedly dominate an arithmetic
# mean/std over 50,000 paths. Clipping the simple-return array before
# computing MEAN/VOL (never percentiles — the 1st/5th-percentile drawdown
# region this module cares about lives on the opposite, naturally-floored-at
# -100% tail, untouched by an upper clip) keeps ``mean_pct``/``vol_pct``
# finite-sample-stable without moving a single reported percentile or
# probability.
_MAX_SIMPLE_RETURN = 50.0  # 5,000% — orders of magnitude past any realistic
# portfolio combination remaining valid (no rebalancing/weight-cap modeled
# past this point anyway), so it never touches a real percentile/probability.


@dataclass(slots=True)
class DistributionRead:
    """One model's (normal or student_t) simulated annual book-return
    distribution. All ``*_pct`` fields are in PERCENT units; ``prob_below`` is
    a FRACTION (0-1) keyed by ``DRAWDOWN_LABELS``."""

    method: str  # "normal" | "student_t"
    mean_pct: float
    vol_pct: float
    pct_5th: float
    pct_1st: float
    prob_below: dict[str, float] = field(default_factory=dict[str, float])
    detail: str = ""  # one-line provenance of the shock construction


@dataclass(slots=True)
class MonteCarloRead:
    """The book-level fat-tail simulation + the coverage/provenance to trust
    it. ``risky_weight_pct`` + ``cash_like_weight_pct`` == ``modeled_weight_pct``;
    ``unmodeled_weight_pct`` is the book fraction with no usable history
    (dropped, with a reason, never silently omitted — see ``dropped``)."""

    tickers: list[str]  # names that entered the covariance panel, sorted
    n_obs: int
    prices_through: date | None
    dropped: dict[str, str]  # ticker -> reason (never silent)
    risky_weight_pct: float
    cash_like_weight_pct: float
    modeled_weight_pct: float
    unmodeled_weight_pct: float
    n_paths: int
    seed: int
    t_df: int
    drift_source: str
    normal: DistributionRead
    student_t: DistributionRead
    # Closed-form sqrt(w' Sigma_annual w) over the risky panel (excludes
    # cash-likes, same as the covariance panel itself) — the "cov-based"
    # book vol, computed independently of the simulation noise. Should track
    # ``normal.vol_pct`` closely (both derive from the same Sigma_annual; the
    # simulated figure also folds in the log->simple expm1() convexity and
    # cash-like contribution the closed form skips) — a cross-check that the
    # simulation is calibrated, and the number export_book_cma.py's "cov-
    # based" field reads directly.
    analytic_vol_pct: float


def _drop_degenerate_columns(
    tickers: Sequence[str], matrix: np.ndarray, upstream_dropped: Mapping[str, str]
) -> tuple[list[str], np.ndarray, dict[str, str]]:
    """Same degenerate-column guard as ``portfolio_correlation``'s disk
    assembler (identical reason text — a reader must never have to guess
    which stage dropped a name), so a flat/halted name never poisons the
    covariance matrix silently."""
    stds = matrix.std(axis=0)
    keep = [i for i, s in enumerate(stds) if float(s) > 0.0 and np.isfinite(s)]
    dropped = dict(upstream_dropped)
    if len(keep) < len(tickers):
        for i, t in enumerate(tickers):
            if i not in keep:
                dropped[t] = _DEGENERATE_REASON
        matrix = matrix[:, keep]
    kept = [tickers[i] for i in keep]
    return kept, matrix, dropped


def _safe_cholesky(cov: np.ndarray) -> np.ndarray | None:
    """Cholesky of the annual covariance, with a small diagonal jitter retried
    on a near-singular matrix (highly-correlated pairs can leave real daily
    data numerically indefinite). ``None`` on genuine failure — the caller
    degrades to "not enough data" rather than crashing a render."""
    n = cov.shape[0]
    jitter = 0.0
    for _ in range(6):
        try:
            return np.linalg.cholesky(cov + jitter * np.eye(n))
        except np.linalg.LinAlgError:
            jitter = max(jitter * 10.0, 1e-12)
    return None


def _distribution_read(method: str, port_ret: np.ndarray, detail: str) -> DistributionRead:
    # Percentiles/probabilities read the RAW array (they live on the opposite,
    # naturally-floored-at -100% tail — untouched by the clip below).
    prob_below = {
        label: float(np.mean(port_ret < thr))
        for label, thr in zip(DRAWDOWN_LABELS, DRAWDOWN_THRESHOLDS, strict=True)
    }
    pct_5th = float(np.percentile(port_ret, 5)) * 100.0
    pct_1st = float(np.percentile(port_ret, 1)) * 100.0
    # mean/vol read the CLIPPED array (see _MAX_SIMPLE_RETURN) — otherwise a
    # single pathological path can send an arithmetic mean/std to nonsense
    # magnitudes under the Student-t model (module docstring).
    clipped = np.clip(port_ret, -1.0, _MAX_SIMPLE_RETURN)
    return DistributionRead(
        method=method,
        mean_pct=float(np.mean(clipped)) * 100.0,
        vol_pct=float(np.std(clipped, ddof=1)) * 100.0,
        pct_5th=pct_5th,
        pct_1st=pct_1st,
        prob_below=prob_below,
        detail=detail,
    )


def build_book_monte_carlo(
    repo_root: Path,
    weights: Mapping[str, float],
    *,
    n_paths: int = DEFAULT_N_PATHS,
    seed: int = DEFAULT_SEED,
    t_df: int = DEFAULT_T_DF,
    drift_annual_override: Mapping[str, float] | None = None,
) -> MonteCarloRead | None:
    """Simulate the book's annual return under normal + Student-t(df) models.

    ``weights`` is ticker (any case) -> fraction-of-book. Cash-like tickers
    (``CASH_LIKE_TICKERS``) get the fixed low-vol treatment and never enter
    the covariance panel. The remaining ("risky") names need the same >=2
    with a usable aligned daily-return overlap that ``portfolio_correlation``
    requires (``allocation.price_history.build_aligned_returns`` — 252-obs
    lookback, 120-obs floor); ``None`` when there's nothing risky to simulate
    (no weights at all, or fewer than two risky names have history — matches
    ``build_holdings_correlation_from_disk``'s "nothing pairwise to say"
    contract exactly, since the same substrate feeds both).

    ``drift_annual_override`` replaces a name's default sample-mean-annualized
    drift (ticker upper -> annual log-return); names absent from it keep the
    sample-mean default. Deterministic for a given ``(seed, n_paths, t_df)`` —
    the same ``numpy.random.default_rng(seed)`` stream feeds both models (the
    t-model divides the SAME correlated normal draws by a shared chi-square
    scale), so normal-vs-t is a paired comparison, not independently noisy.
    """
    positive = {t.upper(): w for t, w in weights.items() if w > 0}
    if not positive:
        return None

    cash = {t: w for t, w in positive.items() if t in CASH_LIKE_TICKERS}
    risky = {t: w for t, w in positive.items() if t not in CASH_LIKE_TICKERS}
    cash_weight_pct = 100.0 * sum(cash.values())

    returns_by_ticker: dict[str, dict[date, float]] = {}
    missing: dict[str, str] = {}
    for t in sorted(risky):
        rets = daily_log_returns(load_daily_closes(t, repo_root))
        if rets:
            returns_by_ticker[t] = rets
        else:
            missing[t] = "no daily price history on file"
    aligned = build_aligned_returns(returns_by_ticker)
    if aligned is None:
        return None

    tickers, matrix, dropped = _drop_degenerate_columns(aligned.tickers, aligned.matrix, missing)
    dropped.update({t: r for t, r in aligned.dropped.items() if t not in dropped})
    if not tickers:
        return None
    n = len(tickers)

    mean_daily = matrix.mean(axis=0)
    if n == 1:
        cov_daily = np.array([[float(matrix[:, 0].var(ddof=1))]])
    else:
        cov_daily = np.cov(matrix, rowvar=False)
        if cov_daily.ndim == 0:  # numpy quirk: (T,1) input can still collapse to 0-d
            cov_daily = cov_daily.reshape(1, 1)
    mean_annual = mean_daily * TRADING_DAYS_PER_YEAR
    cov_annual = cov_daily * TRADING_DAYS_PER_YEAR

    if drift_annual_override:
        overrides = {k.upper(): v for k, v in drift_annual_override.items()}
        mean_annual = np.array([overrides.get(t, mean_annual[i]) for i, t in enumerate(tickers)])

    chol = _safe_cholesky(cov_annual)
    if chol is None:
        return None

    rng = np.random.default_rng(seed)
    z = rng.standard_normal(size=(n_paths, n))
    correlated_normal = z @ chol.T  # (n_paths, n), Cov ~= cov_annual

    log_ret_normal = mean_annual[None, :] + correlated_normal
    simple_normal = np.expm1(log_ret_normal)

    # Shared crash-mixing chi-square draw: ONE per path, applied to every
    # name on that path, so the tails co-move by construction (see module
    # docstring). t_df/2 mean keeps E[scale^2] finite for df > 2.
    w_chi2 = rng.chisquare(df=t_df, size=n_paths)
    t_scale = np.sqrt(t_df / w_chi2)
    correlated_t = correlated_normal * t_scale[:, None]
    log_ret_t = mean_annual[None, :] + correlated_t
    simple_t = np.expm1(log_ret_t)

    kept_weights = np.array([risky[t] for t in tickers])
    cash_contribution = cash_weight_pct / 100.0 * CASH_LIKE_ANNUAL_RETURN
    port_normal = simple_normal @ kept_weights + cash_contribution
    port_t = simple_t @ kept_weights + cash_contribution

    analytic_var = float(kept_weights @ cov_annual @ kept_weights)
    analytic_vol_pct = math.sqrt(max(analytic_var, 0.0)) * 100.0

    risky_weight_pct = 100.0 * sum(risky[t] for t in tickers)
    modeled_weight_pct = risky_weight_pct + cash_weight_pct
    unmodeled_weight_pct = 100.0 - modeled_weight_pct

    drift_source = (
        "sample mean of the aligned daily log returns, annualized (x252) — "
        "regime-biased by the lookback window, treat as low-confidence (the "
        "tails are the signal here, not the mean)"
        if not drift_annual_override
        else "sample-mean default, overridden for one or more names by the caller"
    )
    normal_detail = (
        "multivariate normal, one shock drawn directly at the annual horizon "
        "from the aligned daily covariance scaled x252 (no daily-shock CLT "
        "averaging)"
    )
    t_detail = (
        f"multivariate Student-t (df={t_df}): the same correlated annual "
        "normal shock divided by sqrt(chi-square(df)/df), ONE chi-square draw "
        "shared across every name per path (crash-mixing) — realized variance "
        f"is ~{t_df / (t_df - 2):.1f}x the normal model's at this df"
    )

    return MonteCarloRead(
        tickers=tickers,
        n_obs=len(aligned.dates),
        prices_through=aligned.dates[-1] if aligned.dates else None,
        dropped=dropped,
        risky_weight_pct=risky_weight_pct,
        cash_like_weight_pct=cash_weight_pct,
        modeled_weight_pct=modeled_weight_pct,
        unmodeled_weight_pct=unmodeled_weight_pct,
        n_paths=n_paths,
        seed=seed,
        t_df=t_df,
        drift_source=drift_source,
        normal=_distribution_read("normal", port_normal, normal_detail),
        student_t=_distribution_read("student_t", port_t, t_detail),
        analytic_vol_pct=analytic_vol_pct,
    )


# ---------------------------------------------------------------------------
# Event-correlation stress: deterministic named scenarios on the CURRENT
# book, as DATA (a registry of EventScenarioDef), not bespoke code per name —
# adding a scenario means appending a definition, never a new code path.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EventScenarioDef:
    """One named event-correlation stress, as data. ``named_tickers`` get a
    dedicated bear-FV lookup (from ``dcf_runs`` via ``portfolio_tail_stress``)
    when one is persisted; ``fallback_returns_pct`` names the labeled
    fallback constant used when it isn't (never silently substituted — the
    result's legs + notes always say which happened). Every OTHER positive-
    weight equity takes ``other_equity_return_pct``; cash-likes are flat."""

    id: str
    title: str
    description: str
    named_tickers: tuple[str, ...]
    fallback_returns_pct: dict[str, float]
    other_equity_return_pct: float = -15.0


EVENT_SCENARIOS: tuple[EventScenarioDef, ...] = (
    EventScenarioDef(
        id="joint_latam",
        title="Joint LatAm credit/macro shock",
        description=(
            "MELI and NU both hit their persisted bear-case DCF fair value "
            "simultaneously (a Brazil-credit / FX shock hits both at once — "
            "the crowding the correlation section already flags for this "
            "pair); every other equity holding takes a generic -15% macro "
            "drag; cash-likes are flat."
        ),
        named_tickers=("MELI", "NU"),
        fallback_returns_pct={"MELI": -38.0, "NU": -52.0},
        other_equity_return_pct=-15.0,
    ),
)


@dataclass(slots=True)
class EventLeg:
    """One name's return leg in an event stress, with the provenance of the
    number (persisted bear FV / labeled fallback / generic macro drag /
    cash-like flat) so the row is never ambiguous about where it came from."""

    ticker: str
    weight_pct: float
    return_pct: float
    label: str


@dataclass(slots=True)
class EventStressResult:
    """The book-level event stress: every positive-weight name gets a leg (no
    coverage gap — the scenario rule always resolves to SOME return, unlike
    the price-history-dependent Monte Carlo), so ``modeled_weight_pct`` is
    always ~100%; ``notes`` flags any fallback constants used."""

    scenario_id: str
    title: str
    description: str
    book_return_pct: float
    modeled_weight_pct: float
    legs: list[EventLeg]
    notes: list[str] = field(default_factory=list[str])


def build_event_stress(
    scenario: EventScenarioDef, db_path: Path, weights: Mapping[str, float]
) -> EventStressResult | None:
    """Apply ``scenario`` to the book described by ``weights``. ``None`` when
    there are no positively-weighted names (nothing to stress)."""
    positive = {t.upper(): w for t, w in weights.items() if w > 0}
    if not positive:
        return None

    tail = build_tail_stress(db_path, positive)
    bear_by_ticker: dict[str, float] = {}
    if tail is not None:
        for row in tail.rows:
            if row.bear_return_pct is not None:
                bear_by_ticker[row.ticker] = row.bear_return_pct

    legs: list[EventLeg] = []
    book_return = 0.0
    fallback_used: list[str] = []
    for t in sorted(positive):
        w = positive[t]
        if t in CASH_LIKE_TICKERS:
            ret = 0.0
            label = "cash-like (flat)"
        elif t in scenario.named_tickers:
            if t in bear_by_ticker:
                ret = bear_by_ticker[t]
                label = "persisted bear-case DCF fair value"
            else:
                ret = scenario.fallback_returns_pct.get(t, scenario.other_equity_return_pct)
                label = f"fallback {ret:+.0f}% (no bear scenario persisted in dcf_runs)"
                fallback_used.append(t)
        else:
            ret = scenario.other_equity_return_pct
            label = "other equities (generic macro drag)"
        contribution = w * ret
        book_return += contribution
        legs.append(EventLeg(ticker=t, weight_pct=w * 100.0, return_pct=ret, label=label))

    legs.sort(key=lambda leg: leg.return_pct)
    notes: list[str] = []
    if fallback_used:
        notes.append(
            "fallback constants used (no live bear scenario persisted) for: "
            + ", ".join(sorted(fallback_used))
        )
    return EventStressResult(
        scenario_id=scenario.id,
        title=scenario.title,
        description=scenario.description,
        book_return_pct=book_return,
        modeled_weight_pct=100.0 * sum(positive.values()),
        legs=legs,
        notes=notes,
    )


def build_joint_latam_stress(
    db_path: Path, weights: Mapping[str, float]
) -> EventStressResult | None:
    """Convenience wrapper: ``build_event_stress`` for the ``joint_latam``
    scenario specifically (the Risk panel's headline event-stress row)."""
    scenario = next((s for s in EVENT_SCENARIOS if s.id == "joint_latam"), None)
    if scenario is None:  # pragma: no cover — registry always defines it
        return None
    return build_event_stress(scenario, db_path, weights)
