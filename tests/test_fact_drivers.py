"""Pure DCF field bounds, unit conversion, and refusal safeguards."""

from __future__ import annotations

import pytest

from dcf import fact_drivers
from dcf.fact_drivers import DRIVER_FIELDS_BY_KEY, FactDriverError, convert_to_driver


def test_wacc_is_not_an_injectable_field() -> None:
    """WACC has no input cell (derived from rf/ERP/beta/Kd) — it must never be a
    directly-writable driver (a guardrail, not an oversight)."""
    assert "wacc" not in DRIVER_FIELDS_BY_KEY
    keys = {f.key for f in fact_drivers.DRIVER_FIELDS}
    # The CAPM levers a "set WACC" fact should target instead ARE present.
    assert {"risk_free_rate", "equity_risk_premium", "beta", "cost_of_debt"} <= keys


def test_registry_fields_are_unique_and_labeled() -> None:
    fields = fact_drivers.DRIVER_FIELDS
    assert len(DRIVER_FIELDS_BY_KEY) == len(fields)
    assert len({field.key for field in fields}) == len(fields)
    assert all(field.label for field in fields)


# --------------------------------------------------------------------------- #
# Converter — proportions (the 4.3-vs-0.043 trap)
# --------------------------------------------------------------------------- #


def test_percent_scales_to_decimal_ratio() -> None:
    """A margin stored as percent 42.0 must become the ratio 0.42 the Dashboard
    expects — NOT 42.0 (the headline bug the whole converter exists to prevent)."""
    field = DRIVER_FIELDS_BY_KEY["near_op_margin"]
    out = convert_to_driver(42.0, "percent", field)
    assert out.value == pytest.approx(0.42)
    assert out.target_unit == "ratio"


def test_ratio_source_is_identity() -> None:
    field = DRIVER_FIELDS_BY_KEY["near_op_margin"]
    assert convert_to_driver(0.42, "ratio", field).value == pytest.approx(0.42)


def test_bps_scales_to_ratio() -> None:
    """A spread/rate stored in bps: 430 bps → 0.043."""
    field = DRIVER_FIELDS_BY_KEY["risk_free_rate"]
    assert convert_to_driver(430.0, "bps", field).value == pytest.approx(0.043)


def test_absent_unit_rejected_for_proportion() -> None:
    """Without a stored unit we cannot tell 4.3 from 0.043 — refuse, don't guess."""
    field = DRIVER_FIELDS_BY_KEY["near_op_margin"]
    with pytest.raises(FactDriverError, match="no stored unit"):
        convert_to_driver(4.3, None, field)


def test_dollar_unit_rejected_for_a_margin() -> None:
    """A $ level offered to a margin field is a category error — cross-family
    conversion is refused, never silently rescaled."""
    field = DRIVER_FIELDS_BY_KEY["near_op_margin"]
    with pytest.raises(FactDriverError, match="incompatible units"):
        convert_to_driver(1000.0, "millions", field)


# --------------------------------------------------------------------------- #
# Converter — money ($M)
# --------------------------------------------------------------------------- #


def test_actual_dollars_scale_to_millions() -> None:
    """Capex stored in raw dollars ($1.5B) → 1500 ($M)."""
    field = DRIVER_FIELDS_BY_KEY["capex_2026_m"]
    out = convert_to_driver(1_500_000_000.0, "actual", field)
    assert out.value == pytest.approx(1500.0)
    assert out.target_unit == "millions"


def test_billions_scale_to_millions() -> None:
    field = DRIVER_FIELDS_BY_KEY["capex_2026_m"]
    assert convert_to_driver(1.5, "billions", field).value == pytest.approx(1500.0)


def test_percent_rejected_for_money_field() -> None:
    field = DRIVER_FIELDS_BY_KEY["capex_2026_m"]
    with pytest.raises(FactDriverError, match="incompatible units"):
        convert_to_driver(12.0, "percent", field)


# --------------------------------------------------------------------------- #
# Converter — RAW (beta, exit multiple in turns)
# --------------------------------------------------------------------------- #


def test_raw_beta_taken_as_is() -> None:
    field = DRIVER_FIELDS_BY_KEY["beta"]
    out = convert_to_driver(1.15, "ratio", field)
    assert out.value == pytest.approx(1.15)
    assert out.target_unit is None


def test_raw_exit_multiple_from_ratio() -> None:
    field = DRIVER_FIELDS_BY_KEY["exit_multiple"]
    assert convert_to_driver(12.5, "ratio", field).value == pytest.approx(12.5)


def test_raw_rejects_a_percent_rate() -> None:
    """A percent fact is a RATE — it belongs in a rate field, not used raw as a
    turn-count/beta (which would be a 100× error)."""
    field = DRIVER_FIELDS_BY_KEY["exit_multiple"]
    with pytest.raises(FactDriverError, match="rate"):
        convert_to_driver(12.5, "percent", field)


# --------------------------------------------------------------------------- #
# Converter — sanity bounds + non-finite
# --------------------------------------------------------------------------- #


def test_out_of_bounds_value_rejected() -> None:
    """A 'margin' that converts to 4.2 (e.g. a 420% mis-typed fact) is degenerate
    — rejected before it can reach the model."""
    field = DRIVER_FIELDS_BY_KEY["near_op_margin"]
    with pytest.raises(FactDriverError, match="outside the safe range"):
        convert_to_driver(420.0, "percent", field)  # → 4.2 ratio, above 0.95


def test_negative_margin_within_bounds_is_allowed() -> None:
    """A real loss-making margin (-30%) is a legitimate driver, not degenerate."""
    field = DRIVER_FIELDS_BY_KEY["near_op_margin"]
    assert convert_to_driver(-30.0, "percent", field).value == pytest.approx(-0.3)


def test_non_finite_rejected() -> None:
    field = DRIVER_FIELDS_BY_KEY["near_op_margin"]
    with pytest.raises(FactDriverError, match="not a finite number"):
        convert_to_driver(float("nan"), "ratio", field)


def test_exit_multiple_out_of_bounds_rejected() -> None:
    field = DRIVER_FIELDS_BY_KEY["exit_multiple"]
    with pytest.raises(FactDriverError, match="outside the safe range"):
        convert_to_driver(0.42, "ratio", field)  # a margin mis-mapped to a multiple
