"""Read-only financial-table projection over admitted canonical observations.

Calendar buckets are display coordinates, never issuer fiscal identity. This
reader neither defines metrics nor ranks raw provider/legacy candidates.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from provenance.canonical_fact_resolution import CanonicalFactResolutionEngine
from provenance.fact_read_model import FactReadModel, ProvenanceBundle
from provenance.metric_ontology import MetricOntology

FinancialReportConcept = Literal[
    "revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "eps_diluted",
    "operating_cash_flow",
    "free_cash_flow",
    "capital_expenditure",
]
REPORT_CONCEPTS: tuple[FinancialReportConcept, ...] = (
    "revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "eps_diluted",
    "operating_cash_flow",
    "free_cash_flow",
    "capital_expenditure",
)


class FinancialTableCell(BaseModel):
    """One exact source coordinate or an explicit rejection of that coordinate."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    concept: FinancialReportConcept
    canonical_metric_cell_id: str
    metric_id: str
    metric_definition_revision_id: str | None = None
    canonical_resolution_revision_id: str | None = None
    cadence: Literal["quarterly", "annual", "unsupported"] = "unsupported"
    display_coordinate: str | None = None
    reason_codes: tuple[str, ...] = ()
    provenance: ProvenanceBundle | None = None
    source_url: str | None = None
    source_kind: str | None = None
    legacy_document_id: int | None = None
    source_retrieved_at: datetime | None = None

    @property
    def available(self) -> bool:
        return not self.reason_codes and self.provenance is not None

    @property
    def display_value(self) -> Decimal | None:
        if not self.available or self.provenance is None:
            return None
        value = self.provenance.observation.decimal_value
        if value is None:
            return None
        return value if self.concept == "eps_diluted" else value / Decimal(1_000_000)


class FinancialTableProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["canonical-financial-table/v1"] = "canonical-financial-table/v1"
    ticker: str
    as_of: datetime
    cells: tuple[FinancialTableCell, ...] = ()
    reason_codes: tuple[str, ...] = ()


def annual_comparison_supported(
    period_ends: Sequence[date | None], start: int, end: int, periods_per_year: int = 4
) -> bool:
    """Require real year-length spans using the existing 365 ± 20 day policy.

    Check every rolling annual interval so short and long years cannot cancel
    inside a multi-year CAGR. Fiscal labels alone do not establish elapsed time.
    """
    if (
        periods_per_year <= 0
        or not 0 <= start < end < len(period_ends)
        or (end - start) % periods_per_year
        or any(value is None for value in period_ends[start : end + 1])
    ):
        return False
    for index in range(start + periods_per_year, end + 1):
        earlier, later = period_ends[index - periods_per_year], period_ends[index]
        if earlier is None or later is None or abs((later - earlier).days - 365) > 20:
            return False
    return True


def _admit_cell(
    conn: sqlite3.Connection,
    *,
    concept: FinancialReportConcept,
    cell_id: str,
    metric_id: str,
    cutoff: datetime,
    resolver: CanonicalFactResolutionEngine,
    reader: FactReadModel,
    ontology: MetricOntology,
) -> FinancialTableCell:
    initial = FinancialTableCell(
        concept=concept, canonical_metric_cell_id=cell_id, metric_id=metric_id
    )
    resolution = resolver.as_known(cell_id, cutoff)
    if (
        resolution is None
        or resolution.status != "resolved"
        or resolution.selected_observation_id is None
    ):
        return initial.model_copy(update={"reason_codes": ("canonical_resolution_unavailable",)})
    bundle = reader.provenance_bundle(resolution.selected_observation_id, cutoff=cutoff)
    value, source = bundle.observation, bundle.cell
    reasons: list[str] = []
    definition = ontology.metric_definition_as_known(metric_id, cutoff)
    binding = ontology.binding_as_known(value.observation_id, cutoff)
    admitted = (
        ontology.metric_definition_as_known(metric_id, binding.recorded_at) if binding else None
    )
    semantics = {
        "lifecycle",
        "definition_text",
        "value_kind",
        "period_kind",
        "unit_family",
        "accounting_basis",
        "scope_constraints",
    }
    if (
        definition is None
        or admitted is None
        or definition.lifecycle != "active"
        or binding is None
        or binding.binding_status != "bound"
        or binding.canonical_metric_cell_id != cell_id
        or definition.model_dump(include=semantics) != admitted.model_dump(include=semantics)
        or definition.value_kind != "numeric"
        or definition.period_kind != "duration"
        or definition.unit_family != "currency"
        or definition.accounting_basis != source.accounting_basis
        or definition.scope_constraints.get("reporting_entity_id") != source.reporting_entity_id
        or definition.scope_constraints.get("consolidation_scope") != source.consolidation_scope
    ):
        reasons.append("active_metric_definition_or_binding_unavailable")
    if source.dimensions or source.scope_security_id is not None:
        reasons.append("nonconsolidated_financial_coordinate")
    if bundle.evidence is None or value.decimal_value is None or value.period_start is None:
        reasons.append("exact_reported_numeric_duration_unavailable")
    if value.period_end > cutoff:
        reasons.append("future_period")
    currency = value.currency
    expected_units = (
        {f"{currency}/shares", f"{currency}/share"} if concept == "eps_diluted" else {currency}
    )
    if currency is None or value.unit_key not in expected_units:
        reasons.append("currency_or_scale_unavailable")
    cadence: Literal["quarterly", "annual", "unsupported"] = "unsupported"
    coordinate = None
    if source.fiscal_period in {"Q1", "Q2", "Q3", "Q4"} and source.fiscal_year is not None:
        # Same supported quarter duration as sources.discovery_financials.
        if (
            source.period_start is None
            or not 70 <= (source.period_end - source.period_start).days <= 105
        ):
            reasons.append("unsupported_quarter_duration")
        cadence = "quarterly"
        coordinate = f"{source.period_end.year} Q{(source.period_end.month - 1) // 3 + 1}"
    elif source.fiscal_period == "FY" and source.fiscal_year is not None:
        cadence = "annual"
        coordinate = str(source.fiscal_year)
    else:
        reasons.append("fiscal_cadence_or_year_unavailable")
    document = (
        None
        if bundle.evidence is None
        else conn.execute(
            "SELECT document.legacy_document_id,observation.source_url,observation.source_kind,observation.retrieved_at "
            "FROM evidence_document_versions document JOIN evidence_source_observations observation "
            "ON observation.observation_id=document.observation_id WHERE document.document_version_id=?",
            (bundle.evidence.document_version_id,),
        ).fetchone()
    )
    return FinancialTableCell.model_validate(
        {
            **initial.model_dump(),
            "metric_definition_revision_id": definition.metric_definition_revision_id
            if definition
            else None,
            "canonical_resolution_revision_id": resolution.canonical_resolution_revision_id,
            "cadence": cadence,
            "display_coordinate": coordinate,
            "reason_codes": tuple(reasons),
            "provenance": bundle,
            "legacy_document_id": document[0] if document else None,
            "source_url": document[1] if document else None,
            "source_kind": document[2] if document else None,
            "source_retrieved_at": document[3] if document else None,
        }
    )


def read_financial_table(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    as_of: datetime,
) -> FinancialTableProjection:
    """Resolve retained bindings at one explicit knowledge/recording cutoff."""
    if as_of.tzinfo is None:
        raise ValueError("financial table cutoff must be timezone-aware")
    cutoff = as_of.astimezone(UTC)
    ticker = ticker.upper()
    original_factory = conn.row_factory
    owns_snapshot = not conn.in_transaction
    cells: list[FinancialTableCell] = []
    try:
        if owns_snapshot:
            conn.execute("BEGIN")
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT DISTINCT binding.canonical_metric_cell_id,source.concept_name,target.metric_id "
            "FROM fact_cell_canonical_binding_revisions binding "
            "JOIN fact_cells_v2 source ON source.fact_cell_id=binding.fact_cell_id "
            "JOIN canonical_metric_cells target ON target.canonical_metric_cell_id=binding.canonical_metric_cell_id "
            "JOIN fact_observations_v2 observation ON observation.observation_id=binding.source_observation_id "
            "JOIN evidence_document_versions document ON document.document_version_id=observation.document_version_id "
            "WHERE document.ticker=? AND source.concept_namespace='urn:earnings-summary:legacy:financial' "
            "AND source.concept_name IN (SELECT value FROM json_each(?)) "
            "AND binding.binding_status='bound' "
            "AND julianday(binding.recorded_at)<=julianday(?) AND julianday(binding.knowledge_at)<=julianday(?) "
            "ORDER BY source.concept_name,binding.canonical_metric_cell_id",
            (ticker, json.dumps(REPORT_CONCEPTS), cutoff.isoformat(), cutoff.isoformat()),
        ).fetchall()
        resolver, reader, ontology = (
            CanonicalFactResolutionEngine(conn),
            FactReadModel(conn),
            MetricOntology(conn),
        )
        for cell_id, concept, metric_id in rows:
            # Validate the bounded concept at the external SQLite boundary.
            initial = FinancialTableCell.model_validate(
                {"concept": concept, "canonical_metric_cell_id": cell_id, "metric_id": metric_id}
            )
            try:
                cell = _admit_cell(
                    conn,
                    concept=initial.concept,
                    cell_id=str(cell_id),
                    metric_id=str(metric_id),
                    cutoff=cutoff,
                    resolver=resolver,
                    reader=reader,
                    ontology=ontology,
                )
            except (ValueError, RuntimeError, sqlite3.Error):
                cell = initial.model_copy(update={"reason_codes": ("canonical_evidence_invalid",)})
            cells.append(cell)
    except (ValueError, RuntimeError, sqlite3.Error):
        return FinancialTableProjection(
            ticker=ticker, as_of=cutoff, reason_codes=("canonical_financial_table_unavailable",)
        )
    finally:
        if owns_snapshot and conn.in_transaction:
            conn.rollback()
        conn.row_factory = original_factory
    coordinates: dict[tuple[str, str, str | None], list[int]] = defaultdict(list)
    units: dict[str, set[tuple[str | None, str]]] = defaultdict(set)
    for index, cell in enumerate(cells):
        if cell.display_coordinate is not None:
            coordinates[(cell.concept, cell.cadence, cell.display_coordinate)].append(index)
        if cell.available and cell.provenance is not None:
            value = cell.provenance.observation
            units[cell.concept].add((value.currency, value.unit_key))
    ambiguous = {index for indexes in coordinates.values() if len(indexes) > 1 for index in indexes}
    table_currencies = {
        cell.provenance.observation.currency
        for cell in cells
        if cell.available and cell.provenance is not None
    }
    for index, cell in enumerate(cells):
        reasons = list(cell.reason_codes)
        if len(table_currencies) > 1:
            reasons.append("mixed_currency_table")
        if index in ambiguous:
            reasons.append("ambiguous_table_coordinate")
        if len(units[cell.concept]) > 1:
            reasons.append("incomparable_currency_or_unit_history")
        cells[index] = cell.model_copy(update={"reason_codes": tuple(reasons)})
    return FinancialTableProjection(
        ticker=ticker,
        as_of=cutoff,
        cells=tuple(cells),
        reason_codes=() if cells else ("no_canonical_financial_cells",),
    )
