"""Pure five-year ONON economic FCFF replay, with cash rent and SBC expensed.

CHF-million operating inputs translate into USD-million values. Rent is already
in FCFF, so the equity bridge excludes leases. Shares include existing dilutive
awards; SBC replacement cost is expensed without also forecasting dilution.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import date
from typing import TypedDict

from dcf.input_evidence import canonical_digest
from dcf.valuation import TerminalMetrics, compute_valuation

SCENARIOS = ("bear", "base", "bull")
SCALAR_FIELDS = (
    "sbc",
    "rent",
    "nonlease_da",
    "capex",
    "incremental_nwc",
    "tax",
    "terminal_fcf_multiple",
    "terminal_g",
)
REPORTED_DRIVER_KEYS = frozenset({"shares", "cash", "financial_debt", "lease_liabilities"})
ASSUMPTION_KEYS = frozenset(
    {
        "price_usd",
        "chf_per_usd",
        "revenue_anchor_chf_m",
        "wacc",
        "valuation_date_ordinal",
        "forecast_start_year",
        "guidance_revenue_low_chf_m",
        "guidance_revenue_high_chf_m",
        "guidance_ebitda_margin_low",
        "guidance_ebitda_margin_high",
    }
    | {
        f"{name}_{field}_{year}"
        for name in SCENARIOS
        for field in ("growth", "ebitda_margin")
        for year in range(1, 6)
    }
    | {f"{name}_{field}" for name in SCENARIOS for field in SCALAR_FIELDS}
)
INPUT_KEYS = REPORTED_DRIVER_KEYS | ASSUMPTION_KEYS


class ScenarioResult(TypedDict):
    rows: list[dict[str, float]]
    rate: float
    terminal_g: float
    sustainable_2032_fcff_chf_m: float
    exit_multiple_fair_value_usd: float
    gordon_fair_value_usd: float
    pv_fcff_usd_m: float
    exit_multiple_pv_terminal_usd_m: float
    gordon_pv_terminal_usd_m: float
    gordon_terminal_weight: float
    exit_multiple_terminal_weight: float
    cash_bridge_usd_m: float
    entry_25pct_mos_usd: float
    native_annual_valuation: dict[str, object]
    vps: float
    equity_value: float
    operating_ev: float
    value_per_share_usd: float
    equity_value_usd_m: float
    operating_value_usd_m: float


class ReplayResult(TypedDict):
    vps: float
    equity_value: float
    operating_ev: float
    value_per_share_usd: float
    equity_value_usd_m: float
    operating_value_usd_m: float
    effective_inputs_sha256: str
    bridge: dict[str, float | list[float]]
    scenarios: dict[str, ScenarioResult]
    gordon_sensitivity: dict[str, dict[str, float]]
    one_factor_stresses: dict[str, float]
    reverse_dcf: dict[str, float | str | None]


def _finite_numeric(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_inputs(inputs: Mapping[str, float]) -> tuple[int, float]:
    if frozenset(inputs) != INPUT_KEYS:
        raise ValueError("onon_model_input_population_mismatch")
    if not all(_finite_numeric(v) for v in inputs.values()):
        raise ValueError("onon_model_input_not_finite_numeric")
    if any(
        inputs[key] <= 0
        for key in ("shares", "price_usd", "chf_per_usd", "revenue_anchor_chf_m", "wacc")
    ):
        raise ValueError("onon_positive_baseline_required")
    if any(inputs[key] < 0 for key in ("cash", "financial_debt", "lease_liabilities")):
        raise ValueError("onon_negative_reported_pool")
    if not 0 < inputs["guidance_revenue_low_chf_m"] <= inputs["guidance_revenue_high_chf_m"]:
        raise ValueError("onon_guidance_revenue_range_invalid")
    if not 0 < inputs["guidance_ebitda_margin_low"] <= inputs["guidance_ebitda_margin_high"] < 1:
        raise ValueError("onon_guidance_margin_range_invalid")
    year, ordinal = inputs["forecast_start_year"], inputs["valuation_date_ordinal"]
    if year != int(year) or ordinal != int(ordinal):
        raise ValueError("onon_integer_schedule_required")
    try:
        at = date.fromordinal(int(ordinal))
        year_end = date(int(year) - 1, 12, 31)
    except (ValueError, OverflowError) as exc:
        raise ValueError("onon_valuation_clock_invalid") from exc
    if at.year != year_end.year:
        raise ValueError("onon_valuation_clock_outside_anchor_year")
    for name in SCENARIOS:
        if not 0 <= inputs[f"{name}_terminal_g"] < inputs["wacc"]:
            raise ValueError("onon_terminal_growth_not_below_discount_rate")
        if inputs[f"{name}_terminal_fcf_multiple"] <= 0:
            raise ValueError("onon_terminal_multiple_not_positive")
        for key in SCALAR_FIELDS[:-2]:
            if not 0 <= inputs[f"{name}_{key}"] < 1:
                raise ValueError(f"onon_invalid_ratio:{name}_{key}")
        for index in range(1, 6):
            if not -1 < inputs[f"{name}_growth_{index}"] <= 1:
                raise ValueError("onon_invalid_forecast_growth")
            if not 0 <= inputs[f"{name}_ebitda_margin_{index}"] < 1:
                raise ValueError("onon_invalid_forecast_margin")
    return int(year), (year_end - at).days / 365.25


def _scenario(inputs: Mapping[str, float], name: str) -> dict[str, float]:
    return {
        key.removeprefix(f"{name}_"): value
        for key, value in inputs.items()
        if key.startswith(f"{name}_")
    }


def _evaluate(
    inputs: Mapping[str, float],
    scenario: Mapping[str, float],
    year: int,
    stub: float,
    *,
    rate: float | None = None,
    terminal_g: float | None = None,
    cash_haircut: float = 0,
    fx_rate: float | None = None,
) -> ScenarioResult:
    rate = inputs["wacc"] if rate is None else rate
    fx_rate = inputs["chf_per_usd"] if fx_rate is None else fx_rate
    g = scenario["terminal_g"] if terminal_g is None else terminal_g
    if not rate > g >= 0 or fx_rate <= 0:
        raise ValueError("onon_terminal_or_translation_invalid")
    revenue = inputs["revenue_anchor_chf_m"]
    rows: list[dict[str, float]] = []
    for index in range(1, 6):
        previous = revenue
        growth, margin = scenario[f"growth_{index}"], scenario[f"ebitda_margin_{index}"]
        revenue *= 1 + growth
        ebit = revenue * (margin - scenario["sbc"] - scenario["rent"] - scenario["nonlease_da"])
        nopat = ebit * (1 - scenario["tax"])
        da, capex = revenue * scenario["nonlease_da"], revenue * scenario["capex"]
        dnwc = (revenue - previous) * scenario["incremental_nwc"]
        rows.append(
            {
                "year": float(year + index - 1),
                "revenue_chf_m": revenue,
                "growth": growth,
                "adjusted_ebitda_margin": margin,
                "post_rent_sbc_ebit_chf_m": ebit,
                "nopat_chf_m": nopat,
                "nonlease_da_chf_m": da,
                "capex_chf_m": capex,
                "incremental_nwc_chf_m": dnwc,
                "economic_fcff_chf_m": nopat + da - capex - dnwc,
            }
        )
    last = rows[-1]
    cash = (inputs["cash"] - cash_haircut) / fx_rate
    debt = inputs["financial_debt"] / fx_rate
    native = compute_valuation(
        [row["economic_fcff_chf_m"] / fx_rate for row in rows],
        [int(row["year"]) for row in rows],
        rate,
        basis="EV/FCF",
        terminal_multiple=scenario["terminal_fcf_multiple"],
        terminal=TerminalMetrics(
            revenue=revenue / fx_rate,
            ebit=last["post_rent_sbc_ebit_chf_m"] / fx_rate,
            ebitda=revenue * scenario["ebitda_margin_5"] / fx_rate,
            fcf=last["economic_fcff_chf_m"] / fx_rate,
            net_income=last["nopat_chf_m"] / fx_rate,
        ),
        cash_and_nonop=cash,
        total_debt=debt,
        diluted_shares_M=inputs["shares"],
    )
    sustainable = (
        revenue
        * (1 + g)
        * (
            (
                scenario["ebitda_margin_5"]
                - scenario["sbc"]
                - scenario["rent"]
                - scenario["nonlease_da"]
            )
            * (1 - scenario["tax"])
            + scenario["nonlease_da"]
            - scenario["capex"]
        )
        - revenue * g * scenario["incremental_nwc"]
    )
    stub_factor = (1 + rate) ** stub
    pv_fcff = native.pv_fcff / stub_factor
    multiple_pv = native.pv_terminal / stub_factor
    gordon_pv = sustainable / fx_rate / (rate - g) / (1 + rate) ** 5 / stub_factor
    operating_ev = pv_fcff + gordon_pv
    equity = operating_ev + cash - debt
    vps = equity / inputs["shares"]
    return ScenarioResult(
        rows=rows,
        rate=rate,
        terminal_g=g,
        sustainable_2032_fcff_chf_m=sustainable,
        exit_multiple_fair_value_usd=(pv_fcff + multiple_pv + cash - debt) / inputs["shares"],
        gordon_fair_value_usd=vps,
        pv_fcff_usd_m=pv_fcff,
        exit_multiple_pv_terminal_usd_m=multiple_pv,
        gordon_pv_terminal_usd_m=gordon_pv,
        gordon_terminal_weight=gordon_pv / operating_ev if operating_ev else 0,
        exit_multiple_terminal_weight=native.pv_terminal_pct,
        cash_bridge_usd_m=cash,
        entry_25pct_mos_usd=0.75 * vps,
        native_annual_valuation=asdict(native),
        vps=vps,
        equity_value=equity,
        operating_ev=operating_ev,
        value_per_share_usd=vps,
        equity_value_usd_m=equity,
        operating_value_usd_m=operating_ev,
    )


def _solve(
    fn: Callable[[float], float], lo: float, hi: float, target: float
) -> tuple[float | None, dict[str, float | str]]:
    lower, upper = fn(lo), fn(hi)
    if target == lower:
        return lo, {}
    if target == upper:
        return hi, {}
    if not lower < target < upper:
        return None, {
            "status": "non_increasing_bracket"
            if lower >= upper
            else "target_below_bracket"
            if target < lower
            else "target_above_bracket",
            "lower_bound": lo,
            "upper_bound": hi,
            "lower_value_usd": lower,
            "upper_value_usd": upper,
        }
    for _ in range(100):
        mid = (lo + hi) / 2
        if fn(mid) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2, {}


def replay(inputs: Mapping[str, float]) -> ReplayResult:
    """Return full scenarios, sensitivities and reverse DCF without I/O or clocks."""
    year, stub = validate_inputs(inputs)
    scenarios = {name: _evaluate(inputs, _scenario(inputs, name), year, stub) for name in SCENARIOS}
    base = _scenario(inputs, "base")
    matrix = {
        str(rate): {
            str(g): _evaluate(inputs, base, year, stub, rate=rate, terminal_g=g)["vps"]
            for g in (0.02, 0.03, 0.04)
        }
        for rate in (0.095, 0.105, 0.12)
    }
    stresses: dict[str, float] = {}
    for name, keys, delta in (
        ("ebitda_margin_minus_200bp", tuple(f"ebitda_margin_{i}" for i in range(1, 6)), -0.02),
        ("rent_plus_100bp", ("rent",), 0.01),
        ("tax_plus_200bp", ("tax",), 0.02),
        ("incremental_nwc_plus_3pp", ("incremental_nwc",), 0.03),
    ):
        stress = {**base, **{key: base[key] + delta for key in keys}}
        stresses[name] = _evaluate(inputs, stress, year, stub)["vps"]
    stresses["cash_haircut_chf_500m"] = _evaluate(inputs, base, year, stub, cash_haircut=500)["vps"]
    for name, factor in (("plus", 1.1), ("minus", 0.9)):
        stresses[f"translation_chf_per_usd_{name}_10pct"] = _evaluate(
            inputs, base, year, stub, fx_rate=inputs["chf_per_usd"] * factor
        )["vps"]
    combined = {
        **base,
        "rent": base["rent"] + 0.01,
        "incremental_nwc": base["incremental_nwc"] + 0.03,
        **{f"ebitda_margin_{i}": base[f"ebitda_margin_{i}"] - 0.02 for i in range(1, 6)},
    }
    stresses["combined_margin_rent_nwc"] = _evaluate(inputs, combined, year, stub)["vps"]

    def growth_value(growth: float) -> float:
        schedule = {**base, **{f"growth_{i}": growth for i in range(1, 6)}}
        return _evaluate(inputs, schedule, year, stub)["vps"]

    def margin_value(delta: float) -> float:
        schedule = {
            **base,
            **{f"ebitda_margin_{i}": base[f"ebitda_margin_{i}"] + delta for i in range(1, 6)},
        }
        return _evaluate(inputs, schedule, year, stub)["vps"]

    growth, growth_diagnostic = _solve(growth_value, 0.0, 0.30, inputs["price_usd"])
    delta, margin_diagnostic = _solve(margin_value, -0.10, 0.05, inputs["price_usd"])
    quoted_equity = inputs["shares"] * inputs["price_usd"]
    quoted_ev_chf = (
        quoted_equity * inputs["chf_per_usd"]
        - inputs["cash"]
        + inputs["financial_debt"]
        + inputs["lease_liabilities"]
    )
    core = scenarios["base"]
    return ReplayResult(
        vps=core["vps"],
        equity_value=core["equity_value"],
        operating_ev=core["operating_ev"],
        value_per_share_usd=core["vps"],
        equity_value_usd_m=core["equity_value"],
        operating_value_usd_m=core["operating_ev"],
        effective_inputs_sha256=canonical_digest(
            {key: float(value) for key, value in inputs.items()}
        ),
        bridge={
            "diluted_economic_shares_m": inputs["shares"],
            "stub_years": stub,
            "equity_value_at_quote_usd_m": quoted_equity,
            "ev_with_leases_chf_m": quoted_ev_chf,
            "ev_to_fy26_adj_ebitda_range": [
                quoted_ev_chf
                / (inputs["guidance_revenue_high_chf_m"] * inputs["guidance_ebitda_margin_high"]),
                quoted_ev_chf
                / (inputs["guidance_revenue_low_chf_m"] * inputs["guidance_ebitda_margin_low"]),
            ],
        },
        scenarios=scenarios,
        gordon_sensitivity=matrix,
        one_factor_stresses=stresses,
        reverse_dcf={
            "required_constant_2027_2031_growth": growth,
            "required_2031_revenue_chf_m": None
            if growth is None
            else inputs["revenue_anchor_chf_m"] * (1 + growth) ** 5,
            "required_margin_shift": delta,
            "required_2031_ebitda_margin": None
            if delta is None
            else base["ebitda_margin_5"] + delta,
            **{f"growth_{key}": value for key, value in growth_diagnostic.items()},
            **{f"margin_shift_{key}": value for key, value in margin_diagnostic.items()},
        },
    )
