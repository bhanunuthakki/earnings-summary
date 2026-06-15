"""Portfolio-fit scoring for evaluation (screen-candidate) names.

The cockpit's Evaluation table scores a candidate's *standalone merit* —
``research_cockpit.eval_attractiveness`` = DCF upside × Rev growth × FCF margin
× PEG, all from the name's own fundamentals. That number is blind to the book
the name would join: a great business that is highly correlated to what you
already own is a worse *next dollar* than an equally-great diversifier. This
module is the companion **Fit** read — "how would this name sit in my book?" —
expressed in the same risk vocabulary the portfolio tracker / Risk tab use for
the held book, so the two surfaces are cohesive rather than disjoint.

Four factors, each a transparent band multiplier centered at 1.0 (>1 accretive
to the book, <1 dilutive), mirroring the score chip's reproducible-by-eye style:

* **Marginal Sharpe** — the textbook marginal-Sharpe criterion: a name lifts the
  book's Sharpe at the margin iff ``SR_candidate > SR_book · ρ(candidate, book)``.
  ``SR_book`` is the tracker's reported book Sharpe (the same number the Risk tab
  shows); ``SR_candidate`` and ``ρ`` come from local daily price history.
* **Diversification** — the candidate's correlation to the book (lower = better)
  and the marginal volatility a first dollar of it would add.
* **Factor fit** — the candidate's growth tilt (QQQ-minus-SPY beta) against the
  book's current tilt: piling into the book's existing lean drags; balancing it
  lifts.
* **Sector fit** — the candidate's sector against the book's current weight in
  that sector: adding to an already-heavy sector drags.

Score and Fit stay cleanly separated — forward merit lives in the score, book
geometry lives here, so neither double-counts the other. A factor whose inputs
are unavailable (thin price history — common for recent IPOs — or a tracker that
has never been reached) contributes a **neutral 1.0** and flags the read partial,
so an unknowable fit reads "neutral", never a fake "dilutive".

Two seams keep it cheap and honest:

* The scorer :func:`score_candidate_fit` is pure (dataclasses in, dataclass out)
  so the band logic tests without files or a network.
* The gatherer :func:`compute_candidate_fit` does the price-history I/O. It is far
  too heavy for the GET / render path (a covariance-grade read per candidate), so
  it is meant to run in the morning pipeline and materialize a cache the render
  reads — the pattern ``cockpit_fundamentals`` already established.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np

from allocation.price_history import build_aligned_returns, daily_log_returns, load_daily_closes

# Candidate-vs-book history bar: a name needs this many common trading days with
# the book (and with SPY/QQQ for the factor leg) before its price-derived factors
# are trusted — the same floor the book covariance uses (book_risk.MIN_OVERLAP_OBS).
CAND_MIN_OBS = 120
ANNUALIZE = math.sqrt(252.0)
TRADING_DAYS = 252.0

# Benchmark proxies for the candidate's market / growth betas — the same pair the
# tracker rolls up for held names (SPY market beta, QQQ growth beta).
_SPY, _QQQ = "SPY", "QQQ"


# --------------------------------------------------------------------------- #
# Inputs / outputs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BookContext:
    """The held-book state the fit factors compare a candidate against.

    Assembled by the materializer from the risk snapshot (``sharpe``,
    ``risk_free_annual``, ``growth_tilt``), the tracker's positioning
    (``sector_weights``), and the weights cache (``weights``), then passed in so
    the scorer stays pure. Every book-level number is optional — a factor whose
    book input is missing degrades to neutral rather than guessing."""

    weights: dict[str, float]  # held book, fractions (ticker upper -> weight)
    sharpe: float | None = None  # book realized Sharpe (tracker /beta)
    risk_free_annual: float | None = None  # rf used for that Sharpe (tracker /beta)
    growth_tilt: float | None = None  # book qqq_beta - spy_beta (risk snapshot)
    sector_weights: dict[str, float] = field(default_factory=dict[str, float])  # frac by sector
    captured_at: str | None = None  # snapshot freshness, for a low-confidence flag


@dataclass(frozen=True, slots=True)
class CandidateRisk:
    """One candidate's price-derived geometry against the book + benchmarks.

    The intermediate the gatherer computes and the scorer consumes; carrying it
    explicitly keeps :func:`score_candidate_fit` pure and lets tests drive the
    bands directly. Every field is optional — thin or missing history leaves the
    dependent factor unscored (neutral + partial)."""

    ticker: str
    corr_to_book: float | None = None  # corr(candidate, book) over the overlap
    marginal_vol_ann: float | None = None  # marginal vol of a first dollar, annualized
    sharpe: float | None = None  # candidate standalone Sharpe (same rf as the book)
    growth_tilt: float | None = None  # candidate qqq_beta - spy_beta
    sector: str | None = None  # FMP profile sector
    obs: int | None = None  # common trading days behind the geometry
    history_thin: bool = False  # had price history but < CAND_MIN_OBS overlap


@dataclass(frozen=True, slots=True)
class FitFactor:
    """One fit factor's contribution to the composite."""

    key: str  # "sharpe" | "divers" | "factor" | "sector" — the why-string token
    label: str  # human label for the breakdown peek ("Marginal Sharpe")
    multiplier: float  # band multiplier (1.0 when the inputs are missing)
    detail: str  # the reading, formatted; "n/a" when missing
    missing: bool


@dataclass(frozen=True, slots=True)
class CandidateFit:
    """The portfolio-fit decomposition for one evaluation name.

    ``fit`` is the product of the factor multipliers, centered at 1.0 (>1
    accretive to the book, <1 dilutive). ``partial`` is set when any factor's
    inputs were unavailable (those contribute a neutral 1.0). ``why`` is the
    reproducible-by-eye string the chip surfaces as its hover, mirroring the
    score chip's format."""

    ticker: str
    factors: list[FitFactor]
    fit: float
    why: str
    partial: bool
    obs: int | None = None


# --------------------------------------------------------------------------- #
# Band tables (best-first; first threshold met wins). Centered at 1.0 so the
# composite reads like the score: >1 lifts the book, <1 drags it.
# --------------------------------------------------------------------------- #

# Marginal Sharpe: the improvement margin SR_cand - SR_book * corr (>= threshold).
_SHARPE_BANDS: tuple[tuple[float, float], ...] = (
    (0.30, 1.25),
    (0.10, 1.12),
    (-0.10, 1.0),
    (-0.30, 0.90),
)
_SHARPE_FLOOR = 0.80

# Diversification: correlation to book (lower is better; value <= threshold).
_DIVERS_BANDS: tuple[tuple[float, float], ...] = (
    (0.2, 1.20),
    (0.4, 1.10),
    (0.6, 1.0),
    (0.8, 0.92),
)
_DIVERS_FLOOR = 0.85

# Factor fit: alignment = sign(book tilt) · candidate tilt. Positive means the
# candidate deepens the book's existing growth/value lean (crowding → drag).
_FACTOR_CROWD, _FACTOR_BALANCE = 0.30, -0.30
_FACTOR_CROWD_MULT, _FACTOR_BALANCE_MULT = 0.88, 1.12

# Sector fit: the book's existing fraction in the candidate's sector.
_SECTOR_HEAVY, _SECTOR_WARM, _SECTOR_LIGHT = 0.30, 0.20, 0.05

# Composite tone thresholds (render-side): accretive vs dilutive to the book.
FIT_HI, FIT_LO = 1.10, 0.90


def fit_tone(fit: float) -> str:
    """'hi' (accretive) / 'lo' (dilutive) / '' (neutral). The chip and the
    breakdown peek share this so they tone the same fit identically."""
    if fit >= FIT_HI:
        return "hi"
    if fit <= FIT_LO:
        return "lo"
    return ""


def _band(value: float, bands: tuple[tuple[float, float], ...], floor: float) -> float:
    for threshold, mult in bands:
        if value >= threshold:
            return mult
    return floor


def _band_le(value: float, bands: tuple[tuple[float, float], ...], floor: float) -> float:
    for threshold, mult in bands:
        if value <= threshold:
            return mult
    return floor


# --------------------------------------------------------------------------- #
# Pure scorer
# --------------------------------------------------------------------------- #


def _sharpe_factor(risk: CandidateRisk, book: BookContext) -> FitFactor:
    if risk.sharpe is None or book.sharpe is None or risk.corr_to_book is None:
        return FitFactor("sharpe", "Marginal Sharpe", 1.0, "n/a", True)
    hurdle = book.sharpe * risk.corr_to_book
    margin = risk.sharpe - hurdle
    detail = (
        f"SR {risk.sharpe:+.2f} vs book {book.sharpe:+.2f} x corr "
        f"{risk.corr_to_book:+.2f} -> {margin:+.2f}"
    )
    return FitFactor(
        "sharpe", "Marginal Sharpe", _band(margin, _SHARPE_BANDS, _SHARPE_FLOOR), detail, False
    )


def _divers_factor(risk: CandidateRisk) -> FitFactor:
    if risk.corr_to_book is None:
        return FitFactor("divers", "Diversification", 1.0, "n/a", True)
    mvol = (
        f" · marg vol {risk.marginal_vol_ann * 100.0:+.1f}%/yr"
        if risk.marginal_vol_ann is not None
        else ""
    )
    detail = f"corr to book {risk.corr_to_book:+.2f}{mvol}"
    return FitFactor(
        "divers",
        "Diversification",
        _band_le(risk.corr_to_book, _DIVERS_BANDS, _DIVERS_FLOOR),
        detail,
        False,
    )


def _factor_factor(risk: CandidateRisk, book: BookContext) -> FitFactor:
    if risk.growth_tilt is None or book.growth_tilt is None:
        return FitFactor("factor", "Factor fit", 1.0, "n/a", True)
    book_sign = 1.0 if book.growth_tilt > 0 else -1.0 if book.growth_tilt < 0 else 0.0
    align = book_sign * risk.growth_tilt
    if align >= _FACTOR_CROWD:
        mult, note = _FACTOR_CROWD_MULT, "adds to crowding"
    elif align <= _FACTOR_BALANCE:
        mult, note = _FACTOR_BALANCE_MULT, "balances the book"
    else:
        mult, note = 1.0, "neutral"
    detail = f"growth tilt {risk.growth_tilt:+.2f} vs book {book.growth_tilt:+.2f} ({note})"
    return FitFactor("factor", "Factor fit", mult, detail, False)


def _sector_factor(risk: CandidateRisk, book: BookContext) -> FitFactor:
    if risk.sector is None or not book.sector_weights:
        return FitFactor("sector", "Sector fit", 1.0, "n/a", True)
    sw = book.sector_weights.get(risk.sector, 0.0)
    if sw >= _SECTOR_HEAVY:
        mult, note = 0.88, "adds to a heavy sector"
    elif sw >= _SECTOR_WARM:
        mult, note = 0.95, "adds to a warm sector"
    elif sw <= _SECTOR_LIGHT:
        mult, note = 1.08, "under-represented sector"
    else:
        mult, note = 1.0, "balanced sector"
    detail = f"{risk.sector} = {sw * 100.0:.0f}% of book ({note})"
    return FitFactor("sector", "Sector fit", mult, detail, False)


def score_candidate_fit(risk: CandidateRisk, book: BookContext) -> CandidateFit:
    """Pure scorer: a candidate's price-derived geometry + the book state → the
    four-factor fit decomposition. Missing factors contribute a neutral 1.0 and
    flag the read partial (an unknowable fit must not read as dilutive)."""
    factors = [
        _sharpe_factor(risk, book),
        _divers_factor(risk),
        _factor_factor(risk, book),
        _sector_factor(risk, book),
    ]
    fit = 1.0
    for f in factors:
        fit *= f.multiplier
    partial = any(f.missing for f in factors)
    why = " x ".join(f"{f.key} {f.multiplier:.2f} ({f.detail})" for f in factors) + f" = {fit:.2f}"
    return CandidateFit(
        ticker=risk.ticker, factors=factors, fit=fit, why=why, partial=partial, obs=risk.obs
    )


# --------------------------------------------------------------------------- #
# Gatherer (price-history I/O) — morning-pipeline path, not the render path.
# --------------------------------------------------------------------------- #


def _mean_var(xs: list[float]) -> tuple[float, float]:
    """Population mean and variance of a return series."""
    n = len(xs)
    if n == 0:
        return 0.0, 0.0
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / n
    return mean, var


def _cov(xs: list[float], ys: list[float]) -> float:
    """Population covariance over paired, equal-length return series."""
    n = len(xs)
    if n == 0:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    return sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / n


def _beta(asset: list[float], market: list[float]) -> float | None:
    """OLS beta of ``asset`` on ``market`` (cov / var_market). None when the
    market series has zero variance."""
    _m_mean, var_m = _mean_var(market)
    if var_m <= 0.0:
        return None
    return _cov(asset, market) / var_m


def _book_return_series(
    repo_root: Path, weights: Mapping[str, float], *, lookback_obs: int, min_overlap_obs: int
) -> dict[date, float]:
    """The held book's daily log-return series r_book(t) = Σ_i w_i · r_i(t) over
    the holdings' common trading calendar, at the passed weights (renormalized
    over the priced names). Empty when fewer than two holdings have usable,
    sufficiently-overlapping history — the book-relative factors then degrade."""
    returns_by_ticker: dict[str, dict[date, float]] = {}
    for t in weights:
        rets = daily_log_returns(load_daily_closes(t, repo_root))
        if rets:
            returns_by_ticker[t] = rets
    if len(returns_by_ticker) < 2:
        return {}
    aligned = build_aligned_returns(
        returns_by_ticker, lookback_obs=lookback_obs, min_overlap_obs=min_overlap_obs
    )
    if aligned is None:
        return {}
    w = np.array([max(weights.get(t, 0.0), 0.0) for t in aligned.tickers], dtype=np.float64)
    w_sum = float(w.sum())
    w = w / w_sum if w_sum > 0.0 else np.full(len(aligned.tickers), 1.0 / len(aligned.tickers))
    r_book = aligned.matrix @ w
    return dict(zip(aligned.dates, (float(x) for x in r_book), strict=True))


def _overlap(a: Mapping[date, float], b: Mapping[date, float]) -> list[date]:
    return sorted(set(a) & set(b))


def _candidate_risk(
    repo_root: Path,
    ticker: str,
    *,
    book_series: Mapping[date, float],
    book: BookContext,
    spy_returns: Mapping[date, float],
    qqq_returns: Mapping[date, float],
    sector: str | None,
    min_overlap_obs: int,
) -> CandidateRisk:
    """One candidate's geometry against the book + benchmarks, from price history.

    The book-relative legs (corr, marginal vol, Sharpe) need ``CAND_MIN_OBS``
    common days with the book; the growth-tilt leg needs the same against
    SPY/QQQ. Each leg degrades independently — a name can have a Sharpe read but
    no growth tilt (e.g. no benchmark cache)."""
    cand_returns = daily_log_returns(load_daily_closes(ticker, repo_root))
    if not cand_returns:
        return CandidateRisk(ticker=ticker, sector=sector)

    corr: float | None = None
    marginal_vol_ann: float | None = None
    sharpe: float | None = None
    obs: int | None = None
    history_thin = False

    book_dates = _overlap(cand_returns, book_series)
    if book_series:
        if len(book_dates) >= min_overlap_obs:
            cand = [cand_returns[d] for d in book_dates]
            bk = [book_series[d] for d in book_dates]
            obs = len(book_dates)
            _cand_mean, cand_var = _mean_var(cand)
            _bk_mean, bk_var = _mean_var(bk)
            cand_sd = math.sqrt(cand_var)
            bk_sd = math.sqrt(bk_var)
            cov = _cov(cand, bk)
            if cand_sd > 0.0 and bk_sd > 0.0:
                corr = cov / (cand_sd * bk_sd)
            if bk_sd > 0.0:
                marginal_vol_ann = (cov / bk_sd) * ANNUALIZE
            if cand_sd > 0.0 and book.risk_free_annual is not None:
                ann_mean = _cand_mean * TRADING_DAYS
                sharpe = (ann_mean - book.risk_free_annual) / (cand_sd * ANNUALIZE)
        else:
            history_thin = True

    growth_tilt: float | None = None
    spy_dates = _overlap(cand_returns, spy_returns)
    qqq_dates = _overlap(cand_returns, qqq_returns)
    if len(spy_dates) >= min_overlap_obs and len(qqq_dates) >= min_overlap_obs:
        beta_spy = _beta([cand_returns[d] for d in spy_dates], [spy_returns[d] for d in spy_dates])
        beta_qqq = _beta([cand_returns[d] for d in qqq_dates], [qqq_returns[d] for d in qqq_dates])
        if beta_spy is not None and beta_qqq is not None:
            growth_tilt = beta_qqq - beta_spy

    return CandidateRisk(
        ticker=ticker,
        corr_to_book=corr,
        marginal_vol_ann=marginal_vol_ann,
        sharpe=sharpe,
        growth_tilt=growth_tilt,
        sector=sector,
        obs=obs,
        history_thin=history_thin,
    )


def compute_candidate_fit(
    repo_root: Path,
    candidates: Sequence[str],
    book: BookContext,
    *,
    sectors: Mapping[str, str] | None = None,
    lookback_obs: int = 252,
    min_overlap_obs: int = CAND_MIN_OBS,
) -> dict[str, CandidateFit]:
    """Portfolio-fit per evaluation name, from on-disk daily price history + the
    passed book state. One :class:`CandidateFit` per candidate (upper-cased key);
    a name with no usable history still gets an all-neutral, partial fit rather
    than vanishing. ``sectors`` maps ticker → sector (the FMP profile sector the
    caller already reads); when absent the sector factor degrades. Heavy enough
    (a price-history read per name) that it belongs in the morning pipeline, not
    the render path."""
    book_series = _book_return_series(
        repo_root, book.weights, lookback_obs=lookback_obs, min_overlap_obs=min_overlap_obs
    )
    spy_returns = daily_log_returns(load_daily_closes(_SPY, repo_root))
    qqq_returns = daily_log_returns(load_daily_closes(_QQQ, repo_root))
    sectors = sectors or {}
    out: dict[str, CandidateFit] = {}
    for raw in candidates:
        t = raw.strip().upper()
        if not t:
            continue
        risk = _candidate_risk(
            repo_root,
            t,
            book_series=book_series,
            book=book,
            spy_returns=spy_returns,
            qqq_returns=qqq_returns,
            sector=sectors.get(t),
            min_overlap_obs=min_overlap_obs,
        )
        out[t] = score_candidate_fit(risk, book)
    return out
