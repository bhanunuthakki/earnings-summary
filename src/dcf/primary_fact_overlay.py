"""Read-only primary-fact overlay for generic FCFF FMP-shaped rows.

The generic workbook builder predates the long-form fact store and consumes the
cached FMP quarterly JSON shape.  This module is a deliberately narrow bridge:
it overlays only exact-period, primary-document facts that have an unambiguous
semantic mapping to an existing FMP field.  It may derive only the two explicit
bridge aggregates registered below when every required same-period component is
present.  It never writes the database, creates a durable projection, accepts a
partial aggregate, or substitutes a mismatched currency/unit/period.
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


ResolutionIdentifierStatus = Literal["available", "unavailable_in_canonical_relation"]


@dataclass(frozen=True, slots=True)
class ComponentLineage:
    """The canonical fact and document inputs to a derived aggregate."""

    line_item: str
    period_end: str
    fiscal_period_type: str
    fact_id: int
    primary_value: float
    currency: str | None
    unit: str | None
    source_doc_id: int
    source_tier: str
    source_type: str | None
    source_url: str | None
    as_of: str
    locator: str | None
    reported_observation_id: str | None
    reported_observation_id_status: ResolutionIdentifierStatus
    resolution_id: str | None
    resolution_id_status: ResolutionIdentifierStatus

    def to_dict(self) -> dict[str, object]:
        return {
            "line_item": self.line_item,
            "period_end": self.period_end,
            "fiscal_period_type": self.fiscal_period_type,
            "fact_id": self.fact_id,
            "primary_value": self.primary_value,
            "currency": self.currency,
            "unit": self.unit,
            "source_doc_id": self.source_doc_id,
            "source_tier": self.source_tier,
            "source_type": self.source_type,
            "source_url": self.source_url,
            "as_of": self.as_of,
            "locator": self.locator,
            "reported_observation_id": self.reported_observation_id,
            "reported_observation_id_status": self.reported_observation_id_status,
            "resolution_id": self.resolution_id,
            "resolution_id_status": self.resolution_id_status,
        }


@dataclass(frozen=True, slots=True)
class AggregateDerivation:
    """Deterministic formula and full canonical inputs for one aggregate."""

    formula: str
    version: str
    components: tuple[ComponentLineage, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "formula": self.formula,
            "version": self.version,
            "components": [component.to_dict() for component in self.components],
        }


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
    currency: str | None
    unit: str | None
    reported_observation_id: str | None
    reported_observation_id_status: ResolutionIdentifierStatus
    resolution_id: str | None
    resolution_id_status: ResolutionIdentifierStatus
    derivation: AggregateDerivation | None = None

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
            "currency": self.currency,
            "unit": self.unit,
            "reported_observation_id": self.reported_observation_id,
            "reported_observation_id_status": self.reported_observation_id_status,
            "resolution_id": self.resolution_id,
            "resolution_id_status": self.resolution_id_status,
            "derivation": self.derivation.to_dict() if self.derivation is not None else None,
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
    derivation: AggregateDerivation | None = None

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
            "derivation": self.derivation.to_dict() if self.derivation is not None else None,
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
        FieldMapping("finance_lease_liability", "financeLeaseLiability", "actual"),
        FieldMapping("finance_lease_liability_current", "financeLeaseLiabilityCurrent", "actual"),
        FieldMapping(
            "finance_lease_liability_non_current", "financeLeaseLiabilityNoncurrent", "actual"
        ),
        FieldMapping("operating_lease_liability", "operatingLeaseLiability", "actual"),
        FieldMapping(
            "operating_lease_liability_current", "operatingLeaseLiabilityCurrent", "actual"
        ),
        FieldMapping(
            "operating_lease_liability_non_current", "operatingLeaseLiabilityNoncurrent", "actual"
        ),
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
class _AggregateMapping:
    line_item: str
    component_line_items: tuple[str, str]
    formula: str


_DERIVED_AGGREGATES: Final[tuple[_AggregateMapping, ...]] = (
    _AggregateMapping(
        "cash_and_short_term_investments",
        ("cash_and_equivalents", "short_term_investments"),
        "cash_and_equivalents + short_term_investments",
    ),
    _AggregateMapping(
        "total_debt",
        ("long_term_debt", "short_term_debt"),
        "long_term_debt + short_term_debt",
    ),
)
_DERIVATION_VERSION: Final[str] = "primary_fact_aggregate_v1"

_CANDIDATE_QUERY_BASE: Final[str] = """
    FROM v_financial_facts_resolved_current AS fact
    LEFT JOIN documents AS document
      ON document.id = fact.source_doc_id
     AND UPPER(document.ticker) = UPPER(fact.ticker)
    WHERE UPPER(fact.ticker) = UPPER(?)
    ORDER BY fact.period_end, fact.line_item, document.fetched_at DESC, fact.id DESC
"""
_CANDIDATE_QUERY_WITH_BOTH_RESOLUTION_IDS: Final[str] = (
    """
    SELECT fact.id, fact.line_item, fact.period_end, fact.fiscal_period_type,
           fact.value, fact.currency, fact.unit, fact.source_doc_id,
           fact.locator, document.source_quality_tier, document.source_url,
           document.fetched_at, document.source_type,
           fact.reported_observation_id, fact.resolution_id
"""
    + _CANDIDATE_QUERY_BASE
)
_CANDIDATE_QUERY_WITH_REPORTED_OBSERVATION_ID: Final[str] = (
    """
    SELECT fact.id, fact.line_item, fact.period_end, fact.fiscal_period_type,
           fact.value, fact.currency, fact.unit, fact.source_doc_id,
           fact.locator, document.source_quality_tier, document.source_url,
           document.fetched_at, document.source_type,
           fact.reported_observation_id, NULL
"""
    + _CANDIDATE_QUERY_BASE
)
_CANDIDATE_QUERY_WITH_RESOLUTION_ID: Final[str] = (
    """
    SELECT fact.id, fact.line_item, fact.period_end, fact.fiscal_period_type,
           fact.value, fact.currency, fact.unit, fact.source_doc_id,
           fact.locator, document.source_quality_tier, document.source_url,
           document.fetched_at, document.source_type,
           NULL, fact.resolution_id
"""
    + _CANDIDATE_QUERY_BASE
)
_CANDIDATE_QUERY_WITHOUT_RESOLUTION_IDS: Final[str] = (
    """
    SELECT fact.id, fact.line_item, fact.period_end, fact.fiscal_period_type,
           fact.value, fact.currency, fact.unit, fact.source_doc_id,
           fact.locator, document.source_quality_tier, document.source_url,
           document.fetched_at, document.source_type,
           NULL, NULL
"""
    + _CANDIDATE_QUERY_BASE
)


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
    reported_observation_id: str | None
    reported_observation_id_status: ResolutionIdentifierStatus
    resolution_id: str | None
    resolution_id_status: ResolutionIdentifierStatus


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
    mapping_by_line_item = {mapping.line_item: mapping for mapping in mappings}
    for record in copied:
        period_end = _date_value(record.get("date"))
        fiscal_period = _text_value(record.get("period"))
        reported_currency = _text_value(record.get("reportedCurrency"))
        if period_end is None or fiscal_period is None:
            continue
        selected_by_line_item: dict[str, _FactCandidate] = {}
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
            selected_by_line_item[mapping.line_item] = selected
            primary_value = selected.value
            if primary_value is None or not _has_complete_document_lineage(selected):
                continue
            prior_value = _numeric_value(record.get(mapping.fmp_field))
            record[mapping.fmp_field] = primary_value
            lineage = _field_lineage(
                selected,
                mapping=mapping,
                period_end=period_end,
                fiscal_period=fiscal_period,
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
        for aggregate in _DERIVED_AGGREGATES:
            # An eligible disclosed aggregate is authoritative, even when the
            # components happen to be complete and add to a different number.
            if aggregate.line_item in selected_by_line_item:
                continue
            mapping = mapping_by_line_item.get(aggregate.line_item)
            if mapping is None:
                continue
            components = tuple(
                selected_by_line_item.get(line_item) for line_item in aggregate.component_line_items
            )
            if any(component is None for component in components):
                continue
            selected_components = cast("tuple[_FactCandidate, _FactCandidate]", components)
            if not all(
                _has_complete_document_lineage(component) for component in selected_components
            ):
                continue
            component_values = tuple(component.value for component in selected_components)
            if any(value is None for value in component_values):
                continue
            primary_value = sum(cast("tuple[float, float]", component_values))
            prior_value = _numeric_value(record.get(mapping.fmp_field))
            record[mapping.fmp_field] = primary_value
            derivation = AggregateDerivation(
                formula=aggregate.formula,
                version=_DERIVATION_VERSION,
                components=tuple(
                    _component_lineage(component) for component in selected_components
                ),
            )
            applied.append(
                _field_lineage(
                    selected_components[0],
                    mapping=mapping,
                    period_end=period_end,
                    fiscal_period=fiscal_period,
                    fmp_value=prior_value,
                    primary_value=primary_value,
                    derivation=derivation,
                )
            )
            if prior_value is not None and prior_value != primary_value:
                conflicts.append(
                    OverlayFinding(
                        line_item=mapping.line_item,
                        fmp_field=mapping.fmp_field,
                        period_end=period_end,
                        fiscal_period_type=fiscal_period,
                        reason="value_conflict",
                        source_doc_id=selected_components[0].source_doc_id,
                        fact_id=selected_components[0].fact_id,
                        fmp_value=prior_value,
                        primary_value=primary_value,
                        derivation=derivation,
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
    if fact_relation != "v_financial_facts_resolved_current":
        raise RuntimeError(
            "canonical financial-fact cutover is unavailable; refusing a legacy read"
        )
    view_columns = {
        str(column[0]).lower()
        for column in conn.execute(
            "SELECT * FROM v_financial_facts_resolved_current LIMIT 0"
        ).description
    }
    has_reported_observation_id = "reported_observation_id" in view_columns
    has_resolution_id = "resolution_id" in view_columns
    if has_reported_observation_id and has_resolution_id:
        query = _CANDIDATE_QUERY_WITH_BOTH_RESOLUTION_IDS
    elif has_reported_observation_id:
        query = _CANDIDATE_QUERY_WITH_REPORTED_OBSERVATION_ID
    elif has_resolution_id:
        query = _CANDIDATE_QUERY_WITH_RESOLUTION_ID
    else:
        query = _CANDIDATE_QUERY_WITHOUT_RESOLUTION_IDS
    rows = conn.execute(
        query,
        (ticker,),
    ).fetchall()
    requested_line_items = {mapping.line_item for mapping in mappings}
    candidates = [
        _candidate_from_row(
            row,
            reported_observation_id_status=(
                "available" if has_reported_observation_id else "unavailable_in_canonical_relation"
            ),
            resolution_id_status=(
                "available" if has_resolution_id else "unavailable_in_canonical_relation"
            ),
        )
        for row in rows
    ]
    return [candidate for candidate in candidates if candidate.line_item in requested_line_items]


def _candidate_from_row(
    row: sqlite3.Row | tuple[object, ...],
    *,
    reported_observation_id_status: ResolutionIdentifierStatus,
    resolution_id_status: ResolutionIdentifierStatus,
) -> _FactCandidate:
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
        reported_observation_id=_text_value(values[13]),
        reported_observation_id_status=reported_observation_id_status,
        resolution_id=_text_value(values[14]),
        resolution_id_status=resolution_id_status,
    )


def _has_complete_document_lineage(candidate: _FactCandidate) -> bool:
    return candidate.source_doc_id is not None and candidate.source_tier is not None


def _component_lineage(candidate: _FactCandidate) -> ComponentLineage:
    primary_value = candidate.value
    source_doc_id = candidate.source_doc_id
    source_tier = candidate.source_tier
    if primary_value is None or source_doc_id is None or source_tier is None:
        raise ValueError("aggregate components require complete canonical document lineage")
    return ComponentLineage(
        line_item=candidate.line_item,
        period_end=candidate.period_end,
        fiscal_period_type=candidate.fiscal_period_type,
        fact_id=candidate.fact_id,
        primary_value=primary_value,
        currency=candidate.currency,
        unit=candidate.unit,
        source_doc_id=source_doc_id,
        source_tier=source_tier,
        source_type=candidate.source_type,
        source_url=candidate.source_url,
        as_of=candidate.fetched_at or candidate.period_end,
        locator=candidate.locator,
        reported_observation_id=candidate.reported_observation_id,
        reported_observation_id_status=candidate.reported_observation_id_status,
        resolution_id=candidate.resolution_id,
        resolution_id_status=candidate.resolution_id_status,
    )


def _field_lineage(
    candidate: _FactCandidate,
    *,
    mapping: FieldMapping,
    period_end: str,
    fiscal_period: str,
    fmp_value: float | None,
    primary_value: float,
    derivation: AggregateDerivation | None = None,
) -> FieldLineage:
    source_doc_id = candidate.source_doc_id
    source_tier = candidate.source_tier
    if source_doc_id is None or source_tier is None:
        raise ValueError("applied overlay facts require complete canonical document lineage")
    return FieldLineage(
        line_item=mapping.line_item,
        fmp_field=mapping.fmp_field,
        period_end=period_end,
        fiscal_period_type=fiscal_period,
        source_doc_id=source_doc_id,
        source_tier=source_tier,
        source_type=candidate.source_type,
        source_url=candidate.source_url,
        as_of=candidate.fetched_at or period_end,
        fact_id=candidate.fact_id,
        locator=candidate.locator,
        fmp_value=fmp_value,
        primary_value=primary_value,
        currency=candidate.currency,
        unit=candidate.unit,
        reported_observation_id=candidate.reported_observation_id,
        reported_observation_id_status=candidate.reported_observation_id_status,
        resolution_id=candidate.resolution_id,
        resolution_id_status=candidate.resolution_id_status,
        derivation=derivation,
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
