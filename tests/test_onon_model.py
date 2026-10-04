"""Economic rent/SBC model regressions against the dated offline ONON memo."""

from datetime import date

import pytest

from dcf.input_evidence import canonical_digest
from dcf.onon_model import replay


def memo_inputs() -> dict[str, float]:
    inputs = {
        "shares": 336.1087012,
        "cash": 1204.7,
        "financial_debt": 0.0,
        "lease_liabilities": 562.5,
        "price_usd": 30.85,
        "chf_per_usd": 0.8283,
        "revenue_anchor_chf_m": 3515.0,
        "wacc": 0.105,
        "valuation_date_ordinal": float(date(2026, 10, 3).toordinal()),
        "forecast_start_year": 2027.0,
        "guidance_revenue_low_chf_m": 3470.0,
        "guidance_revenue_high_chf_m": 3560.0,
        "guidance_ebitda_margin_low": 0.195,
        "guidance_ebitda_margin_high": 0.20,
    }
    schedules = {
        "bear": (
            [0.12, 0.10, 0.10, 0.08, 0.06],
            [0.185, 0.185, 0.185, 0.185, 0.18],
            [0.023, 0.035, 0.016, 0.035, 0.22, 0.21, 12.0, 0.025],
        ),
        "base": (
            [0.168, 0.168, 0.168, 0.15, 0.14],
            [0.205, 0.213, 0.22, 0.225, 0.23],
            [0.02, 0.03, 0.016, 0.028, 0.20, 0.20, 20.0, 0.03],
        ),
        "bull": (
            [0.21, 0.21, 0.21, 0.18, 0.16],
            [0.21, 0.225, 0.24, 0.245, 0.25],
            [0.018, 0.028, 0.016, 0.025, 0.18, 0.19, 24.0, 0.035],
        ),
    }
    fields = (
        "sbc",
        "rent",
        "nonlease_da",
        "capex",
        "incremental_nwc",
        "tax",
        "terminal_fcf_multiple",
        "terminal_g",
    )
    for name, (growth, margins, scalars) in schedules.items():
        for index, value in enumerate(growth, 1):
            inputs[f"{name}_growth_{index}"] = value
        for index, value in enumerate(margins, 1):
            inputs[f"{name}_ebitda_margin_{index}"] = value
        inputs.update({f"{name}_{key}": value for key, value in zip(fields, scalars, strict=True)})
    return inputs


def test_offline_memo_replay_and_cash_bridge() -> None:
    result = replay(memo_inputs())
    assert result["vps"] == pytest.approx(34.65819096184405)
    assert result["equity_value"] == pytest.approx(result["operating_ev"] + 1204.7 / 0.8283)
    assert result["scenarios"]["bear"]["gordon_fair_value_usd"] == pytest.approx(16.166935724254245)
    assert result["scenarios"]["bull"]["gordon_fair_value_usd"] == pytest.approx(49.31114111363013)
    assert result["scenarios"]["base"]["exit_multiple_fair_value_usd"] == pytest.approx(
        39.90431859423962
    )
    assert result == replay(dict(reversed(list(memo_inputs().items()))))


def test_leases_are_not_subtracted_twice_and_sbc_reduces_cash() -> None:
    inputs = memo_inputs()
    base = replay(inputs)
    assert replay({**inputs, "lease_liabilities": 900.0})["vps"] == base["vps"]
    assert replay({**inputs, "base_sbc": 0.03})["vps"] < base["vps"]
    assert replay({**inputs, "financial_debt": 100.0})["vps"] == pytest.approx(
        base["vps"] - 100 / 0.8283 / inputs["shares"]
    )


def test_terminal_uses_steady_state_working_capital() -> None:
    result = replay(memo_inputs())
    base = result["scenarios"]["base"]
    assert base["sustainable_2032_fcff_chf_m"] > base["rows"][-1]["economic_fcff_chf_m"] * 1.03
    matrix = result["gordon_sensitivity"]
    assert matrix["0.095"]["0.03"] > matrix["0.105"]["0.03"] > matrix["0.12"]["0.03"]
    assert matrix["0.105"]["0.02"] < matrix["0.105"]["0.03"] < matrix["0.105"]["0.04"]


@pytest.mark.parametrize(
    "quote,status", [(0.1, "target_below_bracket"), (500.0, "target_above_bracket")]
)
def test_reverse_quote_outside_brackets_keeps_fair_values(quote: float, status: str) -> None:
    inputs = memo_inputs()
    baseline = replay(inputs)
    result = replay({**inputs, "price_usd": quote})
    for key in (
        "vps",
        "equity_value",
        "operating_ev",
        "scenarios",
        "gordon_sensitivity",
        "one_factor_stresses",
    ):
        assert result[key] == baseline[key]
    reverse = result["reverse_dcf"]
    assert reverse["required_constant_2027_2031_growth"] is None
    assert reverse["required_2031_revenue_chf_m"] is None
    assert reverse["required_margin_shift"] is None
    assert reverse["required_2031_ebitda_margin"] is None
    for driver in ("growth", "margin_shift"):
        assert reverse[f"{driver}_status"] == status
        assert isinstance(reverse[f"{driver}_lower_bound"], float)
        assert isinstance(reverse[f"{driver}_upper_bound"], float)
        assert isinstance(reverse[f"{driver}_lower_value_usd"], float)
        assert isinstance(reverse[f"{driver}_upper_value_usd"], float)


@pytest.mark.parametrize(
    "driver,endpoint",
    [("growth", 0.0), ("growth", 0.3), ("margin_shift", -0.1), ("margin_shift", 0.05)],
)
def test_reverse_exact_endpoint_is_solved(driver: str, endpoint: float) -> None:
    inputs = memo_inputs()
    schedule = {
        f"base_growth_{index}": endpoint if driver == "growth" else inputs[f"base_growth_{index}"]
        for index in range(1, 6)
    }
    if driver == "margin_shift":
        schedule.update(
            {
                f"base_ebitda_margin_{index}": inputs[f"base_ebitda_margin_{index}"] + endpoint
                for index in range(1, 6)
            }
        )
    endpoint_quote = replay({**inputs, **schedule})["vps"]
    reverse = replay({**inputs, "price_usd": endpoint_quote})["reverse_dcf"]
    key = "required_constant_2027_2031_growth" if driver == "growth" else "required_margin_shift"
    assert reverse[key] == endpoint
    assert f"{driver}_status" not in reverse


def test_in_bracket_reverse_output_keeps_existing_shape() -> None:
    output = replay(memo_inputs())
    assert (
        canonical_digest(dict(output))
        == "c11d7af296ed4d5ddd947df1e280fb484517260498b78504f263d6c2ddc37209"  # pragma: allowlist secret -- fixed public replay digest
    )
    reverse = output["reverse_dcf"]
    assert set(reverse) == {
        "required_constant_2027_2031_growth",
        "required_2031_revenue_chf_m",
        "required_margin_shift",
        "required_2031_ebitda_margin",
    }
    assert all(isinstance(value, float) for value in reverse.values())


def test_one_unavailable_reverse_diagnostic_keeps_the_other_solution() -> None:
    inputs = {**memo_inputs(), "financial_debt": 52.0}
    bounds = replay({**inputs, "price_usd": 0.1})["reverse_dcf"]
    lower_growth, lower_margin = (
        bounds["growth_lower_value_usd"],
        bounds["margin_shift_lower_value_usd"],
    )
    assert isinstance(lower_growth, float) and isinstance(lower_margin, float)
    assert lower_growth > lower_margin > 0
    quote = (lower_growth + lower_margin) / 2
    reverse = replay({**inputs, "price_usd": quote})["reverse_dcf"]
    assert reverse["required_constant_2027_2031_growth"] is None
    assert reverse["growth_status"] == "target_below_bracket"
    assert isinstance(reverse["required_margin_shift"], float)
    assert isinstance(reverse["required_2031_ebitda_margin"], float)
    assert "margin_shift_status" not in reverse


@pytest.mark.parametrize(
    "key,value",
    [
        ("wacc", 0.03),
        ("shares", 0),
        ("chf_per_usd", 0),
        ("base_tax", 1),
        ("base_growth_1", -1),
        ("cash", -1),
        ("base_sbc", float("nan")),
        ("forecast_start_year", 2027.5),
    ],
)
def test_invalid_model_inputs_fail_closed(key: str, value: float) -> None:
    with pytest.raises(ValueError):
        replay({**memo_inputs(), key: value})


def test_incomplete_population_and_future_schedule_rejected() -> None:
    inputs = memo_inputs()
    del inputs["bull_rent"]
    with pytest.raises(ValueError, match="input_population"):
        replay(inputs)
    with pytest.raises(ValueError, match="valuation_clock"):
        replay({**memo_inputs(), "valuation_date_ordinal": float(date(2028, 1, 1).toordinal())})
