"""Tests for src/allocation/covariance.py — Ledoit-Wolf + marginal risk."""

from __future__ import annotations

import math

import numpy as np
import pytest

from allocation.covariance import ledoit_wolf, marginal_risk


def _sample(true_cov: np.ndarray, t_obs: int, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.multivariate_normal(np.zeros(true_cov.shape[0]), true_cov, size=t_obs)


def test_ledoit_wolf_recovers_known_covariance() -> None:
    true_cov = np.array([[0.0004, 0.0001], [0.0001, 0.0009]])
    est = ledoit_wolf(_sample(true_cov, 4000))
    assert 0.0 <= est.shrinkage <= 1.0
    assert est.n_obs == 4000
    assert np.allclose(est.sigma, est.sigma.T)
    assert np.allclose(est.sigma, true_cov, rtol=0.15, atol=1e-5)


def test_ledoit_wolf_shrinks_small_samples_more() -> None:
    true_cov = np.array([[0.0004, 0.0001], [0.0001, 0.0009]])
    small = ledoit_wolf(_sample(true_cov, 25))
    large = ledoit_wolf(_sample(true_cov, 2500))
    assert small.shrinkage > large.shrinkage


def test_ledoit_wolf_psd_and_bounded_on_thin_panel() -> None:
    # More names than observations — the case shrinkage exists for.
    rng = np.random.default_rng(3)
    est = ledoit_wolf(rng.normal(0.0, 0.02, size=(8, 12)))
    assert 0.0 <= est.shrinkage <= 1.0
    eigvals = np.linalg.eigvalsh(est.sigma)
    assert float(eigvals.min()) >= -1e-12


def test_ledoit_wolf_degenerate_constant_returns() -> None:
    est = ledoit_wolf(np.zeros((10, 3)))
    assert est.shrinkage == 0.0
    assert np.allclose(est.sigma, 0.0)


def test_ledoit_wolf_validates_shape() -> None:
    with pytest.raises(ValueError):
        ledoit_wolf(np.zeros((1, 3)))
    with pytest.raises(ValueError):
        ledoit_wolf(np.zeros(5))


def test_marginal_risk_hand_computed_two_assets() -> None:
    sigma = np.array([[0.04, 0.01], [0.01, 0.09]])
    w = np.array([0.5, 0.5])
    risk = marginal_risk(sigma, w)
    assert risk is not None
    pvol = math.sqrt(0.25 * 0.04 + 0.25 * 0.09 + 2 * 0.25 * 0.01)
    assert risk.portfolio_vol == pytest.approx(pvol)
    assert risk.marginal_vol[0] == pytest.approx(0.025 / pvol)
    assert risk.marginal_vol[1] == pytest.approx(0.05 / pvol)
    assert risk.corr_to_book[0] == pytest.approx(0.025 / (0.2 * pvol))
    assert risk.corr_to_book[1] == pytest.approx(0.05 / (0.3 * pvol))
    # Euler decomposition: weighted marginal vols sum to portfolio vol.
    assert float(w @ risk.marginal_vol) == pytest.approx(pvol)


def test_marginal_risk_low_correlation_means_lower_marginal_vol() -> None:
    # Same variances; the uncorrelated third asset must add less marginal risk
    # than the two correlated ones at equal weights.
    sigma = np.array(
        [
            [0.04, 0.036, 0.0],
            [0.036, 0.04, 0.0],
            [0.0, 0.0, 0.04],
        ]
    )
    risk = marginal_risk(sigma, np.array([1 / 3, 1 / 3, 1 / 3]))
    assert risk is not None
    assert risk.marginal_vol[2] < risk.marginal_vol[0]
    assert risk.marginal_vol[2] < risk.marginal_vol[1]
    assert risk.corr_to_book[2] < risk.corr_to_book[0]


def test_marginal_risk_degenerate_returns_none() -> None:
    assert marginal_risk(np.zeros((2, 2)), np.array([0.5, 0.5])) is None
    sigma = np.array([[0.04, 0.0], [0.0, 0.04]])
    assert marginal_risk(sigma, np.zeros(2)) is None


def test_marginal_risk_shape_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        marginal_risk(np.zeros((2, 2)), np.array([1.0, 0.0, 0.0]))
