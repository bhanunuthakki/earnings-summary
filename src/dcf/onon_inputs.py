"""Fixed ONON cash-rent/SBC recipe on sealed, semantically admitted actuals.

Forecasts, FX and the guidance midpoint remain attributed assumptions. TTM,
nonlease depreciation and A-equivalent shares are model calculations, not facts.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal

from dcf.equity_bridge import EquityBridgeReceipt
from dcf.input_evidence import (
    InputEvidenceError,
    InputRequirement,
    ModelCalculation,
    ModelInputReceipt,
    ModelInputRequest,
    canonical_digest,
    verify_model_inputs,
)
from dcf.onon_model import ASSUMPTION_KEYS, REPORTED_DRIVER_KEYS, replay, validate_inputs

RECIPE = "onon-cash-rent-sbc-inputs/v1"
MILLIONS = Decimal("0.000001")
FLOW_KEYS = (
    "revenue",
    "adjusted_ebitda",
    "operating_income",
    "depreciation_amortization",
    "rou_depreciation",
    "sbc",
    "ppe_purchases",
    "intangible_purchases",
    "lease_principal",
    "lease_interest_expense",
    "operating_cash_flow",
)
OUTFLOW_KEYS = frozenset({"ppe_purchases", "intangible_purchases", "lease_principal"})
NEGATIVE_KEYS = OUTFLOW_KEYS | {"lease_interest_expense", "rou_depreciation", "sbc"}
POINT_KEYS = (
    "reported_cash",
    "restricted_cash",
    "receivables",
    "inventory",
    "payables",
    "current_lease_liabilities",
    "noncurrent_lease_liabilities",
    "reported_other_nonlease_financial_liabilities",
    "class_a_outstanding",
    "class_b_outstanding",
    "class_a_dilutive_awards",
    "class_b_dilutive_awards",
)


def _requirement(key: str, *, instant: bool = False) -> InputRequirement:
    shares = key.startswith("class_")
    constraints: dict[str, object] = {
        "fiscal_year_end": "12-31",
        "reported_population": "actual",
    }
    basis = "management" if key == "adjusted_ebitda" else "ifrs"
    if key in OUTFLOW_KEYS:
        constraints["reported_sign"] = "negative_cash_outflow"
    if key in {"lease_interest_expense", "rou_depreciation", "sbc"}:
        constraints["reported_sign"] = "negative_expense"
    if key == "sbc":
        constraints["basis"] = "recognized_share_based_compensation_expense"
    if key == "rou_depreciation":
        constraints["basis"] = "lease_asset_rollforward_depreciation_decrease"
    if key == "depreciation_amortization":
        constraints["basis"] = "statement_of_cash_flows_adjustment"
    if key == "lease_interest_expense":
        constraints["basis"] = "lease_interest_expense_not_cash_interest_assertion"
    if key == "reported_other_nonlease_financial_liabilities":
        constraints["financial_liability_scope"] = (
            "other_nonlease_financial_liabilities_excluding_trade_payables_and_leases"
        )
    if shares:
        constraints.update(
            {
                "share_class": "A" if key.startswith("class_a_") else "B",
                "share_measurement": "period_end_dilutive_awards"
                if key.endswith("awards")
                else "period_end_outstanding",
                "class_b_economic_conversion_to_a": "0.1",
            }
        )
    return InputRequirement(
        key=key,
        role=f"onon.{key}",
        unit_key="shares" if shares else "CHF",
        currency=None if shares else "CHF",
        period_kind="instant" if instant else "duration",
        annual=not instant,
        accounting_basis=basis,
        scale=MILLIONS,
        definition_constraints=constraints,
    )


POINT_REQUIREMENTS = tuple(_requirement(key, instant=True) for key in POINT_KEYS)


def requirements_for(period_end: date) -> tuple[InputRequirement, ...]:
    if (period_end.month, period_end.day) not in {(3, 31), (6, 30), (9, 30), (12, 31)}:
        raise InputEvidenceError("unsupported_onon_fiscal_period")
    periods = (
        [("fy", date(period_end.year, 1, 1), period_end)]
        if period_end.month == 12
        else [
            ("fy", date(period_end.year - 1, 1, 1), date(period_end.year - 1, 12, 31)),
            ("ytd", date(period_end.year, 1, 1), period_end),
            (
                "prior_ytd",
                date(period_end.year - 1, 1, 1),
                period_end.replace(year=period_end.year - 1),
            ),
        ]
    )
    return (
        *(
            _requirement(key).model_copy(
                update={
                    "key": f"{key}_{label}",
                    "period_start": start,
                    "period_end": end,
                    "annual": label == "fy",
                }
            )
            for key in FLOW_KEYS
            for label, start, end in periods
        ),
        *POINT_REQUIREMENTS,
    )


def effective_numeric_inputs(values: Mapping[str, object]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key in ASSUMPTION_KEYS | REPORTED_DRIVER_KEYS:
        value = values.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (float, int))
            or not math.isfinite(value)
        ):
            raise InputEvidenceError(f"effective_input_missing_or_invalid:{key}")
        out[key] = float(value)
    return out


def calculate_actuals(
    receipt: ModelInputReceipt,
) -> tuple[dict[str, float], tuple[ModelCalculation, ...]]:
    required = {req.key for req in requirements_for(receipt.request.financial_period_end)}
    values = {item.key: item.value for item in receipt.inputs}
    if set(values) != required or len(values) != len(receipt.inputs):
        raise InputEvidenceError("onon_reported_input_population_mismatch")
    for req in requirements_for(receipt.request.financial_period_end):
        value = values[req.key]
        role = req.role.removeprefix("onon.")
        if not math.isfinite(value):
            raise InputEvidenceError("onon_actual_not_finite")
        if role in NEGATIVE_KEYS and value > 0:
            raise InputEvidenceError(f"onon_reported_cash_outflow_sign_invalid:{req.key}")
        if role not in NEGATIVE_KEYS | {"operating_cash_flow", "operating_income"} and value < 0:
            raise InputEvidenceError(f"onon_reported_nonnegative_value_required:{req.key}")
    actuals = {key: values[key] for key in POINT_KEYS}
    calculations: list[ModelCalculation] = []
    for key in FLOW_KEYS:
        operands = (
            (f"{key}_fy",)
            if receipt.request.financial_period_end.month == 12
            else (f"{key}_fy", f"{key}_ytd", f"{key}_prior_ytd")
        )
        actuals[key] = (
            values[operands[0]]
            if len(operands) == 1
            else (values[operands[0]] + values[operands[1]] - values[operands[2]])
        )
        calculations.append(
            ModelCalculation(
                key=key,
                formula="reported_fy" if len(operands) == 1 else "fy_plus_ytd_minus_prior_ytd",
                operands=operands,
                value=actuals[key],
            )
        )
    derived = (
        (
            "sbc_expense",
            "negative_reported_sbc_expense",
            ("sbc",),
            -actuals["sbc"],
        ),
        (
            "shares",
            "a_plus_b_div10_plus_a_awards_plus_b_awards_div10",
            (
                "class_a_outstanding",
                "class_b_outstanding",
                "class_a_dilutive_awards",
                "class_b_dilutive_awards",
            ),
            actuals["class_a_outstanding"]
            + actuals["class_b_outstanding"] / 10
            + actuals["class_a_dilutive_awards"]
            + actuals["class_b_dilutive_awards"] / 10,
        ),
        (
            "cash",
            "reported_cash_minus_restricted_cash",
            ("reported_cash", "restricted_cash"),
            actuals["reported_cash"] - actuals["restricted_cash"],
        ),
        (
            "financial_debt",
            "reported_other_nonlease_financial_liabilities_conservative_bridge_deduction",
            ("reported_other_nonlease_financial_liabilities",),
            actuals["reported_other_nonlease_financial_liabilities"],
        ),
        (
            "lease_liabilities",
            "current_plus_noncurrent_leases",
            ("current_lease_liabilities", "noncurrent_lease_liabilities"),
            actuals["current_lease_liabilities"] + actuals["noncurrent_lease_liabilities"],
        ),
        (
            "nonlease_da",
            "cashflow_da_plus_negative_rou_depreciation",
            ("depreciation_amortization", "rou_depreciation"),
            actuals["depreciation_amortization"] + actuals["rou_depreciation"],
        ),
        (
            "capex",
            "negative_ppe_purchases_minus_intangible_purchases",
            ("ppe_purchases", "intangible_purchases"),
            -actuals["ppe_purchases"] - actuals["intangible_purchases"],
        ),
        (
            "cash_rent_proxy",
            "negative_lease_principal_minus_lease_interest_expense",
            ("lease_principal", "lease_interest_expense"),
            -actuals["lease_principal"] - actuals["lease_interest_expense"],
        ),
        (
            "nwc",
            "receivables_plus_inventory_minus_payables",
            ("receivables", "inventory", "payables"),
            actuals["receivables"] + actuals["inventory"] - actuals["payables"],
        ),
    )
    for key, formula, operands, value in derived:
        actuals[key] = value
        calculations.append(
            ModelCalculation(key=key, formula=formula, operands=operands, value=value)
        )
    if any(actuals[key] < 0 for key in ("cash", "capex", "cash_rent_proxy", "nonlease_da")):
        raise InputEvidenceError("onon_economic_pool_reconciliation_failed")
    if any(
        actuals[key] <= 0
        for key in ("revenue", "shares", "class_a_outstanding", "class_b_outstanding")
    ):
        raise InputEvidenceError("onon_positive_baseline_required")
    return actuals, tuple(calculations)


def model_output(inputs: Mapping[str, float]) -> dict[str, object]:
    return dict(replay(inputs))


def review_drivers(
    actuals: Mapping[str, float], inputs: Mapping[str, float]
) -> dict[str, tuple[float, float]]:
    revenue = actuals["revenue"]
    return {
        "revenue_anchor_vs_ttm": (revenue, inputs["revenue_anchor_chf_m"]),
        "adjusted_ebitda_margin": (
            actuals["adjusted_ebitda"] / revenue,
            inputs["base_ebitda_margin_1"],
        ),
        "sbc_ratio": (actuals["sbc_expense"] / revenue, inputs["base_sbc"]),
        "cash_rent_expense_proxy_ratio": (
            actuals["cash_rent_proxy"] / revenue,
            inputs["base_rent"],
        ),
        "nonlease_da_ratio": (actuals["nonlease_da"] / revenue, inputs["base_nonlease_da"]),
        "capex_ratio": (actuals["capex"] / revenue, inputs["base_capex"]),
        "level_nwc_vs_incremental_forecast_ratio": (
            actuals["nwc"] / revenue,
            inputs["base_incremental_nwc"],
        ),
    }


def _verify_review(
    receipt: ModelInputReceipt,
    actuals: Mapping[str, float],
    inputs: Mapping[str, float],
    as_of: datetime,
) -> None:
    review = receipt.request.assumption_review
    if review is None:
        raise InputEvidenceError("assumption_review_unverified")
    if (
        review.reviewed_at > as_of
        or review.reviewed_at < max(item.recorded_at for item in receipt.inputs)
        or review.effective_inputs_sha256 != canonical_digest(dict(inputs))
        or review.actuals_sha256 != canonical_digest(dict(actuals))
    ):
        raise InputEvidenceError("assumption_review_clock_or_basis_mismatch")
    drivers = review_drivers(actuals, inputs)
    if set(review.drivers) != set(drivers):
        raise InputEvidenceError("assumption_review_population_mismatch")
    for key, (observed, forecast) in drivers.items():
        item = review.drivers[key]
        if not all(
            math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
            for a, b in (
                (item.observed, observed),
                (item.forecast, forecast),
                (item.variance, forecast - observed),
            )
        ):
            raise InputEvidenceError(f"assumption_review_variance_mismatch:{key}")


def prepare_onon_inputs(
    conn: sqlite3.Connection,
    request: ModelInputRequest,
    *,
    effective_inputs: Mapping[str, float],
    as_of: datetime,
) -> tuple[dict[str, float], ModelInputReceipt]:
    if request.ticker != "ONON":
        raise InputEvidenceError("onon_ticker_required")
    proposed = {key: value for key, value in effective_inputs.items() if key in ASSUMPTION_KEYS}
    proof = verify_model_inputs(
        conn,
        request,
        recipe=RECIPE,
        requirements=requirements_for(request.financial_period_end),
        effective_inputs=proposed,
        assumption_keys=ASSUMPTION_KEYS,
        as_of=as_of,
    )
    actuals, calculations = calculate_actuals(proof)
    complete = {**proposed, **{key: actuals[key] for key in REPORTED_DRIVER_KEYS}}
    numeric = effective_numeric_inputs(complete)
    for key in REPORTED_DRIVER_KEYS:
        if key in effective_inputs and effective_inputs[key] != numeric[key]:
            raise InputEvidenceError(f"reported_input_value_mismatch:{key}")
    try:
        validate_inputs(numeric)
    except ValueError as exc:
        raise InputEvidenceError(str(exc)) from exc
    valuation_date = date.fromordinal(int(numeric["valuation_date_ordinal"]))
    if valuation_date > as_of.date() or request.financial_period_end > valuation_date:
        raise InputEvidenceError("onon_valuation_clock_or_forecast_anchor_mismatch")
    for key, assumption in request.assumptions.items():
        if (
            assumption.source_reference is None
            or assumption.source_as_of is None
            or assumption.recorded_at is None
        ):
            raise InputEvidenceError(f"onon_assumption_source_clock_required:{key}")
        if (
            assumption.source_as_of > valuation_date
            or assumption.recorded_at > as_of
            or assumption.recorded_at > proof.verified_at
        ):
            raise InputEvidenceError(f"onon_assumption_source_clock_invalid:{key}")
    _verify_review(proof, actuals, numeric, as_of)
    return numeric, proof.model_copy(
        update={
            "effective_inputs_sha256": canonical_digest(numeric),
            "calculations": calculations,
            "actuals_sha256": canonical_digest(actuals),
            "model_output_sha256": canonical_digest(model_output(numeric)),
        }
    )


def verify_onon_inputs(
    conn: sqlite3.Connection,
    receipt: ModelInputReceipt,
    *,
    effective_inputs: Mapping[str, float],
    as_of: datetime,
) -> ModelInputReceipt:
    if receipt.recipe != RECIPE or receipt.verified_at > as_of:
        raise InputEvidenceError("model_input_receipt_recipe_or_clock_invalid")
    complete, verified = prepare_onon_inputs(
        conn, receipt.request, effective_inputs=effective_inputs, as_of=as_of
    )
    if (
        dict(effective_inputs) != complete
        or receipt.effective_inputs_sha256 != verified.effective_inputs_sha256
    ):
        raise InputEvidenceError("effective_input_digest_mismatch")
    if (
        receipt.required_keys != verified.required_keys
        or receipt.inputs != verified.inputs
        or receipt.snapshot_member_sha256 != verified.snapshot_member_sha256
        or receipt.inventory_snapshot_ids != verified.inventory_snapshot_ids
        or receipt.calculations != verified.calculations
        or receipt.actuals_sha256 != verified.actuals_sha256
        or receipt.model_output_sha256 != verified.model_output_sha256
    ):
        raise InputEvidenceError("model_input_receipt_mismatch")
    return verified


def build_onon_equity_bridge(
    conn: sqlite3.Connection,
    receipt: ModelInputReceipt,
    *,
    effective_inputs: Mapping[str, float],
    as_of: datetime,
) -> EquityBridgeReceipt:
    """Reconstruct the canonical bridge after receipt verification in this transaction.

    Source lineages retain canonical identities. The bridge deducts the reported
    other nonlease financial liability aggregate, a conservative broader set
    than borrowing debt. Trade payables enter NWC; rent enters scenario FCFF.
    """
    verified = verify_onon_inputs(conn, receipt, effective_inputs=effective_inputs, as_of=as_of)
    requirements = {req.key: req for req in requirements_for(verified.request.financial_period_end)}
    for item in verified.inputs:
        req = requirements[item.key]
        if (
            item.period_start != req.period_start
            or item.period_end != (req.period_end or verified.request.financial_period_end)
            or item.unit_key != req.unit_key
            or item.currency != req.currency
            or item.observation_kind != "reported"
        ):
            raise InputEvidenceError(f"onon_bridge_reported_coordinate_mismatch:{item.key}")
    actuals, calculations = calculate_actuals(verified)
    if verified.calculations != calculations:
        raise InputEvidenceError("onon_bridge_calculation_population_mismatch")
    output = replay(effective_inputs)
    numeric = effective_numeric_inputs(effective_inputs)
    for key in REPORTED_DRIVER_KEYS:
        if numeric[key] != actuals[key]:
            raise InputEvidenceError(f"onon_bridge_operand_mismatch:{key}")
    index = {item.key: item for item in verified.inputs}
    derived = {item.key: item for item in calculations}

    def lineage(key: str) -> dict[str, object]:
        calculation = derived[key]
        return {
            "key": key,
            "value_m": calculation.value,
            "calculation": calculation.model_dump(mode="json"),
            "sources": [index[operand].model_dump(mode="json") for operand in calculation.operands],
        }

    cash, debt, shares = (lineage(key) for key in ("cash", "financial_debt", "shares"))
    op = float(output["operating_ev"])
    vps = float(output["vps"])
    recomputed = (
        Decimal(str(op))
        + (Decimal(str(numeric["cash"])) - Decimal(str(numeric["financial_debt"])))
        / Decimal(str(numeric["chf_per_usd"]))
    ) / Decimal(str(numeric["shares"]))
    delta = recomputed - Decimal(str(vps))
    if abs(delta) > Decimal("0.00000001"):
        raise InputEvidenceError("onon_bridge_arithmetic_mismatch")
    context: dict[str, object] = {
        "schema_version": "dcf_canonical_bridge_context.v1",
        "ticker": "ONON",
        "period_end": verified.request.financial_period_end.isoformat(),
        "fiscal_period_type": "FY" if verified.request.financial_period_end.month == 12 else "YTD",
        "reporting_currency": "CHF",
        "cash": cash,
        "shares": shares,
        "financial_debt": debt,
        "bridge_deduction_policy": "Conservatively deduct all reported other current and noncurrent financial liabilities; this is broader than borrowing debt. Exclude trade payables already in NWC and leases already expensed in FCFF.",
        "lease_liabilities": lineage("lease_liabilities"),
        "lease_policy": "cash_rent_and_sbc_expensed_leases_excluded",
        "model_output_sha256": canonical_digest(output),
        "model_input_receipt_sha256": canonical_digest(receipt.model_dump(mode="json")),
    }
    return EquityBridgeReceipt(
        ticker="ONON",
        status="verified",
        arithmetic_status="verified",
        operating_value_usd_m=op,
        cash_m=numeric["cash"],
        total_debt_m=numeric["financial_debt"],
        diluted_shares_m=numeric["shares"],
        fx_to_usd=1 / numeric["chf_per_usd"],
        stored_value_per_share_usd=vps,
        recomputed_value_per_share_usd=float(recomputed),
        arithmetic_delta=float(delta),
        reporting_currency="CHF",
        bridge_period_end=verified.request.financial_period_end.isoformat(),
        bridge_fiscal_period_type=str(context["fiscal_period_type"]),
        bridge_context=context,
        cash_lineage=cash,
        total_debt_lineage=debt,
        debt_scope="other_nonlease_financial_liabilities",
        debt_component_lineage=(debt,),
        reasons=(),
    )
