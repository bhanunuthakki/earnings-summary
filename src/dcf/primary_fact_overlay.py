"""Read-only primary-fact overlay for generic FCFF FMP-shaped rows.

The generic workbook builder predates the long-form fact store and consumes the
cached FMP quarterly JSON shape.  This module is a deliberately narrow bridge:
it overlays only exact-period, primary-document facts that have an unambiguous
semantic mapping to an existing FMP field.  It never writes the database,
creates a durable projection, derives debt, or substitutes a mismatched
currency/unit/period.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Final, Literal, cast

from provenance.financial_fact_resolution import canonical_fact_relation

Statement = Literal["income", "balance", "cash_flow"]


@dataclass(frozen=True, slots=True)
class FieldMapping:
    line_item: str
    fmp_field: str
    unit: str
    currency_required: bool = True


@dataclass(frozen=True, slots=True)
class FieldLineage:
    line_item: str
    fmp_field: str
    period_end: str
    fiscal_period_type: str
    source_doc_id: int
    source_tier: str
    source_type: str | None
    source_url: str | None
    as_of: str
    fact_id: int
    locator: str | None
    fmp_value: float | None
    primary_value: float

    def to_dict(self) -> dict[str, object]:
        return {
            "line_item": self.line_item,
            "fmp_field": self.fmp_field,
            "period_end": self.period_end,
            "fiscal_period_type": self.fiscal_period_type,
            "source_doc_id": self.source_doc_id,
            "source_tier": self.source_tier,
            "source_type": self.source_type,
            "source_url": self.source_url,
            "as_of": self.as_of,
            "fact_id": self.fact_id,
            "locator": self.locator,
            "fmp_value": self.fmp_value,
            "primary_value": self.primary_value,
        }


@dataclass(frozen=True, slots=True)
class OverlayFinding:
    line_item: str
    fmp_field: str
    period_end: str
    fiscal_period_type: str
    reason: str
    source_doc_id: int | None = None
    fact_id: int | None = None
    fmp_value: float | None = None
    primary_value: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "line_item": self.line_item,
            "fmp_field": self.fmp_field,
            "period_end": self.period_end,
            "fiscal_period_type": self.fiscal_period_type,
            "reason": self.reason,
            "source_doc_id": self.source_doc_id,
            "fact_id": self.fact_id,
            "fmp_value": self.fmp_value,
            "primary_value": self.primary_value,
        }


@dataclass(frozen=True, slots=True)
class OverlayResult:
    records: list[dict[str, object]]
    applied: tuple[FieldLineage, ...]
    conflicts: tuple[OverlayFinding, ...]
    rejected: tuple[OverlayFinding, ...]
    degraded_reason: str | None = None

    def to_provenance_dict(self) -> dict[str, object]:
        return {
            "status": "degraded" if self.degraded_reason else "ok",
            "degraded_reason": self.degraded_reason,
            "applied": [lineage.to_dict() for lineage in self.applied],
            "conflicts": [finding.to_dict() for finding in self.conflicts],
            "rejected": [finding.to_dict() for finding in self.rejected],
        }


_MAPPINGS: Final[dict[Statement, tuple[FieldMapping, ...]]] = {
    "income": (
        FieldMapping("revenue", "revenue", "actual"),
        FieldMapping("cost_of_revenue", "costOfRevenue", "actual"),
        FieldMapping("gross_profit", "grossProfit", "actual"),
        FieldMapping("research_and_development", "researchAndDevelopmentExpenses", "actual"),
        FieldMapping("sga", "sellingGeneralAndAdministrativeExpenses", "actual"),
        FieldMapping("operating_income", "operatingIncome", "actual"),
        FieldMapping("net_income", "netIncome", "actual"),
        FieldMapping(
            "weighted_avg_shares_diluted",
            "weightedAverageShsOutDil",
            "count",
            currency_required=False,
        ),
    ),
    "balance": (
        FieldMapping("cash_and_equivalents", "cashAndCashEquivalents", "actual"),
        FieldMapping("short_term_investments", "shortTermInvestments", "actual"),
        FieldMapping(
            "cash_and_short_term_investments",
            "cashAndShortTermInvestments",
            "actual",
        ),
        FieldMapping("total_current_assets", "totalCurrentAssets", "actual"),
        FieldMapping("total_assets", "totalAssets", "actual"),
        FieldMapping("total_current_liabilities", "totalCurrentLiabilities", "actual"),
        FieldMapping("short_term_debt", "shortTermDebt", "actual"),
        FieldMapping("long_term_debt", "longTermDebt", "actual"),
        FieldMapping("total_debt", "totalDebt", "actual"),
        FieldMapping("total_stockholders_equity", "totalStockholdersEquity", "actual"),
    ),
    "cash_flow": (
        FieldMapping("depreciation_and_amortization", "depreciationAndAmortization", "actual"),
        FieldMapping("operating_cash_flow", "operatingCashFlow", "actual"),
        FieldMapping("capital_expenditure", "capitalExpenditure", "actual"),
        FieldMapping("stock_based_compensation", "stockBasedCompensation", "actual"),
    ),
}
_PRIMARY_SOURCE_TYPES: Final[frozenset[str]] = frozenset({"sec_xbrl", "ir_doc"})
_PRIMARY_TIERS: Final[frozenset[str]] = frozenset({"sec_official"})


@dataclass(frozen=True, slots=True)
class _FactCandidate:
    fact_id: int
    line_item: str
    period_end: str
    fiscal_period_type: str
    value: float | None
    currency: str | None
    unit: str | None
    source_doc_id: int | None
    source_tier: str | None
    source_type: str | None
    source_url: str | None
    fetched_at: str | None
    locator: str | None


def overlay_quarterly_records(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    statement: Statement,
    records: Sequence[Mapping[str, object]],
) -> OverlayResult:
    """Return copied FMP-shaped rows with safe primary-fact substitutions.

    A DB or schema failure is deliberately a no-op result rather than a builder
    failure: the cached FMP row remains the available source and the degradation
    is carried forward into the DCF provenance.
    """

    copied = [dict(record) for record in records]
    mappings = _MAPPINGS[statement]
    try:
        candidates = _load_candidates(conn, ticker=ticker, mappings=mappings)
    except (RuntimeError, sqlite3.Error) as error:
        return OverlayResult(copied, (), (), (), f"primary fact query unavailable: {error}")

    by_period_line: dict[tuple[str, str], list[_FactCandidate]] = defaultdict(list)
    for candidate in candidates:
        by_period_line[(candidate.period_end, candidate.line_item)].append(candidate)

    applied: list[FieldLineage] = []
    conflicts: list[OverlayFinding] = []
    rejected: list[OverlayFinding] = []
    for record in copied:
        period_end = _date_value(record.get("date"))
        fiscal_period = _text_value(record.get("period"))
        reported_currency = _text_value(record.get("reportedCurrency"))
        if period_end is None or fiscal_period is None:
            continue
        for mapping in mappings:
            matches = by_period_line.get((period_end, mapping.line_item), [])
            selected = _select_candidate(
                matches,
                mapping=mapping,
                fiscal_period=fiscal_period,
                reported_currency=reported_currency,
                rejected=rejected,
            )
            if selected is None:
                continue
            primary_value = selected.value
            if (
                primary_value is None
                or selected.source_doc_id is None
                or selected.source_tier is None
            ):
                continue
            prior_value = _numeric_value(record.get(mapping.fmp_field))
            record[mapping.fmp_field] = primary_value
            lineage = FieldLineage(
                line_item=mapping.line_item,
                fmp_field=mapping.fmp_field,
                period_end=period_end,
                fiscal_period_type=fiscal_period,
                source_doc_id=selected.source_doc_id,
                source_tier=selected.source_tier,
                source_type=selected.source_type,
                source_url=selected.source_url,
                as_of=selected.fetched_at or period_end,
                fact_id=selected.fact_id,
                locator=selected.locator,
                fmp_value=prior_value,
                primary_value=primary_value,
            )
            applied.append(lineage)
            if prior_value is not None and prior_value != primary_value:
                conflicts.append(
                    OverlayFinding(
                        line_item=mapping.line_item,
                        fmp_field=mapping.fmp_field,
                        period_end=period_end,
                        fiscal_period_type=fiscal_period,
                        reason="value_conflict",
                        source_doc_id=selected.source_doc_id,
                        fact_id=selected.fact_id,
                        fmp_value=prior_value,
                        primary_value=primary_value,
                    )
                )
    return OverlayResult(copied, tuple(applied), tuple(conflicts), tuple(rejected))


def _load_candidates(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    mappings: Sequence[FieldMapping],
) -> list[_FactCandidate]:
    fact_relation = canonical_fact_relation(conn, "financial_facts").sql
    placeholders = ", ".join("?" for _ in mappings)
    rows = conn.execute(
        f"""
        SELECT fact.id, fact.line_item, fact.period_end, fact.fiscal_period_type,
               fact.value, fact.currency, fact.unit, fact.source_doc_id,
               fact.locator, document.source_quality_tier, document.source_url,
               document.fetched_at, document.source_type
        FROM {fact_relation} AS fact
        LEFT JOIN documents AS document
          ON document.id = fact.source_doc_id
         AND UPPER(document.ticker) = UPPER(fact.ticker)
        WHERE UPPER(fact.ticker) = UPPER(?)
          AND fact.line_item IN ({placeholders})
        ORDER BY fact.period_end, fact.line_item, document.fetched_at DESC, fact.id DESC
        """,
        (ticker, *(mapping.line_item for mapping in mappings)),
    ).fetchall()
    return [_candidate_from_row(row) for row in rows]


def _candidate_from_row(row: sqlite3.Row | tuple[object, ...]) -> _FactCandidate:
    values = cast("Sequence[object]", row)
    fact_id = _int_value(values[0])
    if fact_id is None:
        raise sqlite3.DataError("financial fact id is not an integer")
    return _FactCandidate(
        fact_id=fact_id,
        line_item=str(values[1]),
        period_end=_date_value(values[2]) or "",
        fiscal_period_type=_text_value(values[3]) or "",
        value=_numeric_value(values[4]),
        currency=_text_value(values[5]),
        unit=_text_value(values[6]),
        source_doc_id=_int_value(values[7]),
        locator=_text_value(values[8]),
        source_tier=_text_value(values[9]),
        source_url=_text_value(values[10]),
        fetched_at=_text_value(values[11]),
        source_type=_text_value(values[12]),
    )


def _select_candidate(
    candidates: Sequence[_FactCandidate],
    *,
    mapping: FieldMapping,
    fiscal_period: str,
    reported_currency: str | None,
    rejected: list[OverlayFinding],
) -> _FactCandidate | None:
    eligible: list[_FactCandidate] = []
    for candidate in candidates:
        reason = _rejection_reason(
            candidate,
            mapping=mapping,
            fiscal_period=fiscal_period,
            reported_currency=reported_currency,
        )
        if reason is not None:
            rejected.append(_finding(candidate, mapping, fiscal_period, reason))
            continue
        eligible.append(candidate)
    if not eligible:
        return None
    values = {candidate.value for candidate in eligible}
    if len(values) != 1:
        for candidate in eligible:
            rejected.append(
                _finding(candidate, mapping, fiscal_period, "conflicting_primary_values")
            )
        return None
    return eligible[0]


def _rejection_reason(
    candidate: _FactCandidate,
    *,
    mapping: FieldMapping,
    fiscal_period: str,
    reported_currency: str | None,
) -> str | None:
    if (
        candidate.source_tier not in _PRIMARY_TIERS
        and candidate.source_type not in _PRIMARY_SOURCE_TYPES
    ):
        return "source_not_primary"
    if candidate.fiscal_period_type.upper() != fiscal_period.upper():
        return "fiscal_period_mismatch"
    if candidate.value is None:
        return "invalid_numeric_value"
    if candidate.unit is None or candidate.unit.lower() != mapping.unit:
        return "unit_mismatch"
    if not mapping.currency_required:
        return None
    if candidate.currency is None:
        return "missing_primary_currency"
    if reported_currency is None:
        return "missing_fmp_currency"
    if candidate.currency.upper() != reported_currency.upper():
        return "currency_mismatch"
    return None


def _finding(
    candidate: _FactCandidate,
    mapping: FieldMapping,
    fiscal_period: str,
    reason: str,
) -> OverlayFinding:
    return OverlayFinding(
        line_item=mapping.line_item,
        fmp_field=mapping.fmp_field,
        period_end=candidate.period_end,
        fiscal_period_type=fiscal_period,
        reason=reason,
        source_doc_id=candidate.source_doc_id,
        fact_id=candidate.fact_id,
        primary_value=candidate.value,
    )


def _date_value(value: object) -> str | None:
    text = _text_value(value)
    return text[:10] if text is not None and len(text) >= 10 else None


def _text_value(value: object) -> str | None:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _numeric_value(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _int_value(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


__all__ = ["OverlayResult", "overlay_quarterly_records"]
