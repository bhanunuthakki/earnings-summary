"""Canonical financial inputs for discovery; unknown ratio definitions stay unknown."""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from itertools import pairwise
from typing import Literal

from pydantic import BaseModel, ConfigDict

from sources.discovery_financials import (
    CanonicalFinancialHistory,
    FinancialConcept,
    GrowthFactReference,
    read_financial_history,
)
from sources.discovery_market import DiscoveryMarketContext


class FinancialScreenValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["available", "unavailable", "degraded"]
    value: Decimal | None = None
    unit: str | None = None
    currency: str | None = None
    reasons: tuple[str, ...] = ()
    references: tuple[GrowthFactReference, ...] = ()
    formula: str
    decision_grade: Literal[False] = False


class DiscoveryFinancialInputs(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ticker: str
    as_of: date
    revenue_yoy: FinancialScreenValue
    operating_margin_ttm: FinancialScreenValue
    free_cash_flow_ttm: FinancialScreenValue
    roic_ttm: FinancialScreenValue
    net_debt_to_ebitda: FinancialScreenValue
    acquisition_completeness: Literal["unverified"] = "unverified"
    source_contract: Literal["canonical-discovery-financial-inputs/v1"] = (
        "canonical-discovery-financial-inputs/v1"
    )


def _unavailable(reason: str, formula: str) -> FinancialScreenValue:
    return FinancialScreenValue(status="unavailable", reasons=(reason,), formula=formula)


def comparable_quarter_window(
    history: CanonicalFinancialHistory, concept: FinancialConcept, count: int
) -> tuple[GrowthFactReference, ...] | str:
    if history.reason_codes:
        return history.reason_codes[0]
    selected = tuple(
        sorted(
            (item for item in history.references if item.concept == concept),
            key=lambda item: item.period_end,
            reverse=True,
        )[:count]
    )
    if len(selected) != count:
        return "insufficient_resolved_quarterly_history"
    if len({item.period_end for item in selected}) != count:
        return "ambiguous_quarterly_values"
    if (
        len(
            {
                (
                    item.metric_id,
                    item.reporting_entity_id,
                    item.currency,
                    item.unit,
                    item.accounting_basis,
                    item.consolidation_scope,
                )
                for item in selected
            }
        )
        != 1
    ):
        return "incomparable_source_coordinates_or_metric_definition"
    if any(not 70 <= (item.period_end - item.period_start).days <= 105 for item in selected):
        return "nonquarterly_or_unsupported_duration"
    if any(
        (newer.period_start - older.period_end).days != 1 for newer, older in pairwise(selected)
    ):
        return "quarterly_duration_gap_or_overlap"
    # Individually plausible quarters can still span far less/more than a year.
    # Reuse the existing 365 +/- 20-day annual comparison tolerance below.
    if count == 4 and abs((selected[0].period_end - selected[-1].period_start).days + 1 - 365) > 20:
        return "four_quarter_duration_not_annual"
    if any(key == concept and end >= selected[-1].period_end for key, end in history.unresolved):
        return "unresolved_canonical_period"
    if (history.as_of - selected[0].period_end).days > 400:
        return "stale_latest_reporting_period"
    return selected


def calculate_financial_inputs(history: CanonicalFinancialHistory) -> DiscoveryFinancialInputs:
    revenue = comparable_quarter_window(history, "revenue", 5)
    revenue_ttm = comparable_quarter_window(history, "revenue", 4)
    operating = comparable_quarter_window(history, "operating_income", 4)
    cash = comparable_quarter_window(history, "free_cash_flow", 4)
    growth_formula = "latest_quarter_revenue / same_quarter_prior_year_revenue - 1"
    margin_formula = "sum_4_quarter_operating_income / sum_same_4_quarter_revenue"
    cash_formula = "sum_4_contiguous_quarter_free_cash_flow"
    growth = _unavailable(
        revenue if isinstance(revenue, str) else "nonpositive_revenue_denominator", growth_formula
    )
    if not isinstance(revenue, str) and revenue[4].value > 0:
        if abs((revenue[0].period_end - revenue[4].period_end).days - 365) > 20:
            growth = _unavailable("year_over_year_period_mismatch", growth_formula)
        else:
            growth = FinancialScreenValue(
                status="available",
                value=revenue[0].value / revenue[4].value - 1,
                unit="ratio",
                currency=revenue[0].currency,
                references=revenue,
                formula=growth_formula,
            )
    margin = _unavailable(
        operating
        if isinstance(operating, str)
        else revenue_ttm
        if isinstance(revenue_ttm, str)
        else "operating_income_revenue_coordinates_mismatch",
        margin_formula,
    )
    if not isinstance(revenue_ttm, str) and not isinstance(operating, str):
        monetary = (*revenue_ttm, *operating)
        aligned = [(item.period_start, item.period_end) for item in revenue_ttm] == [
            (item.period_start, item.period_end) for item in operating
        ]
        comparable = (
            len(
                {
                    (
                        item.reporting_entity_id,
                        item.currency,
                        item.unit,
                        item.accounting_basis,
                        item.consolidation_scope,
                    )
                    for item in monetary
                }
            )
            == 1
        )
        denominator = sum((item.value for item in revenue_ttm), Decimal(0))
        if aligned and comparable and denominator > 0:
            margin = FinancialScreenValue(
                status="available",
                value=sum((item.value for item in operating), Decimal(0)) / denominator,
                unit="ratio",
                currency=revenue_ttm[0].currency,
                references=monetary,
                formula=margin_formula,
            )
    free_cash_flow = _unavailable(cash if isinstance(cash, str) else "unavailable", cash_formula)
    if not isinstance(cash, str):
        free_cash_flow = FinancialScreenValue(
            status="available",
            value=sum((item.value for item in cash), Decimal(0)),
            unit=cash[0].unit,
            currency=cash[0].currency,
            references=cash,
            formula=cash_formula,
        )
    return DiscoveryFinancialInputs(
        ticker=history.ticker,
        as_of=history.as_of,
        revenue_yoy=growth,
        operating_margin_ttm=margin,
        free_cash_flow_ttm=free_cash_flow,
        roic_ttm=_unavailable("roic_definition_selection_pending", "unselected"),
        net_debt_to_ebitda=_unavailable(
            "canonical_leverage_definition_inputs_unavailable", "unavailable"
        ),
    )


def read_financial_inputs(
    conn: sqlite3.Connection, ticker: str, *, as_of: date
) -> DiscoveryFinancialInputs:
    return calculate_financial_inputs(read_financial_history(conn, ticker, as_of=as_of))


def free_cash_flow_yield(
    financials: DiscoveryFinancialInputs, market: DiscoveryMarketContext
) -> FinancialScreenValue:
    cash = financials.free_cash_flow_ttm
    formula = "canonical_ttm_free_cash_flow / captured_provider_market_cap"
    if cash.status != "available" or cash.value is None:
        return _unavailable("canonical_free_cash_flow_unavailable", formula)
    if market.status != "available" or market.market_cap is None:
        return _unavailable("provider_market_cap_unavailable", formula)
    if cash.currency != market.currency:
        return _unavailable("financial_market_currency_mismatch", formula)
    if cash.unit not in (cash.currency, "actual"):
        return _unavailable("financial_market_unit_scale_unverified", formula)
    return FinancialScreenValue(
        status="available",
        value=cash.value / market.market_cap,
        unit="ratio",
        currency=cash.currency,
        references=cash.references,
        formula=formula,
    )


def financial_parity_receipt(
    canonical: DiscoveryFinancialInputs,
    cash_yield: FinancialScreenValue,
    *,
    legacy_values: tuple[float | None, float | None, float | None],
    legacy_source_hashes: dict[str, str],
) -> dict[str, object]:
    """Retain bounded numerical shadow evidence without asserting provider definition parity."""
    from math import isfinite

    values = (canonical.revenue_yoy, canonical.operating_margin_ttm, cash_yield)
    fields = ("revenue_yoy", "operating_margin_ttm", "free_cash_flow_yield")
    comparisons: dict[str, object] = {}
    for name, legacy, current in zip(fields, legacy_values, values, strict=True):
        comparable = (
            legacy is not None
            and isfinite(legacy)
            and current.value is not None
            and current.status == "available"
            and bool(legacy_source_hashes)
        )
        difference = (
            Decimal(str(legacy)) - current.value
            if comparable and current.value is not None
            else None
        )
        comparisons[name] = {
            "status": "NUMERICAL_MATCH"
            if difference is not None and abs(difference) <= Decimal("1e-12")
            else "NUMERICAL_DIVERGENCE"
            if difference is not None
            else "UNAVAILABLE",
            "legacy_value": legacy if legacy is not None and isfinite(legacy) else None,
            "canonical_value": str(current.value) if current.value is not None else None,
            "canonical_formula": current.formula,
            "canonical_observation_ids": [item.observation_id for item in current.references],
        }
    return {
        "schema_version": "discovery-financial-shadow/v1",
        "scope": "numerical_comparison_only_not_definition_or_source_authority_parity",
        "legacy_source_hashes": legacy_source_hashes,
        "fields": comparisons,
        "definition_parity": "UNVERIFIED",
        "roic": "UNAVAILABLE_DEFINITION_SELECTION_PENDING",
        "leverage": "UNAVAILABLE_CANONICAL_INPUTS",
    }
