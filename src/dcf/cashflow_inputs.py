"""Reviewed nonfinancial US-GAAP equity cash-flow recipe.

CFO already includes cash interest and tax under this bounded method. Debt is
retained as reported leverage evidence, not subtracted a second time. Net new
borrowing is zero in the forecast. Carve-out adjustments are analyst inputs.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

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

RECIPE = "operating_cashflow_equity.v1"
ENGINE = "operating_cashflow_equity"
FLOW_CONCEPTS = ("operating_cash_flow", "capital_expenditure", "stock_based_compensation")
ASSUMPTION_KEYS = frozenset(
    {
        "growth",
        "cost_of_equity",
        "terminal_growth",
        "years",
        "cash_reserve",
        "cashflow_normalization",
        "incremental_capex",
        "dilution_shares",
    }
)
REPORTED_KEYS = frozenset({"owner_cashflow", "reported_cash", "reported_debt", "reported_shares"})


class CashflowPeriod(FrozenModel):
    role: Literal["fy", "current_ytd", "prior_ytd"]
    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self) -> CashflowPeriod:
        if self.start >= self.end:
            raise ValueError("cashflow period must be an exact positive duration")
        return self


class CashflowRecipeContext(FrozenModel):
    economic_method: Literal["nonfinancial_operating_company"]
    reporting_regime: Literal["sec_domestic_10k_10q"]
    currency: Literal["USD"]
    accounting_basis: Literal["us_gaap"]
    source_scope_label: Literal["consolidated", "combined_carve_out"]
    consolidation_scope: Literal["consolidated", "other"]
    point_accounting_basis: Literal["us_gaap", "management"] = "us_gaap"
    shares_accounting_basis: Literal["us_gaap", "management"] = "us_gaap"
    point_scope_label: Literal["consolidated", "combined_carve_out"] = "consolidated"
    point_consolidation_scope: Literal["consolidated", "other"] = "consolidated"
    flow_periods: tuple[CashflowPeriod, ...] = Field(min_length=1, max_length=3)
    balance_date: date
    shares_date: date
    cash_interest_and_tax: Literal["included_in_operating_cash_flow"]
    capex_basis: Literal["positive_cash_outflow"]
    sbc_basis: Literal["reported_operating_cashflow_addback"]
    shares_basis: Literal["period_end_common_shares_plus_analyst_dilution"]
    net_new_borrowing: Literal["zero"]
    method_reviewer: str = Field(min_length=1)
    method_rationale: str = Field(min_length=30)

    @model_validator(mode="after")
    def _population(self) -> CashflowRecipeContext:
        roles = [item.role for item in self.flow_periods]
        if roles != ["fy"] and roles != ["fy", "current_ytd", "prior_ytd"]:
            raise ValueError("cashflow recipe requires exact annual or FY/YTD/prior-YTD membership")
        expected = "consolidated" if self.source_scope_label == "consolidated" else "other"
        if self.consolidation_scope != expected:
            raise ValueError("cashflow recipe source scope label mismatch")
        point_expected = "consolidated" if self.point_scope_label == "consolidated" else "other"
        if self.point_consolidation_scope != point_expected:
            raise ValueError("cashflow recipe point source scope label mismatch")
        fy = self.flow_periods[0]
        if not 350 <= (fy.end - fy.start).days <= 380:
            raise ValueError("cashflow recipe requires a real reported fiscal year")
        if len(self.flow_periods) == 3:
            current, prior = self.flow_periods[1:]
            if current.start != fy.end + timedelta(days=1):
                raise ValueError("cashflow YTD must follow the exact fiscal-year end")
            if (
                not 345 <= (current.start - prior.start).days <= 385
                or not 345 <= (current.end - prior.end).days <= 385
            ):
                raise ValueError("cashflow YTD comparators do not have matching annual spans")
        return self


def context_for(request: ModelInputRequest) -> CashflowRecipeContext:
    if request.recipe != RECIPE or request.recipe_context is None:
        raise InputEvidenceError("cashflow_recipe_context_required")
    try:
        context = CashflowRecipeContext.model_validate(request.recipe_context)
    except ValueError as exc:
        raise InputEvidenceError("cashflow_recipe_context_invalid_or_method_unsupported") from exc
    if (
        context.flow_periods[-2 if len(context.flow_periods) == 3 else 0].end
        != request.financial_period_end
    ):
        raise InputEvidenceError("cashflow_anchor_period_mismatch")
    if (
        context.balance_date != request.financial_period_end
        or context.shares_date < request.financial_period_end
    ):
        raise InputEvidenceError("cashflow_point_basis_not_current_reported_period")
    return context


def requirements_for(request: ModelInputRequest) -> tuple[InputRequirement, ...]:
    context = context_for(request)
    constraints: dict[str, object] = {"source_scope_label": context.source_scope_label}
    requirements: list[InputRequirement] = []
    for period in context.flow_periods:
        for concept in FLOW_CONCEPTS:
            requirements.append(
                InputRequirement(
                    key=f"{concept}_{period.role}",
                    role=f"cashflow_equity.{concept}",
                    unit_key="USD",
                    currency="USD",
                    period_kind="duration",
                    period_start=period.start,
                    period_end=period.end,
                    annual=period.role == "fy",
                    accounting_basis=context.accounting_basis,
                    consolidation_scope=context.consolidation_scope,
                    scale=Decimal("0.000001"),
                    definition_constraints=constraints,
                )
            )
    for key, concept, point, unit in (
        ("reported_cash", "cash_and_equivalents", context.balance_date, "USD"),
        ("reported_debt", "total_financial_debt", context.balance_date, "USD"),
        ("reported_shares", "shares_outstanding", context.shares_date, "shares"),
    ):
        requirements.append(
            InputRequirement(
                key=key,
                role=f"cashflow_equity.{concept}",
                unit_key=unit,
                currency="USD" if unit == "USD" else None,
                period_kind="instant",
                period_end=point,
                accounting_basis=context.shares_accounting_basis
                if key == "reported_shares"
                else context.point_accounting_basis,
                consolidation_scope=context.point_consolidation_scope,
                scale=Decimal("0.000001"),
                definition_constraints={"source_scope_label": context.point_scope_label},
            )
        )
    return tuple(requirements)


def effective_numeric_inputs(values: Mapping[str, object]) -> dict[str, float]:
    keys = ASSUMPTION_KEYS | REPORTED_KEYS
    if frozenset(values) != keys:
        raise InputEvidenceError("cashflow_effective_input_population_mismatch")
    result: dict[str, float] = {}
    for key, value in values.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise InputEvidenceError(f"cashflow_numeric_input_invalid:{key}")
        result[key] = float(value)
    return result


def calculate_actuals(
    receipt: ModelInputReceipt,
) -> tuple[dict[str, float], tuple[ModelCalculation, ...]]:
    context = context_for(receipt.request)
    values = {item.key: item.value for item in receipt.inputs}
    actuals = {key: values[key] for key in ("reported_cash", "reported_debt", "reported_shares")}
    calculations: list[ModelCalculation] = []
    for concept in FLOW_CONCEPTS:
        operands = tuple(f"{concept}_{period.role}" for period in context.flow_periods)
        source_inputs = [item for item in receipt.inputs if item.key in operands]
        if (
            len(source_inputs) != len(operands)
            or len(
                {
                    (
                        item.reference.metric_id,
                        item.reporting_entity_id,
                        item.unit_key,
                        item.currency,
                    )
                    for item in source_inputs
                }
            )
            != 1
        ):
            raise InputEvidenceError(f"cashflow_operand_comparability_unreviewed:{concept}")
        value = (
            values[operands[0]]
            if len(operands) == 1
            else values[operands[0]] + values[operands[1]] - values[operands[2]]
        )
        if concept != "operating_cash_flow" and value < 0:
            raise InputEvidenceError(f"cashflow_positive_outflow_or_addback_required:{concept}")
        actuals[concept] = value
        calculations.append(
            ModelCalculation(
                key=concept,
                formula="reported_fy" if len(operands) == 1 else "fy_plus_ytd_minus_prior_ytd",
                operands=operands,
                value=value,
            )
        )
    actuals["reported_fcf_after_sbc"] = (
        actuals["operating_cash_flow"]
        - actuals["capital_expenditure"]
        - actuals["stock_based_compensation"]
    )
    normalization = receipt.request.assumptions["cashflow_normalization"].value
    incremental = receipt.request.assumptions["incremental_capex"].value
    actuals["owner_cashflow"] = actuals["reported_fcf_after_sbc"] + normalization - incremental
    calculations.extend(
        (
            ModelCalculation(
                key="reported_fcf_after_sbc",
                formula="cfo_minus_cash_capex_minus_sbc",
                operands=FLOW_CONCEPTS,
                value=actuals["reported_fcf_after_sbc"],
            ),
            ModelCalculation(
                key="owner_cashflow",
                formula="reported_fcf_after_sbc_plus_analyst_normalization_minus_incremental_capex",
                operands=("reported_fcf_after_sbc", "cashflow_normalization", "incremental_capex"),
                value=actuals["owner_cashflow"],
            ),
        )
    )
    return actuals, tuple(calculations)


def model_output(inputs: Mapping[str, float]) -> dict[str, object]:
    years = inputs["years"]
    discount, terminal = inputs["cost_of_equity"], inputs["terminal_growth"]
    if (
        not years.is_integer()
        or not 1 <= years <= 30
        or not 0 < discount < 1
        or not -1 < terminal < discount
        or not -1 < inputs["growth"] < 1
    ):
        raise InputEvidenceError("cashflow_forecast_bounds_invalid")
    if (
        not 0 <= inputs["cash_reserve"] <= inputs["reported_cash"]
        or inputs["incremental_capex"] < 0
        or inputs["dilution_shares"] < 0
        or inputs["reported_debt"] < 0
    ):
        raise InputEvidenceError("cashflow_allocation_bounds_invalid")
    shares = inputs["reported_shares"] + inputs["dilution_shares"]
    if shares <= 0 or inputs["owner_cashflow"] <= 0:
        raise InputEvidenceError("cashflow_positive_equity_basis_required")
    rows = []
    pv = 0.0
    for year in range(1, int(years) + 1):
        flow = inputs["owner_cashflow"] * (1 + inputs["growth"]) ** year
        discounted = flow / (1 + discount) ** year
        pv += discounted
        rows.append({"year": year, "equity_cashflow_m": flow, "present_value_m": discounted})
    final_flow = inputs["owner_cashflow"] * (1 + inputs["growth"]) ** int(years)
    terminal_pv = final_flow * (1 + terminal) / (discount - terminal) / (1 + discount) ** int(years)
    equity = pv + terminal_pv + inputs["reported_cash"] - inputs["cash_reserve"]
    return {
        "vps": equity / shares,
        "equity_value": equity,
        "operating_ev": None,
        "credit_equity_value": None,
        "rows": rows,
        "terminal_present_value_m": terminal_pv,
        "net_new_borrowing": "zero",
        "currency": "USD",
    }


def prepare_cashflow_inputs(
    conn: sqlite3.Connection,
    request: ModelInputRequest,
    *,
    effective_inputs: Mapping[str, float],
    as_of: datetime,
    source_context: SourceReadContext | None = None,
) -> tuple[dict[str, float], ModelInputReceipt]:
    if frozenset(effective_inputs) not in {ASSUMPTION_KEYS, ASSUMPTION_KEYS | REPORTED_KEYS}:
        raise InputEvidenceError("cashflow_effective_input_population_mismatch")
    if any(
        isinstance(value, bool) or not math.isfinite(value) for value in effective_inputs.values()
    ):
        raise InputEvidenceError("cashflow_numeric_input_invalid")
    proposed = {key: value for key, value in effective_inputs.items() if key in ASSUMPTION_KEYS}
    proof = verify_model_inputs(
        conn,
        request,
        recipe=RECIPE,
        requirements=requirements_for(request),
        effective_inputs=proposed,
        assumption_keys=ASSUMPTION_KEYS,
        as_of=as_of,
        source_context=source_context,
    )
    actuals, calculations = calculate_actuals(proof)
    complete = {**proposed, **{key: actuals[key] for key in REPORTED_KEYS}}
    if any(
        key in effective_inputs and effective_inputs[key] != complete[key] for key in REPORTED_KEYS
    ):
        raise InputEvidenceError("cashflow_reported_or_calculated_input_mismatch")
    review = request.assumption_review
    if (
        review is None
        or review.reviewed_at > as_of
        or review.reviewed_at < max(item.recorded_at for item in proof.inputs)
        or review.effective_inputs_sha256 != canonical_digest(complete)
        or review.actuals_sha256 != canonical_digest(actuals)
    ):
        raise InputEvidenceError("assumption_review_clock_or_basis_mismatch")
    drivers = {
        "cashflow_normalization": (actuals["reported_fcf_after_sbc"], actuals["owner_cashflow"]),
        "cash_reserve": (actuals["reported_cash"], complete["cash_reserve"]),
        "dilution_shares": (
            actuals["reported_shares"],
            actuals["reported_shares"] + complete["dilution_shares"],
        ),
    }
    if set(review.drivers) != set(drivers):
        raise InputEvidenceError("assumption_review_population_mismatch")
    for key, (observed, forecast) in drivers.items():
        item = review.drivers[key]
        if any(
            not math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
            for a, b in (
                (item.observed, observed),
                (item.forecast, forecast),
                (item.variance, forecast - observed),
            )
        ):
            raise InputEvidenceError(f"assumption_review_variance_mismatch:{key}")
    output = model_output(complete)
    return complete, proof.model_copy(
        update={
            "effective_inputs_sha256": canonical_digest(complete),
            "calculations": calculations,
            "actuals_sha256": canonical_digest(actuals),
            "model_output_sha256": canonical_digest(output),
        }
    )


def verify_cashflow_inputs(
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
    if receipt.recipe != RECIPE or receipt.verified_at > as_of:
        raise InputEvidenceError("model_input_receipt_recipe_or_clock_invalid")
    complete, verified = prepare_cashflow_inputs(
        conn,
        receipt.request,
        effective_inputs=effective_inputs,
        as_of=as_of,
        source_context=source_context,
    )
    if (
        complete != dict(effective_inputs)
        or verified.model_dump(exclude={"verified_at", "raw_documents"})
        != receipt.model_dump(exclude={"verified_at", "raw_documents"})
        or tuple(item.model_dump(exclude={"verified_at"}) for item in verified.raw_documents)
        != tuple(item.model_dump(exclude={"verified_at"}) for item in receipt.raw_documents)
    ):
        raise InputEvidenceError("model_input_receipt_mismatch")
    return verified
