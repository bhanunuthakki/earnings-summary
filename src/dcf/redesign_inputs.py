"""Run-bound retained-source recipe for the existing redesigned FCFF engine."""

from __future__ import annotations

import dataclasses
import math
import sqlite3
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Literal

from pydantic import Field, model_validator

from dcf import equity_bridge, redesign
from dcf.input_evidence import (
    FrozenModel,
    InputEvidenceError,
    InputRequirement,
    ModelCalculation,
    ModelInputReceipt,
    ModelInputRequest,
    SourceReadContext,
    canonical_digest,
    verify_assumption_review,
    verify_model_inputs,
)

RECIPE = "redesign_fcff.v1"
ENGINE = "redesign_fcff"
FLOW_CONCEPTS = (
    "revenue",
    "operating_income",
    "depreciation_and_amortization",
    "stock_based_compensation",
    "capital_expenditure",
)
SCALARS = frozenset(
    {
        "near_op_margin",
        "terminal_op_margin",
        "tax_rate",
        "capex_2026_m",
        "terminal_capex_da",
        "da_ratio",
        "consensus_years",
        "wacc",
        "beta",
        "risk_free_rate",
        "equity_risk_premium",
        "cost_of_debt",
        "exit_multiple",
        "terminal_growth_g",
        "current_price",
        "cash_m",
        "total_debt_m",
        "diluted_shares_m",
        "fx_to_usd",
        "country_risk_premium",
        "growth_fade_curvature",
        "near_sbc_pct",
        "terminal_sbc_pct",
        "weight_bull",
        "weight_base",
        "weight_bear",
    }
)
DELTA_KEYS = tuple(field.name for field in dataclasses.fields(redesign.ScenarioDeltas))
BASE_KEYS = (
    SCALARS
    | {"segment_count", "terminal_method_code", "terminal_basis_code"}
    | {f"{scenario}_{key}" for scenario in ("bull", "bear") for key in DELTA_KEYS}
)
METHODS = (redesign.EXIT_MULTIPLE, redesign.PERPETUITY)
BASES = ("EV/Sales", "EV/EBITDA", "EV/EBIT", "EV/FCF")


class FinancialPeriod(FrozenModel):
    start: date
    end: date

    @model_validator(mode="after")
    def ordered(self) -> FinancialPeriod:
        if self.start >= self.end:
            raise ValueError("financial period must have a positive duration")
        return self


class RedesignRecipeContext(FrozenModel):
    segments: tuple[str, ...] = Field(min_length=1, max_length=20)
    fiscal_periods: tuple[FinancialPeriod, ...]
    balance_date: date
    shares_period: FinancialPeriod
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    accounting_basis: Literal["us_gaap", "ifrs"]
    consolidation_scope: Literal["consolidated", "other"]
    source_scope_label: Literal["consolidated", "combined_carve_out"]
    monetary_units: dict[str, Literal["currency", "millions", "billions"]]
    capex_sign: Literal["positive_outflow", "negative_outflow"]
    cash_scope: Literal["cash_and_equivalents", "cash_and_short_term_investments"]
    cash_basis: Literal["reported_aggregate"]
    debt_scope: Literal["interest_bearing_debt_only", "debt_and_lease_obligations"]
    debt_basis: Literal["reported_aggregate"]
    finance_lease_basis: Literal["reported_aggregate", "complete_component_sum"] | None = None
    operating_lease_basis: Literal["reported_aggregate", "complete_component_sum"] | None = None

    @model_validator(mode="after")
    def population(self) -> RedesignRecipeContext:
        periods = self.fiscal_periods
        if len(periods) not in {1, 2, 4}:
            raise ValueError(
                "full fiscal year requires one annual, two half-year, or four quarterly periods"
            )
        if any(right.start != left.end + timedelta(days=1) for left, right in pairwise(periods)):
            raise ValueError("full fiscal year periods must be contiguous")
        if not 350 <= (periods[-1].end - periods[0].start).days <= 380:
            raise ValueError("full fiscal year required")
        bounds = {1: (350, 380), 2: (165, 200), 4: (70, 105)}[len(periods)]
        if any(
            not bounds[0] <= (period.end - period.start).days <= bounds[1] for period in periods
        ):
            raise ValueError("full fiscal year has invalid component period durations")
        if self.balance_date < periods[-1].end or self.shares_period.end != self.balance_date:
            raise ValueError("latest equity bridge must cover at least the fiscal year")
        if not 70 <= (self.shares_period.end - self.shares_period.start).days <= 380:
            raise ValueError("weighted diluted shares require an explicit reported duration")
        expected_scope = "consolidated" if self.source_scope_label == "consolidated" else "other"
        if self.consolidation_scope != expected_scope:
            raise ValueError("statement scope label mismatch")
        if len(set(self.segments)) != len(self.segments) or any(
            not name.strip() for name in self.segments
        ):
            raise ValueError("unique named segments required")
        if (self.finance_lease_basis is not None) != (
            self.debt_scope == "interest_bearing_debt_only"
        ):
            raise ValueError("pure debt requires an explicit complete finance lease basis")
        if (self.operating_lease_basis is not None) != (
            self.debt_scope == "debt_and_lease_obligations"
        ):
            raise ValueError("debt and leases requires an explicit complete operating lease basis")
        expected_units = {
            f"{concept}_{index}" for concept in FLOW_CONCEPTS for index in range(len(periods))
        } | {key for key, _concept, _field in bridge_source_fields(self)}
        if set(self.monetary_units) != expected_units:
            raise ValueError("monetary unit population mismatch")
        return self


def bridge_source_fields(context: RedesignRecipeContext) -> tuple[tuple[str, str, str], ...]:
    if context.cash_scope == "cash_and_equivalents":
        cash = (("reported_cash_m", "cash_and_equivalents", "cashAndCashEquivalents"),)
    else:
        cash = (
            (
                "reported_cash_and_short_term_investments_m",
                "cash_and_short_term_investments",
                "cashAndShortTermInvestments",
            ),
        )
    debt = (("reported_total_debt_m", "total_financial_debt", "totalDebt"),)
    lease_type = "finance" if context.debt_scope == "interest_bearing_debt_only" else "operating"
    lease_basis = (
        context.finance_lease_basis if lease_type == "finance" else context.operating_lease_basis
    )
    lease_field = f"{lease_type}LeaseLiability"
    leases = (
        (
            (
                f"reported_{lease_type}_lease_liability_m",
                f"{lease_type}_lease_liability",
                lease_field,
            ),
        )
        if lease_basis == "reported_aggregate"
        else (
            (
                f"reported_{lease_type}_lease_liability_current_m",
                f"{lease_type}_lease_liability_current",
                lease_field + "Current",
            ),
            (
                f"reported_{lease_type}_lease_liability_non_current_m",
                f"{lease_type}_lease_liability_non_current",
                lease_field + "Noncurrent",
            ),
        )
    )
    return (*cash, *debt, *leases)


def context_for(request: ModelInputRequest) -> RedesignRecipeContext:
    if request.recipe != RECIPE or request.recipe_context is None:
        raise InputEvidenceError("redesign_recipe_context_required")
    try:
        context = RedesignRecipeContext.model_validate(request.recipe_context)
    except ValueError as exc:
        raise InputEvidenceError("redesign_recipe_context_invalid") from exc
    if request.financial_period_end != context.balance_date:
        raise InputEvidenceError("redesign_financial_period_mismatch")
    return context


def requirements_for(request: ModelInputRequest) -> tuple[InputRequirement, ...]:
    context = context_for(request)
    result: list[InputRequirement] = []
    for index, period in enumerate(context.fiscal_periods):
        for concept in FLOW_CONCEPTS:
            key = f"{concept}_{index}"
            unit = context.monetary_units[key]
            result.append(
                InputRequirement(
                    key=key,
                    role=f"financial_statement.{concept}",
                    unit_key=context.currency if unit == "currency" else unit,
                    currency=context.currency,
                    period_kind="duration",
                    period_start=period.start,
                    period_end=period.end,
                    annual=len(context.fiscal_periods) == 1,
                    accounting_basis=context.accounting_basis,
                    consolidation_scope=context.consolidation_scope,
                    scale={
                        "currency": Decimal("0.000001"),
                        "millions": Decimal(1),
                        "billions": Decimal(1000),
                    }[unit],
                    definition_constraints={"source_scope_label": context.source_scope_label},
                )
            )
    for key, concept in (
        *((key, concept) for key, concept, _field in bridge_source_fields(context)),
        ("diluted_shares_m", "weighted_avg_shares_diluted"),
    ):
        shares = key == "diluted_shares_m"
        unit = context.monetary_units.get(key, "currency")
        result.append(
            InputRequirement(
                key=key,
                role=f"financial_statement.{concept}",
                unit_key="shares" if shares else context.currency if unit == "currency" else unit,
                currency=None if shares else context.currency,
                period_kind="duration" if shares else "instant",
                period_start=context.shares_period.start if shares else None,
                period_end=context.balance_date,
                accounting_basis=context.accounting_basis,
                consolidation_scope=context.consolidation_scope,
                scale=Decimal("0.000001")
                if shares
                else {
                    "currency": Decimal("0.000001"),
                    "millions": Decimal(1),
                    "billions": Decimal(1000),
                }[unit],
                definition_constraints={"source_scope_label": context.source_scope_label},
            )
        )
    return tuple(result)


def serialize_flat_inputs(inp: redesign.RedesignInputs) -> dict[str, float]:
    result = {key: float(getattr(inp, key)) for key in SCALARS}
    result.update(
        segment_count=float(len(inp.segments)),
        terminal_method_code=float(METHODS.index(inp.terminal_method)),
        terminal_basis_code=float(BASES.index(inp.terminal_basis)),
    )
    total = sum(inp.base_revenue_by_segment.values())
    if total <= 0:
        raise InputEvidenceError("redesign_positive_base_revenue_required")
    for index, name in enumerate(inp.segments):
        result.update(
            {
                f"segment_{index}_revenue_m": inp.base_revenue_by_segment[name],
                f"segment_{index}_allocation": inp.base_revenue_by_segment[name] / total,
                f"segment_{index}_near_growth": inp.near_growth_by_segment[name],
                f"segment_{index}_terminal_growth": inp.terminal_growth_by_segment[name],
            }
        )
    for scenario in ("bull", "bear"):
        delta = getattr(inp, f"{scenario}_deltas")
        result.update({f"{scenario}_{key}": float(getattr(delta, key)) for key in DELTA_KEYS})
    return effective_numeric_inputs(result)


def effective_numeric_inputs(values: Mapping[str, object]) -> dict[str, float]:
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
        for value in values.values()
    ):
        raise InputEvidenceError("redesign_numeric_input_invalid")
    result = {key: float(value) for key, value in values.items() if isinstance(value, (int, float))}
    count = result.get("segment_count", 0.0)
    if not count.is_integer() or not 1 <= count <= 20:
        raise InputEvidenceError("redesign_segment_count_invalid")
    expected = BASE_KEYS | {
        f"segment_{index}_{field}"
        for index in range(int(count))
        for field in ("revenue_m", "allocation", "near_growth", "terminal_growth")
    }
    if frozenset(result) != expected:
        raise InputEvidenceError("redesign_effective_input_population_mismatch")
    if (
        any(
            not result[key].is_integer()
            for key in ("consensus_years", "terminal_method_code", "terminal_basis_code")
        )
        or not 1 <= result["consensus_years"] <= redesign.N_FC
        or result["terminal_method_code"] not in range(len(METHODS))
        or result["terminal_basis_code"] not in range(len(BASES))
    ):
        raise InputEvidenceError("redesign_discrete_input_invalid")
    fractions = [result[f"segment_{index}_allocation"] for index in range(int(count))]
    if any(value < 0 for value in fractions) or not math.isclose(
        sum(fractions), 1.0, rel_tol=0, abs_tol=1e-12
    ):
        raise InputEvidenceError("redesign_segment_allocation_invalid")
    if (
        result["diluted_shares_m"] <= 0
        or result["fx_to_usd"] <= 0
        or result["cash_m"] < 0
        or result["total_debt_m"] < 0
        or not 0 < result["wacc"] < 1
        or not 0 <= result["tax_rate"] < 1
    ):
        raise InputEvidenceError("redesign_equity_or_discount_basis_invalid")
    weights = [result[f"weight_{scenario}"] for scenario in ("bull", "base", "bear")]
    if any(value < 0 for value in weights) or not math.isclose(
        sum(weights), 1.0, rel_tol=0, abs_tol=1e-12
    ):
        raise InputEvidenceError("redesign_scenario_weights_invalid")
    return result


def reported_keys(inputs: Mapping[str, float]) -> frozenset[str]:
    return frozenset(
        {"da_ratio", "cash_m", "total_debt_m", "diluted_shares_m"}
        | {f"segment_{index}_revenue_m" for index in range(int(inputs["segment_count"]))}
    )


def calculate_actuals(
    receipt: ModelInputReceipt,
) -> tuple[dict[str, float], tuple[ModelCalculation, ...]]:
    context = context_for(receipt.request)
    values = {item.key: item.value for item in receipt.inputs}
    expected = {item.key for item in requirements_for(receipt.request)}
    if set(values) != expected or len(values) != len(receipt.inputs):
        raise InputEvidenceError("redesign_verified_input_population_mismatch")
    fields = bridge_source_fields(context)
    record = {field: values[key] for key, _concept, field in fields}
    if any(value < 0 for value in record.values()):
        raise InputEvidenceError("redesign_equity_bridge_negative_reported_component")
    cash_field = (
        "cashAndCashEquivalents"
        if context.cash_scope == "cash_and_equivalents"
        else "cashAndShortTermInvestments"
    )
    cash = equity_bridge.resolve_complete_aggregate(
        record,
        aggregate_field=cash_field,
        component_fields=(),
    )
    debt = equity_bridge.resolve_debt_scope(record, scope=context.debt_scope)
    if (
        cash is None
        or debt is None
        or cash.basis != context.cash_basis
        or debt.debt_basis != context.debt_basis
    ):
        raise InputEvidenceError("redesign_equity_bridge_scope_unresolved")
    actuals = {
        "cash_m": cash.value,
        "total_debt_m": debt.value,
        "diluted_shares_m": values["diluted_shares_m"],
    }
    calculations: list[ModelCalculation] = []
    cash_operands = tuple(
        key
        for key, _concept, field in fields
        if field in {cash_field, "cashAndCashEquivalents", "shortTermInvestments"}
    )
    calculations.append(
        ModelCalculation(key="cash_m", formula=cash.basis, operands=cash_operands, value=cash.value)
    )
    debt_operands: list[str] = []
    for aggregate_field, sign in debt.operations:
        resolved = equity_bridge.resolve_complete_aggregate(
            record,
            aggregate_field=aggregate_field,
            component_fields={
                "totalDebt": (),
                "financeLeaseLiability": (
                    "financeLeaseLiabilityCurrent",
                    "financeLeaseLiabilityNoncurrent",
                ),
                "operatingLeaseLiability": (
                    "operatingLeaseLiabilityCurrent",
                    "operatingLeaseLiabilityNoncurrent",
                ),
            }[aggregate_field],
        )
        if resolved is None:
            raise InputEvidenceError("redesign_equity_bridge_scope_unresolved")
        key = (
            "reported_debt_and_finance_leases_m"
            if aggregate_field == "totalDebt"
            else "reported_finance_lease_scope_m"
            if sign < 0
            else "reported_operating_lease_scope_m"
        )
        operands = tuple(
            source_key
            for source_key, _concept, field in fields
            if field == aggregate_field or field in resolved.component_fields
        )
        actuals[key] = resolved.value
        calculations.append(
            ModelCalculation(
                key=key, formula=resolved.basis, operands=operands, value=resolved.value
            )
        )
        debt_operands.append(key)
    calculations.append(
        ModelCalculation(
            key="total_debt_m",
            formula=debt.calculation,
            operands=tuple(debt_operands),
            value=debt.value,
        )
    )
    for concept in FLOW_CONCEPTS:
        operands = tuple(f"{concept}_{index}" for index in range(len(context.fiscal_periods)))
        selected = [item for item in receipt.inputs if item.key in operands]
        if (
            len(
                {
                    (item.reference.metric_id, item.reporting_entity_id, item.currency)
                    for item in selected
                }
            )
            != 1
        ):
            raise InputEvidenceError(f"redesign_operand_comparability_unreviewed:{concept}")
        actuals[concept] = sum(values[key] for key in operands)
        calculations.append(
            ModelCalculation(
                key=concept,
                formula="sum_exact_full_fiscal_year",
                operands=operands,
                value=actuals[concept],
            )
        )
    sign = 1 if context.capex_sign == "positive_outflow" else -1
    if any(
        values[f"capital_expenditure_{index}"] * sign < 0
        for index in range(len(context.fiscal_periods))
    ):
        raise InputEvidenceError("redesign_capex_sign_basis_mismatch")
    actuals["cash_capex_m"] = actuals["capital_expenditure"] * sign
    revenue = actuals["revenue"]
    if (
        revenue <= 0
        or actuals["depreciation_and_amortization"] < 0
        or actuals["stock_based_compensation"] < 0
    ):
        raise InputEvidenceError("redesign_reported_anchor_invalid")
    actuals["da_ratio"] = actuals["depreciation_and_amortization"] / revenue
    actuals["operating_margin_before_sbc"] = (
        actuals["operating_income"] + actuals["stock_based_compensation"]
    ) / revenue
    calculations.extend(
        (
            ModelCalculation(
                key="cash_capex_m",
                formula=f"reported_capex_times_{sign}",
                operands=("capital_expenditure",),
                value=actuals["cash_capex_m"],
            ),
            ModelCalculation(
                key="da_ratio",
                formula="reported_fy_da_divided_by_revenue",
                operands=("depreciation_and_amortization", "revenue"),
                value=actuals["da_ratio"],
            ),
        )
    )
    calculations.append(
        ModelCalculation(
            key="operating_margin_before_sbc",
            formula="reported_operating_income_plus_sbc_divided_by_revenue",
            operands=("operating_income", "stock_based_compensation", "revenue"),
            value=actuals["operating_margin_before_sbc"],
        )
    )
    return actuals, tuple(calculations)


def review_drivers(
    actuals: Mapping[str, float], inputs: Mapping[str, float]
) -> Mapping[str, tuple[float, float]]:
    return {
        "near_op_margin": (
            actuals["operating_margin_before_sbc"],
            inputs["near_op_margin"],
        ),
        "near_sbc_pct": (
            actuals["stock_based_compensation"] / actuals["revenue"],
            inputs["near_sbc_pct"],
        ),
        "capex_2026_m": (actuals["cash_capex_m"], inputs["capex_2026_m"]),
        "terminal_sbc_pct": (
            actuals["stock_based_compensation"] / actuals["revenue"],
            inputs["terminal_sbc_pct"],
        ),
        "terminal_op_margin": (
            actuals["operating_margin_before_sbc"],
            inputs["terminal_op_margin"],
        ),
    }


def reconstruct_inputs(
    inputs: Mapping[str, float], *, segments: tuple[str, ...] | None = None
) -> redesign.RedesignInputs:
    checked = effective_numeric_inputs(inputs)
    count = int(checked["segment_count"])
    names = (
        segments if segments is not None else tuple(f"segment_{index}" for index in range(count))
    )
    if len(names) != count or len(set(names)) != count or any(not name.strip() for name in names):
        raise InputEvidenceError("redesign_segment_population_mismatch")
    payload: dict[str, object] = {key: checked[key] for key in SCALARS}
    payload.update(
        segments=names,
        base_revenue_by_segment={
            name: checked[f"segment_{index}_revenue_m"] for index, name in enumerate(names)
        },
        near_growth_by_segment={
            name: checked[f"segment_{index}_near_growth"] for index, name in enumerate(names)
        },
        terminal_growth_by_segment={
            name: checked[f"segment_{index}_terminal_growth"] for index, name in enumerate(names)
        },
        terminal_method=METHODS[int(checked["terminal_method_code"])],
        terminal_basis=BASES[int(checked["terminal_basis_code"])],
    )
    payload.update(
        {
            f"{scenario}_deltas": {key: checked[f"{scenario}_{key}"] for key in DELTA_KEYS}
            for scenario in ("bull", "bear")
        }
    )
    return redesign.RedesignInputs.from_dict(payload)


def model_output(inputs: Mapping[str, float]) -> dict[str, object]:
    inp = reconstruct_inputs(inputs)
    result = redesign.value(inp)
    return {
        "vps": result.value_per_share_usd,
        "equity_value": result.equity_value_usd_m,
        "operating_ev": result.operating_value_usd_m,
        "credit_equity_value": None,
        "valuation": dataclasses.asdict(result),
        "scenarios": dataclasses.asdict(redesign.scenario_values(inp)),
    }


def effective_inputs_from_receipt(receipt: ModelInputReceipt) -> dict[str, float]:
    """Reconstruct the sealed vector; callers must still verify its source receipt."""
    context = context_for(receipt.request)
    actuals, _ = calculate_actuals(receipt)
    complete = {key: basis.value for key, basis in receipt.request.assumptions.items()}
    complete.update(
        {key: actuals[key] for key in ("da_ratio", "cash_m", "total_debt_m", "diluted_shares_m")}
    )
    for index in range(len(context.segments)):
        allocation = complete.get(f"segment_{index}_allocation")
        if allocation is None:
            raise InputEvidenceError("redesign_segment_allocation_missing")
        complete[f"segment_{index}_revenue_m"] = actuals["revenue"] * allocation
    return effective_numeric_inputs(complete)


def prepare_redesign_inputs(
    conn: sqlite3.Connection,
    request: ModelInputRequest,
    *,
    effective_inputs: Mapping[str, float],
    as_of: datetime,
    source_context: SourceReadContext | None = None,
) -> tuple[dict[str, float], ModelInputReceipt]:
    context = context_for(request)
    proposed = effective_numeric_inputs(effective_inputs)
    if len(context.segments) != int(proposed["segment_count"]):
        raise InputEvidenceError("redesign_segment_population_mismatch")
    assumption_keys = frozenset(proposed) - reported_keys(proposed)
    proof = verify_model_inputs(
        conn,
        request,
        recipe=RECIPE,
        requirements=requirements_for(request),
        effective_inputs={key: proposed[key] for key in assumption_keys},
        assumption_keys=assumption_keys,
        as_of=as_of,
        source_context=source_context,
    )
    actuals, calculations = calculate_actuals(proof)
    complete = {
        **proposed,
        **{key: actuals[key] for key in ("da_ratio", "cash_m", "total_debt_m", "diluted_shares_m")},
    }
    allocations: list[ModelCalculation] = []
    for index in range(len(context.segments)):
        key = f"segment_{index}_revenue_m"
        complete[key] = actuals["revenue"] * proposed[f"segment_{index}_allocation"]
        allocations.append(
            ModelCalculation(
                key=key,
                formula="reported_fy_revenue_times_reviewed_allocation",
                operands=("revenue", f"segment_{index}_allocation"),
                value=complete[key],
            )
        )
    if any(
        not math.isclose(proposed[key], complete[key], rel_tol=1e-12, abs_tol=1e-9)
        for key in reported_keys(proposed)
    ):
        raise InputEvidenceError("redesign_reported_or_calculated_input_mismatch")
    verify_assumption_review(proof, actuals, complete, as_of, review_drivers)
    output = model_output(complete)
    return complete, proof.model_copy(
        update={
            "effective_inputs_sha256": canonical_digest(complete),
            "actuals_sha256": canonical_digest(actuals),
            "calculations": (*calculations, *allocations),
            "model_output_sha256": canonical_digest(output),
        }
    )


def verify_redesign_inputs(
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
    complete, verified = prepare_redesign_inputs(
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
