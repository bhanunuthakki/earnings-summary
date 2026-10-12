"""Per-name risk contribution off a shrunk covariance — the one place the book's
covariance is assembled.

``marginal_risk`` (the Ledoit–Wolf estimator + risk geometry below) gives the
risk geometry (portfolio vol, marginal vol, correlation to book) at a set of
weights; this module wires it to the on-disk daily-return history: load each
holding's prices, align them onto a common calendar, Ledoit–Wolf shrink the
covariance, renormalize the weights over the names that made the matrix, and
turn the result into per-name **risk contributions** —
``RC_i = w_i · ∂σ_p/∂w_i``, which by Euler's theorem sum to the portfolio vol,
so ``risk_share_i = RC_i / σ_p`` is the name's share of total book risk.

Two consumers share this single assembly so they can never disagree on "the
book's risk": the next-dollar model's diversification factor
(``allocation.model._diversification``) and the L7 risk-budget allocator's risk
leg (``risk_reward``). Pure over the price files + the passed weights (no DB).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import numpy.typing as npt

from allocation.price_history import build_aligned_returns, daily_log_returns, load_daily_closes

COV_LOOKBACK_OBS = 252  # ~1 trading year of daily returns in the covariance
MIN_OVERLAP_OBS = 120  # min common trading days for a name to enter the matrix
ANNUALIZE = math.sqrt(252.0)


# ---------------------------------------------------------------------------
# Shrunk covariance estimation + marginal risk (folded in from the former
# ``allocation.covariance``). Pure math over a TxN daily log-return matrix —
# no DB, no I/O — so the next-dollar model's risk leg is testable against
# synthetic data.
#
# The estimator is Ledoit-Wolf (2004), "A well-conditioned estimator for
# large-dimensional covariance matrices": the sample covariance S is shrunk
# toward the scaled identity F = μI (μ = mean sample variance), with the
# shrinkage intensity δ estimated from the data itself. With ~250 daily
# observations on ~10 names the sample estimate is already decent; shrinkage
# mostly guards the panel against a short overlapping window (post-IPO names)
# turning sampling noise into spurious "diversification".
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShrunkCovariance:
    sigma: npt.NDArray[np.float64]  # NxN covariance, same (daily) units as the input
    shrinkage: float  # delta in [0, 1] applied toward the scaled identity
    n_obs: int  # T — rows of the return matrix


def ledoit_wolf(returns: npt.NDArray[np.float64]) -> ShrunkCovariance:
    """Σ* = δ·μI + (1−δ)·S over a T×N return matrix (rows = days).

    δ = min(b̄², d²)/d² with d² = ‖S − μI‖²_F and
    b̄² = (1/T²) Σ_t ‖x_t x_tᵀ − S‖²_F (x_t demeaned) — the standard
    Ledoit–Wolf intensity. A degenerate panel (d² = 0, e.g. constant
    returns) keeps the sample estimate with δ = 0.
    """
    x = np.asarray(returns, dtype=np.float64)
    if x.ndim != 2 or x.shape[0] < 2 or x.shape[1] < 1:
        raise ValueError(f"need a TxN matrix with T >= 2, got shape {x.shape}")
    t_obs, n = x.shape
    xc = x - x.mean(axis=0)
    s: npt.NDArray[np.float64] = (xc.T @ xc) / t_obs
    mu = float(np.trace(s)) / n
    target: npt.NDArray[np.float64] = mu * np.eye(n)
    d2 = float(((s - target) ** 2).sum())
    if d2 <= 0.0:
        return ShrunkCovariance(sigma=s, shrinkage=0.0, n_obs=t_obs)
    b_bar2 = 0.0
    for row in xc:
        b_bar2 += float(((np.outer(row, row) - s) ** 2).sum())
    b_bar2 /= t_obs**2
    delta = min(1.0, b_bar2 / d2)  # b² = min(b̄², d²) ⇒ δ ∈ [0, 1]
    sigma: npt.NDArray[np.float64] = delta * target + (1.0 - delta) * s
    return ShrunkCovariance(sigma=sigma, shrinkage=delta, n_obs=t_obs)


@dataclass(frozen=True, slots=True)
class MarginalRisk:
    """Per-asset risk geometry at the given weights (units of the input Σ)."""

    portfolio_vol: float  # sigma_p = sqrt(w' S w)
    marginal_vol: npt.NDArray[np.float64]  # d sigma_p / d w_i = (S w)_i / sigma_p
    corr_to_book: npt.NDArray[np.float64]  # corr(asset i, portfolio)


def marginal_risk(
    sigma: npt.NDArray[np.float64], weights: npt.NDArray[np.float64]
) -> MarginalRisk | None:
    """How the next marginal dollar of each asset moves portfolio volatility.

    ``marginal_vol`` is the derivative of σ_p with respect to asset i's
    weight — the risk leg of marginal Sharpe (the return leg lives in the
    model's expected-return factor). Low (or negative) marginal vol = the
    asset diversifies the book at current weights. Returns None when the
    portfolio variance is non-positive (degenerate Σ or all-zero weights).
    """
    s = np.asarray(sigma, dtype=np.float64)
    w = np.asarray(weights, dtype=np.float64)
    if s.ndim != 2 or s.shape[0] != s.shape[1] or w.shape != (s.shape[0],):
        raise ValueError(f"shape mismatch: sigma {s.shape}, weights {w.shape}")
    pvar = float(w @ s @ w)
    if pvar <= 0.0 or not math.isfinite(pvar):
        return None
    pvol = math.sqrt(pvar)
    cov_to_book: npt.NDArray[np.float64] = s @ w
    marginal_vol: npt.NDArray[np.float64] = cov_to_book / pvol
    asset_vol: npt.NDArray[np.float64] = np.sqrt(np.clip(np.diag(s), 0.0, None))
    denom: npt.NDArray[np.float64] = asset_vol * pvol
    safe: npt.NDArray[np.float64] = np.where(denom > 0.0, denom, 1.0)
    corr: npt.NDArray[np.float64] = np.where(denom > 0.0, cov_to_book / safe, 0.0)
    return MarginalRisk(portfolio_vol=pvol, marginal_vol=marginal_vol, corr_to_book=corr)


@dataclass(slots=True)
class BookRisk:
    """Per-name risk geometry of the modeled book at the given weights.

    All vols are annualized fractions. ``risk_contribution_ann`` sums (over the
    covariance names) to ``portfolio_vol_ann`` by Euler's theorem, so
    ``risk_share`` sums to ~1.0 — each name's share of total book risk (it can go
    slightly negative for a genuine diversifier). ``weights`` are the book
    weights renormalized over the names that entered the matrix. ``hidden_reason``
    is set (and every map empty) when no covariance could be built — fewer than
    two priced names, too little calendar overlap, or a degenerate matrix."""

    tickers: list[str]
    weights: dict[str, float]
    marginal_vol_ann: dict[str, float]
    risk_contribution_ann: dict[str, float]
    risk_share: dict[str, float]
    corr_to_book: dict[str, float]
    portfolio_vol_ann: float | None
    prices_through: date | None
    cov_obs: int | None
    shrinkage: float | None
    dropped: dict[str, str] = field(default_factory=dict[str, str])
    hidden_reason: str | None = None


def _empty(reason: str, no_file: list[str]) -> BookRisk:
    return BookRisk(
        tickers=[],
        weights={},
        marginal_vol_ann={},
        risk_contribution_ann={},
        risk_share={},
        corr_to_book={},
        portfolio_vol_ann=None,
        prices_through=None,
        cov_obs=None,
        shrinkage=None,
        dropped={t: "no daily price history on file" for t in no_file},
        hidden_reason=reason,
    )


def build_book_risk(
    repo_root: Path,
    tickers: Sequence[str],
    weights: Mapping[str, float],
    *,
    lookback_obs: int = COV_LOOKBACK_OBS,
    min_overlap_obs: int = MIN_OVERLAP_OBS,
) -> BookRisk:
    """Risk contribution per holding off a Ledoit–Wolf shrunk covariance of ~1y
    of daily log returns, at the passed weights (renormalized over the priced
    names; equal-weight when the passed weights sum to zero). Returns a
    ``BookRisk`` whose ``hidden_reason`` is set when the matrix can't be built."""
    returns_by_ticker: dict[str, dict[date, float]] = {}
    for t in tickers:
        rets = daily_log_returns(load_daily_closes(t, repo_root))
        if rets:
            returns_by_ticker[t] = rets
    no_file = [t for t in tickers if t not in returns_by_ticker]
    if len(returns_by_ticker) < 2:
        return _empty("fewer than two holdings with price history on file", no_file)
    aligned = build_aligned_returns(
        returns_by_ticker, lookback_obs=lookback_obs, min_overlap_obs=min_overlap_obs
    )
    if aligned is None:
        return _empty(
            f"holdings share fewer than {min_overlap_obs} common trading days of history", no_file
        )
    cov = ledoit_wolf(aligned.matrix)
    w_arr = np.array([weights.get(t, 0.0) for t in aligned.tickers], dtype=np.float64)
    w_sum = float(w_arr.sum())
    if w_sum > 0.0:
        w_arr = w_arr / w_sum
    else:
        w_arr = np.full(len(aligned.tickers), 1.0 / len(aligned.tickers), dtype=np.float64)
    risk = marginal_risk(cov.sigma, w_arr)
    if risk is None:
        return _empty("degenerate covariance (zero portfolio variance)", no_file)

    pvol_ann = risk.portfolio_vol * ANNUALIZE
    weights_used: dict[str, float] = {}
    marginal_vol_ann: dict[str, float] = {}
    risk_contribution_ann: dict[str, float] = {}
    risk_share: dict[str, float] = {}
    corr_to_book: dict[str, float] = {}
    for i, t in enumerate(aligned.tickers):
        w_i = float(w_arr[i])
        mvol = float(risk.marginal_vol[i]) * ANNUALIZE
        rc = w_i * mvol
        weights_used[t] = w_i
        marginal_vol_ann[t] = mvol
        risk_contribution_ann[t] = rc
        risk_share[t] = rc / pvol_ann if pvol_ann > 0.0 else 0.0
        corr_to_book[t] = float(risk.corr_to_book[i])

    dropped = dict(aligned.dropped)
    for t in no_file:
        dropped[t] = "no daily price history on file"
    return BookRisk(
        tickers=list(aligned.tickers),
        weights=weights_used,
        marginal_vol_ann=marginal_vol_ann,
        risk_contribution_ann=risk_contribution_ann,
        risk_share=risk_share,
        corr_to_book=corr_to_book,
        portfolio_vol_ann=pvol_ann,
        prices_through=aligned.dates[-1],
        cov_obs=cov.n_obs,
        shrinkage=cov.shrinkage,
        dropped=dropped,
    )
