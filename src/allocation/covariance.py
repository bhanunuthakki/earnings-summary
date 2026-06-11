"""Shrunk covariance estimation + marginal risk contributions.

Pure math over a T×N daily log-return matrix — no DB, no I/O — so the
next-dollar model's risk leg is testable against synthetic data.

The estimator is Ledoit–Wolf (2004), "A well-conditioned estimator for
large-dimensional covariance matrices": the sample covariance S is shrunk
toward the scaled identity F = μI (μ = mean sample variance), with the
shrinkage intensity δ estimated from the data itself. With ~250 daily
observations on ~10 names the sample estimate is already decent; shrinkage
mostly guards the panel against a short overlapping window (post-IPO names)
turning sampling noise into spurious "diversification".
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt


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
