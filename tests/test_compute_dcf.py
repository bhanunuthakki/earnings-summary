"""Tests for src/compute/dcf.py."""

from __future__ import annotations

import math

import pytest

from compute.dcf import DcfInputs, compute_dcf


def test_compute_dcf_simple_round_trip() -> None:
    """Sanity-check a known set of inputs against hand-computed expected output."""
    inputs = DcfInputs(
        base_revenue=100.0,
        revenue_growths=[0.10, 0.10, 0.10],
        fcf_margin=0.20,
        wacc=0.10,
        terminal_growth=0.02,
        shares_outstanding=10.0,
    )
    result = compute_dcf(inputs)

    # Year 1 FCF: 100 * 1.1 * 0.2 = 22
    # Year 2: 100 * 1.21 * 0.2 = 24.2
    # Year 3: 100 * 1.331 * 0.2 = 26.62
    assert math.isclose(result.projected_fcfs[0], 22.0, rel_tol=1e-9)
    assert math.isclose(result.projected_fcfs[1], 24.2, rel_tol=1e-9)
    assert math.isclose(result.projected_fcfs[2], 26.62, rel_tol=1e-9)

    expected_sum_dcf = 22.0 / 1.10 + 24.2 / 1.10**2 + 26.62 / 1.10**3
    assert math.isclose(result.sum_of_discounted_fcfs, expected_sum_dcf, rel_tol=1e-9)

    # Terminal: TF = 26.62 * 1.02 = 27.1524; TV = 27.1524 / (0.10 - 0.02) = 339.405
    # Discounted: TV / 1.10^3 = 339.405 / 1.331 = ~255
    expected_tv = 27.1524 / 0.08
    expected_disc_tv = expected_tv / 1.10**3
    assert math.isclose(result.terminal_value, expected_tv, rel_tol=1e-9)
    assert math.isclose(result.discounted_terminal, expected_disc_tv, rel_tol=1e-9)

    expected_npv = expected_sum_dcf + expected_disc_tv
    assert math.isclose(result.npv, expected_npv, rel_tol=1e-9)
    assert result.npv_per_share is not None
    assert math.isclose(result.npv_per_share, expected_npv / 10.0, rel_tol=1e-9)


def test_compute_dcf_rejects_wacc_below_terminal_growth() -> None:
    """Gordon Growth requires WACC > terminal growth."""
    inputs = DcfInputs(
        base_revenue=100.0,
        revenue_growths=[0.05],
        fcf_margin=0.10,
        wacc=0.05,
        terminal_growth=0.05,
    )
    with pytest.raises(ValueError, match="must exceed terminal_growth"):
        compute_dcf(inputs)


def test_compute_dcf_rejects_empty_growths() -> None:
    """No projection horizon = no DCF."""
    inputs = DcfInputs(
        base_revenue=100.0,
        revenue_growths=[],
        fcf_margin=0.10,
        wacc=0.10,
        terminal_growth=0.02,
    )
    with pytest.raises(ValueError, match="non-empty"):
        compute_dcf(inputs)


def test_compute_dcf_rejects_unreasonable_fcf_margin() -> None:
    """FCF margin must be in [-1, 1]."""
    inputs = DcfInputs(
        base_revenue=100.0,
        revenue_growths=[0.05],
        fcf_margin=2.0,
        wacc=0.10,
        terminal_growth=0.02,
    )
    with pytest.raises(ValueError, match="fcf_margin"):
        compute_dcf(inputs)


def test_compute_dcf_quarterly_matches_annual_at_constant_growth() -> None:
    """At the same effective annual growth, quarterly and annual DCFs should agree."""
    annual_growth = 0.10
    quarterly_growth = (1 + annual_growth) ** 0.25 - 1
    annual_inputs = DcfInputs(
        base_revenue=100.0,
        revenue_growths=[annual_growth] * 5,
        fcf_margin=0.20,
        wacc=0.10,
        terminal_growth=0.02,
        periods_per_year=1,
    )
    quarterly_inputs = DcfInputs(
        base_revenue=25.0,  # per-period quarterly base = 100 / 4
        revenue_growths=[quarterly_growth] * 20,
        fcf_margin=0.20,
        wacc=0.10,
        terminal_growth=0.02,
        periods_per_year=4,
    )
    annual_result = compute_dcf(annual_inputs)
    quarterly_result = compute_dcf(quarterly_inputs)
    # Both should converge — terminal value dominates so absolute NPVs differ slightly,
    # but they should be within a few percent of each other for the same effective rates.
    ratio = quarterly_result.npv / annual_result.npv
    assert 0.95 < ratio < 1.05, (
        f"quarterly NPV {quarterly_result.npv} vs annual {annual_result.npv}"
    )


def test_compute_dcf_quarterly_40_period_horizon() -> None:
    """A 10-year horizon with periods_per_year=4 produces 40 projected FCFs."""
    inputs = DcfInputs(
        base_revenue=1_000_000.0,
        revenue_growths=[0.02] * 40,
        fcf_margin=0.20,
        wacc=0.10,
        terminal_growth=0.025,
        periods_per_year=4,
    )
    result = compute_dcf(inputs)
    assert len(result.projected_fcfs) == 40


def test_compute_dcf_rejects_zero_periods_per_year() -> None:
    """periods_per_year must be >= 1."""
    inputs = DcfInputs(
        base_revenue=100.0,
        revenue_growths=[0.05],
        fcf_margin=0.10,
        wacc=0.10,
        terminal_growth=0.02,
        periods_per_year=0,
    )
    with pytest.raises(ValueError, match="periods_per_year"):
        compute_dcf(inputs)


def test_compute_dcf_per_share_is_none_when_no_shares() -> None:
    """If shares_outstanding is None or zero, npv_per_share is None."""
    inputs = DcfInputs(
        base_revenue=100.0,
        revenue_growths=[0.05],
        fcf_margin=0.10,
        wacc=0.10,
        terminal_growth=0.02,
        shares_outstanding=None,
    )
    result = compute_dcf(inputs)
    assert result.npv_per_share is None
