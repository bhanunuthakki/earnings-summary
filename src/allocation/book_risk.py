"""Per-name risk contribution off a shrunk covariance — the one place the book's
covariance is assembled.

``allocation.covariance.marginal_risk`` gives the risk geometry (portfolio vol,
marginal vol, correlation to book) at a set of weights; this module wires it to
the on-disk daily-return history: load each holding's prices, align them onto a
common calendar, Ledoit–Wolf shrink the covariance, renormalize the weights over
the names that made the matrix, and turn the result into per-name **risk
contributions** — ``RC_i = w_i · ∂σ_p/∂w_i``, which by Euler's theorem sum to the
portfolio vol, so ``risk_share_i = RC_i / σ_p`` is the name's share of total book
risk.

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

from allocation.covariance import ledoit_wolf, marginal_risk
from allocation.price_history import build_aligned_returns, daily_log_returns, load_daily_closes

COV_LOOKBACK_OBS = 252  # ~1 trading year of daily returns in the covariance
MIN_OVERLAP_OBS = 120  # min common trading days for a name to enter the matrix
ANNUALIZE = math.sqrt(252.0)


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
