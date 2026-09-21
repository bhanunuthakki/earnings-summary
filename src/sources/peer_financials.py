"""Comparable peer financials from admitted quarterly observations."""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from provenance.immutable_artifact import read_stable_artifact
from sources.discovery_financial_inputs import FinancialScreenValue, comparable_quarter_window
from sources.discovery_financials import read_financial_history


class PeerFinancialInputs(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ticker: str
    as_of: date
    revenue_ttm: FinancialScreenValue
    net_margin_ttm: FinancialScreenValue
    roic_ttm: FinancialScreenValue


def read_peer_financials(
    conn: sqlite3.Connection, ticker: str, *, as_of: date
) -> PeerFinancialInputs:
    history = read_financial_history(conn, ticker, as_of=as_of, concepts=("revenue", "net_income"))
    revenue = comparable_quarter_window(history, "revenue", 4)
    income = comparable_quarter_window(history, "net_income", 4)
    revenue_formula = "sum_4_contiguous_quarter_revenue"
    margin_formula = "sum_4_quarter_net_income / sum_same_4_quarter_revenue"
    reported = FinancialScreenValue(
        status="unavailable",
        formula=revenue_formula,
        reasons=(revenue if isinstance(revenue, str) else "unavailable",),
    )
    margin = FinancialScreenValue(
        status="unavailable",
        formula=margin_formula,
        reasons=(
            income
            if isinstance(income, str)
            else revenue
            if isinstance(revenue, str)
            else "net_income_revenue_coordinates_mismatch",
        ),
    )
    if not isinstance(revenue, str):
        total = sum((item.value for item in revenue), Decimal(0))
        reported = FinancialScreenValue(
            status="available",
            formula=revenue_formula,
            value=total,
            currency=revenue[0].currency,
            unit=revenue[0].unit,
            references=revenue,
        )
        if not isinstance(income, str):
            selected = (*revenue, *income)
            aligned = [(item.period_start, item.period_end) for item in revenue] == [
                (item.period_start, item.period_end) for item in income
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
                        for item in selected
                    }
                )
                == 1
            )
            if aligned and comparable and total > 0:
                margin = FinancialScreenValue(
                    status="available",
                    formula=margin_formula,
                    value=sum((item.value for item in income), Decimal(0)) / total,
                    unit="ratio",
                    currency=revenue[0].currency,
                    references=selected,
                )
    return PeerFinancialInputs(
        ticker=history.ticker,
        as_of=as_of,
        revenue_ttm=reported,
        net_margin_ttm=margin,
        roic_ttm=FinancialScreenValue(
            status="unavailable",
            formula="unselected",
            reasons=("roic_definition_selection_pending",),
        ),
    )


def peer_numerical_shadow(
    source_dir: Path, ticker: str, financials: PeerFinancialInputs
) -> dict[str, object]:
    """Observe retained legacy formulas on exact bytes; never feed their values to the panel."""
    hashes: dict[str, str] = {}
    packets: dict[str, list[dict[str, JsonValue]]] = {}
    for suffix in ("income_statement_quarterly", "key_metrics_ttm", "financial_ratios_ttm"):
        path = source_dir / f"{ticker}_{suffix}.json"
        try:
            snapshot, body = read_stable_artifact(path)
            packets[suffix] = TypeAdapter(list[dict[str, JsonValue]]).validate_json(body)
            hashes[path.name] = snapshot.file_sha256
        except (OSError, ValueError, RuntimeError):
            packets[suffix] = []

    def number(row: dict[str, JsonValue], key: str) -> Decimal | None:
        value = row.get(key)
        return (
            Decimal(str(value))
            if isinstance(value, int | float)
            and not isinstance(value, bool)
            and Decimal(str(value)).is_finite()
            else None
        )

    quarterly = packets["income_statement_quarterly"]
    values = [value for row in quarterly[:4] if (value := number(row, "revenue")) is not None]
    empty_record: dict[str, JsonValue] = {}
    metrics = next(iter(packets["key_metrics_ttm"]), empty_record)
    ratios = next(iter(packets["financial_ratios_ttm"]), empty_record)
    legacy_revenue = sum(values, Decimal(0)) if values else number(metrics, "revenueTTM")
    legacy_margin = number(ratios, "netProfitMarginTTM")
    if legacy_margin is None:
        legacy_margin = number(metrics, "netIncomePerRevenueTTM")
    fields: dict[str, object] = {}
    for name, legacy, current in (
        ("revenue_ttm", legacy_revenue, financials.revenue_ttm),
        ("net_margin_ttm", legacy_margin, financials.net_margin_ttm),
    ):
        difference = (
            legacy - current.value if legacy is not None and current.value is not None else None
        )
        fields[name] = {
            "legacy_value": str(legacy) if legacy is not None else None,
            "canonical_value": str(current.value) if current.value is not None else None,
            "status": "UNAVAILABLE"
            if difference is None
            else "NUMERICAL_MATCH"
            if abs(difference) <= Decimal("1e-12")
            else "NUMERICAL_DIVERGENCE",
        }
    return {
        "schema_version": "peer-financial-shadow/v1",
        "legacy_source_hashes": hashes,
        "fields": fields,
        "definition_parity": "UNVERIFIED",
        "scope": "numerical_comparison_only_not_source_or_definition_parity",
    }
