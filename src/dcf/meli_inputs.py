"""Fixed MELI Commerce/payments FCFF + credit FCFE input recipe.

The recipe is not a new financial taxonomy. Its semantic roles must be assigned
in governed metric-definition revisions before facts can feed this model. Raw
Service/Product labels, local cache values and analyst estimates are insufficient.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, ValidationError

from dcf.input_evidence import (
    FrozenModel,
    InputEvidenceError,
    InputRequirement,
    ModelCalculation,
    ModelInputReceipt,
    ModelInputRequest,
    SourceReadContext,
    canonical_digest,
    verify_model_inputs,
)
from dcf.meli_model import implied_credit_terminal_roe, validate_credit_terminal

RECIPE = "meli-platform-sotp-inputs/v4"
FINANCING_RECIPE = "meli-platform-sotp-inputs/v5"
MILLIONS = Decimal("0.000001")
REPORTED_DRIVER_KEYS = frozenset({"comm_rev0", "fpay_rev0", "cb0", "shares", "net_cash"})
ASSUMPTION_KEYS = frozenset(
    {
        "credit_cash_allocation",
        "operating_cash_reserve",
        "credit_funding_debt_allocation",
        "comm_g_near",
        "comm_g_term",
        "comm_margin_near",
        "comm_margin_term",
        "fpay_g_near",
        "fpay_g_term",
        "fpay_margin_near",
        "fpay_margin_term",
        "da_pct",
        "capex_pct_near",
        "capex_pct_term",
        "nwc_pct",
        "tax",
        "op_exit_ebitda_mult",
        "wacc",
        "cbg_near",
        "cbg_term",
        "nimal_near",
        "nimal_term",
        "credit_opex_ratio",
        "cap_ratio",
        "credit_ke",
        "credit_g_term",
        "credit_terminal_roe",
        "g_term",
        "years",
        "beta_op",
        "beta_credit",
        "country_risk_premium",
        "derive_capm",
    }
)
FINANCING_ASSUMPTION_KEYS = ASSUMPTION_KEYS | {"financing_other_expenses_reserve"}


class MeliFinancingContext(FrozenModel):
    """One source-bound financing event after the reported balance-sheet date."""

    schema_version: Literal["meli_financing_bridge.v1"] = "meli_financing_bridge.v1"
    event_key: str = Field(min_length=1, max_length=256)
    contract_period_end: date
    closing_period_end: date
    diagnostics: Literal["meli-report-driver-checks/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


def is_meli_recipe(recipe: str) -> bool:
    return recipe in {RECIPE, FINANCING_RECIPE}


def assumption_keys_for_recipe(recipe: str) -> frozenset[str]:
    if recipe == RECIPE:
        return ASSUMPTION_KEYS
    if recipe == FINANCING_RECIPE:
        return FINANCING_ASSUMPTION_KEYS
    raise InputEvidenceError("input_recipe_unsupported")


def _financing_context(request: ModelInputRequest, *, as_of: datetime) -> MeliFinancingContext:
    if request.recipe != FINANCING_RECIPE or request.recipe_context is None:
        raise InputEvidenceError("meli_financing_context_required")
    try:
        context = MeliFinancingContext.model_validate(request.recipe_context)
    except ValidationError as exc:
        raise InputEvidenceError("meli_financing_context_invalid") from exc
    if (
        as_of.tzinfo is None
        or not request.financial_period_end
        < context.contract_period_end
        <= context.closing_period_end
        <= as_of.date()
    ):
        raise InputEvidenceError("meli_financing_event_period_invalid")
    return context


def requirements_for_request(
    request: ModelInputRequest, *, as_of: datetime
) -> tuple[InputRequirement, ...]:
    """Retain the original reported anchor; bind event facts to their own dates."""
    if not is_meli_recipe(request.recipe):
        raise InputEvidenceError("input_recipe_mismatch")
    base = requirements_for(request.financial_period_end)
    if request.recipe == RECIPE:
        if request.recipe_context is not None:
            raise InputEvidenceError("meli_recipe_issuer_or_context_mismatch")
        return base
    context = _financing_context(request, as_of=as_of)
    return (
        *base,
        _money(
            "financing_issued_principal",
            instant=True,
            accounting_basis="other",
            constraints={
                "event_key": context.event_key,
                "financing_measure": "issued_principal",
                "completion": "closed",
            },
        ).model_copy(
            update={"period_end": context.closing_period_end, "consolidation_scope": "other"}
        ),
        InputRequirement(
            key="financing_underwriting_purchase_fraction",
            role="meli.financing_underwriting_purchase_fraction",
            unit_key="pure",
            currency=None,
            period_kind="instant",
            period_end=context.contract_period_end,
            accounting_basis="other",
            consolidation_scope="other",
            definition_constraints={
                "event_key": context.event_key,
                "purchase_basis": "underwriter_purchase_price",
                "excludes_other_expenses": True,
            },
        ),
    )


def _money(
    key: str,
    *,
    instant: bool = False,
    constraints: dict[str, object] | None = None,
    accounting_basis: str | None = None,
) -> InputRequirement:
    return InputRequirement(
        key=key,
        role=f"meli.{key}",
        unit_key="USD",
        currency="USD",
        period_kind="instant" if instant else "duration",
        annual=not instant,
        accounting_basis=accounting_basis,
        scale=MILLIONS,
        definition_constraints=constraints or {},
    )


FLOW_KEYS = (
    "comm_rev0",
    "revenue_total",
    "revenue_fintech",
    "revenue_credit",
    "operating_income",
    "depreciation",
    "capex",
)
POINT_REQUIREMENTS = (
    _money(
        "cb0",
        instant=True,
        constraints={
            "loan_measurement": "gross_before_credit_loss_allowance",
            "excludes_customer_float": True,
        },
    ),
    InputRequirement(
        key="shares",
        role="meli.diluted_shares",
        unit_key="shares",
        currency=None,
        period_kind="duration",
        scale=MILLIONS,
    ),
    _money(
        "reported_available_cash_and_investments",
        instant=True,
        accounting_basis="management",
        constraints={
            "basis": "issuer_liquidity_reconciliation",
            "pool_scope": "cash_and_cash_equivalents_short_term_and_long_term_investments_net_debt_reconciliation",
            "cash_excludes": "management_restricted_cash",
            "short_term_excludes": "restricted_or_guaranteed_time_deposits_foreign_debt_and_government_securities",
            "long_term_excludes": "restricted_or_guaranteed_foreign_government_debt_securitization_vie_investments_and_equity_securities_at_cost",
            "analyst_allocations": "not_deducted",
        },
    ),
    _money(
        "reported_total_financial_debt_and_leases",
        instant=True,
        accounting_basis="management",
        constraints={
            "basis": "issuer_financial_debt_reconciliation",
            "pool_scope": "total_loans_payable_other_financial_liabilities_and_operating_lease_liabilities",
            "analyst_credit_funding_allocation": "not_deducted",
        },
    ),
    _money(
        "reported_current_operating_lease_liabilities",
        instant=True,
        accounting_basis="us_gaap",
        constraints={"lease_type": "operating", "maturity": "current"},
    ),
    _money(
        "reported_noncurrent_operating_lease_liabilities",
        instant=True,
        accounting_basis="us_gaap",
        constraints={"lease_type": "operating", "maturity": "noncurrent"},
    ),
    InputRequirement(
        key="nimal_actual",
        role="meli.nimal_after_funding_and_losses",
        unit_key="ratio",
        currency=None,
        period_kind="duration",
        definition_constraints={
            "denominator": "average_gross_credit_portfolio",
            "annualized": True,
            "funding_costs": "deducted",
            "credit_losses": "deducted",
            "loan_sale_result": "excluded",
        },
    ),
)


def requirements_for(period_end: date) -> tuple[InputRequirement, ...]:
    """MELI's calendar is required in each governed definition, never inferred.

    Unsupported fiscal calendars fail admission through definition constraints.
    Interim TTM is model arithmetic on reported FY, current YTD and prior YTD.
    """
    if (period_end.month, period_end.day) not in {(3, 31), (6, 30), (9, 30), (12, 31)}:
        raise InputEvidenceError("unsupported_meli_fiscal_period")
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
    flows = tuple(
        _money(key).model_copy(
            update={
                "key": f"{key}_{label}",
                "period_start": start,
                "period_end": end,
                "annual": label == "fy",
                "definition_constraints": {
                    "fiscal_year_end": "12-31",
                    "reported_population": "actual",
                    **(
                        {
                            "revenue_scope": "fintech_total_including_credit_installment_and_investment_income"
                        }
                        if key == "revenue_fintech"
                        else {
                            "revenue_scope": "credit_portfolio_revenue_excluding_installment_and_investment_income"
                        }
                        if key == "revenue_credit"
                        else {}
                    ),
                },
            }
        )
        for key in FLOW_KEYS
        for label, start, end in periods
    )
    return (*flows, *POINT_REQUIREMENTS)


def effective_numeric_inputs(
    values: Mapping[str, object], *, recipe: str = RECIPE
) -> dict[str, float]:
    """Extract the fixed economic driver set; repricing does not change this hash."""
    out: dict[str, float] = {}
    for key in REPORTED_DRIVER_KEYS | assumption_keys_for_recipe(recipe):
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
    """Calculations are explicitly model-owned, never canonical reported facts."""
    values = {item.key: item.value for item in receipt.inputs}
    calculations: list[ModelCalculation] = []
    actuals = {
        item.key: item.value
        for item in receipt.inputs
        if item.key in {r.key for r in POINT_REQUIREMENTS}
    }
    for key in FLOW_KEYS:
        operands = (
            (f"{key}_fy",)
            if receipt.request.financial_period_end.month == 12
            else (f"{key}_fy", f"{key}_ytd", f"{key}_prior_ytd")
        )
        value = (
            values[operands[0]]
            if len(operands) == 1
            else (values[operands[0]] + values[operands[1]] - values[operands[2]])
        )
        actuals[key] = value
        calculations.append(
            ModelCalculation(
                key=key,
                formula="reported_fy" if len(operands) == 1 else "fy_plus_ytd_minus_prior_ytd",
                operands=operands,
                value=value,
            )
        )
    actuals["fpay_rev0"] = actuals["revenue_fintech"] - actuals["revenue_credit"]
    calculations.append(
        ModelCalculation(
            key="fpay_rev0",
            formula="fintech_minus_credit",
            operands=("revenue_fintech", "revenue_credit"),
            value=actuals["fpay_rev0"],
        )
    )
    allocations = {
        key: receipt.request.assumptions[key].value
        for key in (
            "credit_cash_allocation",
            "operating_cash_reserve",
            "credit_funding_debt_allocation",
        )
    }
    actuals["operating_lease_liabilities"] = (
        actuals["reported_current_operating_lease_liabilities"]
        + actuals["reported_noncurrent_operating_lease_liabilities"]
    )
    actuals["financial_debt_pool"] = (
        actuals["reported_total_financial_debt_and_leases"] - actuals["operating_lease_liabilities"]
    )
    calculations.extend(
        (
            ModelCalculation(
                key="operating_lease_liabilities",
                formula="current_plus_noncurrent_operating_leases",
                operands=(
                    "reported_current_operating_lease_liabilities",
                    "reported_noncurrent_operating_lease_liabilities",
                ),
                value=actuals["operating_lease_liabilities"],
            ),
            ModelCalculation(
                key="financial_debt_pool",
                formula="reported_total_debt_minus_operating_leases_rent_expensed",
                operands=(
                    "reported_total_financial_debt_and_leases",
                    "operating_lease_liabilities",
                ),
                value=actuals["financial_debt_pool"],
            ),
        )
    )
    cash_pool = actuals["reported_available_cash_and_investments"]
    debt_pool = actuals["financial_debt_pool"]
    if (
        cash_pool < 0
        or debt_pool < 0
        or actuals["reported_current_operating_lease_liabilities"] < 0
        or actuals["reported_noncurrent_operating_lease_liabilities"] < 0
        or any(value < 0 for value in allocations.values())
        or allocations["credit_cash_allocation"] + allocations["operating_cash_reserve"] > cash_pool
        or allocations["credit_funding_debt_allocation"] > debt_pool
    ):
        raise InputEvidenceError("cash_funding_allocation_outside_reported_pools")
    actuals["net_cash"] = (
        cash_pool
        - allocations["credit_cash_allocation"]
        - allocations["operating_cash_reserve"]
        - debt_pool
        + allocations["credit_funding_debt_allocation"]
    )
    calculations.append(
        ModelCalculation(
            key="net_cash",
            formula="reported_available_cash_minus_credit_cash_minus_operating_reserve_minus_rent_adjusted_debt_plus_credit_funding",
            operands=(
                "reported_available_cash_and_investments",
                "credit_cash_allocation",
                "operating_cash_reserve",
                "financial_debt_pool",
                "credit_funding_debt_allocation",
            ),
            value=actuals["net_cash"],
        )
    )
    if receipt.recipe == FINANCING_RECIPE:
        _financing_context(receipt.request, as_of=receipt.verified_at)
        reserve = receipt.request.assumptions.get("financing_other_expenses_reserve")
        if reserve is None or reserve.value < 0:
            raise InputEvidenceError("meli_financing_expense_reserve_required")
        principal = values.get("financing_issued_principal")
        fraction = values.get("financing_underwriting_purchase_fraction")
        if (
            principal is None
            or not math.isfinite(principal)
            or principal <= 0
            or fraction is None
            or not math.isfinite(fraction)
            or not 0 < fraction <= 1
        ):
            raise InputEvidenceError("meli_financing_reported_inputs_invalid")
        actuals["baseline_net_cash"] = actuals["net_cash"]
        actuals["financing_issued_principal"] = principal
        actuals["financing_underwriting_purchase_fraction"] = fraction
        actuals["financing_cash_before_other_expenses"] = principal * fraction
        actuals["financing_other_expenses_reserve"] = reserve.value
        actuals["net_cash"] += principal * fraction - principal - reserve.value
        calculations[-1] = calculations[-1].model_copy(update={"key": "baseline_net_cash"})
        calculations.extend(
            (
                ModelCalculation(
                    key="financing_cash_before_other_expenses",
                    formula="issued_principal_times_underwriter_purchase_fraction",
                    operands=(
                        "financing_issued_principal",
                        "financing_underwriting_purchase_fraction",
                    ),
                    value=actuals["financing_cash_before_other_expenses"],
                ),
                ModelCalculation(
                    key="net_cash",
                    formula="baseline_net_cash_plus_financing_cash_minus_principal_minus_expense_reserve",
                    operands=(
                        "baseline_net_cash",
                        "financing_cash_before_other_expenses",
                        "financing_issued_principal",
                        "financing_other_expenses_reserve",
                    ),
                    value=actuals["net_cash"],
                ),
            )
        )
    return actuals, tuple(calculations)


def model_output(inputs: Mapping[str, float]) -> dict[str, object]:
    from dataclasses import asdict

    from dcf.meli_model import Assum, mirror

    model = Assum()
    for key, value in inputs.items():
        setattr(model, key, int(value) if key in {"years", "derive_capm"} else value)
    return asdict(mirror(model))


BRIDGE_SENSITIVITY_CASES = (
    ("funding", "credit_funding_debt_allocation", (0, 2744, 5430, 8372)),
    ("financing_reserve", "financing_other_expenses_reserve", (0, 5, 10, 20)),
    ("credit_liquidity", "credit_cash_allocation", (0, 500, 1000)),
    ("operating_liquidity", "operating_cash_reserve", (0, 1500, 3000)),
)
MELI_DIAGNOSTIC_UNITS = (
    dict.fromkeys(
        (
            "commerce_profit_proxy_year0",
            "payments_profit_proxy_year0",
            "credit_profit_proxy_year0",
            "profit_proxy_year0",
            "profit_proxy_variance",
            "residual_credit_profit_calibration",
            "calibrated_profit_sum",
            "calibrated_profit_variance",
            "forward_profit_year1",
            "forward_profit_variance",
        ),
        "USD_m",
    )
    | dict.fromkeys(("profit_proxy_variance_fraction", "credit_cost_calibration"), "fraction")
    | {
        f"{prefix}_{value}_vps": "USD_per_share"
        for prefix, _, values in BRIDGE_SENSITIVITY_CASES
        for value in values
    }
    | dict.fromkeys(
        (
            "credit_cost_600bp_vps",
            "credit_cost_800bp_vps",
            "credit_cost_1000bp_vps",
            "credit_cost_1200bp_vps",
            "credit_cost_calibrated_vps",
        ),
        "USD_per_share",
    )
)


def driver_diagnostics(
    receipt: ModelInputReceipt,
    actuals: Mapping[str, float],
    inputs: Mapping[str, float],
) -> tuple[ModelCalculation, ...]:
    """Model-owned checks, never reported segment results or a historical bridge.

    The source-qualified caller owns admission. Counterfactual cash allocations
    use the same reported bindings and native bridge, not a patched net_cash.
    Reported annualized NIMAL and an ending book make year-zero profit a proxy. The
    residual cost calibration is an analyst allocation, not observed costs.
    """
    _reconcile(actuals, inputs)
    values: dict[str, float] = {}
    values["commerce_profit_proxy_year0"] = actuals["comm_rev0"] * inputs["comm_margin_near"]
    values["payments_profit_proxy_year0"] = actuals["fpay_rev0"] * inputs["fpay_margin_near"]
    values["credit_profit_proxy_year0"] = actuals["cb0"] * (
        actuals["nimal_actual"] - inputs["credit_opex_ratio"]
    )
    values["profit_proxy_year0"] = sum(values.values())
    values["profit_proxy_variance"] = values["profit_proxy_year0"] - actuals["operating_income"]
    if actuals["operating_income"] == 0 or actuals["cb0"] <= 0:
        raise InputEvidenceError("meli_profit_bridge_denominator_invalid")
    values["profit_proxy_variance_fraction"] = (
        values["profit_proxy_variance"] / actuals["operating_income"]
    )
    values["residual_credit_profit_calibration"] = (
        actuals["operating_income"]
        - values["commerce_profit_proxy_year0"]
        - values["payments_profit_proxy_year0"]
    )
    values["credit_cost_calibration"] = actuals["nimal_actual"] - (
        values["residual_credit_profit_calibration"] / actuals["cb0"]
    )
    values["calibrated_profit_sum"] = (
        values["commerce_profit_proxy_year0"]
        + values["payments_profit_proxy_year0"]
        + values["residual_credit_profit_calibration"]
    )
    values["calibrated_profit_variance"] = (
        values["calibrated_profit_sum"] - actuals["operating_income"]
    )
    observed, forward = review_drivers(actuals, inputs)["aggregate_profit_proxy"]
    values["forward_profit_year1"] = forward
    values["forward_profit_variance"] = forward - observed
    for prefix, key, cases in BRIDGE_SENSITIVITY_CASES:
        for value in cases:
            assumptions = dict(receipt.request.assumptions)
            assumptions[key] = assumptions[key].model_copy(update={"value": float(value)})
            variant = receipt.model_copy(
                update={"request": receipt.request.model_copy(update={"assumptions": assumptions})}
            )
            varied_actuals, _ = calculate_actuals(variant)
            varied = effective_numeric_inputs(
                {**inputs, **{name: varied_actuals[name] for name in REPORTED_DRIVER_KEYS}},
                recipe=receipt.recipe,
            )
            varied[key] = float(value)
            output = model_output(varied)
            values[f"{prefix}_{value}_vps"] = float(str(output["vps"]))
    for label, cost in (
        ("600bp", 0.06),
        ("800bp", 0.08),
        ("1000bp", 0.10),
        ("1200bp", 0.12),
        ("calibrated", values["credit_cost_calibration"]),
    ):
        varied = dict(inputs) | {"credit_opex_ratio": cost}
        varied["credit_terminal_roe"] = implied_credit_terminal_roe(varied)
        validate_credit_terminal(varied)
        values[f"credit_cost_{label}_vps"] = float(str(model_output(varied)["vps"]))
    if set(values) != set(MELI_DIAGNOSTIC_UNITS) or not all(map(math.isfinite, values.values())):
        raise InputEvidenceError("meli_driver_diagnostics_invalid")
    return tuple(
        ModelCalculation(
            key=key,
            formula="meli_reported_baseline_and_explicit_analyst_driver_check/v1",
            operands=("verified_reported_inputs", "reviewed_effective_inputs"),
            value=value,
        )
        for key, value in values.items()
    )


def review_drivers(
    actuals: Mapping[str, float], inputs: Mapping[str, float]
) -> dict[str, tuple[float, float]]:
    """Explicit comparators, not a claim that credit EBIT is separately reported."""
    from dcf.meli_model import Assum, mirror

    model = Assum()
    for key, value in inputs.items():
        setattr(model, key, int(value) if key in {"years", "derive_capm"} else value)
    year1 = mirror(model).rows[0]
    operating_revenue = actuals["comm_rev0"] + actuals["fpay_rev0"]
    return {
        "credit_cash_allocation": (
            actuals["reported_available_cash_and_investments"],
            inputs["credit_cash_allocation"],
        ),
        "operating_cash_reserve": (
            actuals["reported_available_cash_and_investments"],
            inputs["operating_cash_reserve"],
        ),
        "credit_funding_debt_allocation": (
            actuals["financial_debt_pool"],
            inputs["credit_funding_debt_allocation"],
        ),
        "nimal": (actuals["nimal_actual"], inputs["nimal_near"]),
        "aggregate_profit_proxy": (
            actuals["operating_income"],
            year1.op_ebit + year1.credit_ni / (1 - inputs["tax"]),
        ),
        "da_pct_operating_revenue": (actuals["depreciation"] / operating_revenue, inputs["da_pct"]),
        "capex_pct_operating_revenue": (
            actuals["capex"] / operating_revenue,
            inputs["capex_pct_near"],
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


def prepare_meli_inputs(
    conn: sqlite3.Connection,
    request: ModelInputRequest,
    *,
    effective_inputs: Mapping[str, float],
    as_of: datetime,
    source_context: SourceReadContext | None = None,
) -> tuple[dict[str, float], ModelInputReceipt]:
    if request.ticker != "MELI":
        raise InputEvidenceError("input_recipe_issuer_mismatch")
    requirements = requirements_for_request(request, as_of=as_of)
    assumption_keys = assumption_keys_for_recipe(request.recipe)
    proposed = {key: value for key, value in effective_inputs.items() if key in assumption_keys}
    proof = verify_model_inputs(
        conn,
        request,
        recipe=request.recipe,
        requirements=requirements,
        effective_inputs=proposed,
        assumption_keys=assumption_keys,
        as_of=as_of,
        source_context=source_context,
    )
    actuals, calculations = calculate_actuals(proof)
    complete = {**proposed, **{key: actuals[key] for key in REPORTED_DRIVER_KEYS}}
    _reconcile(actuals, complete)
    _verify_review(proof, actuals, complete, as_of)
    if (
        request.recipe == FINANCING_RECIPE
        and _financing_context(request, as_of=as_of).diagnostics is not None
    ):
        calculations += driver_diagnostics(proof, actuals, complete)
    return complete, proof.model_copy(
        update={
            "effective_inputs_sha256": canonical_digest(complete),
            "calculations": calculations,
            "actuals_sha256": canonical_digest(actuals),
            "model_output_sha256": canonical_digest(model_output(complete)),
        }
    )


def verify_meli_inputs(
    conn: sqlite3.Connection,
    receipt: ModelInputReceipt,
    *,
    effective_inputs: Mapping[str, float],
    as_of: datetime,
    source_context: SourceReadContext | None = None,
) -> ModelInputReceipt:
    if (
        receipt.schema_version != "dcf_model_inputs.v3"
        or receipt.source_integrity != "present_bytes_verified"
    ):
        raise InputEvidenceError("model_input_source_legacy_receipt_unverified")
    if not is_meli_recipe(receipt.recipe) or receipt.verified_at > as_of:
        raise InputEvidenceError("model_input_receipt_recipe_or_clock_invalid")
    complete, verified = prepare_meli_inputs(
        conn,
        receipt.request,
        effective_inputs=effective_inputs,
        as_of=as_of,
        source_context=source_context,
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
        or tuple(
            (
                item.document_version_id,
                item.blob_sha256,
                item.byte_size,
                item.reader_policy,
                item.storage_uri_sha256,
            )
            for item in receipt.raw_documents
        )
        != tuple(
            (
                item.document_version_id,
                item.blob_sha256,
                item.byte_size,
                item.reader_policy,
                item.storage_uri_sha256,
            )
            for item in verified.raw_documents
        )
    ):
        raise InputEvidenceError("model_input_receipt_mismatch")
    return verified


def _reconcile(actuals: Mapping[str, float], inputs: Mapping[str, float]) -> None:
    def close(left: float, right: float) -> bool:
        # Issuer tables commonly round to USD millions; allow one million only.
        return abs(left - right) <= 1.0

    if not close(actuals["comm_rev0"] + actuals["revenue_fintech"], actuals["revenue_total"]):
        raise InputEvidenceError("commerce_fintech_revenue_does_not_reconcile")
    if not close(actuals["fpay_rev0"] + actuals["revenue_credit"], actuals["revenue_fintech"]):
        raise InputEvidenceError("payments_credit_revenue_does_not_reconcile")
    if any(actuals[key] <= 0 for key in ("comm_rev0", "fpay_rev0", "cb0", "shares")):
        raise InputEvidenceError("positive_operating_baselines_required")
    if not 0 <= actuals["nimal_actual"] <= 1:
        raise InputEvidenceError("nimal_actual_invalid")
    if inputs["years"] < 2 or inputs["years"] != int(inputs["years"]) or inputs["wacc"] <= 0:
        raise InputEvidenceError("model_terminal_or_capital_inputs_invalid")
    try:
        validate_credit_terminal(inputs)
    except ValueError as exc:
        raise InputEvidenceError(str(exc)) from exc
